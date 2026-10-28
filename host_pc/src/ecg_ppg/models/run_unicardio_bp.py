#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_unicardio_bp.py
===================
把本项目（STM32F411 + AD8232 + MAX30102）采集的同步 CSV 接入已有的
UniCardio 预训练扩散模型，生成连续 BP 波形并提取 SBP / DBP / MAP。

与参考脚本的关系
----------------
项目里已有一份跑通的脚本：
    software/UniCardio/UniCardio/UniCardio-main/base_model/run_gh3220_unicardio_bp.py
它是为 GH3220 硬件写的。本脚本做同一件事，但：

  * 数据源换成"串口 + AD8232/MAX30102"的采集结果（列名完全一致）；
  * 复用同一份模型定义与权重，不重新训练；
  * **严格逐行对齐**参考脚本的输入构造与推理调用（见下面的 API 契约），
    避免"看起来像但跑不通"的假集成。

UniCardio 输入构造契约（逐条对照 run_gh3220_unicardio_bp.py）
------------------------------------------------------------
  1. 列：ir_sync_time_s / ir_processed / ecg_sync_time_s / ecg_processed
  2. 取两路共同有效时间范围，统一重采样到 125 Hz（np.interp）
  3. 每路做 robust_unit_scale 归一化到 [-1, 1]
  4. 片段 = 单条长度 2000 的一维向量，四段拼接：
         [0:500]    PPG(125Hz, 4s, 归一化)
         [500:1000] BP 占位（全零，待模型生成）
         [1000:1500]ECG(125Hz, 4s, 归一化)
         [1500:2000]空占位（全零）
  5. 模型构造：CSDI_base(config, device, L=500*4)  —— L 必须 = 2000
  6. 推理调用：
         model(x[1,1,2000], n_samples=1, model_flag="021",
               borrow_mode=1, DDIM_flag=0, sample_steps=1, train_gen_flag=1)
     model_flag="021" => 条件通道 0(PPG)+2(ECG)，目标通道 1(BP)
  7. 输出反归一化：BP_mmHg = BP_norm * 50 + 100
  8. 从 4s BP 波形取峰/谷得到 SBP/DBP/MAP

用法
----
    python run.py unicardio <同步CSV> [--model-dir <base_model 目录>]

若模型或 torch 不可用，脚本会明确报告缺什么，并只导出"已构造好的输入片段"
供人工核对接口，不会静默失败。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import List, Optional, Tuple

import numpy as np
from ecg_ppg.paths import MODEL_DIR


# UniCardio 输入参数（必须与参考脚本保持一致）
TARGET_FS = 125
SEG_SECONDS = 4
SEG_LEN = TARGET_FS * SEG_SECONDS          # 500
FULL_LEN = SEG_LEN * 4                     # 2000
WINDOW_STEP_S = 2                          # 本项目默认 2s 滑窗（比参考脚本 10s 更密）
N_SAMPLES = 1
SAMPLE_STEPS = 1
MODEL_FLAG = "021"
BORROW_MODE = 1
DDIM_FLAG = 0
TRAIN_GEN_FLAG = 1

# BP 反归一化：训练时 BP_norm = (BP - 100) / 50
BP_NORM_MEAN = 100.0
BP_NORM_SCALE = 50.0

# 默认模型目录：项目里的 UniCardio base_model
DEFAULT_MODEL_DIR = str(MODEL_DIR)


# ---------------------------------------------------------------------------
# 数据读取与输入构造
# ---------------------------------------------------------------------------

