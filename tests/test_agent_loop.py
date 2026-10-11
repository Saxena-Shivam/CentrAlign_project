from __future__ import annotations

import json
import sqlite3
from threading import Thread

import pytest

from centralign_agent import loop as agent_loop
from centralign_agent.loop import AgentLoop
from centralign_agent.model_adapter import MockModelAdapter, OpenAIModelAdapter, ToolAction
from centralign_agent.planner import Planner
from centralign_agent.tools.browser import BrowserToolSet
from company_app.app import create_app
from werkzeug.serving import make_server


def test_plan_creation():
    planner = Planner(MockModelAdapter())
    plan = planner.create_plan("Log in and add a payable for Acme")
    assert plan.brief_plan
    assert plan.success_criteria


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
    assert "allowed_app_origin + '/login'" in system_prompt
    assert "accounting_system_url" in system_prompt


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


def test_hosted_model_cannot_loop_on_payables_inspection(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "inspect_hosted.db"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APP_BASE_URL", f"http://127.0.0.1:{server.server_port}")

    class HostedLikeModel:
        def plan(self, goal, history=None):
            return {"brief_plan": ["Inspect payables."], "success_criteria": ["Table read."]}

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            pytest.fail("A payables-inspection goal should finish through the verified local policy.")

    monkeypatch.setattr(agent_loop, "get_model_adapter", HostedLikeModel)
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
    report_path = tmp_path / "inspect_runs" / result.run_id / "observation.md"
    report = report_path.read_text(encoding="utf-8")
    assert "Payables observed: 2" in report
    assert "| Acme | INV-1042 | 1250.0 | 2026-10-15 |" in report
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

    class ModelThatMustNotDecideNavigation:
        def plan(self, goal, history=None):
            return {
                "brief_plan": ["Log in and inspect the payables table."],
                "success_criteria": ["Report verified payable count and amount calculations."],
            }

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            pytest.fail("The verified read-only calculation should not depend on model navigation.")

    monkeypatch.setattr(
        agent_loop,
        "get_model_adapter",
        lambda: ModelThatMustNotDecideNavigation(),
    )
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
        ["select_option", "request_human_input"],
        {},
    )
    assert action.tool == "select_option"
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

    class ModelThatMustNotChoosePayableFields:
        def plan(self, goal, history=None):
            return {"brief_plan": ["Add and verify the requested payable."], "success_criteria": ["Invoice is visible."]}

        def decide(self, goal, recent_observation, plan, available_tools, memory):
            pytest.fail("An explicit payable goal should use observed form fields, not model-invented selectors.")

    monkeypatch.setattr(agent_loop, "get_model_adapter", ModelThatMustNotChoosePayableFields)
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
    screenshot_files = list((tmp_path / "runs" / result.run_id / "screenshots").glob("*.png"))
    assert len(screenshot_files) == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT vendor, invoice_number, amount, due_date FROM payables WHERE invoice_number = ?",
            ("INV-9007",),
        ).fetchone()
    assert row == ("Contoso", "INV-9007", 275.5, "2026-12-15")


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
