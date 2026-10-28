"""Input contracts, stale replies, original recording, and optional real inference."""
import csv
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import time
import unittest
from types import SimpleNamespace

import numpy as np

from ecg_ppg.acquisition.ecg_ppg_protocol import ecg_adc_to_mv
from ecg_ppg.models.unicardio_inference import SignalQualityError, prepare_input, read_prediction_csv
from ecg_ppg.paths import OUTPUTS_DIR


def signals():
    et, pt = np.arange(1601) / 200, np.arange(801) / 100
    ecg = 350 * np.sin(2 * np.pi * 5 * et) + 100 * np.sin(2 * np.pi * 50 * et)
    ppg = 120000 + 2200 * np.sin(2 * np.pi * 1.25 * (pt - 0.2))
    adc = np.rint(2048 + ecg * 4096 / 3300).astype(int)
    return pt, ppg, et, ecg_adc_to_mv(adc), adc


class InputTests(unittest.TestCase):
    def test_native_rates_alignment_slots_and_raw_immutability(self):
        args = signals()
        copies = [x.copy() for x in args]
        x, info = prepare_input(*args)
        self.assertEqual(x.shape, (2000,))
        self.assertEqual(x.dtype, np.float32)
        np.testing.assert_array_equal(x[500:1000], 0)
        np.testing.assert_array_equal(x[1500:], 0)
        self.assertLessEqual(abs(x).max(), 1)
        times = info["start_s"] + np.arange(500) / 125
        # Independent phase oracle: synchronized resampling must retain timing.
        self.assertGreater(np.corrcoef(x[:500], np.sin(2*np.pi*1.25*(times-0.2)))[0, 1], 0.97)
        self.assertGreater(np.corrcoef(x[1000:1500], np.sin(2*np.pi*5*times))[0, 1], 0.97)
        for actual, before in zip(args, copies):
            np.testing.assert_array_equal(actual, before)

    def test_quality_rejects_clipping_gap_step_and_invalid_time(self):
        pt, pv, et, ev, adc = signals()
        clipped = adc.copy()
        clipped[-250:] = 4095
        with self.assertRaisesRegex(SignalQualityError, "上下限"):
            prepare_input(pt, pv, et, ev, clipped)
        mask = ~((et > 5) & (et < 5.1))
        with self.assertRaisesRegex(SignalQualityError, "中断"):
            prepare_input(pt, pv, et[mask], ev[mask], adc[mask])
        stepped = pv.copy()
        stepped[-100:] += 40000
        with self.assertRaisesRegex(SignalQualityError, "突变"):
            prepare_input(pt, stepped, et, ev, adc)
        bad = et.copy()
        bad[-1] = bad[-2]
        with self.assertRaisesRegex(SignalQualityError, "倒退"):
            prepare_input(pt, pv, bad, ev, adc)
        with self.assertRaisesRegex(SignalQualityError, "至少"):
            prepare_input(pt[:400], pv[:400], et[:800], ev[:800], adc[:800])

    def test_csv_prefers_adc_over_mislabelled_processed_values(self):
        pt, pv, et, ev, adc = signals()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            write_input(path, processed_as_adc=True)
            actual = read_prediction_csv(path)
            np.testing.assert_array_equal(actual[4], adc)
            np.testing.assert_allclose(actual[3], ev)

    def test_missing_model_export_preserves_all_500_samples(self):
        from ecg_ppg.models.run_unicardio_bp import _export_segments_only
        x, _ = prepare_input(*signals())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "segments.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                _export_segments_only(x[None, :], SimpleNamespace(output=str(path), input="unused.csv"))
            with path.open(encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))
            self.assertEqual(len(rows), 500)
            self.assertEqual(rows[-1]["sample_index"], "499")


def write_input(path, processed_as_adc=False):
    pt, pv, et, ev, adc = signals()
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["ecg_sync_time_s", "ecg_processed", "ecg_raw", "ir_sync_time_s", "ir_processed"])
        for i in range(len(et)):
            writer.writerow([et[i], adc[i] if processed_as_adc else ev[i], adc[i],
                             pt[i] if i < len(pt) else "", pv[i] if i < len(pt) else ""])


def example_result():
    wave = (95 + 20*np.sin(2*np.pi*1.25*np.arange(500)/125)).tolist()
    return dict(start_s=4.0, end_s=8.0, sbp=115.0, dbp=75.0, map=95.0,
                inference_seconds=0.1, device="test", ecg_clipped_fraction=0.0,
                bp_mmhg=wave, ecg_input=[0.0]*500, ppg_input=[0.0]*500)


class FakeClient:
    def __init__(self):
        self.events = queue.Queue()
        self.ready, self.busy = True, False
        self.jobs = []

    def submit(self, request):
        self.jobs.append(request)
        self.busy = True
        return True

    def close(self):
        self.ready = False


