import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
path = Path(os.getenv("DB_PATH", "data/bot.sqlite3")).parent / "scheduler-heartbeat"
try:
    age = time.time() - float(path.read_text(encoding="ascii"))
    raise SystemExit(0 if 0 <= age < 180 else 1)
except (OSError, ValueError):
    raise SystemExit(1) from None
