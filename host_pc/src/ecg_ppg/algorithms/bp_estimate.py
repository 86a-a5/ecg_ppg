#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
bp_estimate.py
==============
血压估计：Poon-Zhang 参数化模型 + 特征级回归基线。

实现依据（四川大学本科毕业设计 4.5.1 节，式 4-7 ~ 4-12）
--------------------------------------------------------
标定阶段：记录同时段的袖带血压 (SBP0, DBP0)，并提取该段的 PAT 中位数 PAT0。

    PP0  = SBP0 - DBP0                        (4-7)
    MBP0 = DBP0 + PP0 / 3                     (4-8)

估计阶段：对待测数据提取同一定义的 PAT 中位数 PAT，则

    MBP  = MBP0 + (2/γ)·ln(PAT0 / PAT)        (4-9)
    PP   = PP0 · (PAT0 / PAT)²                (4-10)
    SBP  = MBP + (2/3)·PP                     (4-11)
    DBP  = MBP - (1/3)·PP                     (4-12)

γ 为与血管弹性相关的经验参数，文献常用 0.017 /mmHg 量级；
本脚本通过实验数据拟合确定，或使用 --gamma 手动指定。

重要提示
--------
1. 本系统测的是体表 ECG 与 (胸前或指尖) PPG，R 峰到 PPG 收缩峰的间隔
   既包含脉搏波传播时间，也包含心脏电-机械延迟(PEP)，严格说是 PAT 而非 PTT。
   论文原文也承认这一点，所以模型里的 PAT/PTT 是可互换的经验量。
2. 单点标定只在该个体、该状态下有效。佩戴位置变化、运动干扰或血管状态
   改变都会破坏 PAT–BP 关系。因此本脚本的结果应作为「趋势/Baseline」，
   不能直接当临床血压读数。这也是论文结论里明确写到的局限。
3. 真正的精度提升要靠个体化标定 + 多特征机器学习模型（论文用 XGBoost，
   输入 PAT + RR + PIR + T + 性别/年龄/体重），见 bp_regression 部分。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np


# 默认 γ：Poon-Zhang 原文量级（单位 mmHg⁻¹），可用 --gamma 覆盖
DEFAULT_GAMMA = 0.017

# 生理合理范围，用于剔除明显错误的输出
SBP_RANGE = (60.0, 220.0)
DBP_RANGE = (30.0, 140.0)


@dataclass
class Calibration:
    """一次个体标定结果。"""
    sbp0: float
    dbp0: float
    pat0_s: float
    gamma: float = DEFAULT_GAMMA

    @property
    def pp0(self) -> float:
        return self.sbp0 - self.dbp0

    @property
    def mbp0(self) -> float:
        return self.dbp0 + self.pp0 / 3.0

    def describe(self) -> str:
        return (f"标定：SBP0={self.sbp0:.1f} mmHg, DBP0={self.dbp0:.1f} mmHg, "
                f"PAT0={self.pat0_s * 1000:.1f} ms, γ={self.gamma:.4f}\n"
                f"       PP0={self.pp0:.1f}, MBP0={self.mbp0:.1f}")


@dataclass
class BPResult:
    mbp: float
    pp: float
    sbp: float
    dbp: float


def poon_zhang(pat_s: float, cal: Calibration) -> BPResult:
    """按式 4-9 ~ 4-12 由 PAT 估计血压。"""
    if pat_s <= 0 or cal.pat0_s <= 0:
        raise ValueError("PAT 必须为正数")

    ratio = cal.pat0_s / pat_s

    # MBP = MBP0 + (2/γ)·ln(PAT0/PAT)
    mbp = cal.mbp0 + (2.0 / cal.gamma) * np.log(ratio)
    # PP = PP0 · (PAT0/PAT)²
    pp = cal.pp0 * (ratio ** 2)

    sbp = mbp + (2.0 / 3.0) * pp
    dbp = mbp - (1.0 / 3.0) * pp

    # 生理合理性钳位，避免极端 PAT 导致荒谬输出
    sbp = float(np.clip(sbp, *SBP_RANGE))
    dbp = float(np.clip(dbp, *DBP_RANGE))
    if dbp >= sbp:
        dbp = sbp - 5.0

    return BPResult(float(mbp), float(pp), sbp, dbp)


