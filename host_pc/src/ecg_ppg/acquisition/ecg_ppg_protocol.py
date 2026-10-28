# -*- coding: utf-8 -*-
"""
ecg_ppg_protocol.py
===================
ECG(AD8232) + PPG(MAX30102) 下位机串口协议解析器。

协议（12 字节定长帧，小端）
--------------------------
    [0]      0xAA          帧头
    [1]      帧类型        0x01 = ECG / 0x02 = PPG
    [2]      序号          ECG 与 PPG 共用一个 8 位递增计数器（0~255 循环）
    [3..4]   ECG: uint16 ADC 原始值 (0~4095，中点约 2048)
             PPG: uint16 RED (MAX30102 18bit 原始值 >> 2)
    [5..6]   ECG: 0xFF 0xFF 填充
             PPG: uint16 IR  (MAX30102 18bit 原始值 >> 2)
    [7..10]  uint32 时间戳，TIM5 @1MHz，单位 us，小端，约 71.6 分钟回绕
    [11]     0x55          帧尾

采样率：ECG 200 Hz，PPG 100 Hz，合计 3600 B/s（115200 波特率下余量充足）。

时间戳处理
----------
下位机的 TIM5 是 32 位、1MHz，约 71.6 分钟回绕一次。本模块在解析时做
「回绕展开（unwrap）」，把时间戳还原成单调递增的秒数，因此：
  - 可以直接做跨回绕点的时间对齐与重采样；
  - 不会因为回绕产生负的 dt 或巨大的跳变。

这是与原始 ble04_pc_gui 相比的关键修正：原脚本直接使用 32 位原始时间戳差值，
在回绕点会算出错误的心率/采样率。
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
FRAME_HEAD = 0xAA
FRAME_TAIL = 0x55
FRAME_LEN = 12
TYPE_ECG = 0x01
TYPE_PPG = 0x02

ECG_SAMPLE_RATE_HZ = 200.0
PPG_SAMPLE_RATE_HZ = 100.0

# TIM5：32 位 @1MHz，回绕周期
TIMESTAMP_WRAP_US = 1 << 32          # 4294967296 us ≈ 71.58 min
TIMESTAMP_WRAP_S = TIMESTAMP_WRAP_US / 1e6

# MAX30102：18bit 满量程，协议里传的是 >>2 后的 16bit
PPG_FULL_SCALE_18BIT = 262143
PPG_PROTOCOL_SHIFT = 2

# AD8232 ADC：12bit，VREF=3300mV
ECG_ADC_MAX = 4095
ECG_ADC_CENTER = 2048
ECG_VREF_MV = 3300.0


@dataclass
class Sample:
    """一个解析完成的采样点，时间戳已展开为单调递增的秒数。"""
    value: int            # ECG: ADC 码值 / PPG: RED（18 位量级，低 2 位已丢失）
    value2: Optional[int] # PPG: IR（同上）；ECG 为 None
    timestamp_s: float    # 回绕展开后的秒数
    t_us_raw: int         # 原始 32 位时间戳（调试用）
    sequence: int


@dataclass
class ParserStats:
    """解析统计，用于诊断链路质量。"""
    total_bytes: int = 0
    ecg_frames: int = 0
    ppg_frames: int = 0
    bad_frames: int = 0        # 头/尾校验失败
    lost_frames: int = 0       # 由序号跳变推算出的丢帧数
    fifo_overflow: int = 0     # 保留字段
    timestamp_wraps: int = 0   # 时间戳回绕次数
    duplicate_frames: int = 0  # UART 重试的完全相同帧，未送入滤波/录制

    @property
    def total_frames(self) -> int:
        return self.ecg_frames + self.ppg_frames

    @property
    def frame_loss_rate(self) -> float:
        expected = self.total_frames + self.lost_frames
        return (self.lost_frames / expected) if expected else 0.0


class FrameParser:
    """把串口字节流切分成帧，并解析成 Sample 列表。

    用法::

        parser = FrameParser()
        for chunk in serial_stream:
            ecg_samples, ppg_samples = parser.feed(chunk)
            # ecg_samples / ppg_samples 是本次新增的 Sample 列表
    """

    def __init__(self, keep_raw_tail: bool = True) -> None:
        self._buf = bytearray()
        self.stats = ParserStats()
        self._keep_raw_tail = keep_raw_tail

        # 序号与时间戳跟踪（ECG/PPG 共用一个序号计数器）
        self._last_seq: Optional[int] = None
        self._timestamp_high_water: Optional[int] = None
        # A 4096-byte firmware TX retry can replay more than 256 frames.
        # Include timestamp and payload: the 8-bit sequence alone is not unique.
        self._recent_frames = deque()
        self._recent_frame_set = set()

    def reset_statistics(self) -> None:
        """Begin a measured session without losing partial frames or sequence state."""
        self.stats = ParserStats()

    # -- 时间戳展开 ---------------------------------------------------------
    def _unwrap_timestamp(self, t_us_raw: int) -> float:
        """展开回绕，允许 FIFO 的旧 PPG 点晚于新的 ECG 点到达。

        用最新时间的高水位选择最近的回绕周期，避免跨回绕点的两路交错
        被误计成多次回绕。两路之间允许时间乱序，不强行改写采样时刻。
        """
        reference = self._timestamp_high_water
        if reference is None:
            total_us = t_us_raw
        else:
            delta = (t_us_raw - (reference % TIMESTAMP_WRAP_US)) % TIMESTAMP_WRAP_US
            if delta >= TIMESTAMP_WRAP_US // 2:
                delta -= TIMESTAMP_WRAP_US
            total_us = reference + delta
            if total_us > reference:
                self.stats.timestamp_wraps += total_us // TIMESTAMP_WRAP_US - reference // TIMESTAMP_WRAP_US
        self._timestamp_high_water = max(reference, total_us) if reference is not None else total_us
        return total_us / 1e6

    # -- 序号跟踪 -----------------------------------------------------------
    def _check_sequence(self, seq: int) -> None:
        if self._last_seq is not None:
            gap = (seq - self._last_seq) & 0xFF
            if gap == 0:
                # 重复帧，视为异常丢弃（不计数为丢帧）
                pass
            elif gap > 1:
                self.stats.lost_frames += gap - 1
        self._last_seq = seq

    # -- 单帧解析 -----------------------------------------------------------
    @staticmethod
    def _frame_is_valid(buf: bytearray, index: int) -> bool:
        if index < 0 or (index + FRAME_LEN) > len(buf):
            return False
        if buf[index] != FRAME_HEAD or buf[index + FRAME_LEN - 1] != FRAME_TAIL:
            return False
        ftype = buf[index + 1]
        if ftype == TYPE_ECG:
            # ECG 帧 [5..6] 必须是 0xFFFF 填充
            value = buf[index + 3] | (buf[index + 4] << 8)
            return value <= ECG_ADC_MAX and buf[index + 5] == 0xFF and buf[index + 6] == 0xFF
        if ftype == TYPE_PPG:
            # Both full-scale values are valid saturated PPG, not ECG padding.
            return True
        return False

    def _parse_frame(self, frame: bytes) -> Optional[Tuple[int, Sample]]:
        ftype = frame[1]
        seq = frame[2]
        t_us_raw = struct.unpack_from("<I", frame, 7)[0]
        timestamp_s = self._unwrap_timestamp(t_us_raw)

        if ftype == TYPE_ECG:
            value = frame[3] | (frame[4] << 8)
            return TYPE_ECG, Sample(value, None, timestamp_s, t_us_raw, seq)
        if ftype == TYPE_PPG:
            red = frame[3] | (frame[4] << 8)
            ir = frame[5] | (frame[6] << 8)
            # 恢复 18 位量级；最低两位已在下位机右移时丢失。
            red18 = red << PPG_PROTOCOL_SHIFT
            ir18 = ir << PPG_PROTOCOL_SHIFT
            return TYPE_PPG, Sample(red18, ir18, timestamp_s, t_us_raw, seq)
        return None

    # -- 字节流入口 ---------------------------------------------------------
    def feed(self, chunk: bytes) -> Tuple[List[Sample], List[Sample]]:
        """喂入一段串口字节，返回 (新增 ECG 样本, 新增 PPG 样本)。"""
        self.stats.total_bytes += len(chunk)
        self._buf.extend(chunk)

        ecg_samples: List[Sample] = []
        ppg_samples: List[Sample] = []
        consumed = 0

        while True:
            idx = self._buf.find(bytes([FRAME_HEAD]), consumed)
            if idx < 0:
                # 没有帧头了：保留最后几个字节（可能是被截断的帧头位置）
                consumed = max(consumed, len(self._buf) - 1) if self._keep_raw_tail else len(self._buf)
                break

            if (len(self._buf) - idx) < FRAME_LEN:
                # 数据不足一帧，等下次
                consumed = idx
                break

            if not self._frame_is_valid(self._buf, idx):
                # 校验失败：可能是 0xAA 出现在数据区，前移一个字节重新找
                self.stats.bad_frames += 1
                consumed = idx + 1
                continue

            frame = bytes(self._buf[idx: idx + FRAME_LEN])
            consumed = idx + FRAME_LEN

            if frame in self._recent_frame_set:
                self.stats.duplicate_frames += 1
                continue
            if len(self._recent_frames) >= 512:
                self._recent_frame_set.remove(self._recent_frames.popleft())
            self._recent_frames.append(frame)
            self._recent_frame_set.add(frame)

            parsed = self._parse_frame(frame)
            if parsed is None:
                self.stats.bad_frames += 1
                continue

            ftype, sample = parsed
            self._check_sequence(sample.sequence)
            if ftype == TYPE_ECG:
                self.stats.ecg_frames += 1
                ecg_samples.append(sample)
            else:
                self.stats.ppg_frames += 1
                ppg_samples.append(sample)

        # 丢弃已消费部分
        if consumed > 0:
            del self._buf[:consumed]

        return ecg_samples, ppg_samples


# ---------------------------------------------------------------------------
# 单位换算辅助
# ---------------------------------------------------------------------------

def ecg_adc_to_mv(adc_raw: int) -> float:
    """AD8232 ADC 码值 -> 相对零点的毫伏值（带符号）。"""
    return (adc_raw - ECG_ADC_CENTER) * (ECG_VREF_MV / (ECG_ADC_MAX + 1))


def ecg_adc_to_voltage_mv(adc_raw: int) -> float:
    """AD8232 ADC 码值 -> 绝对输入电压（毫伏），用于判断是否饱和。"""
    return adc_raw * (ECG_VREF_MV / (ECG_ADC_MAX + 1))


def ppg_to_percent(raw18: int) -> float:
    """MAX30102 18bit 原始值 -> 满量程百分比，用于观察直流分量强弱。"""
    raw18 = max(0, min(raw18, PPG_FULL_SCALE_18BIT))
    return raw18 * 100.0 / PPG_FULL_SCALE_18BIT


if __name__ == "__main__":
    # 自检：构造一段帧流（含一次时间戳回绕），验证解析与展开
    def make_frame(ftype, seq, v1, v2, t_us):
        if ftype == TYPE_ECG:
            payload = struct.pack("<HH", v1, 0xFFFF)
        else:
            payload = struct.pack("<HH", v1 >> 2, v2 >> 2)
        return bytes([FRAME_HEAD, ftype, seq]) + payload + struct.pack("<I", t_us) + bytes([FRAME_TAIL])

    stream = b""
    t = TIMESTAMP_WRAP_US - 3 * 10000   # 距回绕 30ms
    for i in range(10):
        stream += make_frame(TYPE_ECG, (i * 2) & 0xFF, 2000 + i, 0, (t + i * 10000) % TIMESTAMP_WRAP_US)
        stream += make_frame(TYPE_PPG, (i * 2 + 1) & 0xFF, 131072 + i, 131000 + i,
                             (t + i * 10000 + 5000) % TIMESTAMP_WRAP_US)

    p = FrameParser()
    ecg, ppg = p.feed(stream)
    print(f"ECG 帧 {len(ecg)} / PPG 帧 {len(ppg)} / 回绕 {p.stats.timestamp_wraps} 次")
    print(f"丢帧 {p.stats.lost_frames} / 坏帧 {p.stats.bad_frames}")
    ts = [s.timestamp_s for s in ecg]
    print(f"时间戳单调递增: {all(b > a for a, b in zip(ts, ts[1:]))}")
    print(f"首末时间: {ts[0]:.4f}s -> {ts[-1]:.4f}s")
    print(f"ECG 首样本 {ecg[0].value} -> {ecg_adc_to_mv(ecg[0].value):.2f} mV")
    print(f"PPG 首样本 RED={ppg[0].value} ({ppg_to_percent(ppg[0].value):.2f}% FS)")
