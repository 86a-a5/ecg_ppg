"""Serial and ATK-BLE04 transports feeding the same binary sample pipeline.

No Tk calls belong here. BLE notifications are byte fragments, not text or frames.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import queue
import threading
import time

from ecg_ppg.acquisition.ecg_ppg_protocol import FrameParser

try:
    import serial
except ImportError:  # BLE can work without a serial adapter.
    serial = None

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = BleakScanner = None

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class BleTarget:
    name: str
    address: str
    rssi: int
    device: object = field(repr=False, compare=False)

    @property
    def label(self):
        return f"{self.name} · {self.address} · {self.rssi} dBm"


class AsyncWorker(threading.Thread):
    """Cancellation also interrupts a pending Windows GATT connection or scan."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.error = None

    def stop(self):
        self.stop_event.set()

    async def _until_stopped(self):
        task = asyncio.create_task(self._session())
        try:
            while not task.done() and not self.stop_event.is_set():
                await asyncio.sleep(0.1)
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def run(self):
        try:
            asyncio.run(self._until_stopped())
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = str(exc) or type(exc).__name__


class BleDiscovery(AsyncWorker):
    def __init__(self, timeout=6.0):
        super().__init__()
        self.timeout = timeout
        self.targets = []

    async def _session(self):
        if BleakScanner is None:
            raise RuntimeError("缺少 bleak，请安装 requirements.txt 中的依赖")
        discovered = await BleakScanner.discover(timeout=self.timeout, return_adv=True)
        for device, adv in discovered.values():
            name = adv.local_name or device.name or "未命名 BLE"
            services = {uuid.lower() for uuid in adv.service_uuids}
            # Some ATK firmware advertisements omit the service UUID. Check
            # the actual service/characteristic again when connecting.
            if SERVICE_UUID in services or name.upper().startswith("ATK-BLE04"):
                self.targets.append(BleTarget(name, device.address, adv.rssi, device))
        self.targets.sort(key=lambda target: (-target.rssi, target.address))


class SampleWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.rx_queue = queue.Queue(maxsize=200000)
        self.stop_event = threading.Event()
        self.error = None
        self.parser = FrameParser()
        self.rx_bytes = 0
        self.ui_dropped = 0
        self.first_frame_wall = {}
        self.last_frame_wall = {}
        self.state = "connecting"

    def feed_bytes(self, chunk):
        self.rx_bytes += len(chunk)
        ecg, ppg = self.parser.feed(chunk)
        received_at = time.monotonic()
        for kind, samples in (("ecg", ecg), ("ppg", ppg)):
            if samples:
                self.first_frame_wall.setdefault(kind, received_at)
                self.last_frame_wall[kind] = received_at
            for sample in samples:
                self._put((kind, sample))

    def _put(self, item):
        try:
            self.rx_queue.put_nowait(item)
        except queue.Full:
            self.ui_dropped += 1

    def stop(self):
        self.stop_event.set()


class SerialWorker(SampleWorker):
    def __init__(self, port, baud):
        super().__init__()
        self.port, self.baud = port, baud
        self.description = f"{port} @ {baud}"

    def run(self):
        try:
            if serial is None:
                raise RuntimeError("缺少 pyserial")
            with serial.Serial(self.port, self.baud, timeout=0.05) as ser:
                ser.reset_input_buffer()
                self.state = "connected"
                while not self.stop_event.is_set():
                    chunk = ser.read(ser.in_waiting or 1)
                    if chunk:
                        self.feed_bytes(chunk)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = str(exc)
        finally:
            self.state = "disconnected"


class BleWorker(SampleWorker):
    def __init__(self, target: BleTarget, settle_seconds=1.0):
        super().__init__()
        self.target = target
        self.description = f"BLE {target.name}\n{target.address}"
        self.settle_seconds = settle_seconds
        self._first_notification = None
        self.startup_bytes = 0
        self.startup_stats = {}
        self.mtu_size = None

    # Share the cancellable event loop without introducing a second thread.
    run = AsyncWorker.run
    _until_stopped = AsyncWorker._until_stopped

    def _notification(self, _characteristic, data):
        if self.stop_event.is_set():
            return
        now = time.monotonic()
        if self._first_notification is None:
            self._first_notification = now
        if now - self._first_notification < self.settle_seconds:
            # Transparent modules can retain UART data collected while BLE was
            # disconnected. Drain that startup backlog before plotting/recording.
            # Parse it to preserve frame alignment across notification boundaries.
            self.startup_bytes += len(data)
            self.parser.feed(bytes(data))
            return
        if self.state != "connected":
            self.startup_stats = dict(vars(self.parser.stats))
            self.parser.reset_statistics()
            self.state = "connected"
        self.feed_bytes(bytes(data))

    async def _session(self):
        if BleakClient is None:
            raise RuntimeError("缺少 bleak，请安装 requirements.txt 中的依赖")
        loop = asyncio.get_running_loop()
        disconnected = asyncio.Event()

        def lost_connection(_client):
            loop.call_soon_threadsafe(disconnected.set)

        try:
            async with BleakClient(self.target.device, timeout=15.0,
                                   disconnected_callback=lost_connection) as client:
                service = client.services.get_service(SERVICE_UUID)
                characteristic = service.get_characteristic(NOTIFY_UUID) if service else None
                if characteristic is None or "notify" not in characteristic.properties:
                    raise RuntimeError("设备不支持 ATK-BLE04 FFF0 / FFF1 通知服务")
                self.mtu_size = client.mtu_size
                self.state = "synchronizing"
                await client.start_notify(characteristic, self._notification)
                await disconnected.wait()
                if not self.stop_event.is_set():
                    raise ConnectionError("蓝牙连接已断开，请检查模块供电、距离并重新扫描连接")
        finally:
            self.state = "disconnected"
