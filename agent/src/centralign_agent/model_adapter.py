from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from centralign_agent.config import get_settings
from centralign_agent.prompts import SYSTEM_PROMPT

try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover
    ChatOpenAI = None


class ToolAction(BaseModel):
    tool: str = Field(..., description="Tool name to execute")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments")
    rationale: str = Field(..., description="Short reason for choosing this tool")


class PlanResponse(BaseModel):
    brief_plan: list[str] = Field(..., min_length=1)
    success_criteria: list[str] = Field(..., min_length=1)

    @field_validator("brief_plan", "success_criteria", mode="before")
    @classmethod
    def normalize_text(cls, value):
        if isinstance(value, str):
            lines = [line.strip(" -*\t") for line in value.splitlines() if line.strip(" -*\t")]
            return lines or [value.strip()]
        return value


class ModelAdapter:
    def plan(self, goal: str, history: list[str] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def decide(
        self,
        goal: str,
        recent_observation: str,
        plan: dict[str, Any],
        available_tools: list[str] | list[dict[str, Any]],
        memory: dict[str, Any],
    ) -> ToolAction:
        raise NotImplementedError


class MockModelAdapter(ModelAdapter):
    _PAYABLE_LABELS = {
        "vendor": "vendor",
        "invoice number": "invoice_number",
        "amount": "amount",
        "amount (usd)": "amount",
        "due date": "due_date",
        "approval code": "approval_code",
    }

    @staticmethod
    def _tool_names(available_tools: list[str] | list[dict[str, Any]]) -> set[str]:
        return {
            tool if isinstance(tool, str) else str(tool.get("name", ""))
            for tool in available_tools
        }

    @classmethod
    def _field_key(cls, element: dict[str, Any]) -> str | None:
        name = str(element.get("name", "")).lower()
        if name in {"vendor", "invoice_number", "amount", "due_date", "approval_code"}:
            return name
        label = re.sub(r"\s+", " ", str(element.get("label", "")).strip().lower())
        return cls._PAYABLE_LABELS.get(label)

    @staticmethod
    def _payable_details(goal: str, vendor_options: list[dict[str, Any]]) -> dict[str, str] | None:
        normalized = goal.lower()
        if not any(word in normalized for word in ("add", "create", "enter", "record")):
            return None
        if "payable" not in normalized and "invoice" not in normalized:
            return None

        invoice = re.search(r"\bINV[- ]?\d+\b", goal, re.IGNORECASE)
        amount = re.search(
            r"(?:\bamount\b\s*(?:of|is|:)?\s*\$?|\$)\s*([\d,]+(?:\.\d{1,2})?)",
            goal,
            re.IGNORECASE,
        )
        due_date = re.search(
            r"\b(?:due(?:\s+date)?|date)\b[^\d]*(\d{4}-\d{2}-\d{2})",
            goal,
            re.IGNORECASE,
        )
        vendor = next(
            (
                str(option["value"])
                for option in vendor_options
                if option.get("value")
                and re.search(
                    rf"\b{re.escape(str(option['value']))}\b",
                    goal,
                    re.IGNORECASE,
                )
            ),
            None,
        )
        if not (vendor and invoice and amount and due_date):
            return None
        return {
            "vendor": vendor,
            "invoice_number": invoice.group(0).replace(" ", "-").upper(),
            "amount": amount.group(1).replace(",", ""),
            "due_date": due_date.group(1),
        }

    def plan(self, goal: str, history: list[str] | None = None) -> dict[str, Any]:
        return {
            "brief_plan": [
                "Inspect the app state and relevant files.",
                "Act on the most relevant visible task.",
                "Verify final results before declaring completion.",
            ],
            "success_criteria": [
                f"The goal '{goal}' is completed and visible in the app or evidence.",
                "The final result is checked independently before completion.",
            ],
        }

    def decide(
        self,
        goal: str,
        recent_observation: str,
        plan: dict[str, Any],
        available_tools: list[str] | list[dict[str, Any]],
        memory: dict[str, Any],
    ) -> ToolAction:
        tool_names = self._tool_names(available_tools)
        try:
            observation = json.loads(recent_observation)
        except (TypeError, json.JSONDecodeError):
            observation = {}

        if not observation or observation == "No observations yet.":
            return ToolAction(
                tool="navigate",
                arguments={"url": f"{get_settings().app_origin}/login"},
                rationale="Open the configured company app login page.",
            )

        if not isinstance(observation, dict):
            observation = {}

        error_page = observation.get("current_page")
        recoverable_payable_error = (
            isinstance(error_page, dict)
            and str(error_page.get("url", "")).rstrip("/").endswith("/payables/new")
            and re.search(r"\b(?:add|create|enter|record)\b", goal, re.IGNORECASE)
            and not re.search(
                r"\b(?:do not|don't|never)\s+(?:save|submit|create|add|record|change)\b",
                goal,
                re.IGNORECASE,
            )
        )
        if observation.get("status") == "error" and not recoverable_payable_error:
            reason = observation.get("message", "The last tool action failed.")
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": f"{reason} Inspect the error and current page before continuing."},
                rationale="Pause for human help instead of repeating a failed browser action.",
            )

        page = error_page if recoverable_payable_error else observation.get("current_page", observation)
        page_text = str(page.get("text", "")).lower() if isinstance(page, dict) else ""
        elements = page.get("interactive_elements", []) if isinstance(page, dict) else []
        if not isinstance(page, dict):
            page = {}

        page_url = str(page.get("url", "")).rstrip("/")
        is_payables_page = page_url.endswith("/payables") or page_url.endswith("/payables/new")
        has_payables_table = any(
            isinstance(table, dict)
            and {"invoice number", "amount"}.issubset(
                {str(header).strip().lower() for header in table.get("headers", [])}
            )
            for table in page.get("tables", [])
        )
        if is_payables_page and (
            "payables dashboard" in page_text
            or "add payable" in page_text
            or "payables table" in page_text
            or has_payables_table
        ):
            is_create_goal = any(word in goal.lower() for word in ("add", "create", "enter", "record"))
            has_vendor_field = any(
                isinstance(item, dict) and self._field_key(item) == "vendor"
                for item in elements
            )
            requested_invoice = re.search(r"\bINV[- ]?\d+\b", goal, re.IGNORECASE)
            if is_create_goal and memory.get("payable_submission_seen") and requested_invoice:
                invoice_number = requested_invoice.group(0).replace(" ", "-").upper()
                if invoice_number not in str(page.get("text", "")).upper():
                    return ToolAction(
                        tool="request_human_input",
                        arguments={
                            "reason": (
                                f"The submitted invoice {invoice_number} is not visible in the current payable register. "
                                "I will not retry the write automatically."
                            )
                        },
                        rationale="Stop after a successful submission response if independent verification is missing.",
                    )
                if not memory.get("target_invoice_verified"):
                    return ToolAction(
                        tool="read_page",
                        arguments={"selector": "body"},
                        rationale="Re-read the payable register to independently verify the submitted invoice.",
                    )
                if "screenshot" in goal.lower() and not memory.get("screenshot_captured"):
                    return ToolAction(
                        tool="take_screenshot",
                        arguments={},
                        rationale="Capture evidence of the independently verified payable register.",
                    )
                return ToolAction(
                    tool="finish",
                    arguments={"message": f"Verified payable {invoice_number} in the payables register."},
                    rationale="The requested invoice appears in a fresh observation after submission.",
                )

            if (
                is_create_goal
                and not memory.get("payable_submission_seen")
                and requested_invoice
                and requested_invoice.group(0).replace(" ", "-").upper()
                in str(page.get("text", "")).upper()
            ):
                return ToolAction(
                    tool="request_human_input",
                    arguments={
                        "reason": (
                            f"{requested_invoice.group(0).replace(' ', '-').upper()} already appears in the payable register. "
                            "I will not create a duplicate."
                        )
                    },
                    rationale="The requested invoice is already listed, so creating another would be a duplicate.",
                )

            if is_create_goal and page_url.endswith("/payables") and not has_vendor_field:
                return ToolAction(
                    tool="navigate",
                    arguments={"url": f"{get_settings().app_origin}/payables/new"},
                    rationale="Open the dedicated payable form to inspect its current fields and vendor suggestions.",
                )

            vendor_field = next(
                (
                    item
                    for item in elements
                    if isinstance(item, dict) and self._field_key(item) == "vendor"
                ),
                None,
            )
            options = vendor_field.get("options", []) if vendor_field else []
            if not options and vendor_field:
                approved_vendors = re.search(
                    r"approved vendors?:\s*(.+?)(?:\.|$)",
                    str(vendor_field.get("description", "")),
                    re.IGNORECASE,
                )
                if approved_vendors:
                    options = [
                        {"value": name.strip()}
                        for name in approved_vendors.group(1).split(",")
                        if name.strip()
                    ]
            options = options or next(
                (
                    item.get("options", [])
                    for item in elements
                    if isinstance(item, dict) and self._field_key(item) == "vendor"
                ),
                [],
            )
            details = self._payable_details(goal, options)
            if is_create_goal and details is None:
                missing_details = []
                if not any(
                    option.get("value")
                    and re.search(rf"\b{re.escape(str(option['value']))}\b", goal, re.IGNORECASE)
                    for option in options
                ):
                    missing_details.append("a listed vendor")
                if not re.search(r"\bINV[- ]?\d+\b", goal, re.IGNORECASE):
                    missing_details.append("an invoice number")
                if not re.search(r"(?:\bamount\b\s*(?:of|is|:)?\s*\$?|\$)\s*([\d,]+(?:\.\d{1,2})?)", goal, re.IGNORECASE):
                    missing_details.append("an amount")
                if not re.search(r"\b(?:due(?:\s+date)?|date)\b[^\d]*(\d{4}-\d{2}-\d{2})", goal, re.IGNORECASE):
                    missing_details.append("a due date")
                return ToolAction(
                    tool="request_human_input",
                    arguments={
                        "reason": (
                            f"Missing payable details: {', '.join(missing_details) or 'a valid listed vendor or complete values'}. "
                            "Please provide the missing information; nothing has been saved."
                        )
                    },
                    rationale="Required payable details cannot be safely inferred from the goal.",
                )

            target_invoice = details["invoice_number"] if details else None
            if target_invoice and target_invoice in str(page.get("text", "")):
                if not memory.get("payable_submission_seen"):
                    return ToolAction(
                        tool="request_human_input",
                        arguments={
                            "reason": (
                                f"{target_invoice} is already visible in the payables table. "
                                "I will not create a duplicate payable."
                            )
                        },
                        rationale="The requested invoice already exists, so adding it would violate the app's duplicate rule.",
                    )
                if not memory.get("target_invoice_verified"):
                    return ToolAction(
                        tool="read_page",
                        arguments={"selector": "body"},
                        rationale="Re-read the payable table independently to verify the requested invoice exists.",
                    )
                if "screenshot" in goal.lower() and not memory.get("screenshot_captured"):
                    return ToolAction(
                        tool="take_screenshot",
                        arguments={},
                        rationale="Capture the verified payable table as requested evidence.",
                    )
                return ToolAction(
                    tool="finish",
                    arguments={"message": f"Verified payable {target_invoice} for {details['vendor']} in the payables table."},
                    rationale="The requested invoice is visible in a fresh payables-table observation.",
                )

            if details:
                vendor_field = next(
                    (
                        item for item in elements
                        if isinstance(item, dict) and self._field_key(item) == "vendor"
                    ),
                    None,
                )
                if vendor_field is None:
                    return ToolAction(
                        tool="request_human_input",
                        arguments={"reason": "The payable form has no visible vendor field; I cannot proceed safely."},
                        rationale="The observed form does not expose the required field.",
                    )
                fields_to_fill = {}
                if vendor_field.get("value") != details["vendor"]:
                    selector = (
                        f'[name="{vendor_field["name"]}"]'
                        if vendor_field.get("name")
                        else f'#{vendor_field["id"]}'
                        if vendor_field.get("id")
                        else None
                    )
                    if selector is None:
                        return ToolAction(
                            tool="request_human_input",
                            arguments={"reason": "The visible vendor field has no safe selector."},
                            rationale="Do not guess how to target the payable vendor field.",
                        )
                    if str(vendor_field.get("tag", "")).lower() == "select":
                        return ToolAction(
                            tool="select_option",
                            arguments={"selector": selector, "value": details["vendor"]},
                            rationale=f"Select the requested vendor {details['vendor']} from the visible options.",
                        )
                    if "fill_fields" in tool_names:
                        fields_to_fill[selector] = details["vendor"]
                    else:
                        return ToolAction(
                            tool="fill",
                            arguments={"selector": selector, "value": details["vendor"]},
                            rationale="Enter the requested vendor into the observed text field.",
                        )

                for field_name in ("invoice_number", "amount", "due_date"):
                    field = next(
                        (
                            item for item in elements
                            if isinstance(item, dict) and self._field_key(item) == field_name
                        ),
                        None,
                    )
                    if field is None:
                        return ToolAction(
                            tool="request_human_input",
                            arguments={
                                "reason": (
                                    f"The payable form does not expose a visible "
                                    f"{field_name.replace('_', ' ')} field."
                                )
                            },
                            rationale="Pause rather than submit without a required payable field.",
                        )
                    if str(field.get("value", "")) != details[field_name]:
                        selector = (
                            f'[name="{field["name"]}"]'
                            if field.get("name")
                            else f'#{field["id"]}'
                            if field.get("id")
                            else None
                        )
                        if selector is None:
                            return ToolAction(
                                tool="request_human_input",
                                arguments={
                                    "reason": (
                                        f"The visible {field_name.replace('_', ' ')} field "
                                        "has no safe selector."
                                    )
                                },
                                rationale="Do not guess selectors while preparing a payable submission.",
                            )
                        if "fill_fields" in tool_names:
                            fields_to_fill[selector] = details[field_name]
                        else:
                            return ToolAction(
                                tool="fill",
                                arguments={"selector": selector, "value": details[field_name]},
                                rationale=f"Fill the observed {field_name.replace('_', ' ')} field with the goal value.",
                            )

                if fields_to_fill:
                    return ToolAction(
                        tool="fill_fields",
                        arguments={"fields": fields_to_fill},
                        rationale="Fill all requested payable values into their currently observed form fields.",
                    )

                required_unfilled = [
                    item for item in elements
                    if isinstance(item, dict)
                    and item.get("required")
                    and not item.get("filled")
                    and self._field_key(item) not in {"vendor", "invoice_number", "amount", "due_date"}
                ]
                if required_unfilled:
                    return ToolAction(
                        tool="request_human_input",
                        arguments={
                            "reason": (
                                f"The visible form requires {required_unfilled[0].get('label') or required_unfilled[0].get('name')}, "
                                "but the goal does not provide it."
                            )
                        },
                        rationale="A required field has no value in the goal, so I will not invent one.",
                    )
                return ToolAction(
                    tool="submit_form",
                    arguments={"selector": "form"},
                    rationale="Submit the payable after all requested visible fields have been filled.",
                )

            if "read_page" in tool_names and not memory.get("payables_inspected"):
                return ToolAction(
                    tool="read_page",
                    arguments={"selector": "body"},
                    rationale="Inspect the rendered payables list and collect visible evidence.",
                )
            if "screenshot" in goal.lower() and not memory.get("screenshot_captured"):
                return ToolAction(
                    tool="take_screenshot",
                    arguments={},
                    rationale="Capture the visible payables dashboard as requested evidence.",
                )
            return ToolAction(
                tool="finish",
                arguments={"message": "Logged in and inspected the payables list."},
                rationale="The payables dashboard and list are visible in the latest page observation.",
            )

        if "/login" in str(page.get("url", "")) or any(
            item.get("name") == "username" for item in elements if isinstance(item, dict)
        ):
            by_name = {
                item.get("name"): item
                for item in elements
                if isinstance(item, dict) and item.get("name")
            }
            username = by_name.get("username")
            password = by_name.get("password")
            if username is not None and not username.get("value"):
                if password is not None and "fill_fields" in tool_names:
                    fields_to_fill = {
                        f'[name="{username["name"]}"]': get_settings().demo_username
                    }
                    if not password.get("filled", bool(password.get("value"))):
                        fields_to_fill[f'[name="{password["name"]}"]'] = get_settings().demo_password
                    return ToolAction(
                        tool="fill_fields",
                        arguments={"fields": fields_to_fill},
                        rationale="Fill the visible login form using configured demo credentials.",
                    )
                return ToolAction(
                    tool="fill",
                    arguments={"selector": f'[name="{username["name"]}"]', "value": get_settings().demo_username},
                    rationale="Enter the configured demo username in the visible login form.",
                )
            if password is not None and not password.get("filled", bool(password.get("value"))):
                return ToolAction(
                    tool="fill",
                    arguments={"selector": f'[name="{password["name"]}"]', "value": get_settings().demo_password},
                    rationale="Enter the configured demo password in the visible login form.",
                )
            return ToolAction(
                tool="submit_form",
                arguments={"selector": "form"},
                rationale="Submit the login form after both credential fields are populated.",
            )

        if not page_text and "read_file" in tool_names and "invoice" in goal.lower():
            return ToolAction(tool="read_file", arguments={"path": "latest_invoice.txt", "kind": "plain"}, rationale="Read the relevant invoice file to locate the target payable data.")
        return ToolAction(
            tool="read_page",
            arguments={"selector": "body"},
            rationale="Refresh the current page observation before choosing another action.",
        )


