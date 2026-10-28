# -*- coding: utf-8 -*-
"""
ecg_ppg_signal.py
=================
ECG/PPG 信号预处理与特征点检测。

实现依据
--------
1. Advanced Science 2026, Chen et al., "Intrinsic PPG–ECG Coupling for Accurate
   and Low-Power Blood Pressure Monitoring"
     - 4.3 节信号耦合方法：
         * 用动态阈值差分法检测 ECG R 波；
         * 把不满足动态阈值条件的点置零以保留相位特性；
         * 加权叠加得到耦合波形  PPG-ECG = α·PPG + β·ECG，α=0.5, β=1；
         * 以 R 波为锚点，在 RR 间期的 0.1~0.9 倍窗口内找局部最小值 = PPG onset；
         * 在 onset 到下一个 R 波之间找局部最大值 = PPG 收缩峰。
     - 4.4 节：耦合信号做 0.5–40 Hz 零相位带通滤波。

2. 四川大学本科毕业设计（虞明威）4.4/4.5.1 节：
     - ECG 预处理去基线漂移、工频干扰，提取 R 峰；
     - PPG 提取波谷(foot)/收缩峰；
     - PAT 定义为同一心搏内 ECG R 峰 → PPG 收缩峰的时间间隔，
       并强调「所有时间点保留在同一同步采集时间轴上」。

关键设计：所有特征点的时间都保留在**统一的采集时间轴**上，
不做各通道独立归零，这正是论文强调的避免人为时间偏移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:
    from scipy import signal as sp_signal
    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    sp_signal = None
    _HAS_SCIPY = False

# 生理合理区间
HR_MIN_BPM = 30.0
HR_MAX_BPM = 220.0
# PPG 收缩峰相对 R 波的合理延迟（秒）：太短说明是上一个心搏，太长是噪声
PAT_MIN_S = 0.05
PAT_MAX_S = 0.60


# ---------------------------------------------------------------------------
# 滤波器
# ---------------------------------------------------------------------------

def _butter_bandpass(low: float, high: float, fs: float, order: int = 3):
    nyq = fs / 2.0
    low_n = max(low / nyq, 1e-4)
    high_n = min(high / nyq, 0.99)
    return sp_signal.butter(order, [low_n, high_n], btype="band")


def bandpass(x: np.ndarray, low: float, high: float, fs: float,
             order: int = 3) -> np.ndarray:
    """零相位带通（filtfilt）。scipy 缺失时退化为去均值 + 简单差分高通。"""
    x = np.asarray(x, dtype=float)
    if x.size < 32:
        return x - np.mean(x) if x.size else x

    if _HAS_SCIPY:
        try:
            b, a = _butter_bandpass(low, high, fs, order)
            padlen = min(3 * max(len(a), len(b)), x.size - 1)
            return sp_signal.filtfilt(b, a, x, padlen=padlen)
        except Exception:
            pass

    # 退化实现：去均值 + 一阶差分（近似高通）
    y = x - np.mean(x)
    return np.concatenate([[0.0], np.diff(y)])


def highpass(x: np.ndarray, cutoff: float, fs: float, order: int = 2) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size < 32 or not _HAS_SCIPY:
        return x - np.mean(x) if x.size else x
    try:
        b, a = sp_signal.butter(order, cutoff / (fs / 2.0), btype="high")
        return sp_signal.filtfilt(b, a, x, padlen=min(3 * max(len(a), len(b)), x.size - 1))
    except Exception:
        return x - np.mean(x)


def lowpass(x: np.ndarray, cutoff: float, fs: float, order: int = 3) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size < 32 or not _HAS_SCIPY:
        return x
    try:
        b, a = sp_signal.butter(order, cutoff / (fs / 2.0), btype="low")
        return sp_signal.filtfilt(b, a, x, padlen=min(3 * max(len(a), len(b)), x.size - 1))
    except Exception:
        return x


# ---------------------------------------------------------------------------
# ECG R 峰检测
# ---------------------------------------------------------------------------

@dataclass
class ECGFeatures:
    r_peaks: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    r_times: np.ndarray = field(default_factory=lambda: np.array([]))
    rr_intervals_s: np.ndarray = field(default_factory=lambda: np.array([]))
    heart_rate_bpm: float = 0.0
    filtered: np.ndarray = field(default_factory=lambda: np.array([]))
    quality: float = 0.0          # 0~1，逐拍抖动质量（越高越稳）
    rr_smoothness: float = 0.0    # 0~1，相邻 RR 平滑度（对连续心率变化不敏感）


def detect_r_peaks(ecg_mv: np.ndarray, fs: float,
                   filter_band: Tuple[float, float] = (0.5, 40.0)) -> ECGFeatures:
    """检测 ECG R 峰。

    策略（Pan-Tompkins 简化版 + 论文的动态阈值差分思想）：
      1. 0.5–40Hz 零相位带通，去基线漂移与工频；
      2. 微分平方得到能量包络，再滑动积分平滑；
      3. 用自适应阈值（信号能量分位数）在包络上找显著峰；
      4. 回到带通信号上，在每个包络峰的邻域内取绝对值最大点作为 R 峰位置。
    """
    x = np.asarray(ecg_mv, dtype=float)
    out = ECGFeatures()
    if x.size < int(fs * 2):
        out.filtered = x
        return out

    filt = bandpass(x, filter_band[0], filter_band[1], fs)
    out.filtered = filt

    if _HAS_SCIPY:
        # --- 能量包络 ---
        diff = np.diff(filt, prepend=filt[0])
        squared = diff ** 2
        win = max(int(0.12 * fs), 3)          # 约 120ms 积分窗
        kernel = np.ones(win) / win
        envelope = np.convolve(squared, kernel, mode="same")

        # --- 自适应阈值找候选峰 ---
        if envelope.max() <= 0:
            return out
        thr = max(np.percentile(envelope, 98) * 0.30,
                  envelope.mean() * 3.0)
        min_dist = max(int(0.25 * fs), 1)     # 生理上限 240bpm 对应的最小间隔
        try:
            cand, _ = sp_signal.find_peaks(envelope, height=thr, distance=min_dist)
        except Exception:
            cand = np.array([], dtype=int)
    else:
        # 无 scipy：用简单的阈值过零法
        thr = np.percentile(np.abs(filt), 98) * 0.5
        above = np.abs(filt) > thr
        cand = np.flatnonzero(above[1:] & ~above[:-1]) + 1

    if cand.size == 0:
        return out

    # --- 在带通信号上精确定位 R 峰（邻域内 |x| 最大点）---
    half = max(int(0.05 * fs), 1)             # ±50ms 搜索窗
    r_peaks: List[int] = []
    for c in cand:
        lo = max(c - half, 0)
        hi = min(c + half + 1, filt.size)
        if hi <= lo:
            continue
        local = np.argmax(np.abs(filt[lo:hi]))
        pos = lo + int(local)
        if not r_peaks or (pos - r_peaks[-1]) >= min_dist:
            r_peaks.append(pos)
        else:
            # 相距太近，保留幅值更大的那个
            if abs(filt[pos]) > abs(filt[r_peaks[-1]]):
                r_peaks[-1] = pos

    r_peaks_arr = np.asarray(r_peaks, dtype=int)
    if r_peaks_arr.size == 0:
        return out

    out.r_peaks = r_peaks_arr
    out.r_times = r_peaks_arr / fs

    if r_peaks_arr.size >= 2:
        rr = np.diff(out.r_times)
        # 剔除生理上不合理的 RR（房早/漏检）
        lo_rr, hi_rr = 60.0 / HR_MAX_BPM, 60.0 / HR_MIN_BPM
        valid = rr[(rr >= lo_rr) & (rr <= hi_rr)]
        out.rr_intervals_s = valid
        if valid.size >= 2:
            med = float(np.median(valid))
            if med > 0:
                out.heart_rate_bpm = 60.0 / med
                # 质量指标：衡量「逐拍抖动」而不是「相对全段中位数的偏差」。
                # 因为运动时心率本来就会连续变化（RR 可能变 50%），若按全局
                # 中位数算偏差，会把正常的心率变化误判成噪声，质量虚低。
                # 这里改为 1 - 归一化中位绝对偏差(MAD/中位数)，只看抖动。
                mad = float(np.median(np.abs(valid - med)))
                out.quality = float(max(0.0, 1.0 - mad / med * 2.0))

                # 另存一个「节律规则性」：相邻 RR 的一阶差分平滑度，
                # 对连续心率变化不敏感，适合判断有无漏检/多检。
                if valid.size >= 4:
                    d = np.abs(np.diff(valid))
                    out.rr_smoothness = float(
                        max(0.0, 1.0 - np.median(d) / med * 4.0))

    return out


# ---------------------------------------------------------------------------
# PPG 特征点检测
# ---------------------------------------------------------------------------

@dataclass
class PPGFeatures:
    systolic_peaks: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    onsets: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    systolic_times: np.ndarray = field(default_factory=lambda: np.array([]))
    onset_times: np.ndarray = field(default_factory=lambda: np.array([]))
    dc: np.ndarray = field(default_factory=lambda: np.array([]))
    ac: np.ndarray = field(default_factory=lambda: np.array([]))
    pi_percent: float = 0.0        # 灌注指数
    quality: float = 0.0


def separate_dc_ac(ppg_raw: np.ndarray, fs: float,
                   dc_cutoff: float = 0.5) -> Tuple[np.ndarray, np.ndarray]:
    """分离 PPG 直流（基线/灌注）与交流（脉搏搏动）。"""
    x = np.asarray(ppg_raw, dtype=float)
    if x.size < 16:
        return x, np.zeros_like(x)
    dc = lowpass(x, dc_cutoff, fs, order=2)
    ac = x - dc
    return dc, ac


def detect_ppg_peaks(ppg_raw: np.ndarray, fs: float,
                     r_times: Optional[np.ndarray] = None) -> PPGFeatures:
    """检测 PPG 收缩峰与 onset。

    两种模式：
      A. 传入 r_times（推荐，对应论文的 R 波锚定法）：
         在每个 RR 间期内，以 R 波为锚点，在 [0.1, 0.9]·RR 窗口找局部最小值作 onset，
         再在 onset 到下一个 R 波之间找局部最大值作收缩峰。
      B. 不传 r_times：
         用自适应阈值 + 最小间距在交流分量上直接找峰，onset 取峰前局部最小。
    """
    x = np.asarray(ppg_raw, dtype=float)
    out = PPGFeatures()
    if x.size < int(fs * 1.5):
        return out

    dc, ac = separate_dc_ac(x, fs)
    out.dc, out.ac = dc, ac

    dc_level = float(np.median(dc))
    if dc_level > 0:
        out.pi_percent = float((np.percentile(ac, 99) - np.percentile(ac, 1))
                               / dc_level * 100.0)

    # 用于检测的波形：交流分量再做一次带通，压制呼吸与运动伪影
    detect_wave = bandpass(ac, 0.5, 8.0, fs) if ac.size > 32 else ac

    if r_times is not None and r_times.size >= 2 and _HAS_SCIPY:
        onsets, peaks = _anchored_search(detect_wave, fs, r_times)
    else:
        onsets, peaks = _free_search(detect_wave, fs)

    out.onsets = onsets
    out.systolic_peaks = peaks
    out.onset_times = onsets / fs
    out.systolic_times = peaks / fs

    if peaks.size >= 3:
        amp = detect_wave[peaks]
        med = float(np.median(amp))
        if med > 0:
            out.quality = float(np.mean(np.abs(amp - med) / med < 0.5))

    return out


def _anchored_search(wave: np.ndarray, fs: float,
                     r_times: np.ndarray,
                     onset_search: Tuple[float, float] = (0.05, 0.45),
                     ) -> Tuple[np.ndarray, np.ndarray]:
    """R 波锚定法（论文 4.3 节）。

    生理事实
    --------
    PPG 收缩峰出现在 R 波之后约 0.15~0.35s（本系统的 PAT，含 PEP），
    之后约 0.2s 还有一次反射波（dicrotic / reflected wave）次峰。

    为什么不能照搬论文的「0.1~0.9·RR 内取全局最小值」
    ------------------------------------------------
    这个窗口会**同时包含本心搏的收缩峰**，argmin 必然落到收缩峰之后的
    舒张期波谷，PAT 被算成 ~0.8s。更糟的是**当心率升高、RR 缩短时**，
    成比例的窗口会把收缩峰越来越早地圈进来，甚至圈到上一个心搏的
    反射波，导致 PAT 忽大忽小（实测 195ms 的正常值会被检成 480~590ms）。

    因此改为「先定峰、再回搜 onset」的两步法，且用**绝对时间**而非
    RR 比例做生理约束，这样对心率变化天然稳健：
      A. 在 [R+pat_lo, R+pat_hi] 的绝对时间窗内找最高点 → 收缩峰；
         （pat_lo/pat_hi 取 PAT_MIN_S / PAT_MAX_S 的生理范围，
           若 RR 较短则用 0.85·RR 收敛，避免越界到下一个心搏）
      B. 从收缩峰向前回搜 0.35s 内的最小值 → onset；
      C. 校验 onset < 峰 且 PAT 在生理范围内，否则丢弃该心搏。
    """
    onsets: List[int] = []
    peaks: List[int] = []

    for i in range(r_times.size - 1):
        r0 = float(r_times[i])            # 单位：秒
        r1 = float(r_times[i + 1])        # 单位：秒
        rr = r1 - r0                      # 单位：秒
        if rr <= 0:
            continue

        n = wave.size
        rr_lo = int(r0 * fs)              # 单位：采样点索引
        rr_hi = min(int(r1 * fs), n - 1)  # 单位：采样点索引
        if rr_hi - rr_lo < 4:
            continue

        # --- A. 绝对时间窗内找收缩峰 ---
        # 窗口下限：R 波后 PAT_MIN_S；上限：PAT_MAX_S 与 0.85·RR 取小
        # （0.85·RR 保证不越到下一个心搏的 R 波之前）
        pat_hi = min(PAT_MAX_S, 0.85 * rr)
        win_lo = r0 + PAT_MIN_S
        win_hi = r0 + pat_hi
        p_lo = max(int(win_lo * fs), rr_lo)
        p_hi = min(int(win_hi * fs), rr_hi)
        if p_hi - p_lo < 3:
            continue

        cand_peak = p_lo + int(np.argmax(wave[p_lo:p_hi]))
        pat = cand_peak / fs - r0         # 索引→秒，再与秒相减
        if not (PAT_MIN_S <= pat <= PAT_MAX_S):
            continue

        # --- B. 从收缩峰向前回搜 onset（脉搏波起点，局部最小值）---
        # 约束：onset 必须晚于 R 波（脉搏波在心脏射血之后才出现），
        # 也不能早于 PAT_MIN_S 太多。回搜窗取 0.35s 与 (PAT - PAT_MIN_S) 的较小值，
        # 否则心搏间期较短时会越过 R 波、把 onset 定位到上一个心搏里，
        # 导致 ptt = onset - R < 0 被丢弃（实测会丢掉约 40% 心搏）。
        max_back = max(pat - PAT_MIN_S, 0.02)
        back = int(min(0.35, max_back) * fs)
        back = max(back, 1)
        o_lo = max(cand_peak - back, rr_lo)
        if cand_peak - o_lo < 2:
            continue
        cand_onset = o_lo + int(np.argmin(wave[o_lo:cand_peak]))

        # --- C. 校验 ---
        if cand_onset >= cand_peak:
            continue
        # onset 必须落在 R 波之后（同一心搏内）
        if cand_onset < rr_lo:
            continue
        if cand_onset / fs - r0 < 0.0:
            continue

        onsets.append(cand_onset)
        peaks.append(cand_peak)

    return (np.asarray(onsets, dtype=int), np.asarray(peaks, dtype=int))


def _free_search(wave: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray]:
    """无 R 波参考时的独立检测（对应论文中的 local extrema 方法）。"""
    if not _HAS_SCIPY or wave.size < 32:
        return np.array([], dtype=int), np.array([], dtype=int)

    min_dist = max(int(0.3 * fs), 1)
    thr = np.percentile(wave, 75)
    try:
        peaks, _ = sp_signal.find_peaks(wave, height=thr, distance=min_dist)
    except Exception:
        return np.array([], dtype=int), np.array([], dtype=int)

    onsets: List[int] = []
    for p in peaks:
        back = max(int(0.35 * fs), 1)
        lo = max(p - back, 0)
        if p - lo < 2:
            onsets.append(lo)
            continue
        onsets.append(lo + int(np.argmin(wave[lo:p])))

    return np.asarray(onsets, dtype=int), np.asarray(peaks, dtype=int)


# ---------------------------------------------------------------------------
# 心搏级配对与 PAT/PTT
# ---------------------------------------------------------------------------

@dataclass
class BeatFeatures:
    """每个心搏一行的特征，可直接构造特征矩阵。"""
    beat_index: int
    r_time_s: float
    onset_time_s: float
    systolic_time_s: float
    pat_s: float          # R 峰 -> 收缩峰（本系统实为 PAT，含 PEP）
    ptt_s: float          # R 峰 -> onset（更接近纯传播时间的近似）
    rr_s: float
    hr_bpm: float
    ppg_amplitude: float
    ppg_pi_percent: float
    t_peak_s: float       # onset -> 收缩峰间隔（论文记为 T）
    r_amplitude: float


def pair_beats(ecg: ECGFeatures, ppg: PPGFeatures) -> List[BeatFeatures]:
    """把 ECG R 峰与 PPG 特征点按「同一心搏」配对。

    配对规则：对每个 PPG 收缩峰，找其之前最近的一个 ECG R 峰；
    并检查 PAT 落在生理范围 [PAT_MIN_S, PAT_MAX_S] 内。这样能避免
    论文中提到的「漏检导致特征点错配、PTT 计算错误」问题。
    """
    beats: List[BeatFeatures] = []
    if ecg.r_peaks.size == 0 or ppg.systolic_peaks.size == 0:
        return beats

    r_times = ecg.r_times
    rr_list = ecg.rr_intervals_s

    # 找 RR 与 onset 的对应（按 onset 最近的前一个 R 峰索引）
    onset_times = ppg.onset_times
    peak_times = ppg.systolic_times

    # onset 到 peak 的配对（同一次检测循环内是一一对应的）
    n_pair = min(onset_times.size, peak_times.size)

    for k in range(n_pair):
        onset_t = onset_times[k]
        peak_t = peak_times[k]

        # 该收缩峰之前最近的 R 峰
        idx = int(np.searchsorted(r_times, peak_t, side="right") - 1)
        if idx < 0:
            continue
        r_t = r_times[idx]

        pat = peak_t - r_t
        if not (PAT_MIN_S <= pat <= PAT_MAX_S):
            continue
        ptt = onset_t - r_t
        if ptt < 0:
            continue

        rr = rr_list[idx] if idx < rr_list.size else 0.0
        hr = 60.0 / rr if rr > 0 else ecg.heart_rate_bpm

        # PPG 幅度与 PI
        amp = 0.0
        if ppg.ac.size > 0:
            p_idx = ppg.systolic_peaks[k] if k < ppg.systolic_peaks.size else None
            o_idx = ppg.onsets[k] if k < ppg.onsets.size else None
            if p_idx is not None and o_idx is not None:
                amp = float(ppg.ac[p_idx] - ppg.ac[o_idx])
        dc_level = float(np.median(ppg.dc)) if ppg.dc.size else 0.0
        pi = (abs(amp) / dc_level * 100.0) if dc_level > 0 else 0.0

        r_amp = float(ecg.filtered[ecg.r_peaks[idx]]) if idx < ecg.r_peaks.size else 0.0

        beats.append(BeatFeatures(
            beat_index=k,
            r_time_s=float(r_t),
            onset_time_s=float(onset_t),
            systolic_time_s=float(peak_t),
            pat_s=float(pat),
            ptt_s=float(ptt),
            rr_s=float(rr),
            hr_bpm=float(hr),
            ppg_amplitude=amp,
            ppg_pi_percent=pi,
            t_peak_s=float(peak_t - onset_t),
            r_amplitude=r_amp,
        ))

    return beats


# ---------------------------------------------------------------------------
# PPG-ECG 耦合波形（论文核心方法）
# ---------------------------------------------------------------------------

def couple_ppg_ecg(ecg_mv: np.ndarray, ppg_raw: np.ndarray,
                   fs_ecg: float, fs_ppg: float,
                   t_ecg: Optional[np.ndarray] = None,
                   t_ppg: Optional[np.ndarray] = None,
                   alpha: float = 0.5, beta: float = 1.0,
                   r_peaks: Optional[np.ndarray] = None,
                   target_fs: float = 125.0
                   ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按论文 Eq.(1) 构造耦合波形：PPG-ECG = α·PPG + β·ECG。

    实现步骤（严格对照论文 4.3 节）：
      1. 两路各自 0.5–40Hz 零相位带通；
      2. ECG 用动态阈值差分法检测 R 波，把不满足阈值条件的点置零
         —— 目的是消除 QS 波对耦合波形相位特性的影响，只保留 R 波作为锚点；
      3. PPG 归一化到与 ECG 同量级（论文中 PPG 幅度约为 ECG 的两倍）；
      4. 两路重采样到同一时间轴（target_fs），再按 α、β 加权叠加。

    返回 (t, coupled, components)，其中 components 形状 (3, N) 为
    [ecg_processed, ppg_processed, coupled]。
    """
    ecg = np.asarray(ecg_mv, dtype=float)
    ppg = np.asarray(ppg_raw, dtype=float)

    if t_ecg is None:
        t_ecg = np.arange(ecg.size) / fs_ecg
    if t_ppg is None:
        t_ppg = np.arange(ppg.size) / fs_ppg

    # 统一时间轴：取两路共同有效范围
    start = max(t_ecg[0], t_ppg[0])
    end = min(t_ecg[-1], t_ppg[-1])
    if end <= start:
        return np.array([]), np.array([]), np.zeros((3, 0))

    n = int((end - start) * target_fs)
    t = start + np.arange(n) / target_fs

    ecg_rs = np.interp(t, t_ecg, ecg)
    ppg_rs = np.interp(t, t_ppg, ppg)

    # --- 1. 带通 ---
    ecg_f = bandpass(ecg_rs, 0.5, 40.0, target_fs)
    ppg_f = bandpass(ppg_rs, 0.5, 40.0, target_fs)

    # --- 2. ECG 动态阈值差分，仅保留 R 波 ---
    ecg_sparse = _dynamic_threshold_rwave(ecg_f, target_fs, r_peaks)

    # --- 3. 幅度归一化 ---
    # 关键：不能用百分位数来定标。ecg_sparse 绝大部分是 0，99.5 百分位会落在
    # 很小的值上，导致 R 波被放大成尖塔、耦合波形完全看不出脉搏成分。
    # 改用「鲁棒峰值」（95 百分位）定标，让 R 波峰值与 PPG 峰值处于可比较的量级，
    # 这样 α=0.5 / β=1 的加权关系才有意义（论文中 PPG 幅度约为 ECG 的 2 倍）。
    nz = np.abs(ecg_sparse)
    nz = nz[nz > 0]
    ecg_scale = float(np.percentile(nz, 95)) if nz.size else 1.0
    ppg_peak = float(np.percentile(np.abs(ppg_f), 99.0)) or 1.0

    ecg_n = ecg_sparse / ecg_scale
    ppg_n = ppg_f / ppg_peak * 2.0

    # --- 4. 加权叠加 ---
    coupled = alpha * ppg_n + beta * ecg_n

    components = np.vstack([ecg_n, ppg_n, coupled])
    return t, coupled, components


