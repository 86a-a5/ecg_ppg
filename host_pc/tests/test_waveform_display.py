"""Numerical-oracle, timestamp, recording and real Tk/Matplotlib regressions."""
import math
from pathlib import Path
import unittest

import numpy as np
from scipy import signal

from ecg_ppg.gui.waveform_display import StreamingDisplayFilter, StableRange, prepare_trace
from ecg_ppg.paths import OUTPUTS_DIR


def ecg_wave(t):
    phase = np.asarray(t) % 0.8
    gaussian = lambda center, width: np.exp(-0.5 * ((phase - center) / width) ** 2)
    return (50 * gaussian(0.08, 0.025) - 90 * gaussian(0.176, 0.008)
            + 600 * gaussian(0.2, 0.012) - 210 * gaussian(0.229, 0.012)
            + 140 * gaussian(0.44, 0.055))


class FilterTests(unittest.TestCase):
    def test_matches_independent_scipy_filter_design(self):
        rng = np.random.default_rng(42)
        for fs, lowpass, notch in ((200.0, 40.0, 50.0), (100.0, 8.0, None)):
            x = 10000 + rng.normal(0, 100, 5000)
            filt = StreamingDisplayFilter(fs, 0.5, lowpass, notch_frequency=notch)
            actual = np.array([filt.process(v, i / fs) for i, v in enumerate(x)])
            b, a = signal.butter(1, 0.5, btype="highpass", fs=fs)
            expected, _ = signal.lfilter(b, a, x, zi=signal.lfilter_zi(b, a) * x[0])
            if notch:
                b, a = signal.iirnotch(notch, 30, fs=fs)
                expected = signal.lfilter(b, a, expected)
            b, a = signal.butter(2, lowpass, fs=fs)
            expected = signal.lfilter(b, a, expected)
            np.testing.assert_allclose(actual, expected, atol=2e-8, rtol=1e-8)

    def gain(self, fs, frequency, lowpass, notch=None):
        t = np.arange(int(fs * 20)) / fs
        filt = StreamingDisplayFilter(fs, 0.5, lowpass, notch_frequency=notch)
        y = np.array([filt.process(v) for v in np.sin(2 * np.pi * frequency * t)])
        return abs(np.mean(y[len(y)//2:] * np.exp(-2j * np.pi * frequency * t[len(y)//2:]))) * 2

    def test_channel_specific_frequency_response(self):
        self.assertLess(self.gain(200, 50, 40, 50), 1e-8)
        self.assertGreater(self.gain(200, 10, 40, 50), 0.98)
        self.assertGreater(self.gain(200, 20, 40, 50), 0.96)
        self.assertAlmostEqual(self.gain(200, 40, 40, 50), 2 ** -0.5, delta=0.01)
        self.assertGreater(self.gain(100, 1.2, 8), 0.90)
        self.assertGreater(self.gain(100, 4, 8), 0.95)
        self.assertLess(self.gain(100, 30, 8), 0.04)
        # 50 Hz is at Nyquist for the PPG channel: do not design an IIR notch there.
        with self.assertRaises(ValueError):
            StreamingDisplayFilter(100, 0.5, 8, notch_frequency=50)

    def test_initial_dc_gap_reset_steps_and_invalid_inputs(self):
        filt = StreamingDisplayFilter(100, 0.5, 8, reject_steps=True)
        self.assertEqual(filt.process(100000, 0), 0)
        self.assertEqual(filt.process(100000, 0.01), 0)
        self.assertTrue(math.isnan(filt.process(200000, 0.02)))
        self.assertEqual(filt.process(200000, 0.03), 0)
        self.assertEqual(filt.process(50000, 1.0), 0)
        self.assertTrue(math.isnan(filt.process(math.nan, 1.01)))
        self.assertEqual(filt.process(80000, 1.02), 0)
        self.assertEqual(filt.process(80000, 0.5), 0)

    def test_ecg_display_smoothing_keeps_r_peak(self):
        fs = 200
        t = np.arange(4000) / fs
        clean = ecg_wave(t)
        filt = StreamingDisplayFilter(fs, 0.5, 40, notch_frequency=50)
        y = np.array([filt.process(v) for v in clean])
        ts, smoothed = prepare_trace(t, y, fs, smooth=True)
        ts, smoothed = np.array(ts), np.array(smoothed)
        for center in (8.2, 9.0, 9.8):
            original = (t > center - 0.05) & (t < center + 0.05)
            displayed = (ts > center - 0.05) & (ts < center + 0.05)
            self.assertGreater(smoothed[displayed].max(), y[original].max() * 0.95)
            self.assertAlmostEqual(ts[displayed][np.argmax(smoothed[displayed])],
                                   t[original][np.argmax(y[original])], delta=1/fs)


class TraceTests(unittest.TestCase):
    def test_single_missing_sample_breaks_trace_and_resets_filter(self):
        for fs in (100, 200):
            t = np.arange(40) / fs
            t[20:] += 1 / fs
            xs, ys = prepare_trace(t, np.ones(40), fs, smooth=True)
            self.assertEqual(sum(math.isnan(y) for y in ys), 1)
            filt = StreamingDisplayFilter(fs, 0.5, 8)
            filt.process(10000, 0)
            self.assertNotEqual(filt.process(11000, 1 / fs), 0)
            self.assertEqual(filt.process(11000, 3 / fs), 0)

    def test_contact_step_is_not_smoothed_across(self):
        t = np.arange(100) / 100
        raw = np.where(t < 0.5, 100000, 200000)
        filt = StreamingDisplayFilter(100, 0.5, 8, reject_steps=True)
        values = [filt.process(v, timestamp) for v, timestamp in zip(raw, t)]
        _, ys = prepare_trace(t, values, 100, smooth=True)
        self.assertEqual(sum(math.isnan(y) for y in ys), 1)
        self.assertTrue(all(y == 0 for y in ys if math.isfinite(y)))

    def test_center_timestamps_polynomial_and_gaps(self):
        t = np.arange(20) / 200
        y = 1 + 2*t + 3*t*t
        xs, ys = prepare_trace(t, y, 200, smooth=True)
        np.testing.assert_allclose(xs, t[2:-2])
        np.testing.assert_allclose(ys, y[2:-2])
        t[10:] += 0.5
        xs, ys = prepare_trace(t, y, 200, smooth=True)
        self.assertEqual(sum(math.isnan(v) for v in ys), 1)

    def test_range_preserves_narrow_peak_and_ignores_idle_repaints(self):
        scale = StableRange(40, 500)
        lo, hi = scale.update([0] * 1000 + [2500], 0)
        self.assertLess(lo, -2500)
        self.assertGreater(hi, 2500)
        for _ in range(100):
            self.assertEqual(scale.update([20, -20], 1), (lo, hi))
        self.assertEqual(scale.update([20, -20], 3.9), (lo, hi))
        self.assertLess(scale.update([20, -20], 4.1)[1], hi)


class ActualGuiReplayTests(unittest.TestCase):
    def test_offscreen_spike_does_not_flatten_visible_waveform(self):
        from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui
        app = SerialEcgPpgGui()
        app.withdraw()
        try:
            timestamps = [0.0] + [18 + i / 200 for i in range(401)]
            app.ecg_times.extend(timestamps)
            app.ecg_disp.extend([2048] * len(timestamps))
            app.ecg_filtered_disp.extend([1e6] + [20 * math.sin(i / 20) for i in range(401)])
            app._refresh_charts(schedule=False)
            self.assertLess(app.ax_ecg.get_ylim()[1], 1000)
        finally:
            app.destroy()

    def test_filtered_replay_clock_raw_recording_blit_and_preview(self):
        from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui, SerialWorker
        from tests.test_acquisition_link import frame
        app = SerialEcgPpgGui()
        app.withdraw()
        try:
            app.worker = SerialWorker("TEST", 115200)
            app.recorder.start()
            payload = bytearray()
            sequence = 0
            for i in range(1600):
                t = i / 200
                ecg_mv = float(ecg_wave(t)) + 220 * math.sin(2 * math.pi * 50 * t)
                ecg_mv += 25 * math.sin(2 * math.pi * 80 * t)
                adc = int(round(2048 + ecg_mv * 4096 / 3300))
                payload.extend(frame(1, sequence, adc, 0, int((1+t)*1e6)))
                sequence += 1
                if i % 2 == 0:
                    pulse = math.sin(2 * math.pi * 1.25 * (t - 0.22))
                    pulse += 0.25 * math.sin(2 * math.pi * 2.5 * (t - 0.22))
                    noise = math.sin(2 * math.pi * 30 * t)
                    ir = int(120000 + 2200*pulse + 900*noise)
                    red = int(50000 + 900*pulse + 400*noise)
                    payload.extend(frame(2, sequence, red, ir, int((1+t)*1e6)))
                    sequence += 1
            ecg, ppg = app.worker.parser.feed(payload)
            for kind, samples in (("ecg", ecg), ("ppg", ppg)):
                for sample in samples:
                    app.worker._put((kind, sample))
            app._pump_samples()
            app._refresh_charts(schedule=False)
            self.assertEqual(app.recorder.series.ecg_raw, [sample.value for sample in ecg])
            self.assertEqual(app.recorder.series.ir_v, [sample.value2 for sample in ppg])
            self.assertEqual(app.recorder.series.red_v, [sample.value for sample in ppg])
            self.assertEqual(app.recorder.series.ir_t, [sample.timestamp_s - 1.0 for sample in ppg])
            self.assertEqual((len(app.ecg_disp), len(app.ppg_ir_filtered_disp)), (1200, 600))
            now = max(app.ecg_times[-1], app.ppg_times[-1])
            self.assertAlmostEqual(app._ln_ecg.get_xdata()[-1], app.ecg_times[-3] - now)
            self.assertAlmostEqual(app._ln_ppg.get_xdata()[-1], app.ppg_times[-3] - now)
            # Establish the background, then prove stable data uses the blit path.
            app.canvas.draw()
            background = app._chart_background
            self.assertIsNotNone(background)
            for _ in range(20):
                app._refresh_charts(schedule=False)
                self.assertIs(app._chart_background, background)
            app._on_chart_resize(None)
            self.assertIsNone(app._chart_background)
            app.canvas.draw()
            output = OUTPUTS_DIR / "display_validation"
            output.mkdir(parents=True, exist_ok=True)
            app.fig.suptitle("合成测试数据（非人体实测）", fontsize=10, y=0.995)
            app.fig.subplots_adjust(top=0.90)
            app.save_waveform_preview(output / "synthetic_waveforms.png", dpi=130)
            self.assertEqual(app._ln_ecg.get_animated(), app._use_blit)
            app._reset_display()
            self.assertEqual(len(app.ppg_ir_filtered_disp), 0)
            self.assertIsNone(app.ir_filter.previous)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
