#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
launch_gui.py — ECG/PPG 上位机启动器（比 .bat 可靠）
====================================================
双击本文件即可启动图形界面。之所以不用 .bat，是因为：

  1. .bat 必须用 CRLF 换行，写成 LF 后 cmd.exe 会解析失败（静默闪退）；
  2. .bat 里写非 ASCII 路径容易因代码页问题乱码；
  3. 出错时 .bat 窗口一闪就没了，看不到原因。

本脚本用系统 Python 解释器自己启动自己，出错会把信息留在窗口里。

用法：
    双击          -> 启动 GUI
    带 --doctor   -> 只做环境体检，不开界面
    带 --console  -> 保留控制台窗口（调试用）
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.join(HERE, "src")
GUI_MODULE = "ecg_ppg.gui.serial_ecg_ppg_gui"
GUI_SCRIPT = os.path.join(SRC_ROOT, "ecg_ppg", "gui", "serial_ecg_ppg_gui.py")

# GUI 必须有 tkinter 的解释器。按优先级找。
CANDIDATES = [
    # 1. 系统 Python（带 tkinter，本机 3.14）
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Programs", "Python", "Python314", "pythonw.exe",
    ),
    # 2. 用户显式指定
    os.environ.get("ECG_PPG_GUI_PY", ""),
    # 3. py 启动器兜底（下面单独处理）
]

NEEDED = ("tkinter", "serial", "numpy", "scipy", "matplotlib", "bleak")
PIP_NAME = {"serial": "pyserial"}          # import 名 ≠ 包名


def _probe(interp: str) -> tuple[bool, str]:
    """检查解释器是否满足 GUI 依赖。返回 (是否齐全, 缺失说明)。"""
    code = (
        "import importlib.util, sys\n"
        f"miss=[m for m in {NEEDED!r}\n"
        "      if importlib.util.find_spec(m) is None]\n"
        "print(' '.join(miss))\n"
    )
    try:
        r = subprocess.run([interp, "-c", code], capture_output=True,
                           text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return False, f"无法调用 {interp}：{exc}"
    miss = (r.stdout or "").strip()
    if r.returncode != 0:
        return False, (r.stderr or "解释器依赖检测失败").strip()
    return (not miss), miss


def _find_interpreter() -> str | None:
    """找一个带 tkinter 的 python.exe（不是 pythonw，便于报错可见）。"""
    for candidate in (os.environ.get("ECG_PPG_GUI_PY", ""),
                      os.path.join(HERE, ".venv", "Scripts", "python.exe"),
                      os.path.join(HERE, ".venv", "bin", "python")):
        if candidate and os.path.isfile(candidate) and _probe(candidate)[0]:
            return candidate
    # 优先系统 3.14 的 python.exe（非 pythonw），前台运行才能看到错误
    sys_py = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Programs", "Python", "Python314", "python.exe",
    )
    if os.path.isfile(sys_py):
        ok, _ = _probe(sys_py)
        if ok:
            return sys_py

    # 退而求其次：谁有 tkinter 用谁
    for cand in (sys.executable, os.environ.get("ECG_PPG_GUI_PY", "")):
        if cand and os.path.isfile(cand):
            ok, _ = _probe(cand)
            if ok:
                return cand

    # 再退：py 启动器报出来的路径
    for ver in ("3.14", "3.13", "3.12", "3.11", "3.10"):
        try:
            r = subprocess.run(["py", f"-{ver}", "-c", "import sys;print(sys.executable)"],
                               capture_output=True, text=True, timeout=30)
            path = (r.stdout or "").strip()
            if path and os.path.isfile(path):
                ok, _ = _probe(path)
                if ok:
                    return path
        except Exception:  # noqa: BLE001
            continue
    return None


def _pause_if_console() -> None:
    """双击运行时，出错后别让窗口立刻消失。"""
    if sys.stdout is None or not sys.stdout.isatty():
        return
    try:
        input("\n按回车键关闭...")
    except EOFError:
        pass


