"""Persistent JSON-lines model worker. stdout is reserved for protocol messages."""
from __future__ import annotations

import contextlib
import json
import sys
import traceback


def send(message):
    print(json.dumps(message, ensure_ascii=False, allow_nan=False), flush=True)


def main():
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from ecg_ppg.models.unicardio_inference import BPModel, SignalQualityError, read_prediction_csv
            from ecg_ppg.acquisition.ecg_ppg_protocol import ecg_adc_to_mv
            import numpy as np
            model = BPModel(sys.argv[1])
        send({"type": "ready", "device": str(model.device)})
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        send({"type": "error", "message": f"模型加载失败：{exc}"})
        return 1

    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if request.get("type") == "close":
                break
            with contextlib.redirect_stdout(sys.stderr):
                if request["type"] == "csv":
                    args = read_prediction_csv(request["path"])
                else:
                    adc = np.asarray(request["ecg_adc"], dtype=float)
                    args = (request["ppg_t"], request["ppg_v"], request["ecg_t"], ecg_adc_to_mv(adc), adc)
                result = model.predict(*args)
            send({"type": "result", "id": request.get("id"), "result": result})
        except SignalQualityError as exc:
            send({"type": "quality", "id": request.get("id"), "message": str(exc)})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            send({"type": "prediction_error", "id": request.get("id"), "message": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
