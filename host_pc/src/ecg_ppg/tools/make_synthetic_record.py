#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
make_synthetic_record.py
========================
生成一段合成的「同步 ECG + PPG」CSV，用于在没有硬件时验证整条算法链路。

生成的信号尽可能贴近真实采集结果的格式与量纲：
  * ECG：AD8232 风格的 PQRST，采样率 200 Hz，含基线漂移、工频干扰、随机噪声；
         输出 ecg_processed 为 ADC 码值（0~4095，中点 2048），与 GUI 导出的
         中间列一致（同时给出 mV 列）。
  * PPG：MAX30102 风格的 18bit 直流 + 交流，采样率 100 Hz；
         直流随呼吸缓慢起伏，交流为双峰脉搏波，含噪声。
  * 时间戳：两路共用同一个 TIM5 起点，模拟真实同步条件。

还支持 --hr-ramp 模拟运动导致的血压变化（心率与 PAT 同时变化），
用于验证 Poon-Zhang 模型能否正确给出血压趋势。

用法
----
    python run.py synth -o records/synth.csv --seconds 60
    python run.py synth -o records/synth_warmup.csv --hr-ramp
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

import numpy as np

from ecg_ppg.acquisition.ecg_ppg_protocol import ECG_SAMPLE_RATE_HZ, PPG_SAMPLE_RATE_HZ  # noqa: E402


def _beat_times(t_end: float, hr_bpm: np.ndarray, t_axis: np.ndarray,
                first_beat: float = 0.25) -> list:
    """按「瞬时心率」生成 R 峰时刻列表。

    注意：心率曲线 hr_bpm 定义在 t_axis 上（通常为 ECG 时间轴），
    每个心搏的 RR 由该心搏起点处的心率决定。

    重要：ECG 与 PPG 必须共用**同一份**心搏时刻表（见 main()），
    否则两条独立累加的 beat train 会因心率曲线插值差异而逐渐漂移，
    到几十秒后可能错开数秒，"PAT" 就完全失去意义了。
    """
    beats = []
    t_now = first_beat
    while t_now < t_end:
        beats.append(t_now)
        idx = int(np.clip(t_now * ECG_SAMPLE_RATE_HZ, 0, hr_bpm.size - 1))
        rr = 60.0 / max(hr_bpm[idx], 1.0)
        t_now += rr
    return beats


def synth_ecg(t: np.ndarray, beat_times: list, seed: int = 0) -> np.ndarray:
    """合成 PQRST 心电：把每个心搏按给定时刻依次排开。"""
    rng = np.random.default_rng(seed)
    signal = np.zeros_like(t)

    for bt in beat_times:
        # P 波
        signal += 0.12 * np.exp(-((t - (bt - 0.16)) ** 2) / (2 * 0.025 ** 2))
        # Q 波
        signal -= 0.08 * np.exp(-((t - (bt - 0.02)) ** 2) / (2 * 0.008 ** 2))
        # R 波
        signal += 1.30 * np.exp(-((t - bt) ** 2) / (2 * 0.010 ** 2))
        # S 波
        signal -= 0.22 * np.exp(-((t - (bt + 0.025)) ** 2) / (2 * 0.010 ** 2))
        # T 波
        signal += 0.28 * np.exp(-((t - (bt + 0.24)) ** 2) / (2 * 0.045 ** 2))

    # AD8232 输出：以 1.65V 为中心，叠加基线漂移与 50Hz 工频
    drift = 0.10 * np.sin(2 * np.pi * 0.25 * t)
    hum = 0.04 * np.sin(2 * np.pi * 50.0 * t)
    noise = 0.02 * rng.standard_normal(t.size)

    mv = 1650.0 + (signal + drift + hum + noise) * 400.0   # ±400 缩放，模拟前级增益
    adc = np.clip(np.round(mv / 3300.0 * 4096.0), 0, 4095).astype(int)
    return adc


