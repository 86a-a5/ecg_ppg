#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run.py — 上位机一键入口（自动配好解释器与依赖路径）
====================================================
从项目虚拟环境或当前解释器启动命令，并设置 src 包路径：

    python run.py extract records/synth_rest.csv --plot
    python run.py bp     records/synth_rest_beats.csv --sbp0 118 --dbp0 76
    python run.py gui
    python run.py synth  -o records/test.csv --seconds 60 --hr-ramp
    python run.py unicardio records/synth_rest.csv
    python run.py selftest
    python run.py test
    python run.py doctor        <- 环境体检，跑不起来先看这个

脚本会按命令自动选择运行环境：

* **算法脚本**优先使用 ECG_PPG_PY 或项目 .venv，再使用当前解释器。
  如需额外依赖路径，可设置 ECG_PPG_EXTRA_PYTHONPATH。

* **图形界面**（gui）优先使用项目 .venv 或 ECG_PPG_GUI_PY 指定的环境。
  必须包含 Tcl/Tk、bleak、pyserial、numpy、scipy、matplotlib。

* **UniCardio**（unicardio）使用模型项目的 venv，或 ECG_PPG_MODEL_PY 指定的解释器。
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.join(HERE, "src")
sys.path.insert(0, SRC_ROOT)
_LOCAL_PYTHONS = [os.path.join(HERE, ".venv", "Scripts", "python.exe"),
                  os.path.join(HERE, ".venv", "bin", "python")]
PY = os.environ.get("ECG_PPG_PY") or next(
    (path for path in _LOCAL_PYTHONS if os.path.isfile(path)), sys.executable)
SITEX = os.environ.get("ECG_PPG_EXTRA_PYTHONPATH", "")

# GUI 使用带完整依赖的解释器，优先用户指定环境和本项目 .venv。
_GUI_CANDIDATES = [
    os.environ.get("ECG_PPG_GUI_PY", ""),
    os.path.join(HERE, ".venv", "Scripts", "python.exe"),
    os.path.join(HERE, ".venv", "bin", "python"),
    sys.executable,
    os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")),
        "Programs", "Python", "Python314", "python.exe",
    ),
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Programs", "Python", "Python313", "python.exe",
    ),
]


def _find_gui_python() -> str | None:
    """返回第一个能导入全部 GUI / BLE 依赖的解释器路径。"""
    probe = "import tkinter, bleak, serial, numpy, scipy, matplotlib; print('tk')"
    for cand in _GUI_CANDIDATES:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            r = subprocess.run([cand, "-c", probe], capture_output=True,
                               text=True, timeout=30)
            if (r.stdout or "").strip() == "tk":
                return cand
        except Exception:  # noqa: BLE001
            continue
    return None


GUI_PY = _find_gui_python() or sys.executable

SCRIPTS = {
    "extract": "ecg_ppg.algorithms.extract_features",
    "features": "ecg_ppg.algorithms.extract_features",
    "bp": "ecg_ppg.algorithms.bp_estimate",
    "gui": "ecg_ppg.gui.serial_ecg_ppg_gui",
    "synth": "ecg_ppg.tools.make_synthetic_record",
    "unicardio": "ecg_ppg.models.run_unicardio_bp",
    "record": "ecg_ppg.acquisition.ecg_ppg_record",
    "protocol": "ecg_ppg.acquisition.ecg_ppg_protocol",
    "signal": "ecg_ppg.algorithms.ecg_ppg_signal",
    "replay": "ecg_ppg.tools.replay_display",
    "verify": "ecg_ppg.tools.verify_live_link",
}

# 需要图形界面（依赖 tkinter）的命令
GUI_COMMANDS = {"gui", "replay", "verify"}


def selftest() -> int:
    print("=" * 60)
    print("解释器:", PY)
    print("sitex  :", SITEX)
    print("=" * 60)
    for name in ("protocol", "record", "signal"):
        print(f"\n--- 自检 {name} ({SCRIPTS[name]}) ---")
        r = subprocess.run([PY, "-B", "-m", SCRIPTS[name]], cwd=HERE, env=_env())
        if r.returncode != 0:
            print(f"[失败] {name} 返回 {r.returncode}")
            return r.returncode
    print("\n全部自检通过。")
    return 0


def _source_env() -> dict:
    env = dict(os.environ)
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = SRC_ROOT + (os.pathsep + old if old else "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _env() -> dict:
    env = dict(os.environ)
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (SRC_ROOT, SITEX, old)))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _check_gui_deps(interp: str) -> None:
    """GUI 依赖体检：缺谁就提示一条 pip 命令，不阻断启动（GUI 自身有降级逻辑）。"""
    code = (
        "import importlib\n"
        "miss=[m for m in ('tkinter','serial','numpy','scipy','matplotlib','bleak')\n"
        "      if importlib.util.find_spec(m) is None]\n"
        "print(' '.join(miss))\n"
    )
    try:
        r = subprocess.run([interp, "-c", code], capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"[run.py] 依赖体检跳过：{exc}")
        return
    miss = (r.stdout or "").strip().split()
    if miss:
        print(f"[run.py] 警告：GUI 解释器缺少 {', '.join(miss)}")
        print(f'[run.py] 安装： "{interp}" -m pip install ' + " ".join(
            {"serial": "pyserial"}.get(m, m) for m in miss
        ))
    else:
        print("[run.py] GUI 与 BLE 依赖体检通过。")