def _dynamic_threshold_rwave(ecg: np.ndarray, fs: float,
                             r_peaks: Optional[np.ndarray] = None) -> np.ndarray:
    """论文 4.3 节的「动态阈值差分法」：只保留 R 波，其余置零。

    若已提供 R 峰索引则直接使用；否则先用能量包络法找出 R 峰。
    """
    out = np.zeros_like(ecg)

    if r_peaks is None or r_peaks.size == 0:
        feats = detect_r_peaks(ecg, fs)
        r_peaks = feats.r_peaks

    if r_peaks is None or r_peaks.size == 0:
        return out

    # 每个 R 波附近保留一个窄脉冲（±20ms），幅度取该处真实信号值
    half = max(int(0.020 * fs), 1)
    for p in r_peaks:
        lo = max(p - half, 0)
        hi = min(p + half + 1, ecg.size)
        if hi > lo:
            out[lo:hi] = ecg[lo:hi]
    return out


# ---------------------------------------------------------------------------
# 一站式处理
# ---------------------------------------------------------------------------

@dataclass
class ProcessedRecord:
    fs_ecg: float
    fs_ppg: float
    t_ecg: np.ndarray
    ecg_mv: np.ndarray
    t_ppg: np.ndarray
    ppg_raw: np.ndarray
    ecg_features: ECGFeatures
    ppg_features: PPGFeatures
    beats: List[BeatFeatures]
    coupling: Tuple[np.ndarray, np.ndarray, np.ndarray] = None