def synth_ppg(t: np.ndarray, beat_times: list, pat_at_beat: np.ndarray,
              seed: int = 1) -> np.ndarray:
    """合成 PPG：大直流 + 随呼吸起伏的基线 + 双峰交流 + 噪声。

    收缩峰严格出现在 **同一份心搏时刻表** 的 R 峰之后 pat 秒处，
    以便与检测端定义的 PAT 对齐。

    pat_at_beat[k] 是第 k 个心搏的 PAT（秒）。这样 PPG 与 ECG 严格共用
    心跳节拍，不会出现两条独立 beat train 漂移的问题。
    """
    rng = np.random.default_rng(seed)

    # DC：约 45% 满量程，随呼吸 0.25Hz 缓慢起伏
    dc = 0.45 * 262143 + 0.02 * 262143 * np.sin(2 * np.pi * 0.25 * t)

    # 交流：按 R 峰 + PAT 生成脉搏波（主峰 + 反射波）
    ac = np.zeros_like(t)
    for k, bt in enumerate(beat_times):
        pat = float(pat_at_beat[min(k, pat_at_beat.size - 1)])
        peak_t = bt + pat                      # 主峰（收缩峰）恰好在此刻
        ac += 1.0 * np.exp(-((t - peak_t) ** 2) / (2 * 0.085 ** 2))
        # 反射波（舒张期次级峰）
        ac += 0.32 * np.exp(-((t - (peak_t + 0.22)) ** 2) / (2 * 0.090 ** 2))

    ac *= 0.018 * 262143                       # 交流约占满量程 1.8%
    noise = 0.0015 * 262143 * rng.standard_normal(t.size)

    ppg = dc + ac + noise
    return np.clip(np.round(ppg), 0, 262143).astype(int)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成合成 ECG/PPG 同步数据")
    ap.add_argument("-o", "--output", default="records/synthetic_sync.csv")
    ap.add_argument("--seconds", type=float, default=60.0, help="时长（秒）")
    ap.add_argument("--hr", type=float, default=72.0, help="静息心率 bpm")
    ap.add_argument("--pat-ms", type=float, default=240.0, help="静息 PAT（毫秒）")
    ap.add_argument("--hr-ramp", action="store_true",
                    help="模拟运动：心率上升、PAT 缩短（血压升高）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    fs_e, fs_p = ECG_SAMPLE_RATE_HZ, PPG_SAMPLE_RATE_HZ
    n_e = int(args.seconds * fs_e)
    n_p = int(args.seconds * fs_p)
    t_e = np.arange(n_e) / fs_e
    t_p = np.arange(n_p) / fs_p

    # 瞬时心率与 PAT 曲线（都定义在 ECG 时间轴上）
    if args.hr_ramp:
        # 前 40% 静息，中间 30% 运动升高，后 30% 恢复
        prog = np.linspace(0, 1, n_e)
        bump = np.exp(-((prog - 0.55) ** 2) / (2 * 0.16 ** 2))
        hr_curve = args.hr + 38.0 * bump
        pat_curve = (args.pat_ms - 55.0 * bump) / 1000.0   # 血压升高 -> PAT 缩短
    else:
        prog = np.linspace(0, 1, n_e)
        hr_curve = np.full(n_e, args.hr)
        pat_curve = np.full(n_e, args.pat_ms / 1000.0)

    # --- 关键：ECG 与 PPG 共用同一份心搏时刻表 ---
    # 若各自独立累加 RR，两条 beat train 会因心率曲线插值差异逐渐漂移，
    # 几十秒后可错开数秒，PAT 就完全失真。
    beat_times = _beat_times(t_e[-1], hr_curve, t_e)
    pat_at_beat = np.interp(np.asarray(beat_times, dtype=float), t_e, pat_curve)

    ecg_adc = synth_ecg(t_e, beat_times, args.seed)
    ppg_ir = synth_ppg(t_p, beat_times, pat_at_beat, args.seed + 1)
    # RED 与 IR 高度相关但略弱（模拟不同波长穿透深度差异）
    red_factor = 0.92
    ppg_red = np.clip(
        np.round(ppg_ir * red_factor + 0.005 * 262143
                 * np.random.default_rng(args.seed + 2).standard_normal(n_p)),
        0, 262143).astype(int)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["ecg_sync_time_s", "ecg_processed", "ecg_raw",
                    "ir_sync_time_s", "ir_processed",
                    "red_sync_time_s", "red_processed"])
        n = max(n_e, n_p)
        for i in range(n):
            row = []
            if i < n_e:
                mv = (ecg_adc[i] - 2048) * (3300.0 / 4096.0)
                row += [f"{t_e[i]:.6f}", f"{mv:.4f}", str(ecg_adc[i])]
            else:
                row += ["", "", ""]
            if i < n_p:
                row += [f"{t_p[i]:.6f}", str(ppg_ir[i])]
            else:
                row += ["", ""]
            if i < n_p:
                row += [f"{t_p[i]:.6f}", str(ppg_red[i])]
            else:
                row += ["", ""]
            w.writerow(row)

    print(f"已生成合成同步数据 -> {os.path.abspath(args.output)}")
    print(f"  ECG {n_e} 点 @ {fs_e:.0f} Hz")
    print(f"  PPG {n_p} 点 @ {fs_p:.0f} Hz")
    print(f"  时长 {args.seconds:.1f}s，心率 {args.hr:.0f} bpm"
          + ("（含运动升压）" if args.hr_ramp else "（静息）"))
    print(f"  PAT  {args.pat_ms:.0f} ms"
          + ("，运动时缩短至 %.0f ms" % (args.pat_ms - 55.0) if args.hr_ramp else ""))
    print("\n下一步：")
    print(f"  python run.py extract {args.output} --plot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