def _probe(interp: str, with_sitex: bool) -> None:
    """打印某个解释器的关键依赖状态。"""
    print(f"\n解释器: {interp}")
    print(f"  存在: {'是' if os.path.isfile(interp) else '否 ← 不可用'}")
    if not os.path.isfile(interp):
        return
    code = (
        "import sys, importlib.util\n"
        "print('  版本:', sys.version.split()[0])\n"
        "for m in ('tkinter','serial','numpy','scipy','matplotlib','bleak'):\n"
        "    s = importlib.util.find_spec(m)\n"
        "    if s is None:\n"
        "        print(f'  {m:11s} 缺失')\n"
        "    else:\n"
        "        try:\n"
        "            mod = __import__(m)\n"
        "            print(f'  {m:11s} OK  {getattr(mod, chr(95)*2+chr(118)+chr(101)+chr(114)+chr(115)+chr(105)+chr(111)+chr(110)+chr(95)*2, \"\")}')\n"
        "        except Exception as e:\n"
        "            print(f'  {m:11s} 导入失败: {e}')\n"
    )
    env = _env() if with_sitex else dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.run([interp, "-c", code], cwd=HERE, env=env, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"  探测失败: {exc}")


def doctor() -> int:
    """一键体检：把两套解释器的依赖状态都打出来，便于排查"跑不起来"。"""
    print("=" * 60)
    print("环境体检")
    print("=" * 60)
    print("\n[1/2] 算法解释器（envs/ecg + sitex）—— extract / bp / synth / unicardio 等")
    _probe(PY, with_sitex=True)
    print("\n[2/2] GUI 解释器（需带 tkinter）—— gui 命令 / 启动上位机.bat")
    _probe(GUI_PY, with_sitex=False)

    print("\n" + "-" * 60)
    print("结论：")
    if os.path.isfile(PY):
        print("  · 算法链路 → 用 run.py <命令>，无需额外配置。")
    else:
        print(f"  · 算法环境缺失（{PY}），请按 README『环境准备』重建。")
    if os.path.isfile(GUI_PY):
        print("  · 图形界面 → 双击『start_gui.bat』（或『launch_gui.py』），或运行 run.py gui。")
        print("    若 GUI 报缺库，执行：")
        print(f'      "{GUI_PY}" -m pip install pyserial numpy scipy matplotlib')
    else:
        print("  · 未找到带 tkinter 的 Python，GUI 无法运行。")
        print("    可用环境变量 ECG_PPG_GUI_PY 指定解释器路径。")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cmd = sys.argv[1].lower()
    if cmd == "selftest":
        return selftest()
    if cmd == "doctor":
        return doctor()
    if cmd == "test":
        return subprocess.run([GUI_PY, "-B", "-m", "unittest", "discover",
                               "-s", "tests", "-t", HERE, "-v", *sys.argv[2:]],
                              cwd=HERE, env=_source_env()).returncode

    if cmd not in SCRIPTS:
        print(f"未知命令：{cmd}\n可用：{', '.join(sorted(SCRIPTS))}, selftest, doctor")
        return 1

    if cmd in GUI_COMMANDS:
        interp = GUI_PY
        if not os.path.isfile(interp):
            print(f"找不到 GUI 解释器：{interp}")
            print("GUI 需要带 tkinter 的 Python（系统 3.14）。")
            print("可用环境变量 ECG_PPG_GUI_PY 指定其它解释器路径。")
            return 127
        _check_gui_deps(interp)
        env = _source_env()  # GUI 不用 sitex，走系统解释器自带的包
    elif cmd == "unicardio":
        from ecg_ppg.models.unicardio_client import BPWorkerClient
        interp = BPWorkerClient().python_path
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONPATH"] = SRC_ROOT
        env["PYTHONIOENCODING"] = "utf-8"
    else:
        interp = PY
        if not os.path.isfile(interp):
            print(f"找不到解释器：{interp}")
            print("请创建项目 .venv 并安装 requirements.txt，或检查 ECG_PPG_PY。")
            return 127
        env = _env()

    argv = [interp, "-B", "-m", SCRIPTS[cmd]] + sys.argv[2:]
    print(f"[run.py] {os.path.basename(interp)} -> {SCRIPTS[cmd]} {' '.join(sys.argv[2:])}".rstrip())
    return subprocess.run(argv, cwd=HERE, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
