"""Non-blocking GUI bridge to a separate, reusable model Python process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from ecg_ppg.paths import APP_ROOT, MODEL_DIR, OUTPUTS_DIR, SRC_ROOT

HERE = APP_ROOT
DEFAULT_MODEL_DIR = MODEL_DIR


class BPWorkerClient:
    def __init__(self, model_dir=None, python_path=None):
        self.model_dir = Path(model_dir or os.environ.get("ECG_PPG_MODEL_DIR", "") or DEFAULT_MODEL_DIR)
        candidates = [python_path, os.environ.get("ECG_PPG_MODEL_PY"),
                      self.model_dir.parent / "venv" / "Scripts" / "python.exe", sys.executable]
        self.python_path = str(next((p for p in candidates if p and Path(p).is_file()), sys.executable))
        self.events = queue.Queue()
        self.jobs = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self.ready = False
        self.busy = False
        self.process = None
        self.thread = None
        self.log_path = OUTPUTS_DIR / "bp_validation" / "worker.log"

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="UniCardio bridge")
        self.thread.start()

    def submit(self, request):
        if self.closed.is_set() or not self.ready or self.busy:
            return False
        self.busy = True
        try:
            self.jobs.put_nowait(request)
        except queue.Full:
            self.busy = False
            return False
        return True

    def _read(self, process):
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"模型进程已退出，日志：{self.log_path}")
        return json.loads(line)

    def _run(self):
        process = None
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            env["PYTHONPATH"] = str(SRC_ROOT)
            env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2")
            with self.log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [self.python_path, "-u", "-B", "-m", "ecg_ppg.models.unicardio_worker", str(self.model_dir)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding="utf-8", bufsize=1, cwd=HERE, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.process = process
                if self.closed.is_set():
                    return
                message = self._read(process)
                self.ready = message["type"] == "ready"
                self.events.put(message)
                if not self.ready:
                    return
                while not self.closed.is_set():
                    job = self.jobs.get()
                    if job is None:
                        break
                    process.stdin.write(json.dumps(job, allow_nan=False) + "\n")
                    process.stdin.flush()
                    message = self._read(process)
                    self.busy = False
                    self.events.put(message)
        except Exception as exc:
            if not self.closed.is_set():
                self.events.put({"type": "error", "message": str(exc)})
        finally:
            self.ready = self.busy = False
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()

    def close(self):
        self.closed.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
