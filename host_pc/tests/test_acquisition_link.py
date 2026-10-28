"""Protocol regressions and a hidden-window test of the actual plotting path."""
import struct
import math
import unittest

from ecg_ppg.acquisition.ecg_ppg_protocol import FrameParser, TIMESTAMP_WRAP_US
from ecg_ppg.gui.ecg_display_filter import NotchFilter


def frame(kind, seq, first, second, timestamp):
    if kind == 1:
        second = 0xFFFF
    else:
        first >>= 2
        second >>= 2
    return struct.pack("<BBBHHIB", 0xAA, kind, seq & 0xFF, first, second,
                       timestamp % TIMESTAMP_WRAP_US, 0x55)


class ProtocolTests(unittest.TestCase):
    def test_dma_retry_does_not_replay_samples_or_invent_sequence_loss(self):
        parser = FrameParser()
        # Full firmware ring retry may cross the 8-bit sequence wrap.
        payload = b"".join(frame(1, i, 2048 + i % 50, 0, i * 5000) for i in range(340))
        first, _ = parser.feed(payload)
        replay, _ = parser.feed(payload + frame(1, 340, 2048, 0, 1700000))
        self.assertEqual(len(first), 340)
        self.assertEqual(len(replay), 1)
        self.assertEqual(parser.stats.duplicate_frames, 340)
        self.assertEqual(parser.stats.lost_frames, 0)
        # Same sequence and value in a later cycle is a legitimate new point.
        more, _ = parser.feed(frame(1, 341, 2048, 0, 1705000))
        self.assertEqual(len(more), 1)

    def test_fragmented_mixed_channels(self):
        data = b"".join(frame(1 if i % 3 else 2, i, 2048 if i % 3 else 40000,
                              120000, 1000000 + i * 5000) for i in range(600))
        for width in (1, 5, 11, 12, 13, 97, 4096):
            parser = FrameParser()
            ecg, ppg = [], []
            for offset in range(0, len(data), width):
                a, b = parser.feed(data[offset:offset + width])
                ecg.extend(a)
                ppg.extend(b)
            self.assertEqual((len(ecg), len(ppg)), (400, 200))
            self.assertTrue(all(s.value == 2048 and s.value2 is None for s in ecg))
            self.assertTrue(all(s.value == 40000 and s.value2 == 120000 for s in ppg))
            self.assertEqual((parser.stats.bad_frames, parser.stats.lost_frames), (0, 0))

    def test_corruption_resynchronizes(self):
        bad = bytearray(frame(1, 1, 2048, 0, 10000))
        bad[-1] = 0
        parser = FrameParser()
        ecg, ppg = parser.feed(b"noise" + frame(1, 0, 1000, 0, 5000)
                               + bad + frame(2, 2, 40000, 120000, 15000))
        self.assertEqual((len(ecg), len(ppg)), (1, 1))
        self.assertEqual(parser.stats.lost_frames, 1)
        self.assertGreater(parser.stats.bad_frames, 0)

    def test_interleaved_fifo_samples_across_wrap(self):
        wrap = TIMESTAMP_WRAP_US
        timestamps = [wrap - 1000, 5000, wrap - 500, 10000, 1000, 15000]
        parser = FrameParser()
        ecg, ppg = parser.feed(b"".join(
            frame(1 if i % 2 == 0 else 2, i, 2048 if i % 2 == 0 else 40000,
                  120000, t) for i, t in enumerate(timestamps)))
        self.assertEqual(parser.stats.timestamp_wraps, 1)
        self.assertEqual([round(s.timestamp_s * 1e6) for s in ecg],
                         [wrap - 1000, wrap - 500, wrap + 1000])
        self.assertEqual([round(s.timestamp_s * 1e6) for s in ppg],
                         [wrap + 5000, wrap + 10000, wrap + 15000])

    def test_full_scale_ppg_is_valid(self):
        ecg, ppg = FrameParser().feed(frame(2, 0, 262143, 262143, 10000))
        self.assertFalse(ecg)
        self.assertEqual((ppg[0].value, ppg[0].value2), (262140, 262140))

    def test_invalid_adc_code_is_rejected(self):
        parser = FrameParser()
        ecg, _ = parser.feed(frame(1, 0, 5000, 0, 5000)
                             + frame(1, 1, 2000, 0, 10000))
        self.assertEqual([s.value for s in ecg], [2000])


