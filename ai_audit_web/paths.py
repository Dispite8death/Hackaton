import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("HACKATON_QUICK_DIR", Path(__file__).resolve().parents[1] / "data" / "quick")).resolve()