# ---------------------------------------------------------------------------
# 特征级回归（论文 XGBoost 思路的轻量替代）
# ---------------------------------------------------------------------------

FEATURE_ORDER = [
    "pat_mean", "rr_mean", "hr_mean", "ppg_pi_mean", "t_peak_mean", "ppg_amp_mean",
]


def load_window_features(path: str) -> Tuple[np.ndarray, List[str]]:
    """读取 extract_features.py 输出的窗口级特征 CSV。"""
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for r in reader:
            rows.append(r)
    if not rows:
        raise ValueError(f"{path} 中没有数据行")

    cols = [c for c in FEATURE_ORDER if c in rows[0]]
    if not cols:
        raise ValueError(f"特征 CSV 里找不到预期列，实际列：{list(rows[0].keys())}")

    X = np.array([[float(r[c]) for c in cols] for r in rows], dtype=float)
    return X, cols


def train_regression(features_csv: str, labels_csv: str, target: str,
                     alpha: float = 1.0) -> dict:
    """训练一个岭回归模型预测 SBP 或 DBP。

    features_csv : extract_features.py 的窗口级输出
    labels_csv   : 每行一个袖带参考血压的 CSV，需含 pat_mean 或 window_start_s
                   用于对齐窗口。若行数正好等于窗口数，则按顺序对齐。
    target       : 'sbp' 或 'dbp'

    说明：论文用的是 XGBoost + 贝叶斯超参优化；这里是等价的轻量版，
          目的是把「特征 → 血压」的整条链路先跑通。数据量够了再换 XGBoost。
    """
    X, cols = load_window_features(features_csv)

    labels = []
    with open(labels_csv, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        key = None
        for c in ("sbp", "SBP", "sbp_mmhg", "systolic"):
            if target == "sbp" and c in (reader.fieldnames or []):
                key = c
                break
        if key is None:
            for c in ("dbp", "DBP", "dbp_mmhg", "diastolic"):
                if target == "dbp" and c in (reader.fieldnames or []):
                    key = c
                    break
        if key is None:
            raise ValueError(f"标签 CSV 里找不到 {target} 列，实际列：{reader.fieldnames}")
        for r in reader:
            labels.append(float(r[key]))
    y = np.asarray(labels, dtype=float)

    if y.size != X.shape[0]:
        raise ValueError(
            f"特征窗口数({X.shape[0]}) 与标签数({y.size}) 不一致。\n"
            f"标签 CSV 需要与窗口一一对应。")

    # 标准化 + 岭回归闭式解
    mu, sigma = X.mean(axis=0), X.std(axis=0)
    sigma[sigma == 0] = 1.0
    Xn = (X - mu) / sigma

    n, d = Xn.shape
    Xa = np.hstack([Xn, np.ones((n, 1))])
    reg = alpha * np.eye(d + 1)
    reg[-1, -1] = 0.0
    w = np.linalg.solve(Xa.T @ Xa + reg, Xa.T @ y)

    pred = Xa @ w
    rmse = float(np.sqrt(np.mean((pred - y) ** 2)))
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return {
        "target": target, "cols": cols, "mu": mu, "sigma": sigma,
        "weights": w, "rmse": rmse, "r2": r2, "n": n,
    }


# ---------------------------------------------------------------------------
# 命令行：用一次标定 + 一段数据做血压估计
# ---------------------------------------------------------------------------

def load_pat_series(beats_csv: str) -> np.ndarray:
    pats = []
    with open(beats_csv, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        col = "pat_s" if "pat_s" in (reader.fieldnames or []) else None
        if col is None:
            raise ValueError(f"{beats_csv} 里没有 pat_s 列")
        for r in reader:
            try:
                pats.append(float(r[col]))
            except (ValueError, TypeError):
                continue
    return np.asarray(pats, dtype=float)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Poon-Zhang 无袖带血压估计（单点个体标定）")
    ap.add_argument("beats_csv", help="extract_features.py 输出的心搏级特征 CSV")
    ap.add_argument("--sbp0", type=float, required=True, help="标定时袖带收缩压 mmHg")
    ap.add_argument("--dbp0", type=float, required=True, help="标定时袖带舒张压 mmHg")
    ap.add_argument("--gamma", type=float, default=DEFAULT_GAMMA,
                    help=f"血管弹性经验参数，默认 {DEFAULT_GAMMA}")
    ap.add_argument("--calib-ratio", type=float, default=0.0,
                    help="用前 N%% 的数据作为标定段计算 PAT0（默认 0，表示用全部数据中位数标定）")
    ap.add_argument("-o", "--output", default=None, help="输出 CSV 路径")
    args = ap.parse_args()

    pats = load_pat_series(args.beats_csv)
    if pats.size < 3:
        print("PAT 数据太少，无法估计。")
        return 1

    # ---- 标定段 PAT0 ----
    if args.calib_ratio > 0:
        n_cal = max(int(pats.size * args.calib_ratio / 100.0), 1)
        pat0 = float(np.median(pats[:n_cal]))
        tested = pats[n_cal:]
        print(f"用前 {n_cal} 个心搏标定，其余 {tested.size} 个心搏参与估计")
    else:
        pat0 = float(np.median(pats))
        tested = pats

    cal = Calibration(sbp0=args.sbp0, dbp0=args.dbp0, pat0_s=pat0,
                      gamma=args.gamma)
    print(cal.describe())
    print(f"PAT 序列：{pats.mean() * 1000:.1f} ± {pats.std() * 1000:.1f} ms "
          f"(n={pats.size})")

    results = []
    for i, p in enumerate(tested):
        try:
            r = poon_zhang(float(p), cal)
        except ValueError:
            continue
        results.append((i, float(p), r))

    if not results:
        print("没有可用的估计结果。")
        return 2

    sbp = np.array([r.sbp for _, _, r in results])
    dbp = np.array([r.dbp for _, _, r in results])
    mbp = np.array([r.mbp for _, _, r in results])

    print(f"\n估计结果（n={len(results)}）：")
    print(f"  SBP = {sbp.mean():.1f} ± {sbp.std():.1f} mmHg "
          f"（{sbp.min():.1f} ~ {sbp.max():.1f}）")
    print(f"  DBP = {dbp.mean():.1f} ± {dbp.std():.1f} mmHg "
          f"（{dbp.min():.1f} ~ {dbp.max():.1f}）")
    print(f"  MBP = {mbp.mean():.1f} ± {mbp.std():.1f} mmHg")
    print(f"\n相对标定值的变化：")
    print(f"  ΔSBP = {sbp.mean() - args.sbp0:+.1f} mmHg")
    print(f"  ΔDBP = {dbp.mean() - args.dbp0:+.1f} mmHg")
    print(f"\n提示：单点标定只在当前个体、当前状态下有效。"
          f"若 Δ 偏移系统性偏大，请检查 PAT 特征点定义是否与标定时一致。")

    out = args.output or os.path.splitext(args.beats_csv)[0] + "_bp_poonzhang.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["beat_index", "pat_s", "mbp", "pp", "sbp", "dbp"])
        for idx, p, r in results:
            w.writerow([idx, f"{p:.6f}", f"{r.mbp:.2f}", f"{r.pp:.2f}",
                        f"{r.sbp:.2f}", f"{r.dbp:.2f}"])
    print(f"\n已导出 -> {os.path.abspath(out)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
