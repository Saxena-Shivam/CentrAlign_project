from __future__ import annotations

import json
import sqlite3
from threading import Thread

import pytest

from centralign_agent import loop as agent_loop
from centralign_agent.loop import AgentLoop, _goal_requests_vendors, _highest_amount_action
from centralign_agent.model_adapter import MockModelAdapter, OpenAIModelAdapter, ToolAction
from centralign_agent.planner import Planner
from centralign_agent.tools.browser import BrowserToolSet
from company_app.app import create_app
from werkzeug.serving import make_server
from centralign_agent.verifier import Verifier


def test_plan_creation():
    planner = Planner(MockModelAdapter())
    plan = planner.create_plan("Log in and add a payable for Acme")
    assert plan.brief_plan
    assert plan.success_criteria


def test_report_goal_does_not_become_vendors_goal():
    goal = (
        "Log in, inspect all six payable records, write a Markdown report containing "
        "every invoice number, vendor, amount, and due date. Do not change financial records."
    )
    assert _goal_requests_vendors(goal) is False


def test_highest_amount_action_reports_all_tied_invoices():
    action = _highest_amount_action(
        {
            "url": "http://127.0.0.1:3000/payables",
            "tables": [
                {
                    "headers": ["Vendor", "Invoice number", "Amount"],
                    "rows": [
                        ["Acme", "INV-1", "$100.00"],
                        ["Fabrikam", "INV-2", "$640.25"],
                        ["Fabrikam", "INV-3", "$640.25"],
                    ],
                }
            ],
        },
        "Identify the highest-amount invoice and list every invoice tied for the highest amount.",
        {"highest_amount_inspected": True},
    )
    assert action is not None
    assert action.tool == "finish"
    assert "INV-2" in action.arguments["message"]
    assert "INV-3" in action.arguments["message"]
    assert "tie" in action.arguments["message"].lower()


def test_highest_amount_uses_existing_table_without_repeating_read():
    action = _highest_amount_action(
        {
            "url": "http://127.0.0.1:3000/payables",
            "tables": [
                {
                    "headers": ["Invoice number", "Amount"],
                    "rows": [["INV-1", "$100.00"], ["INV-2", "$200.00"]],
                }
            ],
        },
        "Identify the highest amount invoice and save a report.",
        {},
    )
    assert action is not None
    assert action.tool == "finish"


def test_smallest_amount_action_reports_invoice_details():
    action = _highest_amount_action(
        {
            "url": "http://127.0.0.1:3000/payables",
            "tables": [
                {
                    "headers": ["Vendor", "Invoice number", "Amount", "Due date"],
                    "rows": [
                        ["Acme", "INV-1", "$100.00", "2026-10-01"],
                        ["Contoso", "INV-2", "$25.50", "2026-10-02"],
                        ["Fabrikam", "INV-3", "$25.50", "2026-10-03"],
                    ],
                }
            ],
        },
        "Identify the invoice with the smallest amount and report its invoice ID, vendor, amount, and due date.",
        {},
    )
    assert action is not None
    assert action.tool == "finish"
    assert "INV-2" in action.arguments["message"]
    assert "INV-3" in action.arguments["message"]
    assert "$25.50" in action.arguments["message"]


def test_latest_due_date_action_reports_all_ties_when_requested():
    from centralign_agent.loop import _latest_invoice_action

    action = _latest_invoice_action(
        {
            "url": "http://127.0.0.1:3000/payables",
            "tables": [
                {
                    "headers": ["Invoice number", "Amount", "Due date"],
                    "rows": [
                        ["INV-1", "$100.00", "2026-12-01"],
                        ["INV-2", "$200.00", "2026-12-22"],
                        ["INV-3", "$300.00", "2026-12-22"],
                    ],
                }
            ],
        },
        "Find all invoices sharing the latest due date.",
        {"latest_invoice_inspected": True},
    )
    assert action is not None
    assert action.tool == "finish"
    assert "INV-2" in action.arguments["message"]
    assert "INV-3" in action.arguments["message"]


