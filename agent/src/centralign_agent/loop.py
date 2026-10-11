from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit

from centralign_agent.config import get_settings
from centralign_agent.llm import get_model_adapter
from centralign_agent.model_adapter import MockModelAdapter, ToolAction
from centralign_agent.planner import Planner
from centralign_agent.state import RunState
from centralign_agent.trace import TraceRecorder
from centralign_agent.tools.browser import (
    BrowserToolSet,
    ClickArgs,
    FillArgs,
    FillFieldsArgs,
    NavigateArgs,
    ReadPageArgs,
    ScreenshotArgs,
    SelectArgs,
    SubmitArgs,
)
from centralign_agent.tools.files import FileToolSet, ReadFileArgs
from centralign_agent.tools.interaction import FinishArgs, HumanInputArgs, InteractionToolSet
from centralign_agent.tools.memory import MemoryToolSet, RecallArgs, RememberArgs
from centralign_agent.tools.registry import ToolRegistry, ToolSpec
from centralign_agent.verifier import Verifier


def _redact_action(action: dict) -> dict:
    safe_action = {**action, "arguments": dict(action.get("arguments", {}))}
    arguments = safe_action["arguments"]
    sensitive_keys = ("password", "secret", "api_key", "token")
    for key in list(arguments):
        if any(marker in key.lower() for marker in sensitive_keys):
            arguments[key] = "[redacted]"
        elif key == "fields" and isinstance(arguments[key], dict):
            arguments[key] = {
                selector: "[redacted]" if any(marker in selector.lower() for marker in sensitive_keys) else value
                for selector, value in arguments[key].items()
            }
    selector = str(arguments.get("selector", "")).lower()
    if "password" in selector and "value" in arguments:
        arguments["value"] = "[redacted]"
    return safe_action


def _login_action(observation: dict, settings) -> ToolAction | None:
    page = observation.get("current_page", observation)
    if not isinstance(page, dict):
        return None

    elements = page.get("interactive_elements", [])
    if not isinstance(elements, list):
        return None

    login_fields = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        name = str(element.get("name", "")).strip().lower()
        label = str(element.get("label", "")).strip().lower()
        field_name = name if name in {"username", "password"} else label
        if field_name in {"username", "password"}:
            login_fields[field_name] = element

    if not login_fields:
        return None

    page_text = str(page.get("text", "")).lower()
    if "invalid username or password" in page_text:
        return ToolAction(
            tool="request_human_input",
            arguments={
                "reason": (
                    "The app rejected the configured demo credentials. Check DEMO_USERNAME and "
                    "DEMO_PASSWORD in the local .env file, then restart the app."
                )
            },
            rationale="Pause after the app rejects local login credentials rather than retrying them.",
        )

    values = {
        "username": settings.demo_username,
        "password": settings.demo_password,
    }
    fields_to_fill = {}
    for field_name, value in values.items():
        element = login_fields.get(field_name)
        if element is None:
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": f"The login form does not expose a {field_name} field."},
                rationale="The visible login form is missing a required credential field.",
            )
        if element.get("filled", bool(element.get("value"))):
            continue
        if not value:
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": f"No {field_name} is configured for automatic login."},
                rationale="A required login credential is not configured locally.",
            )

        name = str(element.get("name", "")).strip()
        element_id = str(element.get("id", "")).strip()
        if name:
            escaped_name = name.replace("\\", "\\\\").replace('"', '\\"')
            selector = f'[name="{escaped_name}"]'
        elif element_id:
            selector = f"#{element_id}"
        else:
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": f"The visible {field_name} field has no usable selector."},
                rationale="The observed login field cannot be safely targeted.",
            )
        fields_to_fill[selector] = value

    if fields_to_fill:
        return ToolAction(
            tool="fill_fields",
            arguments={"fields": fields_to_fill},
            rationale="Enter locally configured login credentials without sending them to the model.",
        )
    return ToolAction(
        tool="submit_form",
        arguments={"selector": "form"},
        rationale="Submit the login form after locally configured credentials are filled.",
    )