class GuiTests(unittest.TestCase):
    def test_separate_channels_units_rates_and_reset(self):
        from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui, SerialWorker, Figure
        self.assertIsNotNone(Figure, "GUI plotting dependencies must be installed")
        app = SerialEcgPpgGui()
        app.withdraw()
        try:
            # This test checks the unchanged received-light mapping explicitly.
            from ecg_ppg.gui.ppg_polarity import ORIGINAL
            app.ppg_polarity_var.set(ORIGINAL)
            worker = SerialWorker("TEST", 115200)
            app.worker = worker
            app.recorder.start()
            ecg, ppg = worker.parser.feed(
                frame(1, 0, 1000, 0, 1000000)
                + frame(2, 1, 40000, 120000, 1000000)
                + frame(1, 2, 2000, 0, 1005000)
                + frame(2, 3, 44000, 120400, 1010000))
            for sample in ecg:
                worker._put(("ecg", sample))
            for sample in ppg:
                worker._put(("ppg", sample))
            app._pump_samples()
            app._refresh_charts()
            self.assertEqual((app.total_ecg, app.total_ppg), (2, 2))
            self.assertEqual((app.latest_red, app.latest_ir), (44000, 120400))
            self.assertAlmostEqual(app.ppg_ir_disp[-1], 392.0)
            self.assertAlmostEqual(app.ppg_red_disp[-1], 3920.0)
            self.assertAlmostEqual(app._ln_ecg.get_ydata()[0], 0.0)
            filtered_second = app._ln_ecg.get_ydata()[1]
            self.assertNotAlmostEqual(app._ln_ppg.get_ydata()[1], app.ppg_ir_disp[-1])
            app.ppg_filter_enabled.set(False)
            app._refresh_charts(schedule=False)
            self.assertAlmostEqual(app._ln_ppg.get_ydata()[1], 392.0)
            self.assertAlmostEqual(app._ln_dc.get_ydata()[1], 3920.0)
            app.notch_enabled.set(False)
            app._refresh_charts()
            self.assertAlmostEqual(app._ln_ecg.get_ydata()[1], -38.671875)
            self.assertNotAlmostEqual(filtered_second, app._ln_ecg.get_ydata()[1])
            ecg_rate, ppg_rate = app._measured_rates()
            self.assertAlmostEqual(ecg_rate, 200.0)
            self.assertAlmostEqual(ppg_rate, 100.0)
            self.assertEqual(len(app._ln_ppg.get_xdata()), 2)
            self.assertEqual(app.recorder.series.ecg_raw, [1000, 2000])
            self.assertEqual(app.recorder.series.ir_v, [120000, 120400])
            self.assertEqual(app.recorder.series.red_v, [40000, 44000])
            self.assertAlmostEqual(app.recorder.series.ecg_t[0], 0.0)
            self.assertAlmostEqual(app.recorder.series.ecg_t[1], 0.005)
            self.assertEqual(app.recorder.series.ir_t[0], 0.0)
            app._reset_display()
            self.assertEqual((app.total_ecg, app.total_ppg), (0, 0))
            self.assertEqual(len(app._ln_ecg.get_ydata()), 0)
        finally:
            app.destroy()


class NotchTests(unittest.TestCase):
    def test_removes_50hz_preserves_slow_wave(self):
        notch = NotchFilter()
        values = [500 + 20 * math.sin(2 * math.pi * 1.2 * i / 200)
                  + 100 * math.sin(2 * math.pi * 50 * i / 200) for i in range(4000)]
        output = [notch.process(x) for x in values]
        def amplitude(signal, frequency):
            points = signal[1000:]
            return abs(sum(x * complex(math.cos(2 * math.pi * frequency * i / 200),
                                       math.sin(2 * math.pi * frequency * i / 200))
                           for i, x in enumerate(points))) * 2 / len(points)
        self.assertLess(amplitude(output, 50), 0.1)
        self.assertAlmostEqual(amplitude(output, 1.2), 20, delta=0.1)

    def test_initial_dc_and_gap_do_not_ring(self):
        notch = NotchFilter()
        self.assertAlmostEqual(notch.process(1500, 0), 1500)
        self.assertAlmostEqual(notch.process(1500, 0.005), 1500)
        self.assertAlmostEqual(notch.process(-500, 1.0), -500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