def test_report_verifier_accepts_relative_path_from_run_directory(monkeypatch, tmp_path):
    report = tmp_path / "runs" / "run-1" / "task_report.md"
    report.parent.mkdir(parents=True)
    report.write_text("verified", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    ok, message = Verifier().verify(
        "Write a Markdown report and verify it.",
        {
            "run_dir": "runs/run-1",
            "memory": {
                "task_report_path": str(report.relative_to(tmp_path)),
                "task_report_verified": True,
                "task_report_read_back": True,
            },
            "evidence": ["report verified"],
        },
    )
    assert ok is True
    assert "complete" in message.lower()


def test_agent_loop_stops_when_human_input_is_required(monkeypatch, tmp_path):
    class PausingModel(MockModelAdapter):
        def decide(self, goal, recent_observation, plan, available_tools, memory):
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": "Test run paused for user input."},
                rationale="The test verifies the visible pause status.",
            )

    monkeypatch.setattr(agent_loop, "get_model_adapter", PausingModel)
    loop = AgentLoop(tmp_path)
    result = loop.run("Review the company dashboard and request human confirmation.")
    assert result.status == "waiting_for_human"


def test_hosted_model_repeating_unchanged_browser_action_is_stopped(monkeypatch, tmp_path):
    class RepeatingModel:
        def plan(self, goal, history=None):
            return MockModelAdapter().plan(goal, history)

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            return ToolAction(
                tool="click",
                arguments={"selector": "#missing-control"},
                rationale="Try the same unavailable control.",
            )

    db_path = tmp_path / "stalled_action.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTO_APPROVE", "true")
    monkeypatch.setattr(agent_loop, "get_model_adapter", RepeatingModel)
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "runs").run("Review the company dashboard.")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "waiting_for_human"
    repeated_clicks = [
        step for step in result.steps
        if step["action"]["tool"] == "click"
        and step["action"]["arguments"].get("selector") == "#missing-control"
    ]
    assert len(repeated_clicks) == 1
    assert "page did not change" in result.final_message.lower()


def test_mock_model_advances_from_login_observations():
    model = MockModelAdapter()
    tools = ["navigate", "fill", "submit_form", "read_page", "finish", "request_human_input"]

    action = model.decide("Login and inspect payables", "No observations yet.", {}, tools, {})
    assert action.tool == "navigate"

    login_page = {
        "status": "ok",
        "url": "http://127.0.0.1:3000/login",
        "text": "CentrAlign Mock Company",
        "interactive_elements": [
            {"name": "username", "value": ""},
            {"name": "password", "value": ""},
        ],
    }
    action = model.decide("Login and inspect payables", json.dumps(login_page), {}, tools, {})
    assert action.tool == "fill"
    assert action.arguments["selector"] == '[name="username"]'

    login_page["interactive_elements"][0]["value"] = "demo"
    action = model.decide("Login and inspect payables", json.dumps(login_page), {}, tools, {})
    assert action.tool == "fill"
    assert action.arguments["selector"] == '[name="password"]'

    login_page["interactive_elements"][1]["value"] = "demo"
    action = model.decide("Login and inspect payables", json.dumps(login_page), {}, tools, {})
    assert action.tool == "submit_form"

    dashboard = {
        "status": "ok",
        "url": "http://127.0.0.1:3000/payables",
        "text": "Payables Dashboard\nPayables table\nAcme INV-1042",
        "interactive_elements": [],
    }
    action = model.decide("Login and inspect payables", json.dumps(dashboard), {}, tools, {})
    assert action.tool == "read_page"
    action = model.decide(
        "Login and inspect payables",
        json.dumps(dashboard),
        {},
        tools,
        {"payables_inspected": True},
    )
    assert action.tool == "finish"


def test_mock_model_pauses_instead_of_repeating_failed_action():
    model = MockModelAdapter()
    action = model.decide(
        "Login and inspect payables",
        '{"status":"error","message":"Connection refused"}',
        {},
        ["navigate", "request_human_input"],
        {},
    )
    assert action.tool == "request_human_input"
    assert "Inspect" in action.arguments["reason"]


