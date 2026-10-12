from __future__ import annotations

import json
import hashlib
import re
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
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
from centralign_agent.tools.memory import (
    MemoryToolSet,
    RecallArgs,
    RememberArgs,
    TableAnalysisArgs,
    analyze_table,
)
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


def _log_field_verification(action: ToolAction, result: dict) -> None:
    if action.tool not in {"fill", "fill_fields"}:
        return
    page = result.get("current_page", result)
    if not isinstance(page, dict):
        return
    if result.get("status") == "error":
        print(f"[CentrAlign] Field verification failed: {result.get('message', 'Unknown field error')}")
        return

    fields = []
    for element in page.get("interactive_elements", []):
        if not isinstance(element, dict):
            continue
        name = element.get("name") or element.get("label") or element.get("id")
        if element.get("type") == "password" or element.get("role") == "password":
            value = "[redacted]" if element.get("filled") else ""
        else:
            value = element.get("value", "")
        if name:
            fields.append(f"{name}={value!r}")
    if fields:
        print(f"[CentrAlign] Field verification: {', '.join(fields[:12])}")


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
    if _goal_requests_latest_invoice(goal) or _goal_requests_vendors(goal) or _goal_requests_update(goal):
        return None
    threshold_action = _payables_average_comparison_action(observation, goal, memory)
    if threshold_action is not None:
        return threshold_action
    asks_count = bool(
        re.search(r"\bcount\b", normalized_goal)
        or "number of payables" in normalized_goal
        or "how many" in normalized_goal
    )
    asks_total = bool(re.search(r"\b(?:total|sum)\b", normalized_goal))
    asks_average = bool(re.search(r"\b(?:average|mean)\b", normalized_goal))
    if not (asks_total or asks_average):
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict):
        return None
    if "/payables" not in str(page.get("url", "")):
        return None
    if not memory.get("payables_calculation_inspected"):
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Read the rendered payable amounts before calculating the requested totals.",
        )

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

    memory["payables_calculation_completed"] = True
    total = sum(amounts, Decimal("0"))
    average = (total / len(amounts)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    amount_list = " + ".join(f"${amount:,.2f}" for amount in amounts)
    results = []
    if asks_count:
        results.append(f"Payables inspected (read-only): {len(amounts)}.")
    if asks_total:
        results.append(f"Total: {amount_list} = ${total:,.2f}.")
    if asks_average:
        results.append(f"Average: ${total:,.2f} / {len(amounts)} = ${average:,.2f}.")
    message = " ".join(results) + " No records were changed."
    return ToolAction(
        tool="finish",
        arguments={"message": message},
        rationale="Calculate the requested metrics directly from the verified visible payable table.",
    )


def _payables_average_comparison_action(
    observation: dict,
    goal: str,
    memory: dict,
) -> ToolAction | None:
    """Evaluate a strict row comparison against the observed average."""
    normalized_goal = goal.lower()
    if not re.search(r"\b(?:average|mean)\b", normalized_goal):
        return None
    if not re.search(
        r"\b(?:greater|more|above|over|higher)\b.{0,30}\b(?:average|mean)\b"
        r"|\b(?:average|mean)\b.{0,30}\b(?:greater|more|above|over|higher)\b",
        normalized_goal,
    ):
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None
    if not memory.get("payables_calculation_inspected"):
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Read every visible payable row before calculating and filtering by the average.",
        )

    table = next(
        (
            item for item in page.get("tables", [])
            if isinstance(item, dict)
            and "amount" in [str(header).strip().lower() for header in item.get("headers", [])]
            and any("invoice" in str(header).strip().lower() for header in item.get("headers", []))
        ),
        None,
    )
    if table is None:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The payables page did not expose invoice and amount columns for comparison."},
            rationale="Do not filter invoices without complete observed rows.",
        )

    headers = [str(header).strip().lower() for header in table["headers"]]
    amount_index = headers.index("amount")
    invoice_index = next(index for index, header in enumerate(headers) if "invoice" in header)
    vendor_index = next((index for index, header in enumerate(headers) if "vendor" in header), None)
    due_date_index = next((index for index, header in enumerate(headers) if "due" in header), None)
    rows: list[tuple[Decimal, str, str, str, str]] = []
    try:
        for row in table.get("rows", []):
            required_indexes = [amount_index, invoice_index]
            if vendor_index is not None:
                required_indexes.append(vendor_index)
            if due_date_index is not None:
                required_indexes.append(due_date_index)
            if len(row) <= max(required_indexes):
                raise ValueError("A payable row is missing a required field.")
            amount = Decimal(str(row[amount_index]).replace("$", "").replace(",", "").strip())
            rows.append(
                (
                    amount,
                    str(row[invoice_index]),
                    str(row[vendor_index]) if vendor_index is not None else "",
                    str(row[due_date_index]) if due_date_index is not None else "",
                    " | ".join(str(cell) for cell in row),
                )
            )
    except (InvalidOperation, ValueError, TypeError, IndexError):
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "At least one payable row was incomplete or had an invalid amount."},
            rationale="Do not report a threshold comparison from incomplete evidence.",
        )
    if not rows:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The payables table contains no rows to compare."},
            rationale="The average and strict comparison are undefined without observed rows.",
        )

    total = sum((row[0] for row in rows), Decimal("0"))
    average = total / len(rows)
    matching_rows = [row for row in rows if row[0] > average]
    memory["payables_calculation_completed"] = True
    memory["payables_average"] = f"${average:,.2f}"
    memory["payables_average_comparison"] = "strictly greater"
    memory["payables_average_matching_invoices"] = [row[1] for row in matching_rows]
    memory["payables_average_comparison_verified"] = True
    details = "; ".join(
        f"{invoice} ({vendor}) ${amount:,.2f}" + (f", due {due_date}" if due_date else "")
        for amount, invoice, vendor, due_date, _ in matching_rows
    )
    if not matching_rows:
        details = "No invoice amount is strictly greater than the observed average."
    return ToolAction(
        tool="finish",
        arguments={
            "message": (
                f"Observed {len(rows)} invoices totaling ${total:,.2f}; "
                f"average = ${total:,.2f} / {len(rows)} = ${average:,.2f}. "
                f"Strict comparison: amount > ${average:,.2f}. "
                f"Matching invoice(s): {details}. No records were changed."
            )
        },
        rationale="Calculate the observed average first, then report every row strictly above it.",
    )


