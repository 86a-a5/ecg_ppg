#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
extract_features.py
===================
从同步 ECG/PPG 记录中提取血压建模所需的特征，输出特征 CSV。

用法
----
    python run.py extract <同步CSV> [-o 特征CSV] [--plot]

输入：serial_ecg_ppg_gui.py 导出的 CSV，或任何含下列列的 CSV：
    ecg_sync_time_s / ecg_processed / ir_sync_time_s / ir_processed

输出特征（每行一个心搏，也是论文 Figure 4a 的特征集）
----------------------------------------------------
    t_r_s            R 峰时间（秒，相对记录起点）
    rr_s             RR 间期
    hr_bpm           瞬时心率
    pat_s            R 峰 → PPG 收缩峰（本系统为 PAT，含 PEP）
    ptt_s            R 峰 → PPG onset（更接近纯 PTT）
    t_peak_s         onset → 收缩峰（论文中的 T）
    ppg_amp          PPG 交流幅度
    ppg_pi_percent   灌注指数 PIR（论文记为 PPG intensity ratio）
    r_amp            R 波幅度
    beat_index       心搏序号

窗口聚合
--------
论文在推理时按 5 秒滑窗取波形特征的移动平均再送入模型。
本脚本对每 5 秒（--window 可调）聚合一次，输出带 window_mean 后缀的列，
并单独输出窗口级 CSV（--window-csv），便于直接对接血压回归。

注意
----
ECG 里 R 波的稳定性和 PPG 收缩峰的检出率直接决定 PAT 质量。
本脚本会同时打印检出率与 PAT 的统计分布，方便判断这一批数据能不能用。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import List

import numpy as np

from ecg_ppg.algorithms.ecg_ppg_signal import BeatFeatures, process_record  # noqa: E402

BEAT_COLUMNS = [
    "beat_index", "t_r_s", "rr_s", "hr_bpm", "pat_s", "ptt_s",
    "t_peak_s", "ppg_amp", "ppg_pi_percent", "r_amp",
]

WINDOW_COLUMNS = [
    "window_start_s", "window_end_s", "n_beats",
    "rr_mean", "hr_mean", "pat_mean", "pat_std", "ptt_mean",
    "t_peak_mean", "ppg_amp_mean", "ppg_pi_mean", "r_amp_mean",
]


def load_sync_csv(path: str) -> tuple:
    """读取同步 CSV，返回 (ecg_t, ecg_v, ppg_t, ppg_v)。

    对列名做兼容：优先用 *_sync_time_s / *_processed，
    退化时接受 *_time_s / *_raw 之类的常见命名。
    """
    ecg_t, ecg_v, ppg_t, ppg_v = [], [], [], []

    def pick(cols, candidates):
        for c in candidates:
            if c in cols:
                return c
        return None

    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        cols = reader.fieldnames or []
        ecg_tc = pick(cols, ["ecg_sync_time_s", "ecg_time_s", "ecg_time"])
        ecg_vc = pick(cols, ["ecg_processed", "ecg_mv", "ecg_value"])
        ppg_tc = pick(cols, ["ir_sync_time_s", "ppg_sync_time_s", "ppg_time_s", "ir_time_s"])
        ppg_vc = pick(cols, ["ir_processed", "ppg_processed", "ir_value"])

        if not all([ecg_tc, ecg_vc, ppg_tc, ppg_vc]):
            raise ValueError(
                f"CSV 缺少必需列。实际列：{cols}\n"
                f"需要：ecg_sync_time_s / ecg_processed / ir_sync_time_s / ir_processed")
        if ppg_vc is None or ppg_tc is None or ecg_vc is None or ecg_tc is None:
            raise ValueError("CSV 列名解析失败")

        for row in reader:
            try:
                if row[ecg_tc] not in ("", None) and row[ecg_vc] not in ("", None):
                    ecg_t.append(float(row[ecg_tc]))
                    ecg_v.append(float(row[ecg_vc]))
                if row[ppg_tc] not in ("", None) and row[ppg_vc] not in ("", None):
                    ppg_t.append(float(row[ppg_tc]))
                    ppg_v.append(float(row[ppg_vc]))
            except (ValueError, KeyError):
                continue

    if len(ecg_t) < 100 or len(ppg_t) < 50:
        raise ValueError(f"有效数据太少：ECG {len(ecg_t)} 点 / PPG {len(ppg_t)} 点")

    return (np.asarray(ecg_t), np.asarray(ecg_v),
            np.asarray(ppg_t), np.asarray(ppg_v))