def read_sync_csv(path: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """读取同步 CSV，返回 (ppg_t, ppg_v, ecg_t, ecg_v)。

    空字段（两路长度不同时的补齐）直接跳过，不做插值填充。
    """
    pt, pv, et, ev = [], [], [], []

    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        cols = reader.fieldnames or []

        def pick(cands):
            for c in cands:
                if c in cols:
                    return c
            return None

        ppg_tc = pick(["ir_sync_time_s", "ppg_sync_time_s", "ppg_time_s"])
        ppg_vc = pick(["ir_processed", "ppg_processed", "ir_value"])
        ecg_tc = pick(["ecg_sync_time_s", "ecg_time_s", "ecg_time"])
        ecg_vc = pick(["ecg_processed", "ecg_mv", "ecg_value"])

        if not all([ppg_tc, ppg_vc, ecg_tc, ecg_vc]):
            raise ValueError(
                f"CSV 缺少必需列。实际列：{cols}\n"
                f"需要：ir_sync_time_s/ir_processed/ecg_sync_time_s/ecg_processed")

        for row in reader:
            try:
                if row[ppg_tc] not in ("", None) and row[ppg_vc] not in ("", None):
                    pt.append(float(row[ppg_tc]))
                    pv.append(float(row[ppg_vc]))
                if row[ecg_tc] not in ("", None) and row[ecg_vc] not in ("", None):
                    et.append(float(row[ecg_tc]))
                    ev.append(float(row[ecg_vc]))
            except (ValueError, KeyError):
                continue

    if len(pt) < 100 or len(et) < 100:
        raise ValueError(f"有效数据太少：PPG {len(pt)} 点 / ECG {len(et)} 点")

    return (np.asarray(pt, dtype=float), np.asarray(pv, dtype=float),
            np.asarray(et, dtype=float), np.asarray(ev, dtype=float))


def robust_unit_scale(x: np.ndarray, lo_pct: float = 1.0,
                      hi_pct: float = 99.0) -> np.ndarray:
    """鲁棒归一化到 [-1, 1]（与 UniCardio 原脚本一致）。"""
    x = np.asarray(x, dtype=float)
    lo = np.percentile(x, lo_pct)
    hi = np.percentile(x, hi_pct)
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    y = 2.0 * (x - lo) / (hi - lo) - 1.0
    return np.clip(y, -1.0, 1.0)


def resample_to_125(ppg_t, ppg_v, ecg_t, ecg_v
                    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按共同有效时间范围重采样到 125 Hz，返回 (t125, ppg125, ecg125)。

    归一化在重采样之后做，与参考脚本顺序一致。
    """
    start = max(ppg_t[0], ecg_t[0])
    end = min(ppg_t[-1], ecg_t[-1])
    if end - start < SEG_SECONDS:
        raise ValueError(
            f"两路共同有效时长只有 {end - start:.2f}s，不足一个 {SEG_SECONDS}s 片段。")

    n = int((end - start) * TARGET_FS)
    t125 = start + np.arange(n) / TARGET_FS

    ppg125_raw = np.interp(t125, ppg_t, ppg_v)
    ecg125_raw = np.interp(t125, ecg_t, ecg_v)

    ppg125 = robust_unit_scale(ppg125_raw)
    ecg125 = robust_unit_scale(ecg125_raw)
    return t125, ppg125, ecg125


def make_segments(t125, ppg125, ecg125, step_s: float = WINDOW_STEP_S
                  ) -> Tuple[np.ndarray, List[int]]:
    """切成 [1,1,2000] 的输入向量，并做基本质量筛查。

    返回 (segments[K,2000], starts[K])，与参考脚本 make_segments 输出等价。
    """
    step = max(int(step_s * TARGET_FS), 1)
    max_start = len(t125) - SEG_LEN
    if max_start < 0:
        raise ValueError("数据长度不足一个片段。")

    segs: List[np.ndarray] = []
    starts: List[int] = []

    for s in range(0, max_start + 1, step):
        e = s + SEG_LEN
        p = ppg125[s:e]
        q = ecg125[s:e]
        if len(p) != SEG_LEN or len(q) != SEG_LEN:
            continue

        # 质量筛查：平线 / 幅值异常片段直接跳过（论文也做了类似筛选）
        if np.std(p) < 1e-3 or np.std(q) < 1e-3:
            continue

        bp_placeholder = np.zeros(SEG_LEN, dtype=np.float32)
        null_placeholder = np.zeros(SEG_LEN, dtype=np.float32)
        full = np.concatenate([
            p.astype(np.float32),
            bp_placeholder,
            q.astype(np.float32),
            null_placeholder,
        ])
        segs.append(full)
        starts.append(s)

    if not segs:
        raise ValueError("没有通过质量筛查的片段。")

    return np.asarray(segs, dtype=np.float32), starts


# ---------------------------------------------------------------------------
# 模型推理
# ---------------------------------------------------------------------------

def load_model(model_dir: str):
    """加载 UniCardio 模型与配置。

    返回 (model, device, torch, config)；失败抛 RuntimeError 并附带原因。
    """
    try:
        import torch
        import yaml
    except ImportError as exc:
        raise RuntimeError(f"缺少依赖：{exc}（需要 torch 与 pyyaml）") from exc

    if not os.path.isdir(model_dir):
        raise RuntimeError(f"模型目录不存在：{model_dir}")

    cfg_path = os.path.join(model_dir, "base_no_compress_original.yaml")
    weight_path = os.path.join(model_dir, "no_compress799.pth")
    model_py = os.path.join(model_dir, "diffusion_model_no_compress_final.py")

    for p in (cfg_path, weight_path, model_py):
        if not os.path.isfile(p):
            raise RuntimeError(f"模型文件缺失：{p}")

    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")

    sys.path.insert(0, model_dir)
    try:
        from diffusion_model_no_compress_final import CSDI_base  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"导入模型定义失败：{exc}") from exc

    with open(cfg_path, "r", encoding="utf-8") as fp:
        config = yaml.safe_load(fp)

    # 与参考脚本一致：CSDI_base(config, device, L=500*4)
    model = CSDI_base(config, device, L=FULL_LEN).to(device)

    try:
        state = torch.load(weight_path, map_location=device, weights_only=True)
    except Exception as exc:
        raise RuntimeError(f"读取权重失败：{exc}") from exc

    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    state = {k.replace("module.", "", 1): v for k, v in state.items()}

    try:
        model.load_state_dict(state, strict=True)
    except RuntimeError as exc:
        raise RuntimeError(f"模型源码与权重不匹配：{exc}") from exc

    model.eval()
    return model, device, torch, config


def infer_segment(model, device, torch, seg: np.ndarray) -> np.ndarray:
    """对单个 2000 点输入向量推理，返回反归一化后的 BP 波形 (mmHg)。"""
    input_tensor = torch.tensor(seg[None, None, :], dtype=torch.float32).to(device)

    with torch.inference_mode():
        results = model(
            input_tensor,
            n_samples=N_SAMPLES,
            model_flag=MODEL_FLAG,       # 0(PPG) + 2(ECG) 条件，1(BP) 目标
            borrow_mode=BORROW_MODE,     # 生成 BP 片段
            DDIM_flag=DDIM_FLAG,
            sample_steps=SAMPLE_STEPS,
            train_gen_flag=TRAIN_GEN_FLAG,
        )

    if isinstance(results, (tuple, list)):
        results = results[0]

    # results: [B, N, 1, 500] 或 [B, N, 500] 或 [B, 500]
    if results.ndim == 4:
        bp_norm = results.mean(dim=1).detach().cpu().numpy()[0, 0, :]
    elif results.ndim == 3:
        bp_norm = results.mean(dim=1).detach().cpu().numpy()[0, :]
    elif results.ndim == 2:
        bp_norm = results.detach().cpu().numpy()[0, :]
    else:
        raise ValueError(f"模型输出形状异常：{tuple(results.shape)}")

    bp_mmhg = np.asarray(bp_norm, dtype=float) * BP_NORM_SCALE + BP_NORM_MEAN
    return bp_mmhg[:SEG_LEN]


def extract_sbp_dbp(bp_wave: np.ndarray, fs: float = TARGET_FS
                    ) -> Tuple[float, float, float, str]:
    """从生成的 BP 波形里取峰(SBP)/谷(DBP)，并算 MAP。与参考脚本一致。"""
    bp_wave = np.asarray(bp_wave, dtype=float)
    if bp_wave.size == 0:
        return 0.0, 0.0, 0.0, "empty"

    bp_clip = np.clip(bp_wave, 40.0, 220.0)

    try:
        from scipy.signal import find_peaks
    except ImportError:
        return (float(bp_clip.max()), float(bp_clip.min()),
                float(bp_clip.mean()), "max/min(no scipy)")

    peaks, _ = find_peaks(bp_clip, distance=int(0.35 * fs), prominence=2.0)
    troughs, _ = find_peaks(-bp_clip, distance=int(0.35 * fs), prominence=1.5)

    if peaks.size >= 2:
        sbp, sm = float(np.mean(bp_clip[peaks])), "peaks"
    else:
        sbp, sm = float(bp_clip.max()), "max"

    if troughs.size >= 2:
        dbp, dm = float(np.mean(bp_clip[troughs])), "troughs"
    else:
        dbp, dm = float(bp_clip.min()), "min"

    mbp = float(np.mean(bp_clip))
    return sbp, dbp, mbp, f"{sm}/{dm}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="用 UniCardio 从同步 ECG/PPG 生成连续血压波形")
    ap.add_argument("input", help="同步 CSV 路径")
    ap.add_argument("--model-dir", default=DEFAULT_MODEL_DIR,
                    help="UniCardio base_model 目录")
    ap.add_argument("--step", type=float, default=WINDOW_STEP_S,
                    help=f"片段滑窗步长（秒），默认 {WINDOW_STEP_S}")
    ap.add_argument("--max-segments", type=int, default=None,
                    help="最多处理多少个片段（调试用）")
    ap.add_argument("-o", "--output", default=None, help="结果 CSV 输出路径")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"找不到文件：{args.input}")
        return 1

    print("=" * 66)
    print("UniCardio 血压波形生成")
    print("=" * 66)

    # --- 1. 读数据 ---
    ppg_t, ppg_v, ecg_t, ecg_v = read_sync_csv(args.input)
    print(f"输入数据：PPG {ppg_t.size} 点 / ECG {ecg_t.size} 点")
    print(f"时长：{min(ecg_t[0], ppg_t[0]):.2f} ~ "
          f"{max(ecg_t[-1], ppg_t[-1]):.2f} s")

    # --- 2. 重采样 + 分段 ---
    t125, ppg125, ecg125 = resample_to_125(ppg_t, ppg_v, ecg_t, ecg_v)
    print(f"重采样到 {TARGET_FS} Hz：{t125.size} 点")

    try:
        segments, starts = make_segments(t125, ppg125, ecg125, args.step)
    except ValueError as exc:
        print(f"分段失败：{exc}")
        return 2
    print(f"切分为 {segments.shape[0]} 个片段，每个 {FULL_LEN} 点"
          f"（PPG|BP占位|ECG|空占位，各 {SEG_LEN}）")

    if args.max_segments:
        segments = segments[:args.max_segments]
        starts = starts[:args.max_segments]
        print(f"（限制为 {len(starts)} 个片段）")

    # --- 3. 加载模型 ---
    try:
        model, device, torch, config = load_model(args.model_dir)
    except RuntimeError as info:
        print(f"\n[模型未加载] {info}")
        print("\n已完成的输入构造信息（可直接用于人工核对模型接口）：")
        print(f"  * 片段形状：{segments.shape}  (期望 [K, 2000])")
        print(f"  * PPG 归一化范围：[{ppg125.min():.2f}, {ppg125.max():.2f}]")
        print(f"  * ECG 归一化范围：[{ecg125.min():.2f}, {ecg125.max():.2f}]")
        print(f"  * 输入拼接：PPG[0:500] + BP占位[500:1000] + "
              f"ECG[1000:1500] + 空占位[1500:2000]")
        print(f"  * 推理参数：n_samples={N_SAMPLES}, model_flag='{MODEL_FLAG}', "
              f"borrow_mode={BORROW_MODE}")
        print(f"\n请确认 --model-dir 指向 UniCardio 的 base_model 目录：")
        print(f"  {args.model_dir}")
        print("\n该目录应包含：base_no_compress_original.yaml / no_compress799.pth /")
        print("              diffusion_model_no_compress_final.py")
        _export_segments_only(segments, args)
        return 3

    print(f"模型已加载：{args.model_dir}（device={device}）")

    # --- 4. 逐片段推理 ---
    rows = []
    bp_waves = {}
    for i, seg in enumerate(segments):
        try:
            bp = infer_segment(model, device, torch, seg)
        except Exception as exc:
            print(f"  片段 {i} 推理失败：{exc}")
            continue
        if bp is None or bp.size == 0:
            continue
        sbp, dbp, mbp, method = extract_sbp_dbp(bp)
        rows.append({
            "segment": i,
            "start_s": round(float(t125[starts[i]] - t125[0]), 3),
            "sbp": round(sbp, 2),
            "dbp": round(dbp, 2),
            "mbp": round(mbp, 2),
            "method": method,
        })
        bp_waves[i] = bp
        if (i + 1) % 10 == 0 or i == len(starts) - 1:
            print(f"  已处理 {i + 1}/{len(starts)} 个片段")

    if not rows:
        print("没有任何片段推理成功。")
        return 4

    sbp_arr = np.array([r["sbp"] for r in rows])
    dbp_arr = np.array([r["dbp"] for r in rows])
    print(f"\n估计结果（n={len(rows)}）：")
    print(f"  SBP = {sbp_arr.mean():.1f} ± {sbp_arr.std():.1f} mmHg")
    print(f"  DBP = {dbp_arr.mean():.1f} ± {dbp_arr.std():.1f} mmHg")

    out = args.output or os.path.splitext(args.input)[0] + "_unicardio_bp.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(
            fp, fieldnames=["segment", "start_s", "sbp", "dbp", "mbp", "method"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n结果 -> {os.path.abspath(out)}")

    wave_out = os.path.splitext(out)[0] + "_waveforms.csv"
    with open(wave_out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["segment", "sample_index", "bp_mmhg"])
        for idx, wave in bp_waves.items():
            for j, v in enumerate(wave):
                w.writerow([idx, j, f"{v:.5f}"])
    print(f"波形 -> {os.path.abspath(wave_out)}")

    print("\n注意：UniCardio 不是针对本硬件训练的模型，其绝对值需要")
    print("      用真实袖带血压校准后才能作为临床参考。")

    return 0


def _export_segments_only(segments: np.ndarray, args) -> None:
    """模型不可用时，仍导出构造好的片段，便于后续排查接口。"""
    out = args.output or os.path.splitext(args.input)[0] + "_unicardio_segments.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["segment", "sample_index",
                    "ppg_norm", "bp_placeholder", "ecg_norm", "null_placeholder"])
        for i in range(segments.shape[0]):
            row = segments[i]
            for j in range(SEG_LEN):
                w.writerow([i, j,
                            f"{row[j]:.5f}", f"{row[SEG_LEN + j]:.5f}",
                            f"{row[2 * SEG_LEN + j]:.5f}",
                            f"{row[3 * SEG_LEN + j]:.5f}"])
    print(f"\n已导出归一化片段 -> {os.path.abspath(out)}")


if __name__ == "__main__":
    raise SystemExit(main())
