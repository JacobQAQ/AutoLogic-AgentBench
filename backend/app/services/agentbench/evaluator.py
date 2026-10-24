from __future__ import annotations

import json
import math
import re

from .tasks import DATA_DIR, get_task

FIELDS = {"fund_id":str,"date_range":str,"price_source_id":str,"price_start":(int,float),"price_end":(int,float),"news_claimed_close":(int,float,type(None)),"weekly_return_pct":(int,float),"fund_flow_status":str,"fund_flow_value":(int,float,type(None)),"evidence_refs":list,"conflict_detected":bool,"risk_notice":str,"conclusion":str}


def close_to(value: object, expected: float, tolerance: float = 0.01) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and abs(value - expected) <= tolerance


def evaluate(run: dict) -> dict:
    """Read private answers here only; neither the runner nor the tools import them."""
    truth = json.loads((DATA_DIR / "private" / "ground_truth.json").read_text(encoding="utf-8"))[run["task_id"]]
    task = get_task(run["task_id"])
    output = run.get("final_output")
    steps = run.get("steps", [])
    tool_results = [step for step in steps if step.get("action_type") == "tool_result" and step.get("status") == "ok"]
    used = {step.get("tool_name") for step in tool_results}
    evidence = {step["tool_result"]["evidence_id"] for step in tool_results if isinstance(step.get("tool_result"), dict) and step["tool_result"].get("evidence_id")}
    findings: list[dict] = []

    def check(condition: bool, category: str, field: str, message: str, step: dict | None = None) -> bool:
        if not condition:
            findings.append({"category":category,"field":field,"message":message,"step_index":step.get("step_index") if step else None})
        return condition

    valid_output = isinstance(output, dict)
    check(valid_output, "OUTPUT_ERROR", "final_output", "最终回答不是 JSON 对象")
    if not valid_output:
        output = {}
    for field, kind in FIELDS.items():
        value = output.get(field)
        correct = field in output and isinstance(value, kind) and not (field in {"price_start", "price_end", "news_claimed_close", "weekly_return_pct", "fund_flow_value"} and isinstance(value, bool))
        if field == "evidence_refs" and correct:
            correct = all(isinstance(ref, str) for ref in value)
        check(correct, "OUTPUT_ERROR", field, "缺少字段或字段类型错误")
    check(output.get("fund_id") == task["fund_id"] and output.get("date_range") == task["date_range"], "CONSTRAINT_ERROR", "fund_id/date_range", "基金或日期区间不符合任务")
    check(output.get("price_source_id") == truth["price_source_id"], "EVIDENCE_ERROR", "price_source_id", "价格未采用结构化行情数据库")
    check(close_to(output.get("price_start"), truth["price_start"]), "EVIDENCE_ERROR", "price_start", "周初价格与行情工具不符")
    check(close_to(output.get("price_end"), truth["price_end"]), "EVIDENCE_ERROR", "price_end", "周末价格与行情工具不符")
    claim = output.get("news_claimed_close")
    claim_ok = claim is None if truth["news_claimed_close"] is None else close_to(claim, truth["news_claimed_close"])
    check(claim_ok, "EVIDENCE_ERROR", "news_claimed_close", "新闻价格声明与新闻工具不符")
    return_value = output.get("weekly_return_pct")
    valid_return = close_to(return_value, truth["weekly_return_pct"])
    check(valid_return, "CALCULATION_ERROR", "weekly_return_pct", "周收益率与结构化价格不符")
    missing_tools = sorted(set(truth["required_tools"]) - used)
    check(not missing_tools, "TOOL_ERROR", "required_tools", f"未成功执行必要工具：{', '.join(missing_tools)}")
    tool_errors = [step for step in steps if step.get("action_type") == "tool_result" and step.get("status") == "error"]
    for step in tool_errors:
        check(False, "TOOL_ERROR", step.get("tool_name", "tool"), "工具参数或执行错误", step)
    price_steps = [step for step in tool_results if step.get("tool_name") == "get_price_data"]
    price_records = price_steps[-1].get("tool_result", {}).get("records", []) if price_steps else []
    prices = [record.get("close") for record in price_records] if isinstance(price_records, list) and all(isinstance(record, dict) for record in price_records) else []
    expected_operands = [prices[0], prices[-1]] if len(prices) >= 2 else None
    calc_steps = [step for step in tool_results if step.get("tool_name") == "calculate"]
    calc_ok = any(
        step.get("tool_result", {}).get("operation") == "percent_change"
        and expected_operands is not None
        and isinstance(step.get("tool_result", {}).get("operands"), list)
        and len(step["tool_result"]["operands"]) == 2
        and all(close_to(actual, expected) for actual, expected in zip(step["tool_result"]["operands"], expected_operands))
        and close_to(step["tool_result"].get("result"), truth["weekly_return_pct"])
        for step in calc_steps
    )
    check(calc_ok, "CALCULATION_ERROR", "calculate.operands", "calculate 的输入未对应实际价格记录或结果错误", calc_steps[-1] if calc_steps else None)
    refs = output.get("evidence_refs") if isinstance(output.get("evidence_refs"), list) else []
    invalid_refs = [ref for ref in refs if not isinstance(ref, str) or ref not in evidence]
    check(not invalid_refs, "EVIDENCE_ERROR", "evidence_refs", f"无效引用：{invalid_refs}")
    absent_refs = sorted(set(truth["required_evidence"]) - set(ref for ref in refs if isinstance(ref, str)))
    check(not absent_refs, "EVIDENCE_ERROR", "evidence_refs", f"缺少必要引用：{', '.join(absent_refs)}")
    # TASK-003 does not require flow, but an Agent may legitimately query it.
    optional_flow = run["task_id"] == "TASK-003" and "get_fund_flow" in used
    expected_flow_status = "available" if optional_flow else truth["fund_flow_status"]
    expected_flow_value = truth["fund_flow_value_if_queried"] if optional_flow else truth["fund_flow_value"]
    check(output.get("fund_flow_status") == expected_flow_status, "CONSTRAINT_ERROR", "fund_flow_status", "资金流状态不符合工具结果")
    flow_value = output.get("fund_flow_value")
    flow_ok = (flow_value is None if expected_flow_value is None else type(flow_value) in (int, float) and abs(flow_value - expected_flow_value) <= 0.01)
    check(flow_ok, "CONSTRAINT_ERROR", "fund_flow_value", "资金流数值缺失或被编造")
    check(output.get("conflict_detected") is truth["conflict_detected"], "EVIDENCE_ERROR", "conflict_detected", "来源冲突识别错误")
    if run["task_id"] == "TASK-002":
        note = f"{output.get('risk_notice', '')} {output.get('conclusion', '')}"
        check(any(word in note for word in ("缺失", "不可用", "未提供", "无数据")), "CONSTRAINT_ERROR", "risk_notice/conclusion", "未明确说明资金流不可用")
        fabricated_flow = re.search(r"(?:资金流|净流入|净流出)[^。；，,\n]{0,24}?[+-]?\d+(?:\.\d+)?\s*(?:万|亿|百万|million|元)", note, flags=re.IGNORECASE) or re.search(r"(?:资金流[^。；，,\n]{0,12}?(?:为|达到|约|净流入|净流出)|净流入|净流出)\s*[+-]?\d+(?:\.\d+)?\b", note)
        check(not fabricated_flow, "CONSTRAINT_ERROR", "risk_notice/conclusion", "文字中出现未经工具支持的资金流数值")
    if run["task_id"] == "TASK-003":
        note = f"{output.get('risk_notice', '')} {output.get('conclusion', '')}"
        check(any(word in note for word in ("冲突", "不一致", "矛盾", "差异")), "EVIDENCE_ERROR", "risk_notice/conclusion", "未说明新闻与数据库数值不一致")
        check(any(word in note for word in ("结构化", "数据库", "行情库")), "EVIDENCE_ERROR", "risk_notice/conclusion", "未说明价格采用结构化行情来源")
        check("4.36" in note and ("4.20" in note or "4.2" in note), "EVIDENCE_ERROR", "risk_notice/conclusion", "未在结论中明确比较新闻和行情库价格")
        preferred_news = re.search(r"以新闻[^。；，,\n]{0,12}为准|(?:采用|采信|依据|选择)新闻(?:价格|报价|数值)?|新闻[^。；，,\n]{0,12}(?:更可靠|是权威|已核实)", note)
        check(not preferred_news, "EVIDENCE_ERROR", "risk_notice/conclusion", "文字结论将未经核实的新闻价格作为权威来源")
        expected_point_gap = abs(truth["news_claimed_return_pct"] - truth["weekly_return_pct"])
        for match in re.finditer(r"([+-]?\d+(?:\.\d+)?)\s*个?百分点", note):
            check(close_to(float(match.group(1)), expected_point_gap, 0.2), "CALCULATION_ERROR", "risk_notice/conclusion.percentage_points", f"百分点差应约为 {expected_point_gap:g}，文字中为 {match.group(1)}")
    check(bool(str(output.get("conclusion") or "").strip()), "OUTPUT_ERROR", "conclusion", "结论为空")
    check(run.get("termination_reason") == "final_answer", "EXECUTION_ERROR", "termination_reason", "Agent 未正常完成")

    categories = {item["category"] for item in findings}
    scores = {
        "task_completion": 40 if valid_return and calc_ok and not ({"CALCULATION_ERROR", "CONSTRAINT_ERROR", "OUTPUT_ERROR", "EXECUTION_ERROR"} & categories) else (20 if valid_return else 0),
        "tool_calls": 20 if not missing_tools and not tool_errors else round(20 * len(set(truth["required_tools"]) & used) / len(truth["required_tools"])) if not tool_errors else 0,
        "evidence_reliability": 25 if "EVIDENCE_ERROR" not in categories else 0,
        "constraint_compliance": 15 if not ({"CONSTRAINT_ERROR", "OUTPUT_ERROR", "EXECUTION_ERROR", "TOOL_ERROR"} & categories) else 0,
    }
    return {"evaluation_version":"2.1","task_success":"PASS" if not findings else "FAIL", "diagnostic_score":sum(scores.values()), "scores":scores, "failure_categories":sorted(categories), "findings":findings, "key_evidence":[step["tool_result"] for step in tool_results if step.get("tool_result", {}).get("evidence_id") in truth["required_evidence"]]}