def _payables_calculation_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    asks_count = any(term in normalized_goal for term in ("count", "number of payables", "how many"))
    asks_total = any(term in normalized_goal for term in ("total", "sum"))
    asks_average = "average" in normalized_goal or "mean" in normalized_goal
    if not (asks_count and asks_total and asks_average):
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict):
        return None
    if "/payables" not in str(page.get("url", "")) or not memory.get("payables_calculation_inspected"):
        return None

    tables = page.get("tables", [])
    if not isinstance(tables, list):
        return None
    payable_table = next(
        (
            table for table in tables
            if isinstance(table, dict)
            and "amount" in [str(header).strip().lower() for header in table.get("headers", [])]
        ),
        None,
    )
    if payable_table is None:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The payables page did not expose a readable table with an Amount column."},
            rationale="I cannot calculate totals without verified payable rows.",
        )

    headers = [str(header).strip().lower() for header in payable_table["headers"]]
    amount_index = headers.index("amount")
    amounts: list[Decimal] = []
    try:
        for row in payable_table.get("rows", []):
            if amount_index >= len(row):
                raise ValueError("A payable row has no amount cell.")
            amount_text = str(row[amount_index]).replace("$", "").replace(",", "").strip()
            amounts.append(Decimal(amount_text))
    except (InvalidOperation, ValueError, TypeError, IndexError):
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "At least one visible payable amount could not be parsed safely."},
            rationale="Avoid reporting a calculation from incomplete or invalid table data.",
        )

    if not amounts:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The payables table contains no rows to calculate."},
            rationale="The requested average is undefined when there are no payables.",
        )

    total = sum(amounts, Decimal("0"))
    average = (total / len(amounts)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    amount_list = " + ".join(f"${amount:,.2f}" for amount in amounts)
    message = (
        f"Payables inspected (read-only): {len(amounts)}. "
        f"Total: {amount_list} = ${total:,.2f}. "
        f"Average: ${total:,.2f} / {len(amounts)} = ${average:,.2f}."
    )
    return ToolAction(
        tool="finish",
        arguments={"message": message},
        rationale="Calculate the requested metrics directly from the verified visible payable table.",
    )


def _local_payables_action(
    observation: dict,
    goal: str,
    plan: dict,
    tools: list[dict],
    memory: dict,
) -> ToolAction | None:
    normalized_goal = goal.lower()
    if not re.search(r"\b(?:add|create|enter|record)\b", normalized_goal):
        return None
    if "payable" not in normalized_goal and "invoice" not in normalized_goal:
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None
    return MockModelAdapter().decide(
        goal,
        json.dumps(observation, ensure_ascii=False),
        plan,
        tools,
        memory,
    )


def _payables_inspection_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    requests_inspection = (
        "payables" in normalized_goal
        and any(term in normalized_goal for term in ("inspect", "view", "check", "review", "show"))
    )
    if not requests_inspection:
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None
    if not memory.get("payables_inspected"):
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Read the visible payables table before completing the requested inspection.",
        )
    if memory.get("observation_report_path"):
        return ToolAction(
            tool="finish",
            arguments={
                "message": (
                    f"Logged in and inspected the payables list. Observation report saved to "
                    f"{memory['observation_report_path']}."
                )
            },
            rationale="The requested observation was written to the run's report artifact.",
        )
    return ToolAction(
        tool="finish",
        arguments={"message": "Logged in and inspected the rendered payables list."},
        rationale="The payables dashboard and table were read and verified.",
    )


def _goal_requests_observation_report(goal: str) -> bool:
    normalized = goal.lower()
    requests_observation = "observation" in normalized or "observations" in normalized
    requests_write = any(term in normalized for term in ("write", "save", "document", "record"))
    return requests_observation and requests_write


def _write_payables_observation(result: dict, report_path: Path, goal: str) -> str:
    tables = result.get("tables", [])
    payable_table = next(
        (
            table for table in tables
            if isinstance(table, dict)
            and "amount" in [str(header).strip().lower() for header in table.get("headers", [])]
        ),
        None,
    )
    if payable_table is None:
        raise ValueError("The inspected page did not contain a readable payables table.")

    headers = [str(header).strip() for header in payable_table.get("headers", [])]
    rows = payable_table.get("rows", [])
    if not headers:
        raise ValueError("The inspected payables table has no column headers.")

    lines = [
        "# Payables Observation",
        "",
        f"- Goal: {goal}",
        f"- Page: {result.get('url', 'Unknown')}",
        f"- Payables observed: {len(rows)}",
        "- Changes made: none (read-only inspection)",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        if len(cells) != len(headers):
            raise ValueError("A payables table row does not match the header column count.")
        lines.append("| " + " | ".join(cells) + " |")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path)


