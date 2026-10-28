"""Stateful second-order 50 Hz notch for display; recorded ADC data stays raw."""
import math


class NotchFilter:
    def __init__(self, sample_rate=200.0, frequency=50.0, quality_factor=20.0):
        if not 0 < frequency < sample_rate / 2 or quality_factor <= 0:
            raise ValueError("Notch frequency must be below Nyquist; Q must be positive")
        self.sample_rate = float(sample_rate)
        omega = 2 * math.pi * frequency / sample_rate
        beta = math.tan(omega / (2 * quality_factor))
        gain = 1 / (1 + beta)
        self.b0 = self.b2 = gain
        self.b1 = self.a1 = -2 * gain * math.cos(omega)
        self.a2 = 2 * gain - 1
        self.reset()

    def reset(self):
        self.s1 = self.s2 = 0.0
        self.initialized = False
        self.last_timestamp = None

    def process(self, value, timestamp=None):
        value = float(value)
        if timestamp is not None and self.last_timestamp is not None:
            dt = timestamp - self.last_timestamp
            if dt <= 0 or dt > 3 / self.sample_rate:
                self.reset()
        if not self.initialized:
            # Initialize to the input's DC steady state, avoiding a startup spike.
            self.s1 = (1 - self.b0) * value
            self.s2 = (self.b2 - self.a2) * value
            self.initialized = True
        result = self.b0 * value + self.s1
        self.s1 = self.b1 * value - self.a1 * result + self.s2
        self.s2 = self.b2 * value - self.a2 * result
        self.last_timestamp = timestamp
        return result