def process_record(ecg_values: np.ndarray, ecg_times: np.ndarray,
                   ppg_values: np.ndarray, ppg_times: np.ndarray
                   ) -> ProcessedRecord:
    """从同步 CSV 的两路数据出发，完成预处理 + 特征点检测 + 配对 + 耦合。"""
    ecg_values = np.asarray(ecg_values, dtype=float)
    ppg_values = np.asarray(ppg_values, dtype=float)
    ecg_times = np.asarray(ecg_times, dtype=float)
    ppg_times = np.asarray(ppg_times, dtype=float)

    fs_ecg = _estimate_fs(ecg_times, 200.0)
    fs_ppg = _estimate_fs(ppg_times, 100.0)

    ecg_f = detect_r_peaks(ecg_values, fs_ecg)
    ppg_f = detect_ppg_peaks(ppg_values, fs_ppg, r_times=ecg_f.r_times)
    beats = pair_beats(ecg_f, ppg_f)
    coupling = couple_ppg_ecg(ecg_values, ppg_values, fs_ecg, fs_ppg,
                              t_ecg=ecg_times, t_ppg=ppg_times,
                              r_peaks=ecg_f.r_peaks)

    return ProcessedRecord(fs_ecg, fs_ppg, ecg_times, ecg_values,
                           ppg_times, ppg_values, ecg_f, ppg_f, beats, coupling)


