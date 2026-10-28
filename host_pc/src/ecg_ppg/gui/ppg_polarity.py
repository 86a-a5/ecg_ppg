"""Display-only PPG polarity. Never changes ADC data, CSV or model input.

MAX30102 reports received light: its systolic excursion is normally negative.
Automatic orientation is a conservative morphology heuristic, not a clinical
feature classifier. Ambiguous/noisy input retains the MAX30102 preset (-1).
"""
import math
from statistics import median, pstdev

AUTO = "自动（MAX30102预设）"
INVERTED = "反相（MAX30102）"
ORIGINAL = "原极性（光强）"
MODES = (AUTO, INVERTED, ORIGINAL)


def percentile(sorted_values, fraction):
    index = (len(sorted_values) - 1) * fraction
    lo = int(index)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (index - lo)


class AutoPpgPolarity:
    """Use >=4 s / >=4 periodic pulses; lock until reconnect or mode reselect.

    A centered sinusoid has no identifiable orientation, so don't arbitrarily
    flip it. Winsorized skewness identifies the narrower dominant excursion;
    amplitude, contiguous timestamps and interval consistency guard the result.
    ECG timing alone is deliberately not used as an absolute 0.15-0.35 s rule.
    """
    def __init__(self, sample_rate=100.0):
        self.sample_rate = sample_rate
        self.reset()

    def reset(self):
        self.sign = -1
        self.locked = False
        self.reason = "待判定，使用MAX30102反相预设"
        self._last_checked = None

    def update(self, timestamps, raw_values, filtered_values):
        if self.locked or len(timestamps) == 0:
            return self.sign
        if not (len(timestamps) == len(raw_values) == len(filtered_values)):
            return self.sign
        end = timestamps[-1]
        if self._last_checked is not None and 0 <= end - self._last_checked < 0.5:
            return self.sign
        self._last_checked = end
        start = 0
        for i, (t, raw, value) in enumerate(zip(timestamps, raw_values, filtered_values)):
            if not all(math.isfinite(v) for v in (t, raw, value)):
                start = i + 1
            elif i and (not 0 < t - timestamps[i-1] <= 1.5 / self.sample_rate
                        or abs(raw - raw_values[i-1]) > max(2000, abs(raw) * 0.2)):
                start = i
        times = timestamps[start:]
        raw = raw_values[start:]
        values = filtered_values[start:]
        if len(values) < 4 * self.sample_rate or times[-1] - times[0] < 3.9:
            return self.sign
        dc = median(raw)
        if not 3000 <= dc <= 250000 or any(v <= 0 or v >= 262140 for v in raw):
            return self.sign
        ordered = sorted(values)
        span = percentile(ordered, 0.95) - percentile(ordered, 0.05)
        if span < max(8, dc * 0.0005):
            return self.sign
        lo, hi = percentile(ordered, 0.01), percentile(ordered, 0.99)
        clipped = [min(hi, max(lo, v)) for v in values]
        mean = sum(clipped) / len(clipped)
        sigma = pstdev(clipped)
        if sigma <= 0:
            return self.sign
        skewness = sum(((v - mean) / sigma) ** 3 for v in clipped) / len(clipped)
        if abs(skewness) < 0.4:
            return self.sign
        candidate = 1 if skewness > 0 else -1
        center = median(values)
        wave = [candidate * (v - center) for v in values]
        distance = max(1, round(0.3 * self.sample_rate))
        radius = max(1, round(0.3 * self.sample_rate))
        peaks = []
        for i in range(radius, len(wave) - radius):
            if not (wave[i] > wave[i-1] and wave[i] >= wave[i+1] and wave[i] > 0.3 * span):
                continue
            prominence = wave[i] - max(min(wave[i-radius:i]), min(wave[i+1:i+radius+1]))
            if prominence < 0.25 * span:
                continue
            if peaks and i - peaks[-1] < distance:
                if wave[i] > wave[peaks[-1]]:
                    peaks[-1] = i
            else:
                peaks.append(i)
        if len(peaks) < 4:
            return self.sign
        intervals = [times[b] - times[a] for a, b in zip(peaks, peaks[1:])]
        period = median(intervals)
        if not 0.3 <= period <= 1.5 or pstdev(intervals) / period > 0.2:
            return self.sign
        self.sign = candidate
        self.locked = True
        self.reason = f"周期形态已判定并锁定：×{candidate:+d}"
        return self.sign