def build_registry(state_path: Path, browser_tools: BrowserToolSet) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="navigate",
            description=(
                f"Navigate only within {browser_tools.allowed_origin}. "
                f"To begin, open {browser_tools.allowed_origin}/login. "
                "Never use URL placeholders or navigate to a different website."
            ),
            input_model=NavigateArgs,
            read_only=True,
            execution=browser_tools.navigate,
        )
    )
    registry.register(
        ToolSpec(
            name="read_page",
            description="Read the current page text and information for the model to reason from.",
            input_model=ReadPageArgs,
            read_only=True,
            execution=browser_tools.read_page,
        )
    )
    registry.register(
        ToolSpec(
            name="take_screenshot",
            description="Capture the current page as a PNG evidence artifact in the current run folder.",
            input_model=ScreenshotArgs,
            read_only=False,
            execution=browser_tools.take_screenshot,
        )
    )
    registry.register(
        ToolSpec(
            name="click",
            description="Click an interactive page element using a CSS selector.",
            input_model=ClickArgs,
            read_only=False,
            execution=browser_tools.click,
        )
    )
    registry.register(
        ToolSpec(
            name="fill",
            description="Type text into a form field.",
            input_model=FillArgs,
            read_only=False,
            execution=browser_tools.fill,
        )
    )
    registry.register(
        ToolSpec(
            name="fill_fields",
            description="Fill multiple visible form fields in one action using their current selectors.",
            input_model=FillFieldsArgs,
            read_only=False,
            execution=browser_tools.fill_fields,
        )
    )
    registry.register(
        ToolSpec(
            name="select_option",
            description="Select an option from a dropdown using a CSS selector.",
            input_model=SelectArgs,
            read_only=False,
            execution=browser_tools.select_option,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_form",
            description="Submit the relevant form on the page using a selector.",
            input_model=SubmitArgs,
            read_only=False,
            execution=browser_tools.submit_form,
        )
    )
    registry.register(
        ToolSpec(
            name="read_file",
            description="Read a text or HTML file from the configured inbox directory.",
            input_model=ReadFileArgs,
            read_only=True,
            execution=FileToolSet.read_file,
        )
    )
    registry.register(
        ToolSpec(
            name="remember",
            description="Persist a concise factual memory for the current run.",
            input_model=RememberArgs,
            read_only=False,
            execution=lambda arguments: MemoryToolSet.remember(arguments, state_path),
        )
    )
    registry.register(
        ToolSpec(
            name="recall",
            description="Recall a stored memory value for the current run.",
            input_model=RecallArgs,
            read_only=True,
            execution=lambda arguments: MemoryToolSet.recall(arguments, state_path),
        )
    )
    registry.register(
        ToolSpec(
            name="request_human_input",
            description="Pause and ask for explicit human input when the agent reaches a blocking decision.",
            input_model=HumanInputArgs,
            read_only=False,
            execution=InteractionToolSet.request_human_input,
        )
    )
    registry.register(
        ToolSpec(
            name="finish",
            description="Finish the run with a verified final message and evidence summary.",
            input_model=FinishArgs,
            read_only=False,
            execution=InteractionToolSet.finish,
        )
    )
    return registry


