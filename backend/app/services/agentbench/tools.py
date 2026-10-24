from __future__ import annotations

import copy
import json
import math
from typing import Any

from .tasks import DATA_DIR, get_task


TOOL_SCHEMAS = [
    {"type":"function","function":{"name":name,"description":description,"parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":False}}}
    for name, description, properties, required in [
        ("get_price_data", "Get fixed structured price records for F001.", {"fund_id":{"type":"string"},"date_range":{"type":"string"}}, ["fund_id","date_range"]),
        ("get_fund_flow", "Get fixed weekly fund flow or explicit missing status.", {"fund_id":{"type":"string"},"date_range":{"type":"string"}}, ["fund_id","date_range"]),
        ("get_market_news", "Get fixed news items for source verification.", {"fund_id":{"type":"string"},"date_range":{"type":"string"}}, ["fund_id","date_range"]),
        ("calculate", "Perform arithmetic; for weekly percentage use operation percent_change and operands [start, end].", {"operation":{"type":"string","enum":["percent_change","add","subtract","multiply","divide"]},"operands":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2}}, ["operation","operands"]),
    ]
]


def execute_tool(task_id: str, name: str, arguments: Any) -> dict:
    task = get_task(task_id)
    if task is None:
        raise ValueError("Unknown task")
    if name not in {schema["function"]["name"] for schema in TOOL_SCHEMAS}:
        raise ValueError("Unknown tool")
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object")
    if name == "calculate":
        if set(arguments) != {"operation", "operands"}:
            raise ValueError("calculate expects operation and operands")
        operation, operands = arguments["operation"], arguments["operands"]
        if operation not in {"percent_change", "add", "subtract", "multiply", "divide"}:
            raise ValueError("Unsupported operation")
        if not isinstance(operands, list) or len(operands) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in operands):
            raise ValueError("operands must be two finite numbers")
        a, b = operands
        if (operation == "percent_change" and a == 0) or (operation == "divide" and b == 0):
            raise ValueError("Division by zero")
        value = {"percent_change":lambda: (b-a)/a*100,"add":lambda:a+b,"subtract":lambda:a-b,"multiply":lambda:a*b,"divide":lambda:a/b}[operation]()
        return {"status":"ok","operation":operation,"operands":operands,"result":round(value, 8)}
    if set(arguments) != {"fund_id", "date_range"} or arguments["fund_id"] != task["fund_id"] or arguments["date_range"] != task["date_range"]:
        raise ValueError("fund_id/date_range must match the selected task")
    fixture = json.loads((DATA_DIR / "fixtures.json").read_text(encoding="utf-8"))
    kind = {"get_price_data":"price","get_fund_flow":"flow","get_market_news":"news"}[name]
    return copy.deepcopy({"fund_id":task["fund_id"],"date_range":task["date_range"],**fixture[task_id][kind]})
