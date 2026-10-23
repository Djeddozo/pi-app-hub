import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("PI_HUB_DATA", str(ROOT / "data")))
MANIFESTS = DATA / "manifests"
ORIGIN = os.environ.get("PI_HUB_ORIGIN", "http://127.0.0.1:8840").rstrip("/")
HELPER = "/usr/local/sbin/pi-app-hub-service"
