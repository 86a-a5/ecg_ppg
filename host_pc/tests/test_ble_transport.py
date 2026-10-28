"""Exercise BLE fragmentation, device selection, loss and cancellation offline."""
import asyncio
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ecg_ppg.acquisition import stream_workers as transport


def packet(kind, seq, timestamp):
    return struct.pack("<BBBHHIB", 0xAA, kind, seq, 1000 if kind == 1 else 10000,
                       0xFFFF if kind == 1 else 30000, timestamp, 0x55)


def wait_for(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not predicate():
        raise AssertionError("background worker did not reach expected state")


class FakeClient:
    instance = None
    valid = True

    def __init__(self, device, disconnected_callback, **kwargs):
        FakeClient.instance = self
        self.device = device
        self.mtu_size = 247
        self.disconnected_callback = disconnected_callback
        self.closed = threading.Event()
        self.characteristic = SimpleNamespace(properties=["notify"])
        self.services = SimpleNamespace(get_service=lambda uuid: SimpleNamespace(
            get_characteristic=lambda uuid: self.characteristic) if self.valid else None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.disconnected_callback(self)
        self.closed.set()

    async def start_notify(self, characteristic, callback):
        self.callback = callback
        # Real 244-byte notifications split the 12-byte frame at arbitrary offsets.
        payload = packet(1, 0, 1000000) + packet(2, 1, 1000000)
        for chunk in (payload[:5], payload[5:17], payload[17:]):
            callback(characteristic, bytearray(chunk))


class BleTransportTests(unittest.TestCase):
    def target(self):
        return transport.BleTarget("ATK-BLE04", "AA:BB", -40, object())

    def test_scan_filters_service_and_fallback_name(self):
        async def discover(**kwargs):
            return {str(i): (SimpleNamespace(name=name, address=str(i)), SimpleNamespace(
                local_name=name, service_uuids=uuids, rssi=rssi))
                    for i, (name, uuids, rssi) in enumerate([
                        ("headphones", [], -20),
                        ("ATK-BLE04", [], -60),
                        ("renamed sensor", [transport.SERVICE_UUID.upper()], -30)])}
        worker = transport.BleDiscovery(timeout=0)
        with patch.object(transport, "BleakScanner", SimpleNamespace(discover=discover)):
            worker.start()
            worker.join(2)
        self.assertIsNone(worker.error)
        self.assertEqual([t.name for t in worker.targets], ["renamed sensor", "ATK-BLE04"])

    def test_binary_notifications_and_user_disconnect(self):
        target = self.target()
        worker = transport.BleWorker(target, settle_seconds=0)
        with patch.object(transport, "BleakClient", FakeClient):
            worker.start()
            try:
                wait_for(lambda: worker.state == "connected")
                self.assertIs(FakeClient.instance.device, target.device)
                self.assertEqual(worker.rx_bytes, 24)
                ecg, ppg = worker.rx_queue.get_nowait(), worker.rx_queue.get_nowait()
                self.assertEqual((ecg[0], ppg[0]), ("ecg", "ppg"))
                self.assertEqual(ecg[1].timestamp_s, ppg[1].timestamp_s)
                self.assertEqual((ppg[1].value, ppg[1].value2), (40000, 120000))
            finally:
                worker.stop()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(FakeClient.instance.closed.is_set())
        self.assertIsNone(worker.error)

    def test_radio_disconnect_is_reported(self):
        worker = transport.BleWorker(self.target(), settle_seconds=0)
        with patch.object(transport, "BleakClient", FakeClient):
            worker.start()
            wait_for(lambda: worker.state == "connected")
            FakeClient.instance.disconnected_callback(FakeClient.instance)
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIn("蓝牙连接已断开", worker.error)

    def test_wrong_gatt_service_rejected(self):
        worker = transport.BleWorker(self.target())
        with patch.object(transport, "BleakClient", FakeClient), patch.object(FakeClient, "valid", False):
            worker.start()
            worker.join(2)
        self.assertIn("FFF0 / FFF1", worker.error)
        self.assertTrue(FakeClient.instance.closed.is_set())

    def test_pending_connection_is_cancellable(self):
        entered, cancelled = threading.Event(), threading.Event()

        class PendingClient(FakeClient):
            async def __aenter__(self):
                entered.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()

        worker = transport.BleWorker(self.target())
        with patch.object(transport, "BleakClient", PendingClient):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
            finally:
                worker.stop()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(worker.error)

    def test_startup_cache_discard_preserves_fragment_alignment(self):
        worker = transport.BleWorker(self.target())
        worker.state = "synchronizing"
        old = packet(1, 0, 1000000)
        current = packet(2, 30, 31000000)
        with patch.object(transport.time, "monotonic", return_value=10.0):
            worker._notification(None, old)
        with patch.object(transport.time, "monotonic", return_value=10.5):
            worker._notification(None, current + packet(1, 31, 31005000)[:5])
        self.assertTrue(worker.rx_queue.empty())
        with patch.object(transport.time, "monotonic", return_value=11.1):
            worker._notification(None, packet(1, 31, 31005000)[5:]
                                 + packet(2, 32, 31010000))
        self.assertEqual(worker.state, "connected")
        self.assertGreater(worker.startup_stats["lost_frames"], 0)
        self.assertEqual(worker.parser.stats.lost_frames, 0)
        self.assertEqual(worker.parser.stats.bad_frames, 0)
        self.assertEqual(worker.rx_queue.qsize(), 2)
        self.assertEqual(worker.rx_queue.get_nowait()[1].timestamp_s, 31.005)


if __name__ == "__main__":
    unittest.main()
