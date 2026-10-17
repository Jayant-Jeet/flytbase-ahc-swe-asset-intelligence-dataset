"""Runtime configuration, all driven by environment variables (.env is loaded if present)."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent


def _split_paths(value: str) -> list[Path]:
    return [Path(p.strip()) for p in value.split(os.pathsep) if p.strip()]


def data_dirs() -> list[Path]:
    """One or more dataset folders. Separate several with os.pathsep (';' on Windows, ':' elsewhere)."""
    return _split_paths(os.getenv("DATA_DIR", str(ROOT / "sample-data")))


def db_path() -> Path:
    return Path(os.getenv("DB_PATH", str(ROOT / "data" / "asset.db")))


def rules_path() -> Path:
    return Path(os.getenv("RULES_PATH", str(ROOT / "rules" / "rules.json")))


def today() -> date:
    return date.fromisoformat(os.getenv("TODAY", "2026-10-17"))


# Each weekly inspection that finds the well not producing counts as this many days of downtime.
DAYS_PER_INSPECTION = int(os.getenv("DAYS_PER_INSPECTION", "7"))
