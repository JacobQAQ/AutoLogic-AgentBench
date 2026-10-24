from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "agentbench"


def public_tasks() -> list[dict]:
    return json.loads((DATA_DIR / "tasks.json").read_text(encoding="utf-8"))


def get_task(task_id: str) -> dict | None:
    return next((task for task in public_tasks() if task["task_id"] == task_id), None)
