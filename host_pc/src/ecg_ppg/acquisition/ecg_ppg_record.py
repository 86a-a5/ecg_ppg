# -*- coding: utf-8 -*-
"""
ecg_ppg_record.py
=================
采集会话管理：把 ECG/PPG 样本落盘为**与现有血压模型兼容的同步 CSV**。

为什么需要这个模块
------------------
项目里已有两个下游模型，它们的输入 CSV 格式已经固定：

1) UniCardio  (software/UniCardio/.../base_model/run_gh3220_unicardio_bp.py)
   读取列：
       PPG 时间列  ir_sync_time_s   /  值列  ir_processed
       ECG 时间列  ecg_sync_time_s  /  值列  ecg_processed
   要求两路共同存在的有效时间范围，且由程序的 np.interp 重采样到 125Hz。
   所以本模块只要保证「时间列是秒、单调递增、两路同源」即可。

2) ABPImputation / 4.5.2 节流程
   同样读取 ECG 与 IR PPG 的时间列与值列，共同时间窗口，重采样到 100Hz。

3) 特征提取 / Poon-Zhang（features 与 bp 模块）
   需要 beat-by-beat 的 R 峰、PPG 收缩峰时间，从同一份 CSV 推导。

因此本模块输出的 CSV 固定包含以下列（多余列不影响下游）：

    ecg_sync_time_s   ECG 采样时刻（秒，已跨回绕展开，相对录音起点）
    ecg_processed     ECG 值（mV，相对 AD8232 零点，已去均值前处理）
    ecg_raw           ECG ADC 原始码值（0~4095，保留原始便于复算）
    ir_sync_time_s    IR PPG 采样时刻（秒）
    ir_processed      IR PPG 值（18bit 原始值）
    red_sync_time_s   RED PPG 采样时刻（秒）
    red_processed     RED PPG 值（18bit 原始值）

两路时间戳源自同一个 TIM5，无需额外时间对齐——这正是论文强调的
「natural synchronization / 消除多传感器时间误差」在本系统的落地点。

注意：CSV 里 ECG(200Hz) 与 PPG(100Hz) 长度不同，因此不能简单拼成同一行。
      采用「各自独立的 time 列 + 值列，长度按各自采样率」的宽表结构，
      与 GH3220 导出的 CSV 结构一致，下游用 np.interp 自行对齐。
"""

from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass, field
from typing import List
from ecg_ppg.paths import RECORDS_DIR

from ecg_ppg.acquisition.ecg_ppg_protocol import (
    ECG_SAMPLE_RATE_HZ,
    PPG_SAMPLE_RATE_HZ,
    ecg_adc_to_mv,
)


@dataclass
class RecordedSeries:
    """一路信号的录制缓冲。"""
    ecg_t: List[float] = field(default_factory=list)
    ecg_mv: List[float] = field(default_factory=list)
    ecg_raw: List[int] = field(default_factory=list)
    ir_t: List[float] = field(default_factory=list)
    ir_v: List[int] = field(default_factory=list)
    red_t: List[float] = field(default_factory=list)
    red_v: List[int] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        ends = []
        if self.ecg_t:
            ends.append(self.ecg_t[-1])
        if self.ir_t:
            ends.append(self.ir_t[-1])
        return max(ends) - self._first_t if ends else 0.0

    _first_t: float = 0.0


