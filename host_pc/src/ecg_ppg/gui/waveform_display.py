"""Streaming display filters and plotting helpers; never used for CSV or PAT.

ECG: 0.5 Hz high pass -> 50 Hz notch -> 40 Hz Butterworth low pass.
PPG: 0.5 Hz high pass -> 8 Hz Butterworth low pass, independent IR/RED states.
The standard-library implementation keeps the GUI's optional-SciPy behavior.
"""
from __future__ import annotations

import math

from ecg_ppg.gui.ecg_display_filter import NotchFilter

# One missing sample is already a discontinuity. Allow ordinary polling jitter.
GAP_PERIODS = 1.5


class StreamingDisplayFilter:
    def __init__(self, sample_rate, highpass=0.5, lowpass=40.0, *,
                 notch_frequency=None, notch_q=30.0, reject_steps=False):
        self.sample_rate = float(sample_rate)
        if not (math.isfinite(self.sample_rate) and 0 < highpass < lowpass < self.sample_rate / 2):
            raise ValueError("Require 0 < highpass < lowpass < Nyquist")
        hp_k = math.tan(math.pi * highpass / self.sample_rate)
        self.hp_b0 = 1.0 / (1.0 + hp_k)
        self.hp_a1 = (hp_k - 1.0) / (hp_k + 1.0)
        k = math.tan(math.pi * lowpass / self.sample_rate)
        norm = 1.0 / (1.0 + math.sqrt(2.0) * k + k * k)
        self.b0 = self.b2 = k * k * norm
        self.b1 = 2.0 * self.b0
        self.a1 = 2.0 * (k * k - 1.0) * norm
        self.a2 = (1.0 - math.sqrt(2.0) * k + k * k) * norm
        self.notch = (NotchFilter(self.sample_rate, notch_frequency, notch_q)
                      if notch_frequency is not None else None)
        self.reject_steps = reject_steps
        self.reset()

    def reset(self):
        self.previous = None
        self.hp_output = 0.0
        self.s1 = self.s2 = 0.0
        self.last_timestamp = None
        if self.notch is not None:
            self.notch.reset()

    def process(self, value, timestamp=None):
        value = float(value)
        if not math.isfinite(value) or (timestamp is not None and not math.isfinite(timestamp)):
            self.reset()
            return math.nan
        if timestamp is not None and self.last_timestamp is not None:
            dt = timestamp - self.last_timestamp
            if dt <= 0 or dt > GAP_PERIODS / self.sample_rate:
                self.reset()
        if (self.reject_steps and self.previous is not None
                and abs(value - self.previous) > max(2000.0, abs(self.previous) * 0.35)):
            # Finger placement/DC steps must not create a giant filter tail.
            # This affects display only: recorder still receives the raw value.
            self.reset()
            self.previous = value
            self.last_timestamp = timestamp
            # A discontinuity marker prevents SG smoothing and plotting from
            # joining the old contact level to the newly initialized baseline.
            return math.nan
        if self.previous is None:
            self.previous = value
        self.hp_output = self.hp_b0 * (value - self.previous) - self.hp_a1 * self.hp_output
        self.previous = value
        x = self.notch.process(self.hp_output) if self.notch is not None else self.hp_output
        output = self.b0 * x + self.s1
        self.s1 = self.b1 * x - self.a1 * output + self.s2
        self.s2 = self.b2 * x - self.a2 * output
        self.last_timestamp = timestamp
        return output


def prepare_trace(timestamps, values, sample_rate, *, smooth=False):
    """Keep source timestamps, break at gaps, optionally apply centered SG(5,2).

    A centered output keeps the center sample's timestamp, with 2-sample display
    latency. Filter phase delays are NOT compensated for feature extraction.
    """
    if len(timestamps) != len(values):
        raise ValueError("Display times and values must have equal length")
    out_t, out_y = [], []
    segment = []

    def flush():
        if not segment:
            return
        if out_t:
            out_t.append(segment[0][0])
            out_y.append(math.nan)
        if smooth and len(segment) >= 5:
            weights = (-3.0, 12.0, 17.0, 12.0, -3.0)
            for i in range(2, len(segment) - 2):
                out_t.append(segment[i][0])
                out_y.append(sum(w * segment[i + j - 2][1] for j, w in enumerate(weights)) / 35.0)
        else:
            out_t.extend(t for t, _ in segment)
            out_y.extend(y for _, y in segment)
        segment.clear()

    for timestamp, value in zip(timestamps, values):
        timestamp, value = float(timestamp), float(value)
        if not (math.isfinite(timestamp) and math.isfinite(value)):
            flush()
            continue
        if segment and not 0 < timestamp - segment[-1][0] <= GAP_PERIODS / sample_rate:
            flush()
        segment.append((timestamp, value))
    flush()
    return out_t, out_y


class StableRange:
    """Symmetric, peak-preserving 1-2-5 ranges with sample-clock hysteresis."""
    def __init__(self, minimum=50.0, initial=500.0):
        self.minimum = float(minimum)
        self.initial = float(initial)
        self.reset()

    def reset(self):
        self.half_range = self.initial
        self.quiet_since = None
        self.last_timestamp = None

    def _nice_range(self, target):
        target = max(self.minimum, target)
        base = 10.0 ** math.floor(math.log10(target))
        for factor in (1.0, 2.0, 5.0, 10.0):
            if factor * base >= target:
                return factor * base

    def update(self, values, timestamp):
        maximum = max((abs(float(v)) for v in values if math.isfinite(v)), default=0.0)
        target = self._nice_range(maximum / 0.85)
        if target > self.half_range:
            self.half_range = target
            self.quiet_since = None
        elif target < self.half_range * 0.6:
            if self.quiet_since is None or (self.last_timestamp is not None and timestamp < self.last_timestamp):
                self.quiet_since = timestamp
            if timestamp - self.quiet_since >= 3.0:
                self.half_range = target
                self.quiet_since = None
        else:
            self.quiet_since = None
        self.last_timestamp = timestamp
        return -self.half_range, self.half_range
