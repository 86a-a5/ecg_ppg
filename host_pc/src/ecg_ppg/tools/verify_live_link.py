"""Capture a real serial or BLE session through the GUI and save its evidence.

Example: python run.py verify --port COM13 --seconds 30 --keep-open
BLE: python run.py verify --ble-address AA:BB:CC:DD:EE:FF --seconds 60
"""
import argparse
import json
from pathlib import Path
import sys
import time

from ecg_ppg.gui.serial_ecg_ppg_gui import SerialEcgPpgGui
from ecg_ppg.paths import OUTPUTS_DIR


def describe(values, times):
    if not values:
        return {"samples": 0, "rate_hz": 0.0}
    deltas = [b - a for a, b in zip(times, times[1:])]
    return {
        "samples": len(values),
        "rate_hz": (len(times) - 1) / (times[-1] - times[0]) if times[-1] > times[0] else 0.0,
        "minimum": min(values), "maximum": max(values),
        "unique_values": len(set(values)),
        "nonpositive_time_steps": sum(dt <= 0 for dt in deltas),
        "max_gap_ms": max(deltas, default=0.0) * 1000,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--port")
    source.add_argument("--ble-address")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=OUTPUTS_DIR / "link_validation")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = SerialEcgPpgGui()
    exit_code = 2
    deadline = time.monotonic() + 45

    def start():
        if args.ble_address:
            app._connect_after_scan = args.ble_address
            app._scan_ble()
        else:
            app.transport_var.set("USB 串口")
            app._transport_changed()
            app.port_var.set(args.port or "COM13")
            app._connect()
        wait_for_data()

    def wait_for_data():
        if app.worker and app.worker.state == "connected" and app.total_ecg and app.total_ppg:
            begin_recording()
        elif time.monotonic() < deadline:
            app.after(100, wait_for_data)
        else:
            report = {"passed": False, "error": "连接或两路数据等待超时",
                      "status": app.status_lbl.cget("text")}
            (args.output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False), flush=True)
            app.destroy()

    def begin_recording():
        app.dur_var.set("0")
        app._toggle_record()
        app.after(int(args.seconds * 1000), finish)

    def finish():
        nonlocal exit_code
        app._stop_record(auto=False)
        worker = app.worker
        series = app.recorder.series
        report = {
            "port": args.port or (None if args.ble_address else "COM13"),
            "ble_address": args.ble_address, "duration_requested_s": args.seconds,
            "source": "physical_ble_gatt" if args.ble_address else "physical_serial_port",
            "ecg": describe(series.ecg_raw, series.ecg_t),
            "ppg_ir": describe(series.ir_v, series.ir_t),
            "ppg_red": describe(series.red_v, series.red_t),
            "parser": dict(vars(worker.parser.stats)) if worker else {},
            "transport_error": worker.error if worker else "Worker disconnected",
            "ui_dropped": worker.ui_dropped if worker else 0,
            "display_filter": "ECG 0.5-40 Hz + 50 Hz notch Q=30; PPG 0.5-8 Hz; display SG(5,2); CSV retains raw samples",
        }
        if worker:
            report["startup_discarded_bytes"] = getattr(worker, "startup_bytes", 0)
            report["startup_parser"] = getattr(worker, "startup_stats", {})
            report["ble_mtu"] = getattr(worker, "mtu_size", None)
            report["host_wall_rate_hz"] = {}
            for kind, count in (("ecg", worker.parser.stats.ecg_frames),
                                ("ppg", worker.parser.stats.ppg_frames)):
                span = worker.last_frame_wall.get(kind, 0) - worker.first_frame_wall.get(kind, 0)
                report["host_wall_rate_hz"][kind] = (count - 1) / span if span > 0 else 0.0
        report["passed"] = (
            worker is not None and not worker.error and worker.ui_dropped == 0
            and worker.parser.stats.bad_frames == 0 and worker.parser.stats.lost_frames == 0
            and report["ecg"]["samples"] >= args.seconds * 160
            and report["ppg_ir"]["samples"] >= args.seconds * 80
            and 190 < report["ecg"]["rate_hz"] < 210
            and 95 < report["ppg_ir"]["rate_hz"] < 105
            and report["ecg"].get("nonpositive_time_steps", 1) == 0
            and report["ppg_ir"].get("nonpositive_time_steps", 1) == 0
        )
        if series.ecg_raw or series.ir_v:
            app.recorder.to_csv(str(args.output / "live_capture.csv"))
        if len(series.ecg_raw) > 800:
            import numpy as np
            from ecg_ppg.gui.waveform_display import StreamingDisplayFilter
            raw = np.asarray(series.ecg_mv, dtype=float)
            filt = StreamingDisplayFilter(200, 0.5, 40, notch_frequency=50)
            filtered = np.asarray([filt.process(x, t) for x, t in zip(raw, series.ecg_t)])
            raw, filtered = raw[400:], filtered[400:]
            window = np.hanning(len(raw))
            freq = np.fft.rfftfreq(len(raw), 1 / 200)
            band = (freq >= 49) & (freq <= 51)
            before = np.sum(abs(np.fft.rfft((raw - raw.mean()) * window))[band] ** 2)
            after = np.sum(abs(np.fft.rfft((filtered - filtered.mean()) * window))[band] ** 2)
            report["ecg_display_49_51hz_attenuation_db"] = float(10 * np.log10(max(before, 1e-30) / max(after, 1e-30)))
        app._refresh_charts(schedule=False)
        report["plot_points"] = [len(line.get_xdata()) for line in
                                 (app._ln_ecg, app._ln_ppg, app._ln_dc)]
        report["plot_x_limits"] = [list(ax.get_xlim()) for ax in
                                   (app.ax_ecg, app.ax_ppg, app.ax_ppg_dc)]
        report["passed"] = (report["passed"] and all(report["plot_points"])
                            and all(limits == report["plot_x_limits"][0]
                                    for limits in report["plot_x_limits"]))
        app.save_waveform_preview(args.output / "live_waveforms.png", dpi=150)
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        exit_code = 0 if report["passed"] else 2
        if not args.keep_open:
            app._disconnect()
            app.destroy()

    app.after(200, start)
    app.mainloop()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