def beats_to_rows(beats: List[BeatFeatures]) -> List[dict]:
    rows = []
    for b in beats:
        rows.append({
            "beat_index": b.beat_index,
            "t_r_s": round(b.r_time_s, 6),
            "rr_s": round(b.rr_s, 6),
            "hr_bpm": round(b.hr_bpm, 3),
            "pat_s": round(b.pat_s, 6),
            "ptt_s": round(b.ptt_s, 6),
            "t_peak_s": round(b.t_peak_s, 6),
            "ppg_amp": round(b.ppg_amplitude, 4),
            "ppg_pi_percent": round(b.ppg_pi_percent, 4),
            "r_amp": round(b.r_amplitude, 4),
        })
    return rows


def aggregate_windows(beats: List[BeatFeatures], window_s: float = 5.0) -> List[dict]:
    """按固定时长的滑窗聚合特征，模拟论文的 5 秒滑窗移动平均。"""
    if not beats:
        return []

    t0 = beats[0].r_time_s
    t_end = beats[-1].r_time_s
    rows = []

    start = t0
    while start < t_end:
        sel = [b for b in beats if start <= b.r_time_s < start + window_s]
        if len(sel) >= 2:
            rr = np.array([b.rr_s for b in sel])
            pat = np.array([b.pat_s for b in sel])
            rows.append({
                "window_start_s": round(start - t0, 3),
                "window_end_s": round(start + window_s - t0, 3),
                "n_beats": len(sel),
                "rr_mean": round(float(rr.mean()), 6),
                "hr_mean": round(float(60.0 / rr.mean()), 3) if rr.mean() > 0 else 0.0,
                "pat_mean": round(float(pat.mean()), 6),
                "pat_std": round(float(pat.std()), 6),
                "ptt_mean": round(float(np.mean([b.ptt_s for b in sel])), 6),
                "t_peak_mean": round(float(np.mean([b.t_peak_s for b in sel])), 6),
                "ppg_amp_mean": round(float(np.mean([b.ppg_amplitude for b in sel])), 4),
                "ppg_pi_mean": round(float(np.mean([b.ppg_pi_percent for b in sel])), 4),
                "r_amp_mean": round(float(np.mean([b.r_amplitude for b in sel])), 4),
            })
        start += window_s

    return rows


