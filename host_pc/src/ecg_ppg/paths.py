"""Project-relative paths shared by launchers, tools and model workers."""
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = APP_ROOT / "src"
RECORDS_DIR = APP_ROOT / "records"
OUTPUTS_DIR = APP_ROOT / "outputs"
MODEL_DIR = APP_ROOT.parent / "UniCardio" / "UniCardio" / "UniCardio-main" / "base_model"