def _estimate_fs(times: np.ndarray, fallback: float) -> float:
    if times.size < 10:
        return fallback
    span = times[-1] - times[0]
    if span <= 0:
        return fallback
    fs = (times.size - 1) / span
    return fs if 1.0 < fs < 5000.0 else fallback


def setup_cjk_font(matplotlib_module=None) -> str:
    """给 matplotlib 配一个中文字体，避免中文标签渲染成方框。

    返回实际生效的字体名；找不到中文字体时返回空串（调用方可据此
    决定是否把标签降级为英文）。各绘图脚本都可以直接调用。
    """
    try:
        if matplotlib_module is None:
            import matplotlib as matplotlib_module
        from matplotlib import font_manager

        candidates = [
            "Microsoft YaHei", "微软雅黑", "SimHei", "黑体",
            "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC",
            "WenQuanYi Micro Hei", "SimSun", "宋体",
        ]
        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in candidates:
            if name in available:
                matplotlib_module.rcParams["font.sans-serif"] = [name]
                matplotlib_module.rcParams["axes.unicode_minus"] = False
                return name

        # 兜底：直接按文件名加载（有些精简字体不会出现在 ttflist 的 name 里）
        import os
        for path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
            if os.path.isfile(path):
                font_manager.fontManager.addfont(path)
                name = font_manager.FontProperties(fname=path).get_name()
                matplotlib_module.rcParams["font.sans-serif"] = [name]
                matplotlib_module.rcParams["axes.unicode_minus"] = False
                return name
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    # 自检：合成一段 ECG(PQRST) + PPG，验证 R 峰、收缩峰与 PAT 检测
    fs_e, fs_p = 200.0, 100.0
    dur = 20.0
    te = np.arange(int(dur * fs_e)) / fs_e
    tp = np.arange(int(dur * fs_p)) / fs_p

    hr = 72.0
    rr = 60.0 / hr
    pat_true = 0.24

    ecg = 0.05 * np.random.randn(te.size)
    for k in range(int(dur / rr)):
        c = k * rr + 1.0
        # 粗略 PQRST：R 波用窄高斯，T 波用宽高斯
        ecg += 1.2 * np.exp(-((te - c) ** 2) / (2 * 0.010 ** 2))
        ecg += 0.15 * np.exp(-((te - (c + 0.25)) ** 2) / (2 * 0.040 ** 2))
        ecg += 0.10 * np.exp(-((te - (c - 0.16)) ** 2) / (2 * 0.025 ** 2))

    ppg = np.full(tp.size, 100000.0)
    for k in range(int(dur / rr)):
        # 收缩峰位置 = R 波 + PAT(真值)；即 onset 就在 R 波处，峰在 onset+PAT
        peak_t = k * rr + 1.0 + pat_true
        ac = 800.0 * np.exp(-((tp - peak_t) ** 2) / (2 * 0.09 ** 2))
        ppg += ac
    ppg += 60.0 * np.random.randn(tp.size)

    rec = process_record(ecg, te, ppg, tp)
    print(f"ECG 实测 fs = {rec.fs_ecg:.1f} Hz, PPG 实测 fs = {rec.fs_ppg:.1f} Hz")
    print(f"检出 R 峰 {rec.ecg_features.r_peaks.size} 个, "
          f"心率 {rec.ecg_features.heart_rate_bpm:.1f} bpm (真值 {hr:.0f})")
    print(f"检出 PPG 收缩峰 {rec.ppg_features.systolic_peaks.size} 个")
    print(f"配对心搏 {len(rec.beats)} 个")
    if rec.beats:
        pats = np.array([b.pat_s for b in rec.beats])
        rrs = np.array([b.rr_s for b in rec.beats])
        print(f"PAT = {pats.mean():.4f} ± {pats.std():.4f} s (真值 {pat_true:.2f})")
        print(f"RR  = {rrs.mean():.4f} s (真值 {rr:.4f})")
    t_c, cpl, comps = rec.coupling
    print(f"耦合波形: {cpl.size} 点 @ 125Hz (约 {t_c[-1]-t_c[0]:.1f}s)")
