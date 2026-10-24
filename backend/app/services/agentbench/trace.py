from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .tasks import DATA_DIR

RUN_DIR = DATA_DIR / "runs"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return str(uuid.uuid4())


def save_run(run: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    target = RUN_DIR / f"{run['run_id']}.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)


def load_run(run_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", run_id):
        return None
    target = RUN_DIR / f"{run_id}.json"
    return json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
