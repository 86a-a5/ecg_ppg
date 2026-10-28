"""Negative optical pulses must display upward without reversing time or data."""
import unittest
import numpy as np

from ecg_ppg.gui.ppg_polarity import AutoPpgPolarity, AUTO, ORIGINAL, INVERTED
from ecg_ppg.gui.waveform_display import StreamingDisplayFilter, prepare_trace


def optical_pulses(sign=-1):
    t = np.arange(1200) / 100
    phase = (t - 0.18) % 0.85
    pulse = (phase / 0.045)**2 * np.exp(-phase / 0.045)
    raw = 120000 + sign * 2000 * pulse + 80 * np.sin(2*np.pi*0.12*t)
    return t, (raw // 4 * 4).astype(int)


def filtered(t, raw):
    filt = StreamingDisplayFilter(100, 0.5, 8, reject_steps=True)
    return [filt.process(float(v), float(ts)) for ts, v in zip(t, raw)]


class AutomaticTests(unittest.TestCase):
    def test_opposite_frontend_polarities_resolve_to_upward_pulses(self):
        for expected in (-1, 1):
            t, raw = optical_pulses(expected)
            values = filtered(t, raw)
            auto = AutoPpgPolarity()
            for end in range(400, len(t)+1, 50):
                start = max(0, end-600)
                auto.update(t[start:end].tolist(), raw[start:end].tolist(), values[start:end])
            self.assertTrue(auto.locked)
            self.assertEqual(auto.sign, expected)
            # The dominant excursion now points up for either optical polarity.
            displayed = np.array(values[-600:]) * auto.sign
            centered = displayed - np.median(displayed)
            self.assertGreater(np.mean(centered**3), 0)
            auto.update(t.tolist(), raw.tolist(), [0.0]*len(t))
            self.assertEqual(auto.sign, expected, "a locked result must not flicker on weak signal")
            auto.reset()
            self.assertFalse(auto.locked)
            self.assertEqual(auto.sign, -1)

    def test_flat_sinusoid_noise_and_single_artifact_stay_unresolved(self):
        t = np.arange(1200) / 100
        rng = np.random.default_rng(7)
        candidates = (np.full(len(t), 120000),
                      120000 + 1500*np.sin(2*np.pi*t),
                      120000 + rng.normal(0, 500, len(t)),
                      120000 + 10000*np.exp(-((t-9)/0.04)**2),
                      np.full(len(t), 262140))
        for raw in candidates:
            auto = AutoPpgPolarity()
            auto.update(t[-600:].tolist(), raw[-600:].tolist(), filtered(t, raw)[-600:])
            self.assertFalse(auto.locked)
            self.assertEqual(auto.sign, -1)

    def test_short_interrupted_or_contact_jump_input_does_not_decide(self):
        t, raw = optical_pulses(1)
        values = filtered(t, raw)
        auto = AutoPpgPolarity()
        auto.update(t[:300].tolist(), raw[:300].tolist(), values[:300])
        self.assertFalse(auto.locked)
        t = t[-600:].copy()
        raw = raw[-600:].copy()
        values = values[-600:]
        t[300:] += 0.2
        auto.update(t.tolist(), raw.tolist(), values)
        self.assertFalse(auto.locked)
        t = np.arange(600) / 100
        raw[300:] += 60000
        auto.reset()
        auto.update(t.tolist(), raw.tolist(), filtered(t, raw))
        self.assertFalse(auto.locked)


class DisplayTests(unittest.TestCase):
    def test_real_gui_changes_orientation_but_keeps_raw_model_and_clock(self):
        from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui, SerialWorker
        from ecg_ppg.acquisition.ecg_ppg_protocol import Sample
        from ecg_ppg.paths import OUTPUTS_DIR
        t, ir = optical_pulses(-1)
        red = (50000 + (ir-120000)*0.4).astype(int)
        app = SerialEcgPpgGui()
        app.withdraw()
        try:
            app.worker = SerialWorker("REPLAY", 115200)
            app.recorder.start()
            for i, (ts, r, v) in enumerate(zip(t, red, ir)):
                app.worker._put(("ppg", Sample(int(r), int(v), float(ts), round(ts*1e6), i % 256)))
            app._pump_samples()
            app._refresh_charts(schedule=False)
            self.assertEqual(app.ppg_polarity_var.get(), AUTO)
            self.assertTrue(app.ppg_polarity.locked)
            self.assertEqual(app.ppg_polarity.sign, -1)
            x = np.array(app._ln_ppg.get_xdata())
            y = np.array(app._ln_ppg.get_ydata())
            red_y = np.array(app._ln_dc.get_ydata())
            self.assertTrue(np.all(np.diff(x) > 0))
            # Orientation changes the sign, never the location of extrema.
            _, original = prepare_trace(list(app.ppg_times), list(app.ppg_ir_filtered_disp), 100, smooth=True)
            np.testing.assert_allclose(y, -np.array(original))
            self.assertEqual(int(np.argmax(y)), int(np.argmin(original)))
            for mode in (ORIGINAL, INVERTED):
                app.ppg_polarity_var.set(mode)
                app._on_ppg_polarity_changed()
                np.testing.assert_array_equal(app._ln_ppg.get_xdata(), x)
                np.testing.assert_allclose(app._ln_ppg.get_ydata(), -y if mode == ORIGINAL else y)
                np.testing.assert_allclose(app._ln_dc.get_ydata(), -red_y if mode == ORIGINAL else red_y)
            app.ppg_filter_enabled.set(False)
            app._refresh_charts(schedule=False)
            np.testing.assert_allclose(app._ln_ppg.get_ydata(), -np.array(app.ppg_ir_disp))
            app.ppg_filter_enabled.set(True)
            app._refresh_charts(schedule=False)
            self.assertEqual(app.recorder.series.ir_v, ir.tolist())
            self.assertEqual(app.recorder.series.red_v, red.tolist())
            self.assertEqual(list(app._bp_ppg_v), ir.tolist())
            np.testing.assert_allclose(app.recorder.series.ir_t, t)
            output = OUTPUTS_DIR / "ppg_polarity_validation"
            output.mkdir(parents=True, exist_ok=True)
            app.fig.suptitle("负向光强PPG合成回放（非人体实测）", fontsize=10)
            app.fig.subplots_adjust(top=0.91)
            app.save_waveform_preview(output / "upward_pulses.png", dpi=130)
            app._reset_display()
            self.assertFalse(app.ppg_polarity.locked)
            self.assertEqual(len(app.ppg_ir_raw_disp), 0)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