def test_hosted_model_receives_configured_app_origin_and_navigation_rules(monkeypatch):
    app_origin = "http://127.0.0.1:4321"
    monkeypatch.setenv("APP_BASE_URL", app_origin)
    captured = {}

    class FakeStructuredModel:
        def invoke(self, messages):
            captured["messages"] = messages
            return ToolAction(
                tool="navigate",
                arguments={"url": f"{app_origin}/login"},
                rationale="Open the configured login page.",
            )

    class FakeClient:
        def with_structured_output(self, output_type, method):
            assert output_type is ToolAction
            assert method == "function_calling"
            return FakeStructuredModel()

    adapter = OpenAIModelAdapter.__new__(OpenAIModelAdapter)
    adapter.client = FakeClient()
    action = adapter.decide(
        "Log in and inspect payables",
        "No observations yet.",
        {},
        [{"name": "navigate", "description": f"Navigate only within {app_origin}"}],
        {},
    )

    system_prompt = captured["messages"][0][1]
    user_payload = json.loads(captured["messages"][1][1])
    assert action.arguments["url"] == f"{app_origin}/login"
    assert user_payload["allowed_app_origin"] == app_origin
    assert f"{app_origin}/login" in system_prompt
    assert "placeholder URL" in system_prompt
    assert "runtime approval" in system_prompt
    assert "independently verify" in system_prompt


def test_browser_resolves_bare_login_field_names(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "selector_login.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = BrowserToolSet(
        f"http://127.0.0.1:{server.server_port}",
        tmp_path / "screenshots",
    )
    try:
        page = browser.navigate({"url": f"http://127.0.0.1:{server.server_port}/login"})
        assert page["url"].endswith("/login")
        filled = browser.fill_fields({"fields": {"username": "demo", "password": "demo"}})
        assert filled["status"] == "ok"
        assert filled["interactive_elements"][0]["value"] == "demo"
        submitted = browser.submit_form({"selector": "form"})
        assert submitted["url"].endswith("/payables")
    finally:
        browser.close()
        server.shutdown()
        thread.join(timeout=5)


def test_browser_click_resolves_observed_absolute_link_href(monkeypatch, tmp_path):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "absolute_href_click.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    browser = BrowserToolSet(origin, tmp_path / "screenshots")
    try:
        browser.navigate({"url": f"{origin}/login"})
        browser.fill_fields({"fields": {"username": "demo", "password": "demo"}})
        dashboard = browser.submit_form({"selector": "form"})
        add_payable_link = next(
            item["href"]
            for item in dashboard["interactive_elements"]
            if item["label"] == "Add payable"
        )

        result = browser.click({"selector": f"a[href='{add_payable_link}']"})

        assert result["status"] == "ok"
        assert result["url"] == f"{origin}/payables/new"
        assert any(item["name"] == "vendor" for item in result["interactive_elements"])
    finally:
        browser.close()
        server.shutdown()
        thread.join(timeout=5)


def test_browser_blocks_direct_click_on_payable_save_button(monkeypatch, tmp_path):
    db_path = tmp_path / "blocked_direct_save.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    browser = BrowserToolSet(origin, tmp_path / "screenshots")
    try:
        browser.navigate({"url": f"{origin}/login"})
        browser.fill_fields({"fields": {"username": "demo", "password": "demo"}})
        dashboard = browser.submit_form({"selector": "form"})
        add_payable_link = next(
            item["href"]
            for item in dashboard["interactive_elements"]
            if item["label"] == "Add payable"
        )
        browser.click({"selector": f"a[href='{add_payable_link}']"})
        browser.fill_fields(
            {
                "fields": {
                    "vendor": "Fabrikam",
                    "invoice_number": "INV-5512",
                    "amount": "640.25",
                    "due_date": "2026-12-22",
                }
            }
        )
        form = browser.read_page({})
        save_button = next(
            item for item in form["interactive_elements"] if item["label"] == "Save payable"
        )

        result = browser.click({"selector": f"#{save_button['id']}"})

        assert result["status"] == "error"
        assert "use submit_form" in result["message"].lower()
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT invoice_number FROM payables WHERE invoice_number = ?",
                ("INV-5512",),
            ).fetchone()
        assert row is None
    finally:
        browser.close()
        server.shutdown()
        thread.join(timeout=5)