class AgentLoop:
    def __init__(self, run_dir: Path | None = None):
        self.settings = get_settings()
        self.run_dir = run_dir or Path("runs")
        self.model = get_model_adapter()

    def run(self, goal: str) -> RunState:
        run_id = uuid.uuid4().hex
        run_dir = self.run_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = RunState(run_id=run_id, goal=goal, run_dir=str(run_dir), trace_path=str(run_dir / "trace.jsonl"))
        trace = TraceRecorder(run_dir)
        state_path = run_dir / "state.json"
        browser_tools = BrowserToolSet(
            self.settings.app_origin,
            run_dir / "screenshots",
            headless=self.settings.headless,
            action_delay_ms=self.settings.browser_action_delay_ms,
        )
        registry = build_registry(state_path, browser_tools)
        planner = Planner(self.model)
        try:
            plan = planner.create_plan(goal)
        except Exception as exc:
            state.status = "failed"
            state.final_message = f"Planning failed: {exc}"
            trace.record({"type": "error", "stage": "planning", "message": state.final_message})
            state.persist(state_path)
            print(f"[CentrAlign] Final status: {state.status}")
            print(f"[CentrAlign] Error: {state.final_message}")
            browser_tools.close()
            return state
        state.plan = plan.model_dump()
        trace.record({"type": "plan", "plan": state.plan})

        print(f"[CentrAlign] Run {run_id} started")
        print(f"[CentrAlign] Goal: {goal}")
        print(f"[CentrAlign] Plan: {'; '.join(plan.brief_plan)}")

        for step_number in range(1, self.settings.agent_max_steps + 1):
            available_tools = registry.list_tools()
            recent_observation = state.steps[-1]["observation"] if state.steps else "No observations yet."
            if not state.steps:
                action = ToolAction(
                    tool="navigate",
                    arguments={"url": f"{self.settings.app_origin}/login"},
                    rationale="Open the configured company app login page.",
                )
            else:
                action = _payables_calculation_action(recent_observation, goal, state.memory)
                if action is None:
                    action = _payables_inspection_action(recent_observation, goal, state.memory)
                if action is None:
                    action = _local_payables_action(
                        recent_observation,
                        goal,
                        state.plan,
                        available_tools,
                        state.memory,
                    )
                if action is None:
                    action = _login_action(recent_observation, self.settings)
                if action is None:
                    try:
                        action = self.model.decide(
                            goal,
                            json.dumps(recent_observation, ensure_ascii=False),
                            state.plan,
                            available_tools,
                            state.memory,
                        )
                    except Exception as exc:
                        state.status = "failed"
                        state.final_message = f"Model decision failed: {exc}"
                        trace.record({"type": "error", "stage": "decision", "step": step_number, "message": state.final_message})
                        state.persist(state_path)
                        print(f"[CentrAlign] Final status: {state.status}")
                        print(f"[CentrAlign] Error: {state.final_message}")
                        break

            registered_tools = {tool["name"] for tool in available_tools}
            if action.tool not in registered_tools:
                namespaced_tool = action.tool.rsplit(".", 1)[-1]
                if namespaced_tool in registered_tools:
                    action = action.model_copy(update={"tool": namespaced_tool})
                else:
                    action = ToolAction(
                        tool="request_human_input",
                        arguments={
                            "reason": (
                                f"The model selected unregistered tool {action.tool!r}. "
                                "The action was not executed."
                            )
                        },
                        rationale="Pause safely instead of repeatedly executing an unknown tool.",
                    )

            step_payload = {"step": step_number, "tool": action.tool, "arguments": action.arguments, "rationale": action.rationale}
            safe_step_payload = {**step_payload, "arguments": _redact_action(step_payload)["arguments"]}
            trace.record({"type": "action", **safe_step_payload})
            print(f"[CentrAlign] Step {step_number}: {action.tool} ({action.rationale})")

            if action.tool == "submit_form":
                try:
                    current_url = browser_tools.current_url()
                except Exception as exc:
                    state.status = "failed"
                    state.final_message = f"Submission was blocked because the current page could not be verified: {exc}"
                    trace.record({"type": "error", "stage": "submission_origin_check", "step": step_number, "message": state.final_message})
                    state.persist(state_path)
                    print(f"[CentrAlign] Final status: {state.status}")
                    print(f"[CentrAlign] Error: {state.final_message}")
                    break
                payable_details = None
                if current_url and urlsplit(current_url).path.rstrip("/") == "/payables":
                    try:
                        payable_details = browser_tools.payable_form_details()
                    except Exception as exc:
                        state.status = "failed"
                        state.final_message = f"Approval preflight failed; payable was not submitted: {exc}"
                        trace.record({"type": "error", "stage": "approval_preflight", "step": step_number, "message": state.final_message})
                        state.persist(state_path)
                        print(f"[CentrAlign] Final status: {state.status}")
                        print(f"[CentrAlign] Error: {state.final_message}")
                        break

                if payable_details is not None:
                    approval = {
                        "step": step_number,
                        "action": "submit_payable",
                        "details": payable_details["fields"],
                        "decision": "auto_approved" if self.settings.auto_approve else "pending",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    if not self.settings.auto_approve:
                        print("[CentrAlign] Approval required before creating this payable:")
                        for name, value in payable_details["fields"].items():
                            print(f"  {name}: {value}")
                        if sys.stdin.isatty():
                            response = input("Approve this exact payable submission? [y/N]: ").strip().lower()
                            approval["decision"] = "approved" if response in {"y", "yes"} else "denied"
                        else:
                            approval["decision"] = "needs_human"

                    state.approvals.append(approval)
                    trace.record({"type": "approval", **approval})
                    if approval["decision"] in {"denied", "needs_human"}:
                        state.status = "waiting_for_human" if approval["decision"] == "needs_human" else "denied"
                        state.final_message = (
                            "Payable submission was not approved; no payable was created."
                            if approval["decision"] == "denied"
                            else "Approval is required to create this payable. Re-run interactively to approve it."
                        )
                        state.persist(state_path)
                        print(f"[CentrAlign] {state.final_message}")
                        break

            try:
                result = registry.execute(action.tool, action.arguments)
            except Exception as exc:  # explicit failure observation for adaptation
                result = {"status": "error", "message": str(exc), "tool": action.tool}

            state.steps.append(
                {"step": step_number, "action": _redact_action(action.model_dump()), "observation": result}
            )
            trace.record({"type": "observation", "step": step_number, "result": result})
            print(f"[CentrAlign] Observation: {json.dumps(result, ensure_ascii=False)[:1200]}")

            if action.tool == "remember":
                key = action.arguments.get("key")
                value = action.arguments.get("value")
                if key is not None:
                    state.memory[key] = value
            elif action.tool == "recall":
                key = action.arguments.get("key")
                state.memory[key] = result.get("value") if isinstance(result, dict) else None

            if result.get("status") == "finished":
                state.final_message = result.get("message", "Task finished by the worker.")
                state.memory["finish_requested"] = True
                break

            if result.get("status") == "needs_human_input":
                state.status = "waiting_for_human"
                state.final_message = result.get("reason", "Human input required.")
                state.evidence.append(state.final_message)
                break

            if result.get("status") == "error":
                state.memory["last_error"] = result.get("message", "Tool call failed")
                trace.record({"type": "error", "step": step_number, "message": result.get("message")})
                continue

            if result.get("status") == "ok" and action.tool == "navigate":
                state.memory["current_url"] = result.get("url")
                state.memory["page_title"] = result.get("title")

            if result.get("status") == "ok" and action.tool == "read_page":
                page_text = str(result.get("text", "")).lower()
                if "payables dashboard" in page_text and "payables table" in page_text:
                    state.memory["login_success"] = True
                    state.memory["payables_inspected"] = True
                    if any(
                        term in goal.lower()
                        for term in ("count", "number of payables", "how many", "total", "sum", "average", "mean")
                    ):
                        state.memory["payables_calculation_inspected"] = True
                    state.evidence.append(
                        f"Observed payables dashboard at {result.get('url')}; the payables table was visible."
                    )
                    if _goal_requests_observation_report(goal):
                        try:
                            report_path = _write_payables_observation(
                                result,
                                run_dir / "observation.md",
                                goal,
                            )
                        except (OSError, ValueError) as exc:
                            state.status = "failed"
                            state.final_message = f"Could not write the requested observation report: {exc}"
                            trace.record(
                                {
                                    "type": "error",
                                    "stage": "observation_report",
                                    "step": step_number,
                                    "message": state.final_message,
                                }
                            )
                            state.persist(state_path)
                            print(f"[CentrAlign] Error: {state.final_message}")
                            break
                        state.memory["observation_report_path"] = report_path
                        state.evidence.append(f"Observation report written to {report_path}.")
                        print(f"[CentrAlign] Observation report: {report_path}")
                    if state.memory.get("payables_calculation_inspected"):
                        state.evidence.append("Payable rows and amounts were captured from the rendered table.")
                    target_invoice = re.search(r"\bINV[- ]?\d+\b", goal, re.IGNORECASE)
                    if target_invoice and target_invoice.group(0).replace(" ", "-").upper() in str(result.get("text", "")).upper():
                        state.memory["target_invoice_verified"] = True
                        state.evidence.append(
                            f"Verified {target_invoice.group(0).replace(' ', '-').upper()} in the rendered payables table."
                        )

            if result.get("status") == "ok" and action.tool == "read_file":
                state.memory["last_file_content"] = result.get("content")
                if "invoice" in goal.lower() and "INV-" in str(result.get("content") or ""):
                    state.memory["latest_invoice_found"] = True

            if result.get("status") == "ok" and action.tool == "take_screenshot":
                state.memory["screenshot_captured"] = True
                state.evidence.append(f"Screenshot captured: {result.get('path')}")

            if (
                result.get("status") == "ok"
                and action.tool == "submit_form"
                and "payable added successfully" in str(result.get("text", "")).lower()
            ):
                state.memory["payable_submission_seen"] = True
                state.write_history.append(
                    {
                        "step": step_number,
                        "tool": action.tool,
                        "details": state.approvals[-1]["details"] if state.approvals else {},
                        "result": "success",
                    }
                )

            state.persist(state_path)

        browser_tools.close()
        verifier = Verifier()
        ok, message = verifier.verify(goal, state.__dict__)
        if state.status == "running":
            state.status = "finished" if ok and state.memory.get("finish_requested") else "failed"
        elif state.status == "finished" and not ok:
            state.status = "failed"
        state.final_message = state.final_message or message
        state.evidence.append(message)
        state.persist(state_path)

        print(f"[CentrAlign] Final status: {state.status}")
        print(f"[CentrAlign] Verification: {message}")
        return state