def _highest_amount_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    direction = _amount_extreme_direction(goal)
    if direction is None:
        return None
    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None
    table = next(
        (
            item for item in page.get("tables", [])
            if isinstance(item, dict)
            and "amount" in [str(header).strip().lower() for header in item.get("headers", [])]
            and any("invoice" in str(header).strip().lower() for header in item.get("headers", []))
        ),
        None,
    )
    if table is None:
        if not memory.get("highest_amount_inspected"):
            return ToolAction(
                tool="read_page",
                arguments={"selector": "body"},
                rationale="Read the visible payable rows before comparing their amounts.",
            )
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The visible payables register did not expose invoice and amount columns."},
            rationale="Do not identify an amount extreme without the required observed fields.",
        )

    headers = [str(header).strip().lower() for header in table["headers"]]
    amount_index = headers.index("amount")
    invoice_index = next(index for index, header in enumerate(headers) if "invoice" in header)
    vendor_index = next((index for index, header in enumerate(headers) if "vendor" in header), None)
    parsed_rows: list[tuple[Decimal, str, str]] = []
    try:
        for row in table.get("rows", []):
            if len(row) <= max(amount_index, invoice_index):
                raise ValueError("A payable row is missing the invoice or amount field.")
            parsed_rows.append(
                (
                    Decimal(str(row[amount_index]).replace("$", "").replace(",", "").strip()),
                    str(row[invoice_index]),
                    str(row[vendor_index]) if vendor_index is not None and len(row) > vendor_index else "",
                )
            )
    except (InvalidOperation, ValueError, TypeError):
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "At least one visible payable amount could not be parsed safely."},
            rationale="Avoid reporting an amount extreme from incomplete or invalid observed data.",
        )
    if not parsed_rows:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The visible payables register contains no invoice rows to compare."},
            rationale="An amount extreme cannot be identified from an empty register.",
        )

    extreme_amount = max(row[0] for row in parsed_rows) if direction == "highest" else min(
        row[0] for row in parsed_rows
    )
    extreme_rows = [row for row in parsed_rows if row[0] == extreme_amount]
    memory["highest_amount_inspected"] = True
    memory["amount_extreme_inspected"] = True
    memory["amount_extreme_verified"] = True
    memory["amount_extreme_direction"] = direction
    memory["amount_extreme"] = f"${extreme_amount:,.2f}"
    memory["amount_extreme_invoices"] = [row[1] for row in extreme_rows]
    if direction == "highest":
        memory["highest_amount_verified"] = True
        memory["highest_amount"] = f"${extreme_amount:,.2f}"
        memory["highest_amount_invoices"] = [row[1] for row in extreme_rows]
    details = "; ".join(
        f"{invoice} ({vendor})" if vendor else invoice
        for _, invoice, vendor in extreme_rows
    )
    tie_text = (
        f"{len(extreme_rows)} invoices tie at {extreme_amount:,.2f}; the observed register shows "
        f"the same amount for {details}."
        if len(extreme_rows) > 1
        else f"No tie was observed; {details} has the {direction} amount."
    )
    label = "highest" if direction == "highest" else "smallest"
    return ToolAction(
        tool="finish",
        arguments={
            "message": (
                f"The {label}-amount invoice amount is ${extreme_amount:,.2f}. "
                f"Matching invoice(s): {details}. {tie_text} No records were changed."
            )
        },
        rationale=f"Compare every observed payable amount and report all rows sharing the {direction} value.",
    )


def _payables_count_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    asks_count = bool(
        re.search(r"\bcount\b", normalized_goal)
        or "number of payables" in normalized_goal
        or "how many payables" in normalized_goal
        or "how many invoices" in normalized_goal
    )
    if not asks_count or not any(term in normalized_goal for term in ("payable", "invoice")):
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None

    if not memory.get("payables_count_inspected"):
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Read the rendered payables table before counting its rows.",
        )

    tables = page.get("tables", [])
    payable_table = next(
        (
            table for table in tables
            if isinstance(table, dict)
            and any("invoice" in str(header).strip().lower() for header in table.get("headers", []))
        ),
        None,
    )
    if payable_table is None:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The current page does not expose a readable payables table to count."},
            rationale="Do not guess the count when rendered invoice rows are unavailable.",
        )

    rows = payable_table.get("rows", [])
    return ToolAction(
        tool="finish",
        arguments={
            "message": (
                f"There are {len(rows)} payables in the rendered register. "
                "This was a read-only check; no records were changed."
            )
        },
        rationale="Count the rows in the fresh rendered payables table without changing records.",
    )


def _local_payables_action(
    observation: dict,
    goal: str,
    plan: dict,
    tools: list[dict],
    memory: dict,
) -> ToolAction | None:
    normalized_goal = goal.lower()
    if _goal_forbids_payable_write(goal):
        return None
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
        any(term in normalized_goal for term in ("payable", "invoice"))
        and any(term in normalized_goal for term in ("inspect", "view", "check", "review", "show"))
    )
    requests_report = _goal_requests_observation_report(goal)
    if not requests_inspection and not requests_report:
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


def _activity_page_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    if "activity" not in normalized_goal and "audit trail" not in normalized_goal:
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict):
        return None
    page_path = urlsplit(str(page.get("url", ""))).path.rstrip("/")

    if page_path == "/activity":
        if not memory.get("activity_inspected"):
            return ToolAction(
                tool="read_page",
                arguments={"selector": "body"},
                rationale="Read the visible activity entries before summarizing them.",
            )
        items = memory.get("activity_items", [])
        if not items:
            summary = "The Activity page contains no recorded activity entries."
        else:
            latest_items = []
            seen_items = set()
            for item in items:
                normalized_item = " ".join(str(item).split())
                if normalized_item and normalized_item.casefold() not in seen_items:
                    latest_items.append(normalized_item)
                    seen_items.add(normalized_item.casefold())
                if len(latest_items) == 5:
                    break
            summary = "Latest activity entries (newest first):\n" + "\n".join(
                f"{index}. {item}" for index, item in enumerate(latest_items, start=1)
            )
        return ToolAction(
            tool="finish",
            arguments={"message": summary + " No records were changed."},
            rationale="Summarize the latest entries from the observed Activity page.",
        )

    if page_path == "/payables":
        links = page.get("interactive_elements", [])
        activity_link = next(
            (
                item for item in links
                if isinstance(item, dict)
                and item.get("tag") == "a"
                and str(item.get("label", "")).strip().lower() == "activity"
                and item.get("href")
            ),
            None,
        )
        if activity_link:
            return ToolAction(
                tool="click",
                arguments={"selector": f"a[href='{activity_link['href']}']"},
                rationale="Follow the observed Activity navigation link requested by the user.",
            )
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The Payables page has no visible Activity navigation link."},
            rationale="Do not guess a route when the requested page link is not visible.",
        )
    return None


def _vendors_page_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    requests_vendors = _goal_requests_vendors(goal)
    if not requests_vendors:
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict):
        return None
    page_path = urlsplit(str(page.get("url", ""))).path.rstrip("/")
    if page_path == "/vendors":
        if not memory.get("vendors_inspected"):
            return ToolAction(
                tool="read_page",
                arguments={"selector": "body"},
                rationale="Read the visible vendor directory before summarizing it.",
            )
        vendors = memory.get("vendor_items", [])
        summary = "Approved vendors in the directory: " + (
            ", ".join(vendors) if vendors else "none listed"
        )
        return ToolAction(
            tool="finish",
            arguments={"message": summary + ". No records were changed."},
            rationale="Summarize the vendors captured from the requested directory page.",
        )

    if page_path == "/payables":
        vendor_link = next(
            (
                item for item in page.get("interactive_elements", [])
                if isinstance(item, dict)
                and item.get("tag") == "a"
                and str(item.get("label", "")).strip().lower() == "vendors"
                and item.get("href")
            ),
            None,
        )
        if vendor_link:
            return ToolAction(
                tool="click",
                arguments={"selector": f"a[href='{vendor_link['href']}']"},
                rationale="Follow the observed Vendors navigation link requested by the user.",
            )
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The Payables page has no visible Vendors navigation link."},
            rationale="Do not guess a route when the requested page link is not visible.",
        )
    return None