def test_agent_enters_local_credentials_before_calling_hosted_model(monkeypatch, tmp_path):
    db_path = tmp_path / "hosted_login.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo-user")
    monkeypatch.setenv("DEMO_PASSWORD", "local-secret")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTO_APPROVE", "false")
    app = create_app()
    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class HostedLikeModel:
        def __init__(self):
            self.observations = []

        def plan(self, goal, history=None):
            return {"brief_plan": ["Inspect the payables list."], "success_criteria": ["List visible."]}

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            self.observations.append(recent_observation)
            page = json.loads(recent_observation)
            assert not any(
                element.get("name") in {"username", "password"}
                for element in page.get("interactive_elements", [])
            )
            if not memory.get("payables_inspected"):
                return ToolAction(
                    tool="read_page",
                    arguments={"selector": "body"},
                    rationale="Inspect the rendered payables list.",
                )
            return ToolAction(
                tool="finish",
                arguments={"message": "The payables list was inspected."},
                rationale="The requested read-only inspection is complete.",
            )

    model = HostedLikeModel()
    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: model)
    try:
        result = AgentLoop(tmp_path / "hosted_runs").run(
            "Log in and inspect general app information."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert result.memory["login_success"] is True
    assert model.observations
    assert all("local-secret" not in observation for observation in model.observations)


def test_hosted_model_cannot_repeat_login_fields_for_read_only_payable_count(monkeypatch, tmp_path):
    db_path = tmp_path / "readonly_count.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTO_APPROVE", "false")
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class StaleLoginModel:
        decide_calls = 0

        def plan(self, goal, history=None):
            return MockModelAdapter().plan(goal, history)

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            self.decide_calls += 1
            return ToolAction(
                tool="fill_fields",
                arguments={
                    "fields": {
                        "username": "demo",
                        "password": "demo",
                    }
                },
                rationale="Repeat login fields despite already being on the register.",
            )

    model = StaleLoginModel()
    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: model)
    try:
        result = AgentLoop(tmp_path / "count_runs").run(
            "Log in and count the payables in the register. Do not change any records."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert "2 payables" in result.final_message
    assert result.memory["payables_count_inspected"] is True
    assert model.decide_calls == 0
    assert [step["action"]["tool"] for step in result.steps] == [
        "navigate",
        "fill_fields",
        "submit_form",
        "read_page",
        "finish",
    ]
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM payables").fetchone()[0]
    assert count == 2


def test_hosted_model_prompt_drives_payables_inspection(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "inspect_hosted.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class HostedLikeModel:
        decide_calls = 0

        def plan(self, goal, history=None):
            return {"brief_plan": ["Inspect payables."], "success_criteria": ["Table read."]}

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            self.decide_calls += 1
            return ToolAction(
                tool="read_page",
                arguments={"selector": "body"},
                rationale="Repeat the already completed page read.",
            )

    model = HostedLikeModel()
    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: model)
    try:
        result = AgentLoop(tmp_path / "inspect_runs").run(
            "Log in and inspect the payables list and write your observation."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert result.memory["payables_inspected"] is True
    assert result.memory["finish_requested"] is True
    assert model.decide_calls == 0
    assert [step["action"]["tool"] for step in result.steps] == [
        "navigate",
        "fill_fields",
        "submit_form",
        "read_page",
        "finish",
    ]
    report_path = tmp_path / "inspect_runs" / result.run_id / "observation.md"
    report = report_path.read_text(encoding="utf-8")
    assert "Payables observed: 2" in report
    assert "| Acme | INV-1042 | $1250.00 | 2026-10-15 |" in report
    assert "Changes made: none" in report
    assert result.memory["observation_report_path"] == str(report_path)


def test_agent_calculates_payables_from_observed_table_without_model_navigation(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "calculation.db"))
    monkeypatch.setenv("CHAOS_MODE", "")
    monkeypatch.setenv("AUTO_APPROVE", "false")
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class RepeatingReadModel:
        decide_calls = 0

        def plan(self, goal, history=None):
            return MockModelAdapter().plan(goal, history)

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            self.decide_calls += 1
            return ToolAction(
                tool="read_page",
                arguments={"selector": "body"},
                rationale="Repeat reading without calculating.",
            )

    model = RepeatingReadModel()
    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: model)
    try:
        result = AgentLoop(tmp_path / "calculation_runs").run(
            "Log in, inspect the payables table, and report the number of payables, "
            "their total amount, and their average amount. Show the calculation and "
            "do not change any records."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert "Payables inspected (read-only): 2." in result.final_message
    assert "$1,250.00 + $980.50 = $2,230.50" in result.final_message
    assert "$2,230.50 / 2 = $1,115.25" in result.final_message
    assert result.memory["login_success"] is True
    assert result.memory["payables_inspected"] is True
    assert model.decide_calls == 0


def test_agent_reports_total_with_calculation_without_model_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "total_only.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class RepeatingReadModel:
        def plan(self, goal, history=None):
            return MockModelAdapter().plan(goal, history)

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            raise AssertionError("Deterministic read-only calculation should bypass the model.")

    monkeypatch.setattr(agent_loop, "get_model_adapter", RepeatingReadModel)
    try:
        result = AgentLoop(tmp_path / "total_runs").run(
            "Log in, inspect all payables, and report the total amount with the calculation. Do not make changes."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert "$1,250.00 + $980.50 = $2,230.50" in result.final_message
    assert "No records were changed." in result.final_message
    assert [step["action"]["tool"] for step in result.steps] == [
        "navigate",
        "fill_fields",
        "submit_form",
        "read_page",
        "finish",
    ]


def test_agent_filters_every_invoice_strictly_above_average(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "average_filter.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: MockModelAdapter())
    try:
        result = AgentLoop(tmp_path / "average_filter_runs").run(
            "Log in and inspect every payable record. Calculate the average invoice amount, "
            "then list every invoice whose amount is strictly greater than that average. "
            "Explain the comparison, include the invoice ID, vendor, and amount, and save a "
            "verified Markdown report containing all invoice details. Do not change financial records."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert result.memory["payables_average_comparison_verified"] is True
    assert result.memory["payables_average_matching_invoices"] == ["INV-1042"]
    assert "amount > $1,115.25" in result.final_message
    assert "INV-1042 (Acme) $1,250.00" in result.final_message
    assert "INV-2041" not in result.final_message


def test_agent_navigates_to_activity_and_summarizes_latest_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "activity.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class RepeatingLoginModel:
        decide_calls = 0

        def plan(self, goal, history=None):
            return MockModelAdapter().plan(goal, history)

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            self.decide_calls += 1
            return ToolAction(
                tool="fill_fields",
                arguments={"fields": {"username": "demo", "password": "demo"}},
                rationale="Repeat login although already authenticated.",
            )

    model = RepeatingLoginModel()
    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: model)
    try:
        result = AgentLoop(tmp_path / "activity_runs").run(
            "Log in, inspect the activity page, and summarize the latest activity entries without changing records."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert result.memory["activity_inspected"] is True
    assert result.memory["activity_items"]
    assert "latest activity entries" in result.final_message.lower()
    assert "logged in" in result.final_message.lower()
    assert model.decide_calls == 0
    assert [step["action"]["tool"] for step in result.steps] == [
        "navigate",
        "fill_fields",
        "submit_form",
        "click",
        "read_page",
        "finish",
    ]


def test_sequential_goals_do_not_leak_destination_or_update_context(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "sequential.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        loop = AgentLoop(tmp_path / "sequential_runs")
        vendors = loop.run("Summarize the approved vendors.")
        latest = loop.run("Find the latest invoice.")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert vendors.status == "finished"
    assert vendors.memory["vendors_inspected"] is True
    assert "/vendors" in vendors.memory["current_url"]
    assert latest.status == "waiting_for_human"
    assert "latest" in latest.final_message.lower()
    assert "update operation" not in latest.final_message.lower()
    assert latest.memory.get("vendors_inspected") is None


def test_latest_invoice_requests_definition_before_selecting(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "latest.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "latest_runs").run("Find the latest invoice.")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "waiting_for_human"
    assert "issue date" in result.final_message.lower()
    assert "due date" in result.final_message.lower()
    assert "highest invoice number" in result.final_message.lower()
    assert result.memory.get("latest_invoice_found") is None


def test_latest_invoice_does_not_choose_between_tied_due_dates(monkeypatch, tmp_path):
    from centralign_agent.loop import _latest_invoice_action

    observation = {
        "current_page": {
            "url": "http://127.0.0.1:3000/payables",
            "tables": [{
                "headers": ["Vendor", "Invoice number", "Amount", "Due date"],
                "rows": [
                    ["Fabrikam", "INV-9030", "$640.25", "2026-12-22"],
                    ["Fabrikam", "INV-5511", "$640.25", "2026-12-22"],
                ],
            }],
        }
    }
    memory = {"latest_invoice_inspected": True}
    action = _latest_invoice_action(observation, "Find the latest invoice by due date.", memory)

    assert action is not None
    assert action.tool == "request_human_input"
    assert "INV-9030" in action.arguments["reason"]
    assert "INV-5511" in action.arguments["reason"]
    assert memory.get("latest_invoice_found") is None


def test_mark_paid_requests_approval_without_mutation(monkeypatch, tmp_path):
    db_path = tmp_path / "mark_paid.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "mark_paid_runs").run(
            "Mark invoice INV-1042 as paid and ask for approval before changing records."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "waiting_for_human"
    assert "proposed action" in result.final_message.lower()
    assert "approval" in result.final_message.lower()
    assert "not available" in result.final_message.lower()
    assert result.memory["payment_proposal"]["invoice_number"] == "INV-1042"
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(payables)")}
        assert "paid" not in columns


def test_read_only_report_does_not_route_to_update_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "read_report.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "read_report_runs").run(
            "Calculate the total of all payables and save a Markdown report. Do not change any records."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert result.memory["task_report_read_back"] is True
    assert "update" not in result.final_message.lower()
    assert "2,230.50" in result.final_message


def test_report_verification_failure_is_not_claimed_complete(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "report_failure.db"))
    monkeypatch.setattr(
        agent_loop,
        "_write_task_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated read-back mismatch")),
    )
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "report_failure_runs").run(
            "Log in, inspect all payables, calculate the total, and save a Markdown report."
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "waiting_for_human"
    assert "read-back mismatch" in result.final_message
    assert result.memory.get("task_report_verified") is not True
    assert result.memory.get("task_report_attempts") == 2


def test_report_readback_checks_all_observed_payable_rows(tmp_path):
    from centralign_agent.loop import _write_task_report

    rows = [
        ["Acme", "INV-1042", "$1250.00", "2026-10-15"],
        ["Northwind", "INV-2041", "$980.50", "2026-11-04"],
        ["Contoso", "INV-5001", "$425.00", "2026-12-01"],
        ["Contoso", "INV-9007", "$275.50", "2026-12-15"],
        ["Fabrikam", "INV-9030", "$640.25", "2026-12-22"],
        ["Fabrikam", "INV-5511", "$640.25", "2026-12-22"],
    ]
    report_path = tmp_path / "task_report.md"
    _write_task_report(
        {
            "url": "http://127.0.0.1:3000/payables",
            "tables": [
                {
                    "headers": ["Vendor", "Invoice number", "Amount", "Due date"],
                    "rows": rows,
                }
            ],
        },
        report_path,
        "Calculate the total and save a Markdown report.",
        "Total: $1,250.00 + $980.50 + $425.00 + $275.50 + $640.25 + $640.25 = $4,211.50.",
    )

    report = report_path.read_text(encoding="utf-8")
    assert report.count("| ") >= 8
    assert "## Independent calculation" in report
    assert "$4,211.50" in report
    for row in rows:
        assert "| " + " | ".join(row) + " |" in report
    assert "$4,211.50" in report


def test_verifier_does_not_treat_read_only_records_as_a_create_goal():
    from centralign_agent.verifier import Verifier

    success, message = Verifier().verify(
        "Log in, inspect the payables table, and report totals. Do not change any records.",
        {
            "memory": {"login_success": True, "payables_inspected": True, "finish_requested": True},
            "evidence": ["Observed the payables dashboard and verified its rendered table."],
        },
    )

    assert success is True
    assert "payables table" in message.lower()


def test_mock_model_extracts_payable_details_from_goal():
    model = MockModelAdapter()
    dashboard = {
        "status": "ok",
        "url": "http://127.0.0.1:3000/payables",
        "text": "Payables Dashboard\nPayables table",
        "interactive_elements": [
            {
                "tag": "input",
                "name": "vendor",
                "value": "",
                "options": [
                    {"label": "Acme", "value": "Acme"},
                    {"label": "Contoso", "value": "Contoso"},
                ],
            }
        ],
    }
    action = model.decide(
        "Add payable for Contoso, invoice INV-9007, amount $275.50, due date 2026-12-15",
        json.dumps(dashboard),
        {},
        ["fill", "request_human_input"],
        {},
    )
    assert action.tool == "fill"
    assert action.arguments == {"selector": '[name="vendor"]', "value": "Contoso"}


def test_mock_model_requests_missing_payable_details():
    model = MockModelAdapter()
    dashboard = {
        "status": "ok",
        "url": "http://127.0.0.1:3000/payables",
        "text": "Payables Dashboard\nPayables table",
        "interactive_elements": [
            {
                "name": "vendor",
                "value": "",
                "options": [{"label": "Acme", "value": "Acme"}],
            }
        ],
    }
    action = model.decide(
        "Add a payable for Acme",
        json.dumps(dashboard),
        {},
        ["request_human_input"],
        {},
    )
    assert action.tool == "request_human_input"
    assert "invoice number" in action.arguments["reason"]


def test_agent_adds_and_independently_verifies_user_goal(monkeypatch, tmp_path):
    db_path = tmp_path / "company.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("CENTRALIGN_MODEL", "mock")
    monkeypatch.setenv("AUTO_APPROVE", "true")
    app = create_app()
    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: MockModelAdapter())
    try:
        result = AgentLoop(tmp_path / "runs").run(
            "Add payable for Contoso, invoice INV-9007, amount $275.50, due date 2026-12-15 and take a screenshot"
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert result.memory["target_invoice_verified"] is True
    assert result.approvals[0]["decision"] == "auto_approved"
    assert result.write_history[0]["details"]["invoice_number"] == "INV-9007"
    assert any(
        (
            step["action"]["tool"] == "fill"
            and step["action"]["arguments"].get("value") == "Contoso"
        )
        or (
            step["action"]["tool"] == "fill_fields"
            and "Contoso" in step["action"]["arguments"].get("fields", {}).values()
        )
        for step in result.steps
    )
    assert not any(step["action"]["tool"] == "select_option" for step in result.steps)
    payable_form_observation = next(
        step["observation"]
        for step in result.steps
        if step["observation"].get("url", "").endswith("/payables/new")
    )
    vendor_control = next(
        item
        for item in payable_form_observation["interactive_elements"]
        if item["name"] == "vendor"
    )
    assert vendor_control["tag"] == "input"
    assert vendor_control["options"] == []
    assert "Acme, Contoso, Fabrikam, Northwind" in vendor_control["description"]
    screenshot_files = list((tmp_path / "runs" / result.run_id / "screenshots").glob("*.png"))
    assert len(screenshot_files) == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT vendor, invoice_number, amount, due_date FROM payables WHERE invoice_number = ?",
            ("INV-9007",),
        ).fetchone()
    assert row == ("Contoso", "INV-9007", 275.5, "2026-12-15")


@pytest.mark.parametrize(
    ("auto_approve", "expected_status"),
    [(True, "finished"), (False, "waiting_for_human")],
)
def test_hosted_agent_save_button_uses_approval_and_post_write_verification(
    monkeypatch,
    tmp_path,
    auto_approve,
    expected_status,
):
    db_path = tmp_path / f"click_save_{auto_approve}.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTO_APPROVE", str(auto_approve).lower())
    app = create_app()
    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class ClickSaveModel:
        def plan(self, goal, history=None):
            return MockModelAdapter().plan(goal, history)

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            page = json.loads(recent_observation)
            url = page.get("url", "")
            if url.endswith("/payables"):
                link = next(
                    item for item in page["interactive_elements"]
                    if item.get("label") == "Add payable"
                )
                return ToolAction(
                    tool="click",
                    arguments={"selector": f"a[href='{link['href']}']"},
                    rationale="Open the payable form.",
                )
            if url.endswith("/payables/new"):
                wanted = {
                    "vendor": "Fabrikam",
                    "invoice_number": "INV-5511",
                    "amount": "640.25",
                    "due_date": "2026-12-22",
                }
                fields = {}
                for name, value in wanted.items():
                    field = next(item for item in page["interactive_elements"] if item.get("name") == name)
                    if str(field.get("value", "")) != value:
                        fields[f'[name="{name}"]'] = value
                if fields:
                    return ToolAction(
                        tool="fill_fields",
                        arguments={"fields": fields},
                        rationale="Fill the requested payable details.",
                    )
                save_button = next(
                    item
                    for item in page["interactive_elements"]
                    if item.get("label") == "Save payable"
                )
                return ToolAction(
                    tool="click",
                    arguments={"selector": f"#{save_button['id']}"},
                    rationale="Save the completed payable form.",
                )
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": "The expected page was not visible."},
                rationale="Pause if the page differs from the expected flow.",
            )

    monkeypatch.setattr(agent_loop, "get_model_adapter", lambda: ClickSaveModel())
    try:
        result = AgentLoop(tmp_path / "runs").run(
            "Save payable for Fabrikam, invoice INV-5511, amount $640.25, due date 2026-12-22"
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == expected_status
    assert result.approvals[0]["decision"] == (
        "auto_approved" if auto_approve else "needs_human"
    )
    assert result.approvals[0]["details"] == {
        "vendor": "Fabrikam",
        "invoice_number": "INV-5511",
        "amount": "640.25",
        "due_date": "2026-12-22",
    }
    actions = [step["action"]["tool"] for step in result.steps]
    assert "submit_form" in actions
    assert not any(
        step["action"]["tool"] == "click"
        and step["action"]["arguments"].get("selector", "").startswith("#element-")
        for step in result.steps
    )
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT vendor, invoice_number, amount, due_date FROM payables WHERE invoice_number = ?",
            ("INV-5511",),
        ).fetchone()

    if auto_approve:
        assert row == ("Fabrikam", "INV-5511", 640.25, "2026-12-22")
        assert result.memory["target_invoice_verified"] is True
        assert result.memory["finish_requested"] is True
        assert result.write_history[0]["details"]["invoice_number"] == "INV-5511"
        assert actions.count("read_page") == 1
        assert result.steps[-1]["action"]["tool"] == "read_page"
        assert not any(
            step["action"]["tool"] in {"fill", "fill_fields"}
            for step in result.steps
            if step["step"] > result.write_history[0]["step"]
        )
    else:
        assert row is None


