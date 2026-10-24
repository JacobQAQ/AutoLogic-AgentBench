from __future__ import annotations

import json
import os
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import BACKEND_ROOT

from .evaluator import evaluate
from .tasks import get_task
from .tools import TOOL_SCHEMAS, execute_tool
from .trace import new_run_id, now, save_run

MODEL_ID = os.environ.get("AGENTBENCH_MODEL", "deepseek-flash").strip()
MODEL_IDENTITY = "DeepSeek-V4.1-Flash-1017"
BASE_URL = "https://api.deepseek.com"
MAX_DECISIONS = 8
TIMEOUT_SECONDS = 60


def api_key() -> str:
    process = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if process:
        return process
    path = BACKEND_ROOT / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    return ""


def call_deepseek(payload: dict, key: str) -> dict:
    request = Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"DeepSeek HTTP {exc.code}") from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"DeepSeek network error: {type(exc).__name__}") from None


def run_agent(task_id: str, *, model_call: Callable[[dict, str], dict] = call_deepseek, key: str | None = None, max_decisions: int = MAX_DECISIONS) -> dict:
    if MODEL_ID != "deepseek-flash":
        raise ValueError("AGENTBENCH_MODEL must be deepseek-flash in V1")
    task = get_task(task_id)
    if task is None:
        raise ValueError("Unknown task_id")
    secret = api_key() if key is None else key
    if not secret:
        raise ValueError("DEEPSEEK_API_KEY is missing. Set the process variable or backend/.env.")
    run = {"run_id":new_run_id(),"task_id":task_id,"model_id":MODEL_ID,"model_identity":MODEL_IDENTITY,"fixture_version":"2.0","created_at":now(),"steps":[],"final_output":None,"raw_final_answer":None,"termination_reason":None}
    messages = [
        {"role":"system","content":"你是金融数据核验 Agent。必须自主调用工具取得数据，并用 calculate 对 get_price_data 返回的周初、周末收盘价计算周收益率。不能臆造数据。最终只输出一个合法 JSON 对象，包含 fund_id、date_range、price_source_id（价格计算采用的 source_id）、price_start（周初收盘价数字）、price_end（周末收盘价数字）、news_claimed_close（新闻声称的周末价格；未见数字则 null）、weekly_return_pct（数字）、fund_flow_status（available/missing/not_requested）、fund_flow_value（数字或 null）、evidence_refs（工具返回的 evidence_id 数组）、conflict_detected（布尔值）、risk_notice（字符串）、conclusion（字符串）。只引用实际工具结果。价格以结构化行情数据库为准。遇到新闻价格冲突，应列出两方数值和来源，不把新闻值当成已核实价格；资金流缺失时不得在任何文字中声称具体数值。"},
        {"role":"user","content":f"任务：{task['goal']}\n基金：{task['fund_id']}\n日期区间：{task['date_range']}\n规则：{task['rule']}"},
    ]

    def add(action_type: str, status: str, latency_ms: int = 0, **fields) -> dict:
        step = {"task_id":task_id,"step_index":len(run["steps"])+1,"action_type":action_type,"tool_name":None,"tool_arguments":None,"tool_result":None,"status":status,"timestamp":now(),"latency_ms":latency_ms,"model_id":MODEL_ID,**fields}
        run["steps"].append(step)
        return step

    for decision in range(max_decisions):
        started = time.monotonic()
        try:
            response = model_call({"model":MODEL_ID,"messages":messages,"tools":TOOL_SCHEMAS,"tool_choice":"auto","thinking":{"type":"disabled"},"max_tokens":1200}, secret)
            message = response["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("Invalid model message")
            if not isinstance(message.get("tool_calls") or [], list) or any(not isinstance(call, dict) for call in message.get("tool_calls") or []):
                raise ValueError("Invalid model tool_calls")
        except Exception as exc:  # API failures are terminal and never replaced by a canned result.
            error = str(exc).replace(secret, "[REDACTED]")
            add("model_decision", "error", round((time.monotonic()-started)*1000), tool_result={"error":error})
            run["termination_reason"] = "api_error"
            break
        latency = round((time.monotonic()-started)*1000)
        calls = message.get("tool_calls") or []
        add("model_decision", "ok", latency, tool_result={"decision":decision+1,"tool_calls":[call.get("function", {}).get("name") for call in calls],"has_final_content":bool(message.get("content"))})
        if calls:
            messages.append({"role":"assistant","content":message.get("content"),"tool_calls":calls})
            for call in calls:
                name = call.get("function", {}).get("name", "")
                raw_arguments = call.get("function", {}).get("arguments", "{}")
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except (ValueError, TypeError):
                    arguments = {"_invalid_json":True}
                add("tool_call", "requested", tool_name=name, tool_arguments=arguments)
                tool_started = time.monotonic()
                try:
                    result = execute_tool(task_id, name, arguments)
                    status = "ok"
                except (ValueError, KeyError, TypeError) as exc:
                    result = {"status":"error","error":str(exc)}
                    status = "error"
                add("tool_result", status, round((time.monotonic()-tool_started)*1000), tool_name=name, tool_arguments=arguments, tool_result=result)
                messages.append({"role":"tool","tool_call_id":call.get("id", ""),"content":json.dumps(result, ensure_ascii=False)})
            continue
        content = message.get("content") or ""
        run["raw_final_answer"] = str(content).replace(secret, "[REDACTED]")
        try:
            parsed = json.loads(content)
            run["final_output"] = parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            run["final_output"] = None
        add("final_answer", "ok" if run["final_output"] is not None else "error", tool_result={"content":run["raw_final_answer"]})
        run["termination_reason"] = "final_answer"
        break
    if run["termination_reason"] is None:
        run["termination_reason"] = "max_steps"
        add("termination", "error", tool_result={"reason":"Maximum model decisions reached"})
    run["evaluation"] = evaluate(run)
    save_run(run)
    return run