def _latest_invoice_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    if not _goal_requests_latest_invoice(goal):
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None
    if not memory.get("latest_invoice_inspected"):
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Read the visible payable rows before identifying a latest invoice.",
        )

    if not re.search(r"\b(?:due date|due-date)\b", normalized_goal):
        return ToolAction(
            tool="request_human_input",
            arguments={
                "reason": (
                    "“Latest invoice” is ambiguous. Should latest mean the latest issue date, latest due date, "
                    "or highest invoice number? The register does not display issue dates, and invoice numbers "
                    "are not a reliable creation order, so I have not guessed."
                )
            },
            rationale="Ask which date definition to use before selecting an invoice.",
        )

    table = next(
        (
            item for item in page.get("tables", [])
            if isinstance(item, dict)
            and any("invoice" in str(header).lower() for header in item.get("headers", []))
            and any("due date" in str(header).lower() for header in item.get("headers", []))
        ),
        None,
    )
    if table is None:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The visible register did not expose invoice and due-date columns."},
            rationale="Do not infer the latest invoice without the required visible data.",
        )

    headers = [str(header).strip().lower() for header in table["headers"]]
    invoice_index = next(index for index, name in enumerate(headers) if "invoice" in name)
    date_index = next(index for index, name in enumerate(headers) if "due date" in name)
    rows = [row for row in table.get("rows", []) if len(row) > max(invoice_index, date_index)]
    if not rows:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "No invoice rows were available to compare due dates."},
            rationale="Do not report a latest invoice when the register is empty.",
        )
    latest = max(rows, key=lambda row: str(row[date_index]))
    latest_date = str(latest[date_index])
    tied_rows = [row for row in rows if str(row[date_index]) == latest_date]
    if len(tied_rows) > 1:
        tied_invoices = ", ".join(str(row[invoice_index]) for row in tied_rows)
        if re.search(r"\ball\b", normalized_goal) and re.search(
            r"\b(?:sharing|tied|same)\b", normalized_goal
        ):
            memory["latest_invoice_found"] = [
                str(row[invoice_index]) for row in tied_rows
            ]
            return ToolAction(
                tool="finish",
                arguments={
                    "message": (
                        f"The latest visible due date is {latest_date}. "
                        f"All invoices sharing that date are: {tied_invoices}."
                    )
                },
                rationale="Report every invoice tied for the latest observed due date.",
            )
        return ToolAction(
            tool="request_human_input",
            arguments={
                "reason": (
                    f"The latest visible due date is {latest_date}, shared by {tied_invoices}. "
                    "The register does not contain issue dates. Please identify which invoice to use, "
                    "or clarify whether latest means issue date, due date, or highest invoice number."
                )
            },
            rationale="Do not select one invoice arbitrarily when the latest visible date is tied.",
        )
    memory["latest_invoice_found"] = str(latest[invoice_index])
    return ToolAction(
        tool="finish",
        arguments={
            "message": (
                f"The latest-due invoice is {latest[invoice_index]} "
                f"({latest[date_index]} due date)."
            )
        },
        rationale="Select the invoice with the greatest due date from the verified visible register.",
    )


def _payable_update_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    if not _goal_requests_update(goal):
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None
    if not memory.get("payables_update_inspected"):
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Inspect the register before responding to the requested payable update.",
        )

    table = next(
        (
            item for item in page.get("tables", [])
            if isinstance(item, dict)
            and "amount" in [str(header).strip().lower() for header in item.get("headers", [])]
        ),
        None,
    )
    largest = None
    if table:
        headers = [str(header).strip().lower() for header in table["headers"]]
        amount_index = headers.index("amount")
        invoice_index = next(
            (index for index, header in enumerate(headers) if "invoice" in header),
            None,
        )
        parsed_rows = []
        try:
            for row in table.get("rows", []):
                if invoice_index is None or amount_index >= len(row) or invoice_index >= len(row):
                    continue
                amount = Decimal(str(row[amount_index]).replace("$", "").replace(",", "").strip())
                parsed_rows.append((amount, str(row[invoice_index])))
        except (InvalidOperation, ValueError):
            parsed_rows = []
        if parsed_rows:
            largest = max(parsed_rows)

    largest_description = (
        f"The largest invoice is {largest[1]} at ${largest[0]:,.2f}. "
        if largest else "I could not identify the largest invoice from the visible rows. "
    )
    return ToolAction(
        tool="request_human_input",
        arguments={
            "reason": (
                largest_description
                + "This app currently has no payable-update page or update operation, so no record was changed "
                "and no update approval can be requested yet. Clarify the field/value to change and use an "
                "update-enabled app before proceeding."
            )
        },
        rationale="Report the inspected largest invoice and stop because updates are not implemented in this app.",
    )


def _invoice_payment_action(observation: dict, goal: str, memory: dict) -> ToolAction | None:
    normalized_goal = goal.lower()
    if not re.search(r"\b(?:mark|set)\b.*\b(?:paid|payment)\b", normalized_goal):
        return None
    if not re.search(r"\b(?:invoice|payable)\b", normalized_goal):
        return None

    page = observation.get("current_page", observation)
    if not isinstance(page, dict) or "/payables" not in str(page.get("url", "")):
        return None
    invoice = _goal_invoice_number(goal)
    if not invoice:
        return ToolAction(
            tool="request_human_input",
            arguments={
                "reason": (
                    "Which invoice should be marked as paid? The goal does not identify an invoice, "
                    "so no record was changed."
                )
            },
            rationale="Request the missing invoice before preparing an approval request.",
        )
    if not memory.get("payables_inspected"):
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Inspect the visible register before preparing the proposed payment action.",
        )
    if not _payables_page_contains_invoice(page, invoice):
        return ToolAction(
            tool="request_human_input",
            arguments={
                "reason": (
                    f"{invoice} is not present in the visible payables register. "
                    "Confirm the invoice number; no record was changed."
                )
            },
            rationale="Do not request approval for an invoice that was not observed.",
        )

    memory["payment_proposal"] = {"invoice_number": invoice, "action": "mark as paid"}
    return ToolAction(
        tool="request_human_input",
        arguments={
            "reason": (
                f"Proposed action: mark invoice {invoice} as paid. "
                "Explicit approval is required before any financial change. "
                "The mark-paid operation is not available in the current app, so approval and execution "
                "are separate: approval can be recorded, but no update will be submitted until "
                "the app supports this operation. No record was changed."
            )
        },
        rationale="Present the exact proposed payment action and pause before any financial mutation.",
    )


def _goal_requests_observation_report(goal: str) -> bool:
    normalized = goal.lower()
    requests_observation = "observation" in normalized or "observations" in normalized
    requests_write = any(term in normalized for term in ("write", "save", "document", "record"))
    return requests_observation and requests_write