def test_payable_submission_waits_for_approval_without_writing(monkeypatch, tmp_path):
    db_path = tmp_path / "approval.db"
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("CENTRALIGN_MODEL", "mock")
    monkeypatch.setenv("AUTO_APPROVE", "false")
    app = create_app()
    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "runs").run(
            "Add payable for Contoso, invoice INV-9008, amount $275.50, due date 2026-12-15"
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "waiting_for_human"
    assert result.approvals[0]["decision"] == "needs_human"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT invoice_number FROM payables WHERE invoice_number = ?",
            ("INV-9008",),
        ).fetchone()
    assert row is None


@pytest.mark.parametrize("chaos_mode", ["rename_field", "move_submit", "first_submit_fails"])
def test_agent_handles_renamed_fields_and_moved_submit(monkeypatch, tmp_path, chaos_mode):
    db_path = tmp_path / f"{chaos_mode}.db"
    monkeypatch.setenv("CHAOS_MODE", chaos_mode)
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("CENTRALIGN_MODEL", "mock")
    monkeypatch.setenv("AUTO_APPROVE", "true")
    app = create_app()
    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "runs").run(
            "Add payable for Contoso, invoice INV-9010, amount $275.50, due date 2026-12-15"
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "finished"
    assert result.memory["target_invoice_verified"] is True
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT vendor, invoice_number, amount, due_date FROM payables WHERE invoice_number = ?",
            ("INV-9010",),
        ).fetchone()
    assert row == ("Contoso", "INV-9010", 275.5, "2026-12-15")


def test_agent_pauses_for_extra_required_field_without_writing(monkeypatch, tmp_path):
    db_path = tmp_path / "extra_required_agent.db"
    monkeypatch.setenv("CHAOS_MODE", "extra_required")
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("CENTRALIGN_MODEL", "mock")
    monkeypatch.setenv("AUTO_APPROVE", "true")
    app = create_app()
    server = make_server("127.0.0.1", 0, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        result = AgentLoop(tmp_path / "runs").run(
            "Add payable for Contoso, invoice INV-9011, amount $275.50, due date 2026-12-15"
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.status == "waiting_for_human"
    assert "Approval code" in result.final_message
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT invoice_number FROM payables WHERE invoice_number = ?",
            ("INV-9011",),
        ).fetchone()
    assert row is None