def doctor() -> int:
    print("=" * 58)
    print("ECG/PPG 上位机 —— 环境体检")
    print("=" * 58)
    print(f"启动器所在目录 : {HERE}")
    print(f"GUI 脚本       : {'存在' if os.path.isfile(GUI_SCRIPT) else '★ 缺失'}")
    print(f"当前解释器     : {sys.executable}  (Python {sys.version.split()[0]})")
    print()

    interp = _find_interpreter()
    if interp is None:
        print("★ 没有找到带 tkinter 且依赖齐全的 Python 解释器。")
        print("  已检查的解释器：")
        for c in CANDIDATES:
            if c:
                print(f"    - {c}  {'存在' if os.path.isfile(c) else '不存在'}")
        return 1

    print(f"选定 GUI 解释器 : {interp}")
    print("-" * 58)
    code = (
        "import importlib.util, sys\n"
        "print('  Python', sys.version.split()[0])\n"
        f"for m in {NEEDED!r}:\n"
        "    s=importlib.util.find_spec(m)\n"
        "    if s is None:\n"
        "        print(f'  {m:11s} 缺失')\n"
        "    else:\n"
        "        try:\n"
        "            mod=__import__(m)\n"
        "            print(f'  {m:11s} OK  {getattr(mod, chr(95)*2+chr(118)+chr(101)+chr(114)+chr(115)+chr(105)+chr(111)+chr(110)+chr(95)*2,\"\")}')\n"
        "        except Exception as e:\n"
        "            print(f'  {m:11s} 导入失败: {e}')\n"
    )
    subprocess.run([interp, "-c", code])
    print("-" * 58)
    print("一切正常。" if os.path.isfile(GUI_SCRIPT) else "GUI 脚本缺失，无法启动。")
    return 0


def install_deps(interp: str, miss: str) -> bool:
    """缺库就地安装。"""
    pkgs = [PIP_NAME.get(m, m) for m in miss.split() if m != "tkinter"]
    if not pkgs:
        return False
    print(f"[启动器] 缺少 {', '.join(miss.split())}，正在安装：{' '.join(pkgs)}")
    r = subprocess.run([interp, "-m", "pip", "install", "--no-cache-dir", *pkgs])
    return r.returncode == 0


def main() -> int:
    args = set(sys.argv[1:])

    if "--doctor" in args:
        rc = doctor()
        _pause_if_console()
        return rc

    if not os.path.isfile(GUI_SCRIPT):
        print(f"★ 找不到 GUI 脚本：{GUI_SCRIPT}")
        print("请确认 src/ecg_ppg/gui/serial_ecg_ppg_gui.py 存在。")
        _pause_if_console()
        return 1

    interp = _find_interpreter()
    if interp is None:
        print("★ 没有找到带 tkinter 的 Python 解释器，GUI 无法启动。")
        print()
        print("解决办法（任一）：")
        print("  1) 安装官方 Python 3.x（勾选 tcl/tk 与 Add to PATH），然后：")
        print("     <你的python> -m pip install -r requirements.txt")
        print("  2) 或在本机已有 Python 上补装依赖后，指定：")
        print('     set ECG_PPG_GUI_PY=<python解释器完整路径>')
        print("     再双击本文件。")
        print()
        print("运行诊断：python launch_gui.py --doctor")
        _pause_if_console()
        return 1

    ok, miss = _probe(interp)
    if not ok:
        print(f"[启动器] 解释器 {interp}")
        print(f"[启动器] 缺少：{miss}")
        if "tkinter" in miss:
            print()
            print("★ 该解释器没有 tkinter，图形界面无法运行。")
            print("  请换用带 tcl/tk 的官方 Python，或设置 ECG_PPG_GUI_PY 指定。")
            _pause_if_console()
            return 1
        if not install_deps(interp, miss):
            print("★ 自动安装失败，请手动执行：")
            print(f'   "{interp}" -m pip install ' +
                  " ".join(PIP_NAME.get(m, m) for m in miss.split()))
            _pause_if_console()
            return 1

    # 启动 GUI（同进程前台运行，异常可捕获并打印）
    print(f"[启动器] 使用解释器：{interp}")
    print(f"[启动器] 启动 {os.path.basename(GUI_SCRIPT)} ...")
    try:
        gui_args = [arg for arg in sys.argv[1:] if arg != "--console"]
        env = dict(os.environ)
        old = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = SRC_ROOT + (os.pathsep + old if old else "")
        r = subprocess.run([interp, "-B", "-m", GUI_MODULE, *gui_args], cwd=HERE, env=env)
        return r.returncode
    except Exception:  # noqa: BLE001
        print("★ 启动失败：")
        traceback.print_exc()
        _pause_if_console()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