def _goal_requests_latest_invoice(goal: str) -> bool:
    normalized = goal.lower()
    return bool(
        re.search(r"\b(?:latest|newest|most recent)\b", normalized)
        and re.search(r"\b(?:invoices?|payables?)\b", normalized)
    )


def _goal_requests_highest_amount(goal: str) -> bool:
    return _amount_extreme_direction(goal) == "highest"


def _amount_extreme_direction(goal: str) -> str | None:
    normalized = goal.lower()
    if not re.search(r"\b(?:amount|invoice|payable)\b", normalized):
        return None
    if re.search(r"\b(?:highest|largest|maximum|max)\b", normalized):
        return "highest"
    if re.search(r"\b(?:smallest|lowest|minimum|min)\b", normalized):
        return "smallest"
    return None


def _goal_requests_vendors(goal: str) -> bool:
    normalized = goal.lower()
    return bool(
        (
            re.search(r"\bvendors\b", normalized)
            or "vendor directory" in normalized
            or "vendors page" in normalized
        )
        and any(term in normalized for term in ("summarize", "list", "show", "review", "inspect", "directory", "page"))
    )


def _goal_requests_update(goal: str) -> bool:
    normalized = goal.lower()
    return bool(
        re.search(r"\b(?:update|edit|modify|change)\b", normalized)
        and re.search(r"\b(?:payable|invoice|record)\b", normalized)
        and not _goal_forbids_payable_write(goal)
        and not _goal_requests_latest_invoice(goal)
    )


def _goal_requires_generic_analysis(goal: str) -> bool:
    normalized = goal.lower()
    analytical_markers = (
        "percentage",
        "percent",
        "average",
        "mean",
        "group",
        "combined",
        "repeated",
        "duplicate",
        "second-highest",
        "second highest",
        "distinct",
        "strictly greater",
        "greater than",
        "financial summary",
        "each invoice",
        "every invoice",
    )
    return any(marker in normalized for marker in analytical_markers)


def _generic_analysis_action(goal: str, observation: dict[str, Any]) -> ToolAction | None:
    tables = observation.get("tables", [])
    if not any(isinstance(table, dict) and table.get("headers") for table in tables):
        return None
    normalized = goal.lower()
    operations: list[dict[str, Any]] = []
    if any(word in normalized for word in ("total", "sum", "financial summary", "percentage", "percent")):
        operations.append({"kind": "total"})
    if any(word in normalized for word in ("average", "mean", "financial summary")):
        operations.append({"kind": "average"})
    if any(phrase in normalized for phrase in ("greater than the average", "above average", "strictly greater")):
        operations.append(
            {"kind": "filter", "operator": ">", "threshold": {"kind": "average"}}
        )
    if any(word in normalized for word in ("percentage", "percent")):
        operations.append({"kind": "percentage", "output": "percentage"})
    if any(word in normalized for word in ("group", "combined", "vendor total")):
        operations.append({"kind": "group_sum", "group_by": "VENDOR"})
    if any(word in normalized for word in ("repeated", "duplicate")):
        operations.append({"kind": "duplicates"})
    if "second-highest" in normalized or "second highest" in normalized:
        operations.append({"kind": "distinct_rank", "rank": 2})
    if "financial summary" in normalized:
        operations.extend(
            operation
            for operation in (
                {"kind": "count"},
                {"kind": "minimum"},
                {"kind": "maximum"},
            )
            if operation not in operations
        )
    return ToolAction(
        tool="analyze_table",
        arguments={"operations": operations or [{"kind": "count"}]},
        rationale="Analyze the complete observed table before producing the requested result.",
    )


def _goal_requests_task_report(goal: str) -> bool:
    normalized = goal.lower()
    asks_to_save = bool(re.search(r"\b(?:write|save|create|generate|export)\b", normalized))
    return asks_to_save and (
        "report" in normalized
        or "markdown" in normalized
        or "observation" in normalized
        or "observations" in normalized
    )


def _write_task_report(result: dict, report_path: Path, goal: str, summary: str) -> str:
    tables = result.get("tables", [])
    payable_rows: list[list[str]] = []
    payable_amounts: list[Decimal] = []
    lines = [
        "# CentrAlign Task Report",
        "",
        f"- Goal: {goal}",
        f"- Page: {result.get('url', 'Unknown')}",
        "",
        "## Result",
        "",
        summary,
    ]
    for table_number, table in enumerate(tables, start=1):
        if not isinstance(table, dict):
            continue
        headers = [str(header).strip() for header in table.get("headers", [])]
        if not headers:
            continue
        rows = table.get("rows", [])
        lines.extend(["", f"## Observed table {table_number}", ""])
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in rows:
            if len(row) != len(headers):
                raise ValueError("A report table row does not match its header column count.")
            cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
            lines.append("| " + " | ".join(cells) + " |")
            normalized_headers = [header.lower() for header in headers]
            amount_index = next(
                (index for index, header in enumerate(normalized_headers) if header == "amount"),
                None,
            )
            invoice_index = next(
                (index for index, header in enumerate(normalized_headers) if "invoice" in header),
                None,
            )
            if amount_index is not None and invoice_index is not None:
                payable_rows.append(cells)
                try:
                    payable_amounts.append(
                        Decimal(str(row[amount_index]).replace("$", "").replace(",", "").strip())
                    )
                except (InvalidOperation, ValueError):
                    raise ValueError("A payable table contains an invalid amount.")

    if payable_amounts:
        amount_list = " + ".join(f"${amount:,.2f}" for amount in payable_amounts)
        total = sum(payable_amounts, Decimal("0"))
        lines.extend(
            [
                "",
                "## Independent calculation",
                "",
                f"{amount_list} = ${total:,.2f}",
                f"Verified payable count: {len(payable_rows)}",
            ]
        )

    if not tables and result.get("activity_items"):
        lines.extend(["", "## Activity entries", ""])
        lines.extend(f"- {item}" for item in result["activity_items"])
    analysis = result.get("table_analysis")
    if isinstance(analysis, dict) and analysis.get("results"):
        lines.extend(["", "## Derived analysis", ""])
        lines.append("```json")
        lines.append(json.dumps(analysis["results"], indent=2, ensure_ascii=False))
        lines.append("```")

    content = "\n".join(lines) + "\n"
    report_path.write_text(content, encoding="utf-8")
    read_back = report_path.read_text(encoding="utf-8")
    if read_back != content:
        raise OSError("The report content did not match after writing.")
    if tables:
        expected_rows = []
        amounts: list[Decimal] = []
        for table in tables:
            if not isinstance(table, dict):
                continue
            headers = [str(header).strip().lower() for header in table.get("headers", [])]
            amount_index = next(
                (index for index, header in enumerate(headers) if header == "amount"),
                None,
            )
            if amount_index is None:
                continue
            for row in table.get("rows", []):
                if len(row) <= amount_index:
                    raise ValueError("The saved report omitted a payable amount cell.")
                expected_rows.append([str(value).strip() for value in row])
                try:
                    amounts.append(
                        Decimal(str(row[amount_index]).replace("$", "").replace(",", "").strip())
                    )
                except (InvalidOperation, ValueError):
                    raise ValueError("The saved report contains an invalid payable amount.")
        for row in expected_rows:
            if "| " + " | ".join(row) + " |" not in read_back:
                raise OSError(f"The saved report is missing observed row: {' | '.join(row)}")
        if amounts:
            expected_total = f"${sum(amounts, Decimal('0')):,.2f}"
            if expected_total not in read_back:
                raise OSError(f"The saved report is missing the verified total {expected_total}.")
    return str(report_path)


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
    if report_path.read_text(encoding="utf-8") != "\n".join(lines) + "\n":
        raise OSError("The observation report content did not match after writing.")
    return str(report_path)