class OpenAIModelAdapter(ModelAdapter):
    def __init__(self, model_name: str, api_key: str, base_url: str | None = None):
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.client = None
        if ChatOpenAI is None:
            raise RuntimeError("langchain-openai is required to use an LLM provider.")
        self.client = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0,
        )

    def plan(self, goal: str, history: list[str] | None = None) -> dict[str, Any]:
        response = self.client.with_structured_output(PlanResponse, method="function_calling").invoke(
            [
                ("system", "Create a short plan and observable success criteria. Do not provide hidden reasoning."),
                ("user", json.dumps({"goal": goal, "history": history or []})),
            ]
        )
        return response.model_dump() if isinstance(response, PlanResponse) else PlanResponse.model_validate(response).model_dump()

    def decide(
        self,
        goal: str,
        recent_observation: str,
        plan: dict[str, Any],
        available_tools: list[str] | list[dict[str, Any]],
        memory: dict[str, Any],
    ) -> ToolAction:
        payload = {
            "goal": goal,
            "plan": plan,
            "memory": memory,
            "recent_observation": recent_observation,
            "tools": available_tools,
            "allowed_app_origin": get_settings().app_origin,
        }
        response = self.client.with_structured_output(ToolAction, method="function_calling").invoke(
            [
                ("system", SYSTEM_PROMPT.format(allowed_app_origin=get_settings().app_origin)),
                ("user", json.dumps(payload)),
            ]
        )
        return response if isinstance(response, ToolAction) else ToolAction.model_validate(response)
