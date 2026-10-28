#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
serial_ecg_ppg_gui.py
=====================
ECG(AD8232) + PPG(MAX30102) BLE / 串口采集上位机。

STM32F411 USART2(PA2/PA3) @115200 → ATK-BLE04 → BLE FFF1 通知。
保留 USB-TTL 串口入口；两种传输共用二进制解析、录制与 MCU 时间轴。

界面
----
  左侧：BLE 扫描 / 串口选择、连接、录制、导出、参数
  右侧：ECG（mV）、IR PPG、RED PPG 三条分离波形；左侧显示直流与链路统计

依赖
----
    pip install -r requirements.txt
（matplotlib 缺失时仍可运行，只是不显示波形，但数据采集与导出不受影响）
"""

from __future__ import annotations

import os
import argparse
import csv
import queue
import sys
import time
from collections import deque
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --- 协议与录制：同目录模块 ---
from ecg_ppg.acquisition.ecg_ppg_protocol import (  # noqa: E402
    ECG_SAMPLE_RATE_HZ,
    PPG_SAMPLE_RATE_HZ,
    ecg_adc_to_mv,
    ppg_to_percent,
)
from ecg_ppg.acquisition.ecg_ppg_record import Recorder, default_output_path  # noqa: E402
from ecg_ppg.gui.waveform_display import StreamingDisplayFilter, StableRange, prepare_trace  # noqa: E402
from ecg_ppg.gui.ppg_polarity import AutoPpgPolarity, AUTO, INVERTED, ORIGINAL, MODES  # noqa: E402
from ecg_ppg.models.unicardio_client import BPWorkerClient  # noqa: E402
from ecg_ppg.acquisition.stream_workers import (  # noqa: E402
    SampleWorker, SerialWorker, BleWorker, BleDiscovery,
)

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

try:
    import numpy as np
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

    # matplotlib 默认字体不含中文字形，坐标轴标题里的中文会变成方框，
    # 且每次重绘刷一堆 "Glyph missing from font(s) DejaVu Sans" 警告。
    # 这里优先注册系统中文字体；失败也不影响采集（标题退化为方框而已）。
    def _setup_matplotlib_cjk() -> None:
        import os as _os
        candidates = [
            r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf",    # 黑体
            r"C:\Windows\Fonts\simsun.ttc",    # 宋体
        ]
        for path in candidates:
            if _os.path.isfile(path):
                try:
                    from matplotlib import font_manager as _fm
                    _fm.fontManager.addfont(path)
                    name = _fm.FontProperties(fname=path).get_name()
                    matplotlib.rcParams["font.sans-serif"] = [name] + list(
                        matplotlib.rcParams.get("font.sans-serif", [])
                    )
                    matplotlib.rcParams["axes.unicode_minus"] = False
                    return
                except Exception:
                    continue

    _setup_matplotlib_cjk()
except Exception:  # pragma: no cover
    np = None
    FigureCanvasTkAgg = None
    Figure = None


# ---------------------------------------------------------------------------
# 配色（浅色主题）
# ---------------------------------------------------------------------------
BG = "#f5f7fa"
PANEL = "#ffffff"
BORDER = "#d8dee9"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#0ea5e9"
ECG_COLOR = "#dc2626"
PPG_COLOR = "#2563eb"
PPG_DC_COLOR = "#94a3b8"
GOOD = "#16a34a"
WARN = "#d97706"
BAD = "#dc2626"

FONT = "Microsoft YaHei UI"

# 显示窗口
DISPLAY_SECONDS = 6.0
ECG_DISPLAY_POINTS = int(DISPLAY_SECONDS * ECG_SAMPLE_RATE_HZ)
PPG_DISPLAY_POINTS = int(DISPLAY_SECONDS * PPG_SAMPLE_RATE_HZ)

# 简单输入状态门限；幅度不能作为波形质量评分。
ECG_FLAT_P2P_MV = 60.0       # 仅提示交流变化小，不据此认定导联脱落
PPG_DC_MIN_PERCENT = 1.0     # IR 直流低于满量程该比例视为手指未放好
PPG_DC_MAX_PERCENT = 95.0    # 过高视为光强饱和

CHART_REFRESH_MS = 50
UI_PUMP_MS = 20


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class SerialEcgPpgGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ECG + PPG 蓝牙 / 串口采集与 UniCardio 血压预测")
        self.geometry("1280x820")
        self.configure(bg=BG)
        self.minsize(1000, 680)

        self.worker: Optional[SampleWorker] = None
        self.discovery = None
        self.ble_targets = {}
        self._connect_after_scan = None
        self.recorder = Recorder(auto_limit_seconds=60.0)

        # 显示缓冲
        self.ecg_disp = deque(maxlen=ECG_DISPLAY_POINTS)
        self.ecg_filtered_disp = deque(maxlen=ECG_DISPLAY_POINTS)
        self.ecg_filter = StreamingDisplayFilter(ECG_SAMPLE_RATE_HZ, 0.5, 40.0,
                                                 notch_frequency=50.0)
        self.ir_filter = StreamingDisplayFilter(PPG_SAMPLE_RATE_HZ, 0.5, 8.0,
                                                reject_steps=True)
        self.red_filter = StreamingDisplayFilter(PPG_SAMPLE_RATE_HZ, 0.5, 8.0,
                                                 reject_steps=True)
        self.notch_enabled = tk.BooleanVar(value=True)
        self.ppg_filter_enabled = tk.BooleanVar(value=True)
        self.ppg_polarity_var = tk.StringVar(value=AUTO)
        self.ppg_polarity = AutoPpgPolarity(PPG_SAMPLE_RATE_HZ)
        self.ppg_ir_raw_disp = deque(maxlen=PPG_DISPLAY_POINTS)
        self.ecg_times = deque(maxlen=ECG_DISPLAY_POINTS)
        self.ppg_times = deque(maxlen=PPG_DISPLAY_POINTS)
        self.ppg_ir_disp = deque(maxlen=PPG_DISPLAY_POINTS)
        self.ppg_red_disp = deque(maxlen=PPG_DISPLAY_POINTS)
        self.ppg_ir_filtered_disp = deque(maxlen=PPG_DISPLAY_POINTS)
        self.ppg_red_filtered_disp = deque(maxlen=PPG_DISPLAY_POINTS)
        self.ppg_dc_disp = deque(maxlen=PPG_DISPLAY_POINTS)
        self._ppg_dc = None          # IR 直流估计（一阶低通）
        self._red_dc = None
        self.last_ecg_time = 0.0
        self.last_ppg_time = 0.0
        self.latest_red = 0
        self.latest_ir = 0
        self.connection_started = 0.0

        self.total_ecg = 0
        self.total_ppg = 0
        self.last_data_time = 0.0
        self.dropped_before = 0
        self.last_quality_update = 0.0
        self._chart_background = None
        self._last_display_modes = None
        self._ranges = (StableRange(40.0, 500.0), StableRange(50.0, 1000.0),
                        StableRange(50.0, 1000.0))
        self.bp_client = None
        self.bp_enabled = False
        self._bp_generation = 0
        self._bp_pending = None
        self._bp_last_end = None
        self._bp_result = None
        self._bp_source = ""
        self._bp_result_wall = None
        self._bp_valid = False
        self._bp_ecg_t = deque(maxlen=3200)
        self._bp_ecg_adc = deque(maxlen=3200)
        self._bp_ppg_t = deque(maxlen=1600)
        self._bp_ppg_v = deque(maxlen=1600)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(CHART_REFRESH_MS, self._refresh_charts)
        self.after(UI_PUMP_MS, self._pump_samples)
        self.after(500, self._refresh_stats)
        self.after(200, self._poll_bp)
        self.after(100, self._poll_discovery)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        sidebar = tk.Frame(self, bg=BG, width=310)
        sidebar.pack(side="left", fill="y", padx=(12, 6), pady=12)
        sidebar.pack_propagate(False)
        controls = tk.Canvas(sidebar, bg=BG, highlightthickness=0, width=290)
        scrollbar = ttk.Scrollbar(sidebar, orient="vertical", command=controls.yview)
        scrollbar.pack(side="right", fill="y")
        controls.pack(side="left", fill="both", expand=True)
        controls.configure(yscrollcommand=scrollbar.set)
        left = tk.Frame(controls, bg=BG)
        left_window = controls.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>", lambda _event: controls.configure(
            scrollregion=controls.bbox("all")))
        controls.bind("<Configure>", lambda event: controls.itemconfigure(
            left_window, width=event.width))

        right = tk.Frame(self, bg=BG)
        right.pack(side="right", fill="both", expand=True, padx=(6, 12), pady=12)

        # --- BLE / 串口连接 ---
        box1 = self._panel(left, "设备连接")
        self.transport_var = tk.StringVar(value="BLE 蓝牙")
        self.transport_combo = ttk.Combobox(box1, textvariable=self.transport_var,
                                            values=["BLE 蓝牙", "USB 串口"], state="readonly")
        self.transport_combo.pack(fill="x", pady=2)
        self.transport_combo.bind("<<ComboboxSelected>>", self._transport_changed)
        row = tk.Frame(box1, bg=PANEL)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="设备", bg=PANEL, fg=TEXT, font=(FONT, 9), width=6,
                 anchor="w").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var,
                                       state="readonly", width=16, font=(FONT, 9))
        self.port_combo.pack(side="left", fill="x", expand=True)
        self.scan_btn = tk.Button(box1, text="搜索蓝牙设备", command=self._refresh_sources,
                  font=(FONT, 9), relief="flat", bg="#e8edf5",
                  activebackground="#dbe4f0")
        self.scan_btn.pack(fill="x", pady=(6, 2))

        row = tk.Frame(box1, bg=PANEL)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="波特率", bg=PANEL, fg=TEXT, font=(FONT, 9), width=6,
                 anchor="w").pack(side="left")
        self.baud_var = tk.StringVar(value="115200")
        self.baud_combo = ttk.Combobox(row, textvariable=self.baud_var, state="disabled", width=16,
                     font=(FONT, 9),
                     values=["115200"])
        self.baud_combo.pack(side="left", fill="x", expand=True)

        self.connect_btn = tk.Button(box1, text="连接", command=self._toggle_connect,
                                     font=(FONT, 10, "bold"), relief="flat",
                                     bg=ACCENT, fg="white", activebackground="#0284c7",
                                     activeforeground="white", height=1)
        self.connect_btn.pack(fill="x", pady=(8, 2))

        self.status_lbl = tk.Label(box1, text="未连接", bg=PANEL, fg=MUTED,
                                   font=(FONT, 9), anchor="w", justify="left",
                                   wraplength=250)
        self.status_lbl.pack(fill="x", pady=(2, 0))

        # --- 录制与导出 ---
        box2 = self._panel(left, "录制与导出")
        self.record_btn = tk.Button(box2, text="开始录制", command=self._toggle_record,
                                    font=(FONT, 10, "bold"), relief="flat",
                                    bg="#16a34a", fg="white",
                                    activebackground="#15803d", activeforeground="white")
        self.record_btn.pack(fill="x", pady=2)

        row = tk.Frame(box2, bg=PANEL)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="时长(s)", bg=PANEL, fg=TEXT, font=(FONT, 9)).pack(side="left")
        self.dur_var = tk.StringVar(value="60")
        tk.Entry(row, textvariable=self.dur_var, width=6, font=(FONT, 9)).pack(
            side="left", padx=4)
        tk.Label(row, text="0=不限", bg=PANEL, fg=MUTED, font=(FONT, 8)).pack(side="left")

        self.export_btn = tk.Button(box2, text="导出 CSV（模型可用）",
                                    command=self._export_csv, font=(FONT, 10),
                                    relief="flat", bg="#e8edf5", state="disabled",
                                    activebackground="#dbe4f0")
        self.export_btn.pack(fill="x", pady=(6, 2))

        self.record_info = tk.Label(box2, text="未录制", bg=PANEL, fg=MUTED,
                                    font=(FONT, 9), anchor="w", justify="left",
                                    wraplength=250)
        self.record_info.pack(fill="x", pady=(2, 0))

        # --- 信号质量 ---
        box3 = self._panel(left, "信号状态")
        self.quality_lbl = tk.Label(box3, text="等待数据…", bg=PANEL, fg=MUTED,
                                    font=(FONT, 9), anchor="w", justify="left",
                                    wraplength=250)
        self.quality_lbl.pack(fill="x")
        tk.Checkbutton(box3, text="ECG 滤波：0.5–40 Hz + 50 Hz 陷波", variable=self.notch_enabled,
                       command=lambda: self._refresh_charts(schedule=False), bg=PANEL, fg=TEXT,
                       font=(FONT, 9), anchor="w").pack(fill="x", pady=(6, 0))
        tk.Checkbutton(box3, text="PPG 滤波：0.5–8 Hz（IR / RED）", variable=self.ppg_filter_enabled,
                       command=lambda: self._refresh_charts(schedule=False), bg=PANEL, fg=TEXT,
                       font=(FONT, 9), anchor="w").pack(fill="x")
        tk.Label(box3, text="PPG显示极性（IR / RED）", bg=PANEL, fg=TEXT,
                 font=(FONT, 9), anchor="w").pack(fill="x", pady=(6, 2))
        self.ppg_polarity_combo = ttk.Combobox(box3, textvariable=self.ppg_polarity_var,
                                              values=MODES, state="readonly")
        self.ppg_polarity_combo.pack(fill="x")
        self.ppg_polarity_combo.bind("<<ComboboxSelected>>", self._on_ppg_polarity_changed)
        self.ppg_polarity_lbl = tk.Label(box3, text="", bg=PANEL, fg=MUTED,
                                        font=(FONT, 8), anchor="w", justify="left", wraplength=250)
        self.ppg_polarity_lbl.pack(fill="x", pady=2)

        # --- 链路统计 ---
        box4 = self._panel(left, "链路统计")
        self.stats_lbl = tk.Label(box4, text="—", bg=PANEL, fg=MUTED,
                                  font=("Consolas", 9), anchor="w", justify="left")
        self.stats_lbl.pack(fill="x")

        # Preserve the existing three-channel view in its own tab.
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        wave_tab = tk.Frame(self.notebook, bg=PANEL)
        self.bp_tab = tk.Frame(self.notebook, bg=PANEL)
        self.notebook.add(wave_tab, text="ECG / PPG 采集波形")
        self.notebook.add(self.bp_tab, text="UniCardio 血压预测")

        # --- 右侧图表 ---
        if Figure is not None:
            self.fig = Figure(figsize=(9, 7), dpi=100, facecolor=PANEL)
            self.ax_ecg = self.fig.add_subplot(311)
            self.ax_ppg = self.fig.add_subplot(312, sharex=self.ax_ecg)
            self.ax_ppg_dc = self.fig.add_subplot(313, sharex=self.ax_ecg)
            self.fig.subplots_adjust(hspace=0.45, left=0.09, right=0.98,
                                     top=0.95, bottom=0.07)
            self.canvas = FigureCanvasTkAgg(self.fig, master=wave_tab)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self._use_blit = bool(self.canvas.supports_blit)
            self._init_axes()
            self.canvas.mpl_connect("draw_event", self._on_chart_draw)
            self.canvas.mpl_connect("resize_event", self._on_chart_resize)
        else:
            tk.Label(wave_tab, text="未安装 matplotlib，波形不可视。\n"
                                 "pip install matplotlib numpy 后可显示。",
                     bg=BG, fg=BAD, font=(FONT, 11)).pack(expand=True)

        self._build_bp_ui()
        self._transport_changed()

    def _build_bp_ui(self):
        controls = tk.Frame(self.bp_tab, bg=PANEL)
        controls.pack(fill="x", padx=12, pady=10)
        for text, command in (("开始实时片段预测", self._start_bp),
                              ("停止预测", self._stop_bp),
                              ("预测 CSV 末段…", self._predict_bp_csv),
                              ("导出上次预测…", self._export_bp)):
            tk.Button(controls, text=text, command=command, font=(FONT, 10),
                      bg="#e8edf5", relief="flat").pack(side="left", padx=(0, 8))
        self.bp_values = tk.StringVar(value="SBP —    DBP —    MAP —  mmHg")
        tk.Label(self.bp_tab, textvariable=self.bp_values, bg=PANEL, fg=ACCENT,
                 font=(FONT, 21, "bold"), anchor="w").pack(fill="x", padx=12)
        self.bp_status = tk.StringVar(value="未启用。连接采集后可开始实时预测，也可选择已保存的 CSV。")
        tk.Label(self.bp_tab, textvariable=self.bp_status, bg=PANEL, fg=TEXT,
                 font=(FONT, 10), anchor="w", justify="left", wraplength=800).pack(fill="x", padx=12, pady=6)
        self.bp_detail = tk.StringVar(value="每次使用同步 ECG + IR PPG 的 4 秒片段；首次需要至少 6 秒连续数据。")
        tk.Label(self.bp_tab, textvariable=self.bp_detail, bg=PANEL, fg=MUTED,
                 font=(FONT, 9), anchor="w", wraplength=800).pack(fill="x", padx=12)
        self.bp_age = tk.StringVar(value="计算期间持续采集，仅处理最新片段；更新速度取决于本机推理耗时。")
        tk.Label(self.bp_tab, textvariable=self.bp_age, bg=PANEL, fg=MUTED,
                 font=(FONT, 9), anchor="w", wraplength=800).pack(fill="x", padx=12)
        tk.Label(self.bp_tab, text="研究用模型预测值，需与参考血压对照验证。", bg=PANEL,
                 fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x", padx=12, pady=(2, 8))
        if Figure is not None:
            self.bp_fig = Figure(figsize=(9, 6), dpi=100, facecolor=PANEL)
            self.bp_ax = self.bp_fig.add_subplot(211)
            self.bp_input_ax = self.bp_fig.add_subplot(212, sharex=self.bp_ax)
            self.bp_fig.subplots_adjust(hspace=0.38, left=0.09, right=0.98, top=0.92, bottom=0.09)
            for ax in (self.bp_ax, self.bp_input_ax):
                ax.grid(alpha=0.25)
                ax.set_xlim(0, 4)
                ax.tick_params(labelsize=8)
            self.bp_ax.set_title("预测 BP 波形（等待输入）", fontsize=10, loc="left")
            self.bp_ax.set_ylabel("mmHg", fontsize=9)
            self.bp_ax.set_ylim(40, 180)
            self.bp_input_ax.set_title("实际送入模型的 ECG / IR PPG", fontsize=10, loc="left")
            self.bp_input_ax.set_ylabel("归一化幅值", fontsize=9)
            self.bp_input_ax.set_xlabel("片段时间 (s)", fontsize=9)
            self.bp_input_ax.set_ylim(-1.15, 1.15)
            self.bp_line, = self.bp_ax.plot([], [], color=ACCENT, lw=1.4)
            self.bp_ecg_line, = self.bp_input_ax.plot([], [], color=ECG_COLOR, lw=0.9, label="ECG")
            self.bp_ppg_line, = self.bp_input_ax.plot([], [], color=PPG_COLOR, lw=1.1, label="IR PPG")
            self.bp_input_ax.legend(loc="upper right", fontsize=8)
            self.bp_canvas = FigureCanvasTkAgg(self.bp_fig, master=self.bp_tab)
            self.bp_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _ensure_bp_worker(self):
        if self.bp_client is None:
            self.bp_client = BPWorkerClient()
            self.bp_client.start()
            self.bp_status.set("正在后台加载 UniCardio 模型，采集和显示可继续使用…")

    def _start_bp(self):
        if self.worker is None:
            messagebox.showwarning("需要采集数据", "请先连接串口，或选择“预测 CSV 末段”。")
            return
        self._bp_generation += 1
        self.bp_enabled = True
        self._bp_pending = None
        self._bp_last_end = None
        self._bp_source = "实时采集"
        self._invalidate_bp("等待连续 ECG / IR PPG 数据…")
        self._ensure_bp_worker()
        self.notebook.select(self.bp_tab)

    def _stop_bp(self):
        self.bp_enabled = False
        self._bp_generation += 1
        self._bp_pending = None
        if self.bp_client is not None:
            self.bp_client.close()
            self.bp_client = None
        self._invalidate_bp("已停止预测；可导出上次预测结果。")

    def _invalidate_bp(self, reason):
        self._bp_valid = False
        self.bp_values.set("SBP —    DBP —    MAP —  mmHg")
        self.bp_status.set(reason)
        if Figure is not None and self._bp_result is not None:
            self.bp_ax.set_title("上次预测 BP 波形（当前无有效新结果）", fontsize=10, loc="left")
            self.bp_canvas.draw_idle()

    def _predict_bp_csv(self):
        path = filedialog.askopenfilename(title="选择同步 ECG / PPG CSV（预测最后4秒）",
                                          filetypes=[("CSV 文件", "*.csv")])
        if path:
            self._queue_bp_csv(path)

    def _queue_bp_csv(self, path):
        self.bp_enabled = False
        self._bp_generation += 1
        self._bp_source = os.path.basename(path)
        self._bp_pending = {"type": "csv", "path": str(path), "id": self._bp_generation}
        self._invalidate_bp(f"等待预测 CSV 末段：{self._bp_source}")
        self._ensure_bp_worker()
        self.notebook.select(self.bp_tab)

    def _poll_bp(self, schedule=True):
        client = self.bp_client
        if client is not None:
            while True:
                try:
                    message = client.events.get_nowait()
                except queue.Empty:
                    break
                kind = message["type"]
                if kind == "ready":
                    self.bp_status.set(f"模型已加载（{message['device']}），等待输入…")
                elif kind == "error":
                    self.bp_enabled = False
                    self._bp_pending = None
                    self._invalidate_bp(message["message"])
                    client.close()
                    self.bp_client = None
                elif message.get("id") == self._bp_generation:
                    if kind == "result":
                        self._show_bp_result(message["result"])
                    else:
                        self._invalidate_bp(message.get("message", "预测未完成"))
            if self.bp_client is not None and client.ready and not client.busy:
                if self._bp_pending is not None:
                    if client.submit(self._bp_pending):
                        self._bp_pending = None
                        self.bp_status.set("正在预测 CSV 最后 4 秒…")
                elif self.bp_enabled and self._bp_ecg_t and self._bp_ppg_t:
                    end = min(self._bp_ecg_t[-1], self._bp_ppg_t[-1])
                    start = max(self._bp_ecg_t[0], self._bp_ppg_t[0])
                    fresh = time.monotonic() - min(self.last_ecg_time, self.last_ppg_time) < 3
                    if fresh and end - start >= 6 and (self._bp_last_end is None or end - self._bp_last_end >= 2):
                        request = {"type": "live", "id": self._bp_generation,
                                   "ecg_t": list(self._bp_ecg_t), "ecg_adc": list(self._bp_ecg_adc),
                                   "ppg_t": list(self._bp_ppg_t), "ppg_v": list(self._bp_ppg_v)}
                        if client.submit(request):
                            self._bp_last_end = end
                            self.bp_status.set("正在后台预测最新 4 秒片段…")
            if (self.bp_enabled and self._bp_valid and self._bp_result is not None
                    and self._bp_ecg_t and self._bp_ppg_t):
                no_data = time.monotonic() - min(self.last_ecg_time, self.last_ppg_time) > 3
                lag = min(self._bp_ecg_t[-1], self._bp_ppg_t[-1]) - self._bp_result["end_s"]
                self.bp_age.set(f"图中为已完成片段的预测，片段结束距最新采样 {max(0, lag):.1f} 秒。")
                allowed_delay = max(10, 2 * self._bp_result["inference_seconds"] + 6)
                behind = (self._bp_ecg_t and self._bp_ppg_t and
                          min(self._bp_ecg_t[-1], self._bp_ppg_t[-1]) - self._bp_result["end_s"] > allowed_delay)
                if no_data or behind:
                    self._invalidate_bp("上次预测已过期，等待新的有效片段…")
        if schedule:
            self.after(200, self._poll_bp)

    def _show_bp_result(self, result):
        result = dict(result, source=self._bp_source)
        self._bp_result = result
        self._bp_result_wall = time.monotonic()
        self._bp_valid = True
        self.bp_values.set(f"SBP {result['sbp']:.1f}    DBP {result['dbp']:.1f}    MAP {result['map']:.1f}  mmHg")
        self.bp_status.set(f"预测完成 · {self._bp_source}")
        if not self.bp_enabled:
            self.bp_age.set("CSV 末段离线预测；图中为该文件最后 4 秒对应的模型输出。")
        self.bp_detail.set(f"采样片段 {result['start_s']:.2f}–{result['end_s']:.2f} s  |  "
                           f"计算 {result['inference_seconds']:.2f} s  |  {result['device']}  |  "
                           f"ECG近饱和点 {result['ecg_clipped_fraction']:.1%}")
        if Figure is not None:
            t = [i / 125 for i in range(500)]
            self.bp_line.set_data(t, result["bp_mmhg"])
            self.bp_ecg_line.set_data(t, result["ecg_input"])
            self.bp_ppg_line.set_data(t, result["ppg_input"])
            self.bp_ax.set_ylim(min(result["bp_mmhg"]) - 5, max(result["bp_mmhg"]) + 5)
            self.bp_ax.set_title("UniCardio 预测 BP 波形", fontsize=10, loc="left")
            self.bp_canvas.draw_idle()

    def _export_bp(self):
        if self._bp_result is None:
            messagebox.showinfo("无预测结果", "先完成一次实时或 CSV 预测。")
            return
        path = filedialog.asksaveasfilename(title="导出上次预测波形和血压数值",
                                           initialfile="unicardio_prediction.csv",
                                           defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")])
        if path:
            try:
                self._write_bp_csv(path)
                messagebox.showinfo("导出完成", f"已保存：\n{path}")
            except Exception as exc:
                messagebox.showerror("导出失败", str(exc))

    def _write_bp_csv(self, path):
        result = self._bp_result
        with open(path, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(["source", "time_s", "bp_mmhg", "ecg_input_norm", "ir_input_norm",
                             "sbp", "dbp", "map", "inference_seconds", "device"])
            for i, bp in enumerate(result["bp_mmhg"]):
                writer.writerow([result["source"], result["start_s"] + i / 125, bp,
                                 result["ecg_input"][i], result["ppg_input"][i],
                                 result["sbp"], result["dbp"], result["map"],
                                 result["inference_seconds"], result["device"]])

    def _panel(self, parent, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1)
        outer.pack(fill="x", pady=(0, 8))
        tk.Label(outer, text=title, bg=PANEL, fg=TEXT, font=(FONT, 9, "bold"),
                 anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        inner = tk.Frame(outer, bg=PANEL)
        inner.pack(fill="x", padx=8, pady=(0, 6))
        return inner

    def _init_axes(self) -> None:
        for ax in (self.ax_ecg, self.ax_ppg, self.ax_ppg_dc):
            ax.set_facecolor("#fbfcfe")
            ax.tick_params(labelsize=7, colors=MUTED)
            for sp in ax.spines.values():
                sp.set_color(BORDER)

        self.ax_ecg.set_title("ECG（AD8232, 200Hz）", fontsize=9, color=TEXT, loc="left")
        self.ax_ecg.set_ylabel("模块输出 (mV)", fontsize=8)
        self.ax_ecg.grid(alpha=0.25)

        self.ax_ppg.set_title("PPG 交流分量（MAX30102 IR, 100Hz）",
                              fontsize=9, color=TEXT, loc="left")
        self.ax_ppg.set_ylabel("AC", fontsize=8)
        self.ax_ppg.grid(alpha=0.25)

        self.ax_ppg_dc.set_title("PPG 交流分量（MAX30102 RED, 100Hz）",
                                 fontsize=9, color=TEXT, loc="left")
        self.ax_ppg_dc.set_ylabel("RED AC", fontsize=8)
        self.ax_ppg_dc.set_xlabel("相对时间 (s)", fontsize=8)
        self.ax_ppg_dc.grid(alpha=0.25)

        self._ln_ecg, = self.ax_ecg.plot([], [], color=ECG_COLOR, lw=1.1, animated=self._use_blit)
        self._ln_ppg, = self.ax_ppg.plot([], [], color=PPG_COLOR, lw=1.1, animated=self._use_blit)
        self._ln_dc, = self.ax_ppg_dc.plot([], [], color="#7c3aed", lw=1.1, animated=self._use_blit)
        for ax, scale in zip((self.ax_ecg, self.ax_ppg, self.ax_ppg_dc), self._ranges):
            ax.set_xlim(-DISPLAY_SECONDS, 0)
            ax.set_ylim(-scale.half_range, scale.half_range)

    # ------------------------------------------------------------ 串口控制 --
    def _transport_changed(self, _event=None):
        self.port_var.set("")
        self.port_combo["values"] = []
        is_ble = self.transport_var.get() == "BLE 蓝牙"
        self.baud_combo.config(state="disabled" if is_ble else "readonly")
        self.scan_btn.config(text="搜索蓝牙设备" if is_ble else "刷新端口列表")
        if is_ble:
            self.status_lbl.config(text="请搜索并选择 ATK-BLE04 兼容设备", fg=MUTED)
        else:
            self._refresh_ports()

    def _set_connection_controls(self, busy):
        self.transport_combo.config(state="disabled" if busy else "readonly")
        self.port_combo.config(state="disabled" if busy else "readonly")
        self.scan_btn.config(state="disabled" if busy else "normal")
        self.baud_combo.config(state="readonly" if not busy and
                               self.transport_var.get() == "USB 串口" else "disabled")

    def _refresh_sources(self):
        if self.transport_var.get() == "BLE 蓝牙":
            self._scan_ble()
        else:
            self._refresh_ports()

    def _scan_ble(self):
        if self.worker is not None or self.discovery is not None:
            return
        self.ble_targets.clear()
        self.port_var.set("")
        self.port_combo["values"] = []
        self.discovery = BleDiscovery()
        self._set_connection_controls(True)
        self.connect_btn.config(state="disabled")
        self.status_lbl.config(text="正在搜索附近的兼容 BLE 设备…", fg=MUTED)
        self.discovery.start()

    def _poll_discovery(self):
        discovery = self.discovery
        if discovery is not None and not discovery.is_alive():
            self.discovery = None
            self._set_connection_controls(False)
            self.connect_btn.config(state="normal")
            self.ble_targets = {target.label: target for target in discovery.targets}
            self.port_combo["values"] = list(self.ble_targets)
            if self.ble_targets:
                self.port_var.set(next(iter(self.ble_targets)))
            text = (f"发现 {len(self.ble_targets)} 个兼容 BLE 设备" if self.ble_targets
                    else "未发现兼容设备；请检查模块供电，并断开其他 BLE 软件后重试")
            self.status_lbl.config(text=discovery.error or text,
                                   fg=BAD if discovery.error else MUTED)
            address, self._connect_after_scan = self._connect_after_scan, None
            if address:
                match = next((target for target in discovery.targets
                              if target.address.upper() == address.upper()), None)
                if match:
                    self.port_var.set(match.label)
                    self._connect()
                else:
                    self.status_lbl.config(text=f"未搜索到指定设备 {address}", fg=BAD)
        self.after(100, self._poll_discovery)

    def _refresh_ports(self) -> None:
        if list_ports is None:
            self.port_combo["values"] = []
            self.status_lbl.config(text="未安装 pyserial：pip install pyserial", fg=BAD)
            return
        ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])
        self.status_lbl.config(
            text=f"发现 {len(ports)} 个串口" if ports else "未发现串口，请插入 USB-TTL 并刷新",
            fg=MUTED)

    def _toggle_connect(self) -> None:
        if self.worker is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        if self.worker is not None or self.discovery is not None:
            return
        is_ble = self.transport_var.get() == "BLE 蓝牙"
        if not is_ble and serial is None:
            messagebox.showerror("缺少依赖", "请先安装 pyserial：\n\npip install pyserial")
            return
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("未选择设备", "请先搜索并选择一个设备。")
            return
        if is_ble and port not in self.ble_targets:
            messagebox.showwarning("设备不可用", "请重新搜索蓝牙设备。")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            messagebox.showerror("参数错误", "波特率必须是整数。")
            return

        if self.recorder.recording:
            self._stop_record(auto=False)
        self._reset_display()
        self.worker = BleWorker(self.ble_targets[port]) if is_ble else SerialWorker(port, baud)
        self.worker.start()
        self._set_connection_controls(True)
        self.connect_btn.config(text="断开", bg=BAD, activebackground="#b91c1c")
        self.status_lbl.config(text=f"{self.worker.description}\n正在连接…", fg=MUTED)
        self.connection_started = time.monotonic()
        self.last_data_time = self.connection_started

    def _reset_display(self) -> None:
        self._stop_bp()
        for buf in (self._bp_ecg_t, self._bp_ecg_adc, self._bp_ppg_t, self._bp_ppg_v):
            buf.clear()
        for buf in (self.ecg_disp, self.ecg_filtered_disp, self.ecg_times, self.ppg_times,
                    self.ppg_ir_disp, self.ppg_red_disp, self.ppg_dc_disp,
                    self.ppg_ir_filtered_disp, self.ppg_red_filtered_disp, self.ppg_ir_raw_disp):
            buf.clear()
        self._ppg_dc = None
        self._red_dc = None
        self.ppg_polarity.reset()
        for filt in (self.ecg_filter, self.ir_filter, self.red_filter):
            filt.reset()
        for scale in self._ranges:
            scale.reset()
        self._chart_background = None
        self._last_display_modes = None
        self.total_ecg = self.total_ppg = 0
        self.last_ecg_time = self.last_ppg_time = 0.0
        self.latest_red = self.latest_ir = 0
        if Figure is not None:
            for line in (self._ln_ecg, self._ln_ppg, self._ln_dc):
                line.set_data([], [])
            self.canvas.draw_idle()

    def _disconnect(self) -> None:
        self._stop_bp()
        if self.recorder.recording:
            self._stop_record(auto=False)
        if self.worker is not None:
            self.worker.stop()
            self.worker.join(timeout=2.0)
            self.worker = None
        self._set_connection_controls(False)
        self.connect_btn.config(text="连接", bg=ACCENT, activebackground="#0284c7")
        self.status_lbl.config(text="已断开", fg=MUTED)

    # ------------------------------------------------------------ 数据处理 --
    def _pump_samples(self) -> None:
        """把后台线程队列里的样本搬到 UI/录制器。"""
        worker = self.worker
        if worker is not None and worker.error:
            err = worker.error
            self._disconnect()
            messagebox.showerror("连接错误", f"数据接收失败：\n\n{err}")
            self.after(UI_PUMP_MS, self._pump_samples)
            return

        processed = 0
        while worker is not None and processed < 4000:
            try:
                kind, sample = worker.rx_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            self.last_data_time = time.monotonic()

            if kind == "ecg":
                self.total_ecg += 1
                self.last_ecg_time = self.last_data_time
                if self.recorder.recording:
                    self.recorder.add_ecg(sample.timestamp_s, sample.value)
                self.ecg_disp.append(sample.value)
                self.ecg_times.append(sample.timestamp_s)
                self._bp_ecg_t.append(sample.timestamp_s)
                self._bp_ecg_adc.append(sample.value)
                self.ecg_filtered_disp.append(self.ecg_filter.process(
                    ecg_adc_to_mv(sample.value), sample.timestamp_s))
            elif kind == "ppg" and sample.value2 is not None:
                self.total_ppg += 1
                self.last_ppg_time = self.last_data_time
                if self.recorder.recording:
                    self.recorder.add_ppg(sample.timestamp_s, sample.value,
                                          sample.value2)
                # IR 直流/交流分离：一阶低通做直流，原始减直流得交流
                # Protocol value=RED, value2=IR. Keep channels separate end to end.
                self.latest_red = sample.value
                self.latest_ir = sample.value2
                self._bp_ppg_t.append(sample.timestamp_s)
                self._bp_ppg_v.append(sample.value2)
                ir = float(sample.value2)
                red = float(sample.value)
                self.ppg_ir_raw_disp.append(ir)
                if self._ppg_dc is None:
                    self._ppg_dc = ir
                else:
                    self._ppg_dc += (ir - self._ppg_dc) * 0.02
                self.ppg_ir_disp.append(ir - self._ppg_dc)
                self.ppg_dc_disp.append(ppg_to_percent(int(self._ppg_dc)))
                if self._red_dc is None:
                    self._red_dc = red
                else:
                    self._red_dc += (red - self._red_dc) * 0.02
                self.ppg_red_disp.append(red - self._red_dc)
                self.ppg_ir_filtered_disp.append(self.ir_filter.process(ir, sample.timestamp_s))
                self.ppg_red_filtered_disp.append(self.red_filter.process(red, sample.timestamp_s))
                self.ppg_times.append(sample.timestamp_s)

        self.after(UI_PUMP_MS, self._pump_samples)

    # --------------------------------------------------------------- 绘图 --
    def _on_ppg_polarity_changed(self, _event=None):
        self.ppg_polarity.reset()
        self._refresh_charts(schedule=False)

    def _ppg_display_sign(self):
        mode = self.ppg_polarity_var.get()
        if mode == ORIGINAL:
            sign, text = 1, "保持接收光强方向（×+1）"
        elif mode == INVERTED:
            sign, text = -1, "收缩峰朝上：基线−光强（×−1）"
        else:
            sign = self.ppg_polarity.update(list(self.ppg_times), list(self.ppg_ir_raw_disp),
                                             list(self.ppg_ir_filtered_disp))
            text = self.ppg_polarity.reason
        self.ppg_polarity_lbl.config(text=text + "\n仅用于显示，原始记录不取反")
        return sign

    def _draw_chart_lines(self) -> None:
        for line in (self._ln_ecg, self._ln_ppg, self._ln_dc):
            line.axes.draw_artist(line)

    def _on_chart_draw(self, event) -> None:
        if self._use_blit and event.canvas is self.canvas:
            self._chart_background = self.canvas.copy_from_bbox(self.fig.bbox)
            self._draw_chart_lines()

    def _on_chart_resize(self, event) -> None:
        self._chart_background = None

    def _present_charts(self, full_redraw=False) -> None:
        if full_redraw:
            self._chart_background = None
        if not self._use_blit or self._chart_background is None:
            self.canvas.draw_idle()
        else:
            self.canvas.restore_region(self._chart_background)
            self._draw_chart_lines()
            self.canvas.blit(self.fig.bbox)

    def save_waveform_preview(self, path, dpi=150) -> None:
        """Include animated/blitted lines when exporting a static preview."""
        lines = (self._ln_ecg, self._ln_ppg, self._ln_dc)
        previous = [line.get_animated() for line in lines]
        try:
            for line in lines:
                line.set_animated(False)
            self.fig.savefig(path, dpi=dpi)
        finally:
            for line, animated in zip(lines, previous):
                line.set_animated(animated)
            self._chart_background = None

    def _refresh_charts(self, schedule=True) -> None:
        if Figure is None:
            return
        try:
            ends = [buf[-1] for buf in (self.ecg_times, self.ppg_times) if buf]
            now_s = max(ends) if ends else 0.0
            ppg_sign = self._ppg_display_sign()
            modes = (bool(self.notch_enabled.get()), bool(self.ppg_filter_enabled.get()),
                     self.ppg_polarity_var.get(), ppg_sign)
            full_redraw = modes != self._last_display_modes
            if full_redraw:
                for scale in self._ranges:
                    scale.reset()
                self.ax_ecg.set_title("ECG（AD8232, 200Hz） · " +
                                     ("0.5–40 Hz / 50 Hz 陷波" if modes[0] else "原始波形"),
                                     fontsize=9, color=TEXT, loc="left")
                for ax, name in ((self.ax_ppg, "IR"), (self.ax_ppg_dc, "RED")):
                    ax.set_title(f"PPG {name}（100Hz） · " +
                                 ("0.5–8 Hz 滤波" if modes[1] else "未低通的交流分量") +
                                 (" · 反相显示" if ppg_sign == -1 else " · 原极性"),
                                 fontsize=9, color=TEXT, loc="left")
                self._last_display_modes = modes

            def update_line(ax, line, times, values, rate, smooth, scale):
                nonlocal full_redraw
                xs, ys = prepare_trace(list(times), list(values), rate, smooth=smooth)
                line.set_data(np.asarray(xs) - now_s, ys)
                # A gap can leave old samples in a count-limited deque long
                # after they leave the visible six-second time window.
                visible = [y for t, y in zip(xs, ys) if now_s - DISPLAY_SECONDS <= t <= now_s]
                bounds = scale.update(visible, now_s)
                if tuple(ax.get_ylim()) != bounds:
                    ax.set_ylim(*bounds)
                    full_redraw = True

            if self.ecg_disp:
                values = self.ecg_filtered_disp if modes[0] else ecg_adc_to_mv(np.asarray(self.ecg_disp))
                update_line(self.ax_ecg, self._ln_ecg, self.ecg_times, values,
                            ECG_SAMPLE_RATE_HZ, modes[0], self._ranges[0])
            if self.ppg_ir_disp:
                values = self.ppg_ir_filtered_disp if modes[1] else self.ppg_ir_disp
                update_line(self.ax_ppg, self._ln_ppg, self.ppg_times, np.asarray(values) * ppg_sign,
                            PPG_SAMPLE_RATE_HZ, modes[1], self._ranges[1])
            if self.ppg_red_disp:
                values = self.ppg_red_filtered_disp if modes[1] else self.ppg_red_disp
                update_line(self.ax_ppg_dc, self._ln_dc, self.ppg_times, np.asarray(values) * ppg_sign,
                            PPG_SAMPLE_RATE_HZ, modes[1], self._ranges[2])
            self._present_charts(full_redraw)
        except Exception as exc:
            self.status_lbl.config(text=f"波形刷新失败：{exc}", fg=BAD)
        if schedule:
            self.after(CHART_REFRESH_MS, self._refresh_charts)

    # ----------------------------------------------------------- 统计/质量 --
    def _refresh_stats(self) -> None:
        worker = self.worker

        # 无数据超时提示
        if worker is not None:
            now = time.monotonic()
            ecg_live = self.total_ecg > 0 and now - self.last_ecg_time < 2.0
            ppg_live = self.total_ppg > 0 and now - self.last_ppg_time < 2.0
            if worker.state == "connecting":
                status, color = "正在连接设备…", MUTED
            elif worker.state == "synchronizing":
                status, color = "已连接，正在同步最新数据…", MUTED
            elif ecg_live and ppg_live:
                status, color = "ECG / PPG 两路持续接收", GOOD
            elif ecg_live:
                status, color = "ECG 正常；PPG 无数据，检查 MAX30102 接线", WARN
            elif ppg_live:
                status, color = "PPG 正常；ECG 无数据，检查 ADC / TIM2", WARN
            elif worker.rx_bytes:
                status, color = "暂无有效波形，检查帧格式或采集是否停止", BAD
            else:
                waited = now - self.connection_started
                status, color = f"已连接，等待数据 {waited:.0f}s\n检查 PA2→模块 RXD、共地和采集固件", WARN
            self.status_lbl.config(text=f"{worker.description}\n{status}", fg=color)

        if worker is not None:
            st = worker.parser.stats
            rates = self._measured_rates()
            self.stats_lbl.config(text=(
                f"接收字节 : {st.total_bytes} B\n"
                f"ECG 帧数 : {st.ecg_frames}  ({rates[0]:.1f} Hz)\n"
                f"PPG 帧数 : {st.ppg_frames}  ({rates[1]:.1f} Hz)\n"
                f"坏帧     : {st.bad_frames}\n"
                f"重复帧   : {st.duplicate_frames}（已去重）\n"
                f"丢帧     : {st.lost_frames} ({st.frame_loss_rate * 100:.2f}%)\n"
                f"时间戳回绕: {st.timestamp_wraps}"
                f"\n界面丢点 : {worker.ui_dropped}"
            ))
            self._update_quality()

        # 自动停止录制
        if self.recorder.recording and self.recorder.auto_stopped:
            self._stop_record(auto=True)

        if self.recorder.recording:
            ecg_n, ppg_n = self.recorder.sample_counts()
            self.record_info.config(
                text=f"录制中 {self.recorder.elapsed_s():.1f}s\n"
                     f"ECG {ecg_n} 点 / PPG {ppg_n} 点", fg=GOOD)

        self.after(500, self._refresh_stats)

    def _measured_rates(self) -> tuple[float, float]:
        """按各路实际时间戳计算显示窗口内帧率，与是否录制无关。"""
        def rate(times) -> float:
            if len(times) < 2 or times[-1] <= times[0]:
                return 0.0
            return (len(times) - 1) / (times[-1] - times[0])
        return rate(self.ecg_times), rate(self.ppg_times)

    def _update_quality(self) -> None:
        if np is None:
            return
        lines = []

        # Input diagnostics refer to amplified AD8232 OUT, not body ECG voltage.
        if len(self.ecg_disp) > ECG_SAMPLE_RATE_HZ:
            arr = np.fromiter(list(self.ecg_disp)[-int(ECG_SAMPLE_RATE_HZ * 2):],
                              dtype=float)
            p2p = (arr.max() - arr.min()) * (3300.0 / 4096.0)
            near_low = np.mean(arr <= 4095 * 0.03)
            near_high = np.mean(arr >= 4095 * 0.97)
            rail_fraction = near_low + near_high
            if rail_fraction > 0.05:
                lines.append((f"ECG: 近0V {near_low*100:.0f}% / 近满量程 {near_high*100:.0f}%", BAD))
            elif p2p < ECG_FLAT_P2P_MV:
                lines.append(("ECG: 交流变化很小，检查输入", WARN))
            else:
                lines.append((f"ECG: 模拟输出峰峰值 {p2p:.0f} mV", MUTED))
            lines.append((f"ADC {int(arr.min())}–{int(arr.max())}；"
                          f"OUT均值≈{arr.mean()*3.3/4096.0:.3f}V", MUTED))
        else:
            lines.append(("ECG: 数据不足…", MUTED))

        # PPG 质量：直流看手指在位，交流看灌注指数
        if self.ppg_dc_disp:
            dc_pct = self.ppg_dc_disp[-1]
            if dc_pct < PPG_DC_MIN_PERCENT:
                lines.append((f"PPG: 手指未放好（DC {dc_pct:.1f}%）", BAD))
            elif dc_pct > PPG_DC_MAX_PERCENT:
                lines.append((f"PPG: 光强饱和（DC {dc_pct:.1f}%）", BAD))
            else:
                lines.append((f"PPG: DC {dc_pct:.1f}%FS", GOOD))
            lines.append((f"IR={self.latest_ir} / RED={self.latest_red}", MUTED))

        if len(self.ppg_ir_disp) > PPG_SAMPLE_RATE_HZ:
            ac = np.fromiter(list(self.ppg_ir_disp)[-int(PPG_SAMPLE_RATE_HZ * 2):],
                             dtype=float)
            dc = self._ppg_dc or 1.0
            pi = (ac.max() - ac.min()) / max(dc, 1.0) * 100
            lines.append((f"PPG: 估计交流峰峰值/DC {pi:.2f}%", MUTED))

        # 链路丢帧
        if self.worker is not None:
            loss = self.worker.parser.stats.frame_loss_rate
            if loss > 0.05:
                lines.append((f"链路丢帧 {loss*100:.1f}%，建议降波特率负载",
                              BAD if loss > 0.2 else WARN))

        text = "\n".join(t for t, _ in lines) if lines else "等待数据…"
        color = MUTED
        for _, c in lines:
            if c == BAD:
                color = BAD
                break
            if c == WARN and color != BAD:
                color = WARN
        self.quality_lbl.config(text=text, fg=color)

    # --------------------------------------------------------------- 录制 --
    def _toggle_record(self) -> None:
        if self.recorder.recording:
            self._stop_record(auto=False)
            return

        try:
            limit = float(self.dur_var.get())
        except ValueError:
            limit = 60.0
        limit = None if limit <= 0 else limit

        self.recorder.auto_limit_seconds = limit
        self.recorder.start()
        self.record_btn.config(text="停止录制", bg=WARN, activebackground="#b45309")
        self.export_btn.config(state="disabled")
        self.record_info.config(text="录制中 0.0s", fg=GOOD)

    def _stop_record(self, auto: bool) -> None:
        self.recorder.stop()
        self.record_btn.config(text="开始录制", bg="#16a34a", activebackground="#15803d")
        ecg_n, ppg_n = self.recorder.sample_counts()
        tag = "已自动停止（到达设定时长）" if auto else "已停止"
        self.record_info.config(
            text=f"{tag}\nECG {ecg_n} 点 / PPG {ppg_n} 点\n时长 "
                 f"{self.recorder.series.duration_s:.1f}s",
            fg=GOOD if (ecg_n > 0 and ppg_n > 0) else WARN)
        self.export_btn.config(state="normal" if ecg_n > 0 else "disabled")

    def _export_csv(self) -> None:
        ecg_n, ppg_n = self.recorder.sample_counts()
        if ecg_n == 0 or ppg_n == 0:
            messagebox.showwarning("无数据", "还没有可用数据，请先录制。")
            return

        if self.recorder.recording:
            self._stop_record(auto=False)

        initial = default_output_path()
        path = filedialog.asksaveasfilename(
            title="导出同步 ECG/PPG 数据",
            initialdir=os.path.dirname(initial),
            initialfile=os.path.basename(initial),
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return

        try:
            out = self.recorder.to_csv(path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return

        ecg_r, ppg_r = self.recorder.measured_rates()
        messagebox.showinfo(
            "导出完成",
            f"已导出：\n{out}\n\n"
            f"ECG {ecg_n} 点 @ {ecg_r:.1f} Hz\n"
            f"PPG {ppg_n} 点 @ {ppg_r:.1f} Hz\n\n"
            f"该 CSV 含 ecg_sync_time_s / ir_sync_time_s 等列，\n"
            f"可直接用于 UniCardio、ABPImputation 与特征提取流程。")

    # -------------------------------------------------------------- 收尾 --
    def _on_close(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        self.destroy()

    def destroy(self) -> None:
        if self.discovery is not None:
            self.discovery.stop()
            self.discovery.join(timeout=2.0)
            self.discovery = None
        if self.worker is not None:
            self.worker.stop()
            if self.worker.ident is not None:
                self.worker.join(timeout=2.0)
            self.worker = None
        client = getattr(self, "bp_client", None)
        if client is not None:
            client.close()
            self.bp_client = None
        # Cancel timers, including pending Matplotlib draws, before destroying Tcl
        # commands. This also makes repeated GUI opens/closes safe in regressions.
        try:
            for callback in self.tk.call("after", "info"):
                # The callback may belong to a child canvas. Leave its registered
                # Tcl command for that widget's own destroy() to clean up once.
                self.tk.call("after", "cancel", callback)
        except tk.TclError:
            pass
        super().destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="ECG / PPG 分离波形采集")
    parser.add_argument("--port", help="启动后自动连接，例如 COM13")
    parser.add_argument("--ble-address", help="搜索后自动连接指定 BLE 地址")
    parser.add_argument("--predict-csv", help="启动后预测同步 CSV 的最后4秒")
    args = parser.parse_args()
    if args.port and args.ble_address:
        parser.error("--port 和 --ble-address 只能选择一个")
    app = SerialEcgPpgGui()
    if args.port:
        app.transport_var.set("USB 串口")
        app._transport_changed()
        app.port_var.set(args.port)
        app.after(100, app._connect)
    else:
        app._connect_after_scan = args.ble_address
        app.after(200, app._scan_ble)
    if args.predict_csv:
        app.after(200, lambda: app._queue_bp_csv(args.predict_csv))
    app.mainloop()


if __name__ == "__main__":
    main()