def _is_payables_table_observation(result: dict) -> bool:
    text = str(result.get("text", "")).lower()
    if "payables dashboard" in text and "payables table" in text:
        return True
    return any(
        isinstance(table, dict)
        and {"amount", "due date"}.issubset(
            {str(header).strip().lower() for header in table.get("headers", [])}
        )
        and any("invoice" in str(header).strip().lower() for header in table.get("headers", []))
        for table in result.get("tables", [])
    )


def _goal_invoice_number(goal: str) -> str | None:
    match = re.search(r"\bINV[- ]?\d+\b", goal, re.IGNORECASE)
    return match.group(0).replace(" ", "-").upper() if match else None


def _payables_page_contains_invoice(page: dict, invoice_number: str) -> bool:
    target = invoice_number.strip().upper().replace(" ", "-")
    for table in page.get("tables", []):
        if not isinstance(table, dict):
            continue
        headers = [str(header).strip().lower() for header in table.get("headers", [])]
        invoice_index = next(
            (index for index, header in enumerate(headers) if "invoice" in header),
            None,
        )
        if invoice_index is None:
            continue
        for row in table.get("rows", []):
            if invoice_index < len(row):
                observed = str(row[invoice_index]).strip().upper().replace(" ", "-")
                if observed == target:
                    return True
    return bool(re.search(rf"\b{re.escape(target)}\b", str(page.get("text", "")).upper()))


def _confirmed_payable_submission(result: dict, invoice_number: str | None) -> bool:
    page = result.get("current_page", result)
    if not isinstance(page, dict):
        return False
    if urlsplit(str(page.get("url", ""))).path.rstrip("/") != "/payables":
        return False
    if "payable added successfully" not in str(page.get("text", "")).lower():
        return False
    return invoice_number is not None and _payables_page_contains_invoice(page, invoice_number)


def _payable_post_submission_action(goal: str, memory: dict) -> ToolAction | None:
    if not memory.get("payable_submission_seen"):
        return None
    invoice_number = _goal_invoice_number(goal)
    if not invoice_number:
        return ToolAction(
            tool="request_human_input",
            arguments={"reason": "The payable was submitted, but the goal does not identify an invoice to verify."},
            rationale="Do not claim a payable write is complete without a specific invoice verification target.",
        )
    if not memory.get("payable_verification_attempted"):
        memory["payable_verification_attempted"] = True
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Independently re-read the payables register after the app confirmed the submission.",
        )
    if not memory.get("target_invoice_verified"):
        return ToolAction(
            tool="request_human_input",
            arguments={
                "reason": (
                    f"The app confirmed submission, but a fresh register read did not verify {invoice_number}. "
                    "No automatic resubmission was attempted."
                )
            },
            rationale="Pause after one independent verification attempt instead of risking a duplicate write.",
        )
    if "screenshot" in goal.lower() and not memory.get("screenshot_captured"):
        if memory.get("screenshot_attempted"):
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": "The payable was verified, but the requested screenshot could not be captured."},
                rationale="Report the missing requested evidence instead of repeating screenshot actions.",
            )
        memory["screenshot_attempted"] = True
        return ToolAction(
            tool="take_screenshot",
            arguments={},
            rationale="Capture evidence of the independently verified payable register.",
        )
    return ToolAction(
        tool="finish",
        arguments={"message": f"Verified payable {invoice_number} in the payables register."},
        rationale="The app confirmed the write and a fresh register observation contains the requested invoice.",
    )