class Recorder:
    """录制器：接收解析后的样本，缓存为等间隔时间序列，并支持导出 CSV。

    参数
    ----
    auto_limit_seconds : 达到该时长自动停止录制（None 表示不限制）。
                         默认 60 秒，避免忘记停止导致内存无限增长。
    max_points_hint    : 仅用于进度提示，不强制截断。
    """

    def __init__(self, auto_limit_seconds: float | None = 60.0) -> None:
        self.series = RecordedSeries()
        self.recording = False
        self._origin_s: float | None = None
        self.started_wall = 0.0
        self.auto_limit_seconds = auto_limit_seconds
        self.auto_stopped = False

    # -- 录制控制 -----------------------------------------------------------
    def start(self, origin_s: float | None = None) -> None:
        self.series = RecordedSeries()
        self._origin_s = origin_s
        self.recording = True
        self.started_wall = time.time()
        self.auto_stopped = False

    def stop(self) -> None:
        self.recording = False

    def elapsed_s(self) -> float:
        return time.time() - self.started_wall if self.recording else 0.0

    def _rel(self, t_s: float) -> float:
        """转成相对录音起点的秒数；首个样本到达时确定起点。"""
        if self._origin_s is None:
            self._origin_s = t_s
        return t_s - self._origin_s

    # -- 数据写入 -----------------------------------------------------------
    def add_ecg(self, timestamp_s: float, adc_raw: int) -> None:
        if not self.recording:
            return
        t = self._rel(timestamp_s)
        self.series.ecg_t.append(t)
        self.series.ecg_mv.append(ecg_adc_to_mv(adc_raw))
        self.series.ecg_raw.append(adc_raw)
        self._check_auto_limit(t)

    def add_ppg(self, timestamp_s: float, red18: int, ir18: int) -> None:
        if not self.recording:
            return
        t = self._rel(timestamp_s)
        self.series.ir_t.append(t)
        self.series.ir_v.append(ir18)
        self.series.red_t.append(t)
        self.series.red_v.append(red18)
        self._check_auto_limit(t)

    def _check_auto_limit(self, t: float) -> None:
        if self.auto_limit_seconds and t >= self.auto_limit_seconds:
            self.recording = False
            self.auto_stopped = True

    # -- 统计 ---------------------------------------------------------------
    def measured_rates(self) -> tuple[float, float]:
        """从时间戳实测 ECG / PPG 帧率（Hz），用于校验下位机采样率。"""
        def rate(ts: List[float]) -> float:
            if len(ts) < 10:
                return 0.0
            span = ts[-1] - ts[0]
            return (len(ts) - 1) / span if span > 0 else 0.0

        return rate(self.series.ecg_t), rate(self.series.ir_t)

    def sample_counts(self) -> tuple[int, int]:
        return len(self.series.ecg_t), len(self.series.ir_t)

    # -- 导出 ---------------------------------------------------------------
    def to_csv(self, path: str) -> str:
        """导出为下游模型可直接读取的同步 CSV，返回实际写入路径。"""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        col_names = [
            "ecg_sync_time_s", "ecg_processed", "ecg_raw",
            "ir_sync_time_s", "ir_processed",
            "red_sync_time_s", "red_processed",
        ]
        n = max(len(self.series.ecg_t), len(self.series.ir_t))

        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp)
            w.writerow(col_names)
            for i in range(n):
                row = []
                if i < len(self.series.ecg_t):
                    row += [f"{self.series.ecg_t[i]:.6f}",
                            f"{self.series.ecg_mv[i]:.4f}",
                            str(self.series.ecg_raw[i])]
                else:
                    row += ["", "", ""]
                if i < len(self.series.ir_t):
                    row += [f"{self.series.ir_t[i]:.6f}", str(self.series.ir_v[i])]
                else:
                    row += ["", ""]
                if i < len(self.series.red_t):
                    row += [f"{self.series.red_t[i]:.6f}", str(self.series.red_v[i])]
                else:
                    row += ["", ""]
                w.writerow(row)
        return os.path.abspath(path)


def default_output_path(base_dir: str | None = None) -> str:
    """生成带时间戳的默认输出路径。"""
    base = base_dir or str(RECORDS_DIR)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(base, f"ecg_ppg_sync_{stamp}.csv")


if __name__ == "__main__":
    # 自检：模拟 3 秒数据，验证导出结构与实测帧率
    import tempfile
    rec = Recorder(auto_limit_seconds=None)
    rec.start(origin_s=1000.0)
    for i in range(int(3 * ECG_SAMPLE_RATE_HZ)):
        rec.add_ecg(1000.0 + i / ECG_SAMPLE_RATE_HZ, 2048 + (i % 50))
    for i in range(int(3 * PPG_SAMPLE_RATE_HZ)):
        rec.add_ppg(1000.0 + i / PPG_SAMPLE_RATE_HZ, 131072 + i, 130000 + i)
    ecg_r, ppg_r = rec.measured_rates()
    print(f"实测帧率: ECG {ecg_r:.2f} Hz / PPG {ppg_r:.2f} Hz")
    print(f"样本数: {rec.sample_counts()}")
    out = os.path.join(tempfile.gettempdir(), "record_selftest.csv")
    print("已导出:", rec.to_csv(out))
    with open(out, encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            if i > 3:
                break
            print("  ", line.rstrip())