def write_csv(path: str, columns: List[str], rows: List[dict]) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return os.path.abspath(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="ECG/PPG 特征提取")
    ap.add_argument("input", help="同步 CSV 路径")
    ap.add_argument("-o", "--output", default=None, help="心搏级特征 CSV 输出路径")
    ap.add_argument("--window-csv", default=None, help="窗口级特征 CSV 输出路径")
    ap.add_argument("--window", type=float, default=5.0, help="窗口时长（秒），默认 5")
    ap.add_argument("--plot", action="store_true", help="绘制检测结果图（需要 matplotlib）")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"找不到文件：{args.input}")
        return 1

    print(f"读取：{args.input}")
    ecg_t, ecg_v, ppg_t, ppg_v = load_sync_csv(args.input)
    print(f"ECG {ecg_t.size} 点 / PPG {ppg_t.size} 点，"
          f"时长约 {max(ecg_t[-1], ppg_t[-1]) - min(ecg_t[0], ppg_t[0]):.1f}s")

    rec = process_record(ecg_v, ecg_t, ppg_v, ppg_t)
    print(f"实测采样率：ECG {rec.fs_ecg:.2f} Hz / PPG {rec.fs_ppg:.2f} Hz")
    print(f"R 峰 {rec.ecg_features.r_peaks.size} 个，"
          f"心率 {rec.ecg_features.heart_rate_bpm:.1f} bpm，"
          f"逐拍抖动质量 {rec.ecg_features.quality * 100:.0f}%，"
          f"节律平滑度 {rec.ecg_features.rr_smoothness * 100:.0f}%")
    print(f"PPG 收缩峰 {rec.ppg_features.systolic_peaks.size} 个，"
          f"灌注指数 {rec.ppg_features.pi_percent:.2f}%")
    print(f"成功配对心搏 {len(rec.beats)} 个")

    if not rec.beats:
        print("\n没有配对成功的心搏。请检查：")
        print("  - ECG 电极是否贴好（波形幅度是否够大）")
        print("  - PPG 手指是否放稳（IR 直流是否在合理范围）")
        return 2

    pats = np.array([b.pat_s for b in rec.beats])
    print(f"\nPAT = {pats.mean() * 1000:.1f} ± {pats.std() * 1000:.1f} ms "
          f"（范围 {pats.min() * 1000:.1f}~{pats.max() * 1000:.1f} ms）")

    base = os.path.splitext(args.input)[0]
    out_beat = args.output or f"{base}_beats.csv"
    out_win = args.window_csv or f"{base}_windows.csv"

    n1 = write_csv(out_beat, BEAT_COLUMNS, beats_to_rows(rec.beats))
    print(f"\n心搏级特征 -> {n1}")

    win_rows = aggregate_windows(rec.beats, args.window)
    n2 = write_csv(out_win, WINDOW_COLUMNS, win_rows)
    print(f"窗口级特征 -> {n2}  （{len(win_rows)} 个 {args.window:.0f}s 窗口）")

    if args.plot:
        _plot(rec, args.input)

    return 0


def _plot(rec, source_path: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("未安装 matplotlib，跳过绘图")
        return

    # 配好中文字体，否则中文标签会显示成方框
    from ecg_ppg.algorithms.ecg_ppg_signal import setup_cjk_font
    font_name = setup_cjk_font(matplotlib)
    if not font_name:
        print("提示：未找到中文字体，图中中文可能显示为方框")

    fig, axes = plt.subplots(3, 1, figsize=(13, 9), sharex=False)

    axes[0].plot(rec.t_ecg, rec.ecg_features.filtered, lw=0.7, color="#dc2626")
    axes[0].plot(rec.ecg_features.r_times,
                 rec.ecg_features.filtered[rec.ecg_features.r_peaks],
                 "v", color="#7f1d1d", ms=5, label="R 峰")
    axes[0].set_title("ECG（带通 + R 峰检测）")
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.25)

    axes[1].plot(rec.t_ppg, rec.ppg_features.ac, lw=0.8, color="#2563eb",
                 label="AC 交流分量")
    axes[1].plot(rec.ppg_features.systolic_times,
                 rec.ppg_features.ac[rec.ppg_features.systolic_peaks],
                 "o", color="#1e3a8a", ms=4, label="收缩峰")
    axes[1].plot(rec.ppg_features.onset_times,
                 rec.ppg_features.ac[rec.ppg_features.onsets],
                 "s", color="#0ea5e9", ms=4, label="onset")
    axes[1].set_title("PPG（交流分量 + 收缩峰/onset）")
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.25)

    t_c, cpl, comps = rec.coupling
    if cpl.size:
        axes[2].plot(t_c, comps[0], lw=0.6, alpha=0.6, label="ECG（仅 R 波）")
        axes[2].plot(t_c, comps[1], lw=0.6, alpha=0.6, label="PPG")
        axes[2].plot(t_c, cpl, lw=1.0, color="#111827", label="耦合波形")
        axes[2].legend(fontsize=8)
    axes[2].set_title("PPG-ECG 耦合波形（论文 Eq.1，α=0.5, β=1）")
    axes[2].set_xlabel("时间 (s)")
    axes[2].grid(alpha=0.25)

    fig.tight_layout()
    out = os.path.join(os.path.dirname(os.path.abspath(source_path)),
                       "features_preview.png")
    fig.savefig(out, dpi=130)
    print(f"预览图 -> {os.path.abspath(out)}")


if __name__ == "__main__":
    raise SystemExit(main())
