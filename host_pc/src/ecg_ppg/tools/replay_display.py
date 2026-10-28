"""Replay an existing raw acquisition CSV through the actual GUI without serial IO."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np

from ecg_ppg.acquisition.ecg_ppg_protocol import Sample, ecg_adc_to_mv
from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui, SerialWorker
from ecg_ppg.gui.waveform_display import StreamingDisplayFilter
from ecg_ppg.gui.ppg_polarity import AUTO, INVERTED, ORIGINAL
from ecg_ppg.paths import OUTPUTS_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUTS_DIR / "display_validation")
    parser.add_argument("--ppg-polarity", choices=("auto", "inverted", "original"), default="auto")
    parser.add_argument("--keep-open", action="store_true", help="打开离线回放窗口供检查显示控件")
    args = parser.parse_args()
    events, ecg, ppg = [], [], []
    with args.input.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            if row.get("ecg_raw", "").strip() and row.get("ecg_sync_time_s", "").strip():
                t, adc = float(row["ecg_sync_time_s"]), int(float(row["ecg_raw"]))
                sample = Sample(adc, None, t, int(t * 1e6) & 0xFFFFFFFF, 0)
                ecg.append(sample)
                events.append((t, "ecg", sample))
            if row.get("ir_processed", "").strip() and row.get("ir_sync_time_s", "").strip():
                t = float(row["ir_sync_time_s"])
                sample = Sample(int(float(row["red_processed"])), int(float(row["ir_processed"])),
                                t, int(t * 1e6) & 0xFFFFFFFF, 0)
                ppg.append(sample)
                events.append((t, "ppg", sample))
    if not ecg or not ppg:
        raise ValueError("Input must contain ECG raw ADC and both raw PPG channels")
    events.sort(key=lambda event: event[0])
    args.output.mkdir(parents=True, exist_ok=True)
    app = SerialEcgPpgGui()
    app.withdraw()
    try:
        app.ppg_polarity_var.set({"auto": AUTO, "inverted": INVERTED, "original": ORIGINAL}[args.ppg_polarity])
        app.worker = SerialWorker("OFFLINE_REPLAY", 115200)
        app.recorder.auto_limit_seconds = None
        app.recorder.start()
        for _, kind, sample in events:
            app.worker._put((kind, sample))
        while not app.worker.rx_queue.empty():
            app._pump_samples()
            # Advance ranges and automatic orientation along the source clock,
            # as in live display; a single final draw would keep startup ranges.
            app._refresh_charts(schedule=False)
        app._refresh_charts(schedule=False)
        if app.recorder.series.ecg_raw != [s.value for s in ecg]:
            raise AssertionError("Replay changed recorded ECG samples")
        if app.recorder.series.ir_v != [s.value2 for s in ppg]:
            raise AssertionError("Replay changed recorded PPG samples")
        if app.recorder.series.red_v != [s.value for s in ppg]:
            raise AssertionError("Replay changed recorded RED samples")
        origin = events[0][0]
        if app.recorder.series.ecg_t != [s.timestamp_s - origin for s in ecg]:
            raise AssertionError("Replay changed ECG timestamps")
        if app.recorder.series.ir_t != [s.timestamp_s - origin for s in ppg]:
            raise AssertionError("Replay changed PPG timestamps")
        raw = np.array([ecg_adc_to_mv(s.value) for s in ecg])
        filt = StreamingDisplayFilter(200, 0.5, 40, notch_frequency=50)
        filtered = np.array([filt.process(v, s.timestamp_s) for v, s in zip(raw, ecg)])
        # Band power of the actual full display IIR chain, excluding startup.
        rejection = None
        if len(raw) > 800:
            window = np.hanning(len(raw) - 400)
            frequencies = np.fft.rfftfreq(len(window), 1 / 200)
            band = (frequencies >= 49) & (frequencies <= 51)
            power = lambda x: float(np.sum(abs(np.fft.rfft((x - x.mean()) * window))[band] ** 2))
            rejection = float(10 * np.log10(max(power(raw[400:]), 1e-30) / max(power(filtered[400:]), 1e-30)))
        app.fig.suptitle("历史实测原始数据离线回放（非本轮实时采集）", fontsize=10, y=0.995)
        app.fig.subplots_adjust(top=0.90)
        app.save_waveform_preview(args.output / "replayed_waveforms.png", dpi=140)
        report = {
            "source": "offline_replay_of_existing_raw_capture",
            "input": str(args.input.resolve()),
            "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
            "ecg_samples": len(ecg), "ppg_pairs": len(ppg),
            "ecg_display_49_51hz_attenuation_db": rejection,
            "raw_ecg_rail_fraction": float(np.mean([s.value <= 123 or s.value >= 3972 for s in ecg])),
            "recording_unchanged": True,
            "ppg_display_polarity": {"mode": args.ppg_polarity,
                                     "sign": app._ppg_display_sign(),
                                     "auto_locked": app.ppg_polarity.locked,
                                     "auto_status": app.ppg_polarity.reason if args.ppg_polarity == "auto" else None},
            "display_filters": {"ecg": "0.5-40 Hz + 50 Hz notch Q30", "ppg": "0.5-8 Hz", "smoothing": "SG(5,2)"},
        }
        (args.output / "replay_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if args.keep_open:
            app._refresh_stats()
            app.recorder.stop()
            app.worker = None
            app.title("ECG + PPG 离线回放 · PPG极性检查")
            app.status_lbl.config(text="历史CSV离线回放，无实时采集")
            app.record_info.config(text="原始文件：" + args.input.name)
            for button in (app.connect_btn, app.scan_btn, app.record_btn, app.export_btn):
                button.config(state="disabled")
            app.transport_combo.config(state="disabled")
            app.port_combo.config(state="disabled")
            app.deiconify()
            app.mainloop()
            app = None  # WM_DELETE_WINDOW already destroyed it.
    finally:
        if app is not None:
            app.destroy()


if __name__ == "__main__":
    main()
