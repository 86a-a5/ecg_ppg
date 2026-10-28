"""Validated ECG/IR inputs and persistent UniCardio inference, outside the GUI."""
from __future__ import annotations

import csv
import time

import numpy as np
from scipy import signal

from ecg_ppg.acquisition.ecg_ppg_protocol import ecg_adc_to_mv
from ecg_ppg.models.run_unicardio_bp import SEG_LEN, TARGET_FS, infer_segment, load_model, read_sync_csv, robust_unit_scale

CONTEXT_SECONDS = 8.0
MIN_SECONDS = 6.0


class SignalQualityError(ValueError):
    """Input cannot support a prediction."""


def _series(t, v, name):
    t, v = np.asarray(t, dtype=float), np.asarray(v, dtype=float)
    if t.ndim != 1 or v.ndim != 1 or t.size != v.size or t.size < 20:
        raise SignalQualityError(f"{name} 有效采样点不足或时间与数值长度不一致")
    if not (np.isfinite(t).all() and np.isfinite(v).all()):
        raise SignalQualityError(f"{name} 包含无效数值")
    if np.any(np.diff(t) <= 0):
        raise SignalQualityError(f"{name} 时间戳重复或倒退，请重新连接后采集")
    return t, v


def prepare_input(ppg_t, ppg_v, ecg_t, ecg_mv, ecg_adc=None):
    """Filter a private copy with context, then construct one four-second input.

    Recording and display buffers stay untouched. Quality thresholds check obvious
    acquisition failures; passing them does not validate blood-pressure accuracy.
    """
    pt, pv = _series(ppg_t, ppg_v, "IR PPG")
    et, ev = _series(ecg_t, ecg_mv, "ECG")
    end = min(pt[-1], et[-1])
    start = max(pt[0], et[0])
    if end - start < MIN_SECONDS:
        raise SignalQualityError(f"等待两路连续数据：需要至少 {MIN_SECONDS:g} 秒")
    start = max(start, end - CONTEXT_SECONDS)
    window_start = end - 4.0
    for t, name, fs in ((et, "ECG", 200), (pt, "PPG", 100)):
        selected = t[(t >= start) & (t <= end)]
        dt = np.diff(selected)
        if dt.size == 0 or dt.max() > 3.5 / fs:
            raise SignalQualityError(f"{name} 采样存在中断，等待新的连续片段")
        rate = 1.0 / np.median(dt)
        if not 0.75 * fs <= rate <= 1.25 * fs:
            raise SignalQualityError(f"{name} 采样率异常：{rate:.1f} Hz")

    emask = (et >= window_start) & (et < end)
    if ecg_adc is not None:
        adc = np.asarray(ecg_adc, dtype=float)
        if adc.shape != ev.shape or not np.isfinite(adc).all():
            raise SignalQualityError("ECG 原始码值与时间戳不匹配")
        clipped = np.mean((adc[emask] <= 123) | (adc[emask] >= 3972))
    else:
        clipped = np.mean(np.abs(ev[emask]) >= 1550)
    if clipped > 0.05:
        raise SignalQualityError(f"ECG 接近 ADC 上下限的点占 {clipped:.1%}，暂停预测")

    pwindow = pv[(pt >= window_start) & (pt < end)]
    dc = float(np.median(pwindow))
    if not 1000 <= dc <= 250000:
        raise SignalQualityError("IR PPG 光强过低或接近饱和，请放稳手指")
    if np.max(np.abs(np.diff(pwindow))) > max(2000, abs(dc) * 0.2):
        raise SignalQualityError("IR PPG 出现接触或运动突变，等待稳定片段")

    # Filter at native nominal rates before ECG downsampling (anti-aliasing).
    te = start + np.arange(int((end - start) * 200)) / 200
    tp = start + np.arange(int((end - start) * 100)) / 100
    e = np.interp(te, et, ev)
    p = np.interp(tp, pt, pv)
    b, a = signal.iirnotch(50, 30, fs=200)
    e = signal.filtfilt(b, a, e)
    e = signal.sosfiltfilt(signal.butter(2, [0.5, 40], btype="bandpass", fs=200, output="sos"), e)
    p = signal.sosfiltfilt(signal.butter(2, [0.5, 8], btype="bandpass", fs=100, output="sos"), p)
    t125 = window_start + np.arange(SEG_LEN) / TARGET_FS
    e, p = np.interp(t125, te, e), np.interp(t125, tp, p)
    if np.ptp(e) < 25:
        raise SignalQualityError("ECG 波动过小，无法可靠输入模型")
    if np.ptp(p) < max(8, abs(dc) * 0.0005):
        raise SignalQualityError("IR PPG 脉动过弱，无法可靠输入模型")
    e_norm, p_norm = robust_unit_scale(e), robust_unit_scale(p)
    segment = np.concatenate((p_norm, np.zeros(SEG_LEN), e_norm, np.zeros(SEG_LEN))).astype(np.float32)
    return segment, {
        "start_s": float(window_start), "end_s": float(end),
        "ecg_clipped_fraction": float(clipped), "ppg_dc": dc,
        "ecg_input": e_norm.tolist(), "ppg_input": p_norm.tolist(),
    }


def read_prediction_csv(path):
    pt, pv, et, ev = read_sync_csv(path)
    # Older synthetic files store ADC counts in ecg_processed; use preserved raw
    # counts when available so both those files and real recordings have mV units.
    raw = []
    with open(path, encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            if row.get("ecg_sync_time_s", "").strip() and row.get("ecg_raw", "").strip():
                raw.append(float(row["ecg_raw"]))
    adc = np.asarray(raw) if len(raw) == len(et) else None
    if adc is not None:
        ev = ecg_adc_to_mv(adc)
    return pt, pv, et, ev, adc


class BPModel:
    def __init__(self, model_dir):
        import torch
        torch.set_num_threads(2)
        self.model, self.device, self.torch, _ = load_model(model_dir)

    def predict(self, pt, pv, et, ev, adc=None):
        segment, info = prepare_input(pt, pv, et, ev, adc)
        started = time.monotonic()
        self.torch.manual_seed(42)
        wave = infer_segment(self.model, self.device, self.torch, segment)
        if wave.shape != (SEG_LEN,) or not np.isfinite(wave).all():
            raise ValueError("模型输出包含无效值或长度异常")
        # Do not clamp invalid predictions into plausible blood-pressure readings.
        if wave.min() < 20 or wave.max() > 260 or np.ptp(wave) < 5:
            raise ValueError("预测波形超出当前有效范围，请检查输入质量并使用参考血压验证")
        peaks, _ = signal.find_peaks(wave, distance=int(0.3 * TARGET_FS), prominence=2)
        troughs, _ = signal.find_peaks(-wave, distance=int(0.3 * TARGET_FS), prominence=1.5)
        sbp = float(np.mean(wave[peaks])) if len(peaks) >= 2 else float(wave.max())
        dbp = float(np.mean(wave[troughs])) if len(troughs) >= 2 else float(wave.min())
        if dbp >= sbp:
            raise ValueError("预测收缩压与舒张压顺序异常")
        info.update(bp_mmhg=wave.tolist(), sbp=sbp, dbp=dbp, map=float(wave.mean()),
                    inference_seconds=time.monotonic() - started, device=str(self.device))
        return info