class GuiTests(unittest.TestCase):
    def test_stale_reply_csv_export_and_live_queue_backpressure(self):
        from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui
        app = SerialEcgPpgGui()
        app.withdraw()
        try:
            client = app.bp_client = FakeClient()
            app._bp_generation = 2
            client.events.put({"type": "result", "id": 1, "result": example_result()})
            app._poll_bp(schedule=False)
            self.assertIsNone(app._bp_result)
            app._bp_source = "synthetic test"
            client.events.put({"type": "result", "id": 2, "result": example_result()})
            app._poll_bp(schedule=False)
            self.assertIn("115.0", app.bp_values.get())
            self.assertGreater(app.bp_ax.get_ylim()[1], max(app._bp_result["bp_mmhg"]))
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "prediction.csv"
                app._write_bp_csv(output)
                with output.open(encoding="utf-8-sig", newline="") as fp:
                    rows = list(csv.DictReader(fp))
                self.assertEqual(len(rows), 500)
                self.assertEqual(rows[0]["source"], "synthetic test")
                self.assertAlmostEqual(float(rows[-1]["time_s"]), 7.992)
            pt, pv, et, _, adc = signals()
            app._bp_ecg_t.extend(et.tolist())
            app._bp_ecg_adc.extend(adc.tolist())
            app._bp_ppg_t.extend(pt.tolist())
            app._bp_ppg_v.extend(pv.tolist())
            app.bp_enabled = True
            app.last_data_time = time.monotonic()
            app.last_ecg_time = app.last_ppg_time = app.last_data_time
            app._poll_bp(schedule=False)
            app._poll_bp(schedule=False)
            self.assertEqual(len(client.jobs), 1)
            self.assertEqual(client.jobs[0]["ppg_v"], pv.tolist())
            app.last_ppg_time = time.monotonic() - 5
            app._poll_bp(schedule=False)
            self.assertFalse(app._bp_valid, "A stalled PPG channel must invalidate the old reading")
            client.events.put({"type": "quality", "id": 2, "message": "ECG 饱和"})
            app._poll_bp(schedule=False)
            self.assertFalse(app._bp_valid)
            self.assertNotIn("115.0", app.bp_values.get())
        finally:
            app._on_close()

    @unittest.skipUnless(os.environ.get("ECG_PPG_TEST_MODEL") == "1", "real weights: ECG_PPG_TEST_MODEL=1")
    def test_real_model_in_gui_while_acquisition_continues(self):
        from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui, SerialWorker
        from tests.test_acquisition_link import frame
        output = OUTPUTS_DIR / "bp_validation"
        output.mkdir(parents=True, exist_ok=True)
        supplied = os.environ.get("ECG_PPG_TEST_CSV")
        prefix = "historical" if supplied else "synthetic"
        input_path = Path(supplied) if supplied else output / "synthetic_input.csv"
        if not supplied:
            write_input(input_path)
        app = SerialEcgPpgGui()
        app.withdraw()
        try:
            app._queue_bp_csv(input_path)
            app.worker = SerialWorker("TEST", 115200)
            app.recorder.start()
            deadline = time.monotonic() + 120
            samples = 0
            last_injected = 0.0
            while app._bp_result is None and time.monotonic() < deadline:
                now = time.monotonic()
                if now - last_injected >= 0.1:
                    payload = bytearray()
                    for j in range(20):
                        i = samples + j
                        payload.extend(frame(1, i, 2100, 0, 1000000 + i*5000))
                    ecg, _ = app.worker.parser.feed(payload)
                    for sample in ecg:
                        app.worker._put(("ecg", sample))
                    samples += 20
                    last_injected = now
                app.update()
                if app.bp_client is None:
                    self.fail(app.bp_status.get())
                if not app._bp_pending and not app.bp_client.busy and "暂停" in app.bp_status.get():
                    self.fail(app.bp_status.get())
                time.sleep(0.01)
            self.assertIsNotNone(app._bp_result, app.bp_status.get())
            self.assertGreater(app.total_ecg, 20, "Serial samples must keep reaching the GUI during inference")
            self.assertEqual(len(app.recorder.series.ecg_raw), app.total_ecg)
            self.assertTrue(all(value == 2100 for value in app.recorder.series.ecg_raw))
            self.assertEqual(len(app._bp_result["bp_mmhg"]), 500)
            self.assertGreater(app._bp_result["sbp"], app._bp_result["dbp"])
            app._write_bp_csv(output / f"{prefix}_prediction.csv")
            title = "历史实采输入的模型预测（无同步参考血压）" if supplied else "合成输入测试（非人体血压实测）"
            app.bp_fig.suptitle(title, fontsize=10)
            app.bp_fig.savefig(output / f"{prefix}_prediction.png", dpi=130)
            report = dict(app._bp_result)
            report.pop("bp_mmhg")
            report.pop("ecg_input")
            report.pop("ppg_input")
            report["ecg_points_received_during_prediction"] = app.total_ecg
            report["input_kind"] = "historical_raw_capture" if supplied else "synthetic"
            (output / f"{prefix}_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            # The same loaded process must report a rejected input without exiting.
            client = app.bp_client
            pid = client.process.pid
            pt, pv, et, _, adc = signals()
            bad = {"type": "live", "id": app._bp_generation, "ppg_t": pt.tolist(),
                   "ppg_v": pv.tolist(), "ecg_t": et.tolist(), "ecg_adc": [4095]*len(adc)}
            self.assertTrue(client.submit(bad))
            deadline = time.monotonic() + 5
            while app._bp_valid and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            self.assertFalse(app._bp_valid, "Rejected input must clear the numeric reading")
            self.assertIn("上下限", app.bp_status.get())
            self.assertTrue(client.ready)
            self.assertEqual(client.process.pid, pid)
        finally:
            client = app.bp_client
            app._on_close()
            if client is not None and client.thread is not None:
                client.thread.join(timeout=5)
                self.assertFalse(client.thread.is_alive(), "Worker must stop when the GUI closes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