def _observation_digest(observation: dict) -> str:
    page = observation.get("current_page", observation)
    if not isinstance(page, dict):
        page = {}
    digestible = {
        "url": page.get("url"),
        "text": page.get("text"),
        "interactive_elements": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "tag": item.get("tag"),
                "value": item.get("value"),
                "filled": item.get("filled"),
            }
            for item in page.get("interactive_elements", [])
            if isinstance(item, dict)
        ],
        "tables": page.get("tables", []),
    }
    encoded = json.dumps(digestible, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _action_digest(action: ToolAction) -> str:
    encoded = json.dumps(
        {"tool": action.tool, "arguments": action.arguments},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _goal_forbids_payable_write(goal: str) -> bool:
    normalized = goal.lower()
    return bool(
        re.search(r"\b(?:do not|don't|never)\s+(?:save|submit|create|add|record|change)\b", normalized)
        or re.search(r"\bwithout\s+(?:saving|submitting|creating|adding|recording)\b", normalized)
        or "read-only" in normalized
        or "do not change any records" in normalized
    )


def _initial_goal_criteria(goal: str) -> dict[str, dict[str, str]]:
    normalized = goal.lower()
    criteria: dict[str, dict[str, str]] = {
        "login": {"status": "pending"},
    }
    if _goal_requests_vendors(goal):
        criteria["vendors_page"] = {"status": "pending"}
    elif "activity" in normalized or "audit trail" in normalized:
        criteria["activity_page"] = {"status": "pending"}
    else:
        criteria["payables_page"] = {"status": "pending"}
    if _goal_requests_task_report(goal) or _goal_requests_observation_report(goal):
        criteria["report"] = {"status": "pending"}
    if any(term in normalized for term in ("total", "sum", "average", "mean", "count")):
        criteria["calculation"] = {"status": "pending"}
    if _goal_requests_latest_invoice(goal):
        criteria["latest_invoice"] = {"status": "pending"}
    if _amount_extreme_direction(goal):
        criteria["amount_extreme"] = {"status": "pending"}
    if _goal_requests_update(goal):
        criteria["update"] = {"status": "pending"}
    if re.search(r"\b(?:add|create|enter|record)\b", normalized) and (
        "payable" in normalized or "invoice" in normalized
    ) and not _goal_forbids_payable_write(goal):
        criteria["approval"] = {"status": "pending"}
    return criteria


def _set_criterion(memory: dict[str, Any], name: str, status: str) -> None:
    criteria = memory.setdefault("criteria", {})
    if isinstance(criteria, dict) and name in criteria:
        criteria[name]["status"] = status


def _is_mutating_browser_action(action: ToolAction) -> bool:
    return action.tool in {"click", "fill", "fill_fields", "navigate", "select_option", "submit_form"}


def _browser_error_recovery_action(observation: dict) -> ToolAction | None:
    if observation.get("status") != "error":
        return None
    page = observation.get("current_page")
    if not isinstance(page, dict) or not page.get("url"):
        return ToolAction(
            tool="request_human_input",
            arguments={
                "reason": (
                    f"The browser action failed and no current page was available for recovery: "
                    f"{observation.get('message', 'unknown browser error')}"
                )
            },
            rationale="Stop safely when the browser cannot provide a page state to recover from.",
        )
    return ToolAction(
        tool="read_page",
        arguments={"selector": "body"},
        rationale="Refresh the actual current page after the browser error before choosing another action.",
    )


def build_registry(
    state_path: Path,
    browser_tools: BrowserToolSet,
    table_context: dict[str, Any] | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    table_context = table_context if table_context is not None else {}
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
            description=(
                "Click an interactive page element using a CSS selector. For payable submission, "
                "use submit_form instead of clicking the save button so the runtime approval gate runs."
            ),
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
            name="analyze_table",
            description=(
                "Run declarative analysis over the latest observed table. Operations support "
                "total, average, minimum, maximum, count, percentage, filter, group_sum, "
                "duplicates, and distinct_rank. Use percentage with amount_column and output; "
                "use filter with operator and threshold such as {'kind':'average'}."
            ),
            input_model=TableAnalysisArgs,
            read_only=True,
            execution=lambda arguments: analyze_table(arguments, table_context["table"]),
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

    def _legacy_action(
        self,
        goal: str,
        recent_observation: dict | str,
        state: RunState,
        available_tools: list,
        browser_tools: BrowserToolSet,
    ) -> ToolAction | None:
        if _goal_requires_generic_analysis(goal):
            action = _login_action(recent_observation, self.settings)
            if action is not None:
                return action
            if state.memory.get("table_analysis"):
                analysis = state.memory["table_analysis"]
                total = next(
                    (item.get("value") for item in analysis.get("results", []) if item.get("kind") == "total"),
                    None,
                )
                average = next(
                    (item.get("value") for item in analysis.get("results", []) if item.get("kind") == "average"),
                    None,
                )
                matching = next(
                    (item.get("rows", []) for item in analysis.get("results", []) if item.get("kind") == "filter"),
                    [],
                )
                if total is not None:
                    state.memory["payables_total"] = str(total)
                if average is not None:
                    state.memory["payables_average"] = str(average)
                if matching:
                    state.memory["payables_average_matching_invoices"] = [
                        row.get("INVOICE NUMBER") for row in matching if row.get("INVOICE NUMBER")
                    ]
                message = "The requested table analysis was completed from observed rows."
                if total is not None and average is not None:
                    rows = state.memory.get("last_table_observation", {}).get("tables", [{}])[0].get("rows", [])
                    amounts = [
                        Decimal(str(row[2]).replace("$", "").replace(",", ""))
                        for row in rows
                        if len(row) > 2
                    ]
                    expression = " + ".join(f"${amount:,.2f}" for amount in amounts)
                    message = (
                        f"Payables inspected (read-only): {len(rows)}. "
                        f"{expression} = ${Decimal(str(total)):,.2f}; "
                        f"${Decimal(str(total)):,.2f} / {len(rows)} = ${Decimal(str(average)):,.2f}. "
                        "No records were changed."
                    )
                if matching and average is not None:
                    details = "; ".join(
                        f"{row.get('INVOICE NUMBER')} ({row.get('VENDOR')}) ${Decimal(str(row.get('AMOUNT')).replace('$', '').replace(',', '')):,.2f}"
                        for row in matching
                    )
                    message += f" Invoices with amount > ${Decimal(str(average)):,.2f}: {details}."
                return ToolAction(
                    tool="finish",
                    arguments={"message": message},
                    rationale="All requested declarative analyses have evidence.",
                )
            if isinstance(recent_observation, dict):
                analysis_action = _generic_analysis_action(goal, recent_observation)
                if analysis_action is not None:
                    return analysis_action
            return _browser_error_recovery_action(recent_observation) or self.model.decide(
                goal,
                json.dumps(recent_observation, ensure_ascii=False),
                state.plan or {},
                available_tools,
                state.memory,
            )
        action = _payable_post_submission_action(goal, state.memory)
        if action is None:
            action = _invoice_payment_action(recent_observation, goal, state.memory)
        if action is None:
            action = _payable_update_action(recent_observation, goal, state.memory)
        if action is None:
            action = _latest_invoice_action(recent_observation, goal, state.memory)
        if action is None:
            action = _highest_amount_action(recent_observation, goal, state.memory)
        if action is None:
            action = _payables_calculation_action(recent_observation, goal, state.memory)
        if action is None:
            action = _payables_count_action(recent_observation, goal, state.memory)
        if action is None:
            action = _payables_inspection_action(recent_observation, goal, state.memory)
        if action is None:
            action = _activity_page_action(recent_observation, goal, state.memory)
        if action is None:
            action = _vendors_page_action(recent_observation, goal, state.memory)
        if action is None:
            action = _local_payables_action(
                recent_observation,
                goal,
                state.plan,
                available_tools,
                state.memory,
            )
        if action is None and isinstance(recent_observation, dict):
            action = _browser_error_recovery_action(recent_observation)
        if action is None:
            action = _login_action(recent_observation, self.settings)
        if action is None:
            action = self.model.decide(
                goal,
                json.dumps(recent_observation, ensure_ascii=False),
                state.plan or {},
                available_tools,
                state.memory,
            )
        return action

    def run(self, goal: str) -> RunState:
        run_id = uuid.uuid4().hex
        run_dir = self.run_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = RunState(run_id=run_id, goal=goal, run_dir=str(run_dir), trace_path=str(run_dir / "trace.jsonl"))
        state.memory["criteria"] = _initial_goal_criteria(goal)
        trace = TraceRecorder(run_dir)
        state_path = run_dir / "state.json"
        browser_tools = BrowserToolSet(
            self.settings.app_origin,
            run_dir / "screenshots",
            headless=self.settings.headless,
            action_delay_ms=self.settings.browser_action_delay_ms,
        )
        table_context: dict[str, Any] = {}
        registry = build_registry(state_path, browser_tools, table_context)
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
        if callable(getattr(self.model, "evaluate_completion", None)):
            state.memory["goal_criteria"] = {
                f"criterion_{index}": {
                    "criterion_id": f"criterion_{index}",
                    "description": criterion,
                    "status": "pending",
                    "evidence": [],
                }
                for index, criterion in enumerate(plan.success_criteria, start=1)
            }
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
                try:
                    action = self._legacy_action(
                        goal,
                        recent_observation,
                        state,
                        available_tools,
                        browser_tools,
                    )
                except Exception as exc:
                    state.status = "failed"
                    state.final_message = f"Action selection failed: {exc}"
                    trace.record({"type": "error", "stage": "decision", "step": step_number, "message": state.final_message})
                    state.persist(state_path)
                    print(f"[CentrAlign] Final status: {state.status}")
                    print(f"[CentrAlign] Error: {state.final_message}")
                    break

            if action.tool == "finish" and _goal_requests_task_report(goal):
                report_source = (
                    dict(state.memory.get("last_table_observation", {}))
                    if isinstance(state.memory.get("last_table_observation"), dict)
                    else (recent_observation if isinstance(recent_observation, dict) else {})
                )
                report_source["table_analysis"] = state.memory.get("table_analysis")
                if not state.memory.get("task_report_verified"):
                    report_error: Exception | None = None
                    report_path: str | None = None
                    attempts = state.memory.get("task_report_attempts", 0)
                    if attempts >= 2:
                        report_error = RuntimeError("The report recovery limit has been exhausted.")
                    for attempt in range(attempts + 1, 3):
                        state.memory["task_report_attempts"] = attempt
                        try:
                            report_path = _write_task_report(
                                report_source,
                                run_dir / "task_report.md",
                                goal,
                                str(action.arguments.get("message", "")),
                            )
                        except (OSError, ValueError) as exc:
                            report_error = exc
                            continue
                        report_error = None
                        break
                    if report_error is not None:
                        action = ToolAction(
                            tool="request_human_input",
                            arguments={
                                "reason": (
                                    f"Could not create and verify the requested report after "
                                    f"{state.memory['task_report_attempts']} bounded attempts: {report_error}"
                                )
                            },
                            rationale="Report generation failed after bounded recovery attempts.",
                        )
                    else:
                        assert report_path is not None
                        state.memory["task_report_path"] = report_path
                        state.memory["task_report_verified"] = True
                        state.memory["task_report_read_back"] = True
                        _set_criterion(state.memory, "report", "completed")
                        state.evidence.append(f"Task report written and verified: {report_path}.")
                        print(f"[CentrAlign] Report verification: read back and verified {report_path}")
                        action = action.model_copy(
                            update={
                                "arguments": {
                                    **action.arguments,
                                    "message": (
                                        f"{action.arguments.get('message', '')}\n"
                                        f"Report saved and verified: {report_path}"
                                    ),
                                }
                            }
                        )

            if action.tool == "click":
                try:
                    if browser_tools.is_payable_submit_click(action.arguments):
                        action = ToolAction(
                            tool="submit_form",
                            arguments={"selector": "form"},
                            rationale=(
                                "Route the payable save through submit_form so the runtime approval gate "
                                "can verify the exact values before writing."
                            ),
                        )
                except Exception as exc:
                    action = ToolAction(
                        tool="request_human_input",
                        arguments={
                            "reason": (
                                "The payable save control could not be safely verified, so it was not clicked. "
                                f"Details: {exc}"
                            )
                        },
                        rationale="Do not click a possible payable submission control when its form cannot be verified.",
                    )

            if action.tool == "submit_form" and _goal_forbids_payable_write(goal):
                try:
                    page_url = browser_tools.current_url() or ""
                except Exception:
                    page_url = ""
                if urlsplit(page_url).path.rstrip("/") in {"/payables", "/payables/new"}:
                    action = ToolAction(
                        tool="request_human_input",
                        arguments={
                            "reason": (
                                "The goal explicitly prohibits saving or changing records, so the payable "
                                "form was not submitted."
                            )
                        },
                        rationale="Preserve the user's read-only/no-write instruction.",
                    )

            recent_state = recent_observation if isinstance(recent_observation, dict) else {}
            if (
                _is_mutating_browser_action(action)
                and state.memory.get("last_mutating_observation_digest")
                == _observation_digest(recent_state)
                and (
                    (
                        state.memory.get("last_mutating_action_digest") == _action_digest(action)
                        and state.memory.get("last_mutating_before_digest")
                        == state.memory.get("last_mutating_observation_digest")
                    )
                    or state.memory.get("stagnant_browser_action_count", 0) >= 2
                )
            ):
                action = ToolAction(
                    tool="request_human_input",
                    arguments={
                        "reason": (
                            "The same browser action was already tried and the observed page did not change. "
                            "I stopped instead of repeating it. Check the visible form fields and report the "
                            "specific control or error that remains."
                        )
                    },
                    rationale="Prevent repeating an action that returned success without observable progress.",
                )

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
                if current_url and urlsplit(current_url).path.rstrip("/") in {"/payables", "/payables/new"}:
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
                    if approval["decision"] in {"approved", "auto_approved"}:
                        _set_criterion(state.memory, "approval", "completed")
                    if approval["decision"] in {"denied", "needs_human"}:
                        _set_criterion(
                            state.memory,
                            "update" if _goal_requests_update(goal) else "approval",
                            "awaiting_human",
                        )
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

            if isinstance(result, dict):
                current_page = result.get("current_page", result)
                if isinstance(current_page, dict):
                    tables = current_page.get("tables")
                    if isinstance(tables, list):
                        table = next(
                            (
                                item for item in tables
                                if isinstance(item, dict)
                                and item.get("headers")
                                and isinstance(item.get("rows"), list)
                            ),
                            None,
                        )
                        if table is not None:
                            table_context["table"] = table
                            state.memory["last_table_observation"] = result
                            if _goal_requires_generic_analysis(goal):
                                state.memory["payables_inspected"] = True
                            if _goal_requires_generic_analysis(goal) and "/payables" in str(result.get("url", "")):
                                state.memory["login_success"] = True

            state.steps.append(
                {"step": step_number, "action": _redact_action(action.model_dump()), "observation": result}
            )
            if action.tool == "analyze_table" and result.get("status") == "ok":
                state.memory["table_analysis"] = result
                state.memory["payables_calculation_completed"] = True
                if any(
                    item.get("kind") == "filter"
                    and item.get("operator") == ">"
                    and isinstance(item.get("threshold"), str)
                    for item in result.get("results", [])
                ):
                    state.memory["payables_average_comparison_verified"] = True
                state.evidence.append(
                    f"Table analysis completed with {len(result.get('results', []))} operations."
                )
            trace.record({"type": "observation", "step": step_number, "result": result})
            print(f"[CentrAlign] Observation: {json.dumps(result, ensure_ascii=False)[:1200]}")
            _log_field_verification(action, result)

            if _is_mutating_browser_action(action):
                result_page = result.get("current_page", result)
                if isinstance(result_page, dict):
                    current_result_digest = _observation_digest(result)
                    previous_result_digest = state.memory.get("last_mutating_observation_digest")
                    if current_result_digest == previous_result_digest:
                        state.memory["stagnant_browser_action_count"] = (
                            state.memory.get("stagnant_browser_action_count", 0) + 1
                        )
                    else:
                        state.memory["stagnant_browser_action_count"] = 0
                    state.memory["last_mutating_action_digest"] = _action_digest(action)
                    recent_state = recent_observation if isinstance(recent_observation, dict) else {}
                    state.memory["last_mutating_before_digest"] = _observation_digest(recent_state)
                    state.memory["last_mutating_observation_digest"] = current_result_digest

            requested_invoice = _goal_invoice_number(goal)
            if (
                not state.memory.get("payable_submission_seen")
                and _confirmed_payable_submission(result, requested_invoice)
            ):
                state.memory["payable_submission_seen"] = True
                approved_details = next(
                    (
                        approval.get("details", {})
                        for approval in reversed(state.approvals)
                        if approval.get("action") == "submit_payable"
                        and approval.get("decision") in {"approved", "auto_approved"}
                    ),
                    {},
                )
                state.write_history.append(
                    {
                        "step": step_number,
                        "tool": action.tool,
                        "details": approved_details,
                        "result": "success",
                    }
                )
                state.evidence.append(
                    f"The app confirmed submission of {requested_invoice}, and it appeared in the payables register."
                )

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
                if _goal_requests_latest_invoice(goal):
                    _set_criterion(state.memory, "latest_invoice", "awaiting_human")
                if _goal_requests_update(goal):
                    _set_criterion(state.memory, "update", "blocked")
                state.status = "waiting_for_human"
                state.final_message = result.get("reason", "Human input required.")
                state.evidence.append(state.final_message)
                break

            if result.get("status") == "error":
                state.memory["last_error"] = result.get("message", "Tool call failed")
                trace.record({"type": "error", "step": step_number, "message": result.get("message")})
                if state.memory.get("payable_submission_seen"):
                    state.persist(state_path)
                continue

            if result.get("status") == "ok" and action.tool == "navigate":
                state.memory["current_url"] = result.get("url")
                state.memory["page_title"] = result.get("title")

            if result.get("status") == "ok" and action.tool == "read_page":
                state.memory["current_url"] = result.get("url")
                state.memory["page_title"] = result.get("title")
                if _is_payables_table_observation(result):
                    state.memory["login_success"] = True
                    state.memory["payables_inspected"] = True
                    _set_criterion(state.memory, "login", "completed")
                    _set_criterion(state.memory, "payables_page", "completed")
                    normalized_goal = goal.lower()
                    if (
                        "payables" in normalized_goal
                        and (
                            re.search(r"\bcount\b", normalized_goal)
                            or "number of payables" in normalized_goal
                            or "how many payables" in normalized_goal
                            or "how many invoices" in normalized_goal
                        )
                    ):
                        state.memory["payables_count_inspected"] = True
                    if (
                        re.search(
                            r"\b(?:count|total|sum|average|mean)\b",
                            goal.lower(),
                        )
                        or "number of payables" in goal.lower()
                        or "how many" in goal.lower()
                    ):
                        state.memory["payables_calculation_inspected"] = True
                        _set_criterion(state.memory, "calculation", "completed")
                    if re.search(
                        r"\b(?:latest|newest|most recent)\b.{0,35}\b(?:invoice|payable)\b",
                        goal.lower(),
                    ):
                        state.memory["latest_invoice_inspected"] = True
                    if re.search(r"\b(?:update|edit|modify|change)\b", goal.lower()) and re.search(
                        r"\b(?:payable|invoice|record)\b", goal.lower()
                    ):
                        state.memory["payables_update_inspected"] = True
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
                        state.memory["task_report_path"] = report_path
                        state.memory["task_report_verified"] = True
                        state.memory["task_report_read_back"] = True
                        _set_criterion(state.memory, "report", "completed")
                        state.evidence.append(f"Observation report written to {report_path}.")
                        print(f"[CentrAlign] Observation report: {report_path}")
                    if state.memory.get("payables_calculation_inspected"):
                        state.evidence.append("Payable rows and amounts were captured from the rendered table.")
                    if state.memory.get("highest_amount_inspected"):
                        _set_criterion(state.memory, "highest_amount", "completed")
                    if state.memory.get("amount_extreme_inspected"):
                        _set_criterion(state.memory, "amount_extreme", "completed")
                    target_invoice = _goal_invoice_number(goal)
                    if target_invoice and _payables_page_contains_invoice(result, target_invoice):
                        state.memory["target_invoice_verified"] = True
                        state.evidence.append(
                            f"Verified {target_invoice} in the rendered payables table."
                        )
                        if (
                            state.memory.get("payable_submission_seen")
                            and "screenshot" not in goal.lower()
                        ):
                            state.memory["finish_requested"] = True
                            state.status = "finished"
                            state.final_message = (
                                f"Verified payable {target_invoice} in the payables register."
                            )
                            state.persist(state_path)
                            break
                elif urlsplit(str(result.get("url", ""))).path.rstrip("/") == "/activity":
                    state.memory["login_success"] = True
                    state.memory["activity_inspected"] = True
                    _set_criterion(state.memory, "login", "completed")
                    _set_criterion(state.memory, "activity_page", "completed")
                    state.memory["activity_items"] = result.get("activity_items", [])
                    state.evidence.append(
                        f"Observed {len(state.memory['activity_items'])} recent activity entries at {result.get('url')}."
                    )
                elif urlsplit(str(result.get("url", ""))).path.rstrip("/") == "/vendors":
                    state.memory["login_success"] = True
                    state.memory["vendors_inspected"] = True
                    _set_criterion(state.memory, "login", "completed")
                    _set_criterion(state.memory, "vendors_page", "completed")
                    vendor_table = next(
                        (
                            table for table in result.get("tables", [])
                            if isinstance(table, dict)
                            and any("vendor" in str(header).lower() for header in table.get("headers", []))
                        ),
                        None,
                    )
                    state.memory["vendor_items"] = (
                        [
                            str(row[0]).strip()
                            for row in vendor_table.get("rows", [])
                            if row and str(row[0]).strip()
                        ]
                        if vendor_table else []
                    )
                    state.evidence.append(
                        f"Observed the vendor directory at {result.get('url')} with "
                        f"{len(state.memory['vendor_items'])} vendor rows."
                    )

            if result.get("status") == "ok" and action.tool == "click":
                state.memory["current_url"] = result.get("url")
                state.memory["page_title"] = result.get("title")

            if result.get("status") == "ok" and action.tool == "read_file":
                state.memory["last_file_content"] = result.get("content")
                if "invoice" in goal.lower() and "INV-" in str(result.get("content") or ""):
                    state.memory["latest_invoice_found"] = True

            if result.get("status") == "ok" and action.tool == "take_screenshot":
                state.memory["screenshot_captured"] = True
                state.evidence.append(f"Screenshot captured: {result.get('path')}")
                if (
                    state.memory.get("payable_submission_seen")
                    and state.memory.get("target_invoice_verified")
                    and "screenshot" in goal.lower()
                ):
                    invoice_number = _goal_invoice_number(goal)
                    state.memory["finish_requested"] = True
                    state.status = "finished"
                    state.final_message = (
                        f"Verified payable {invoice_number} in the payables register and captured "
                        f"the requested screenshot at {result.get('path')}."
                    )
                    state.persist(state_path)
                    break

            state.persist(state_path)

        browser_tools.close()
        if state.memory.get("finish_requested") and state.status != "waiting_for_human":
            goal_criteria = list(state.memory.get("goal_criteria", {}).values())
            try:
                evaluation = self.model.evaluate_completion(
                    goal,
                    goal_criteria,
                    state.evidence,
                    state.__dict__,
                )
                for item in evaluation.criteria:
                    criterion = state.memory["goal_criteria"].get(item.criterion_id)
                    if criterion is not None:
                        criterion["status"] = item.status
                        criterion["evidence"] = item.evidence
                        criterion["reason"] = item.reason
                state.memory["completion_evaluation"] = evaluation.model_dump()
                state.evidence.append(
                    f"Completion evaluation: {evaluation.summary or 'criteria evaluated independently.'}"
                )
            except Exception as exc:
                state.memory["completion_evaluation_error"] = str(exc)
                state.evidence.append(f"Completion evaluation failed: {exc}")
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
