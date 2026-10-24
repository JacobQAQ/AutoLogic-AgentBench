from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from unittest.mock import patch

from app.services.agentbench.evaluator import evaluate
from app.services.agentbench.runner import MAX_DECISIONS, run_agent
from app.services.agentbench.tools import execute_tool
from app.main import ApiHandler


def mock_run(task_id: str) -> dict:
    names = {"TASK-001":["get_price_data","get_fund_flow","get_market_news","calculate"],"TASK-002":["get_price_data","get_fund_flow","calculate"],"TASK-003":["get_market_news","get_price_data","calculate"]}[task_id]
    evidence = {"TASK-001":["P-001","F-001","N-001"],"TASK-002":["P-002","F-002"],"TASK-003":["P-003","N-003"]}[task_id]
    output = {"fund_id":"F001","date_range":"2026-10-19/2026-10-23","price_source_id":"structured_price_db","price_start":4.0,"price_end":4.2,"news_claimed_close":4.36 if task_id == "TASK-003" else None,"weekly_return_pct":5.0,"fund_flow_status":{"TASK-001":"available","TASK-002":"missing","TASK-003":"not_requested"}[task_id],"fund_flow_value":12.5 if task_id == "TASK-001" else None,"evidence_refs":evidence,"conflict_detected":task_id == "TASK-003","risk_notice":"资金流数据缺失" if task_id == "TASK-002" else "新闻 4.36 元与结构化行情数据库 4.20 元不一致，采用数据库数值。" if task_id == "TASK-003" else "仅基于固定历史数据","conclusion":"结构化行情数据库记录 F001 由 4.00 升至 4.20，周收益率 5%。新闻与数据库有冲突。" if task_id == "TASK-003" else "F001 本周上涨 5%；资金流缺失。" if task_id == "TASK-002" else "F001 本周上涨 5%，资金流净流入。"}
    queue = []
    for index, name in enumerate(names):
        args = {"operation":"percent_change","operands":[4,4.2]} if name == "calculate" else {"fund_id":"F001","date_range":"2026-10-19/2026-10-23"}
        queue.append({"choices":[{"message":{"role":"assistant","content":None,"tool_calls":[{"id":f"call-{index}","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]}}]})
    queue.append({"choices":[{"message":{"role":"assistant","content":json.dumps(output, ensure_ascii=False)}}]})
    requests = []

    def fake_call(payload, key):
        requests.append(copy.deepcopy(payload))
        return queue.pop(0)

    with patch("app.services.agentbench.runner.save_run"):
        run = run_agent(task_id, model_call=fake_call, key="test-secret")
    run["_requests"] = requests
    return run


class AgentBenchTests(unittest.TestCase):
    def test_valid_runs_and_real_tool_sequence(self):
        for task_id in ("TASK-001", "TASK-002", "TASK-003"):
            with self.subTest(task_id=task_id):
                run = mock_run(task_id)
                self.assertEqual(run["evaluation"]["task_success"], "PASS")
                self.assertEqual(run["evaluation"]["diagnostic_score"], 100)
                self.assertEqual(run["termination_reason"], "final_answer")
                self.assertGreaterEqual(len(run["_requests"]), 4)
                self.assertEqual(run["_requests"][0]["thinking"], {"type":"disabled"})
                self.assertNotIn("test-secret", json.dumps(run, ensure_ascii=False))

    def test_wrong_return_fails(self):
        run = mock_run("TASK-001")
        run["final_output"]["weekly_return_pct"] = 9
        self.assertIn("CALCULATION_ERROR", evaluate(run)["failure_categories"])

    def test_missing_flow_cannot_be_invented(self):
        run = mock_run("TASK-002")
        run["final_output"]["fund_flow_value"] = 10
        self.assertEqual(evaluate(run)["task_success"], "FAIL")

    def test_conflict_must_be_detected(self):
        run = mock_run("TASK-003")
        run["final_output"]["conflict_detected"] = False
        self.assertIn("EVIDENCE_ERROR", evaluate(run)["failure_categories"])

    def test_conflict_requires_both_prices_and_correct_source(self):
        run = mock_run("TASK-003")
        run["final_output"]["price_source_id"] = "market_news"
        run["final_output"]["news_claimed_close"] = 4.2
        run["final_output"]["risk_notice"] = "新闻和数据库有冲突。"
        findings = evaluate(run)["findings"]
        self.assertTrue({"price_source_id", "news_claimed_close", "risk_notice/conclusion"} <= {item["field"] for item in findings})

    def test_correct_result_from_unrelated_calculation_fails(self):
        run = mock_run("TASK-001")
        calc_step = next(step for step in run["steps"] if step["action_type"] == "tool_result" and step["tool_name"] == "calculate")
        calc_step["tool_result"]["operands"] = [100, 105]
        result = evaluate(run)
        self.assertIn("CALCULATION_ERROR", result["failure_categories"])
        self.assertLess(result["diagnostic_score"], 100)

    def test_missing_flow_value_in_prose_fails(self):
        run = mock_run("TASK-002")
        run["final_output"]["conclusion"] = "资金流缺失，但资金流净流入 12.5。"
        self.assertIn("CONSTRAINT_ERROR", evaluate(run)["failure_categories"])

    def test_conflict_text_cannot_choose_news_as_authority(self):
        run = mock_run("TASK-003")
        run["final_output"]["conclusion"] = "数据库为 4.20，新闻为 4.36，但应以新闻 4.36 为准。"
        self.assertIn("EVIDENCE_ERROR", evaluate(run)["failure_categories"])

    def test_wrong_percentage_point_claim_fails(self):
        run = mock_run("TASK-003")
        run["final_output"]["conclusion"] += "两者相差约 3.8 个百分点。"
        evaluation = evaluate(run)
        self.assertEqual(evaluation["task_success"], "FAIL")
        self.assertLess(evaluation["diagnostic_score"], 100)
        self.assertIn("CALCULATION_ERROR", evaluation["failure_categories"])

    def test_invalid_evidence_is_rejected(self):
        run = mock_run("TASK-001")
        run["final_output"]["evidence_refs"].append("FAKE-999")
        self.assertIn("EVIDENCE_ERROR", evaluate(run)["failure_categories"])

    def test_missing_or_wrong_type_output_field_is_rejected(self):
        run = mock_run("TASK-001")
        del run["final_output"]["risk_notice"]
        run["final_output"]["weekly_return_pct"] = "5.0"
        result = evaluate(run)
        self.assertEqual(result["task_success"], "FAIL")
        self.assertIn("OUTPUT_ERROR", result["failure_categories"])

    def test_invalid_tool_args_logged(self):
        responses = [
            {"choices":[{"message":{"role":"assistant","content":None,"tool_calls":[{"id":"1","type":"function","function":{"name":"get_price_data","arguments":"{\"fund_id\":\"WRONG\",\"date_range\":\"x\"}"}}]}}]},
            {"choices":[{"message":{"role":"assistant","content":"{}"}}]},
        ]
        with patch("app.services.agentbench.runner.save_run"):
            run = run_agent("TASK-001", model_call=lambda *_: responses.pop(0), key="test-secret")
        self.assertTrue(any(step["status"] == "error" and step["action_type"] == "tool_result" for step in run["steps"]))
        self.assertIn("TOOL_ERROR", run["evaluation"]["failure_categories"])

    def test_max_steps_and_api_failure(self):
        tool_response = {"choices":[{"message":{"role":"assistant","content":None,"tool_calls":[{"id":"1","type":"function","function":{"name":"get_price_data","arguments":json.dumps({"fund_id":"F001","date_range":"2026-10-19/2026-10-23"})}}]}}]}
        with patch("app.services.agentbench.runner.save_run"):
            run = run_agent("TASK-001", model_call=lambda *_: tool_response, key="test-secret", max_decisions=MAX_DECISIONS)
        self.assertEqual(run["termination_reason"], "max_steps")
        self.assertIsNone(run["final_output"])
        with patch("app.services.agentbench.runner.save_run"):
            failed = run_agent("TASK-002", model_call=lambda *_: (_ for _ in ()).throw(RuntimeError("test-secret failed")), key="test-secret")
        self.assertEqual(failed["termination_reason"], "api_error")
        self.assertEqual(failed["evaluation"]["task_success"], "FAIL")
        self.assertNotIn("test-secret", json.dumps(failed))

    def test_tool_validation_and_fixture(self):
        self.assertEqual(execute_tool("TASK-001", "calculate", {"operation":"percent_change","operands":[4,4.2]})["result"], 5)
        self.assertEqual(execute_tool("TASK-002", "get_fund_flow", {"fund_id":"F001","date_range":"2026-10-19/2026-10-23"})["status"], "missing")
        for args in ({"operation":"eval","operands":[4,4.2]},{"operation":"divide","operands":[1,0]}):
            with self.assertRaises(ValueError):
                execute_tool("TASK-001", "calculate", args)

    def test_http_routes_accept_all_three_tasks_without_model_charges(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/v1/agentbench"
        requested = []
        try:
            with patch("app.main.agentbench_api_key", return_value="test-secret"), patch("app.main.run_agent", side_effect=lambda task_id: requested.append(task_id) or {"run_id":"mock","task_id":task_id}):
                with urlopen(f"{base}/tasks") as response:
                    tasks = json.load(response)
                self.assertEqual(len(tasks["tasks"]), 3)
                self.assertNotIn("test-secret", json.dumps(tasks))
                for task_id in ("TASK-001", "TASK-002", "TASK-003"):
                    request = Request(f"{base}/run", data=json.dumps({"task_id":task_id}).encode(), headers={"Content-Type":"application/json"}, method="POST")
                    with urlopen(request) as response:
                        self.assertEqual(json.load(response)["task_id"], task_id)
                self.assertEqual(requested, ["TASK-001", "TASK-002", "TASK-003"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
