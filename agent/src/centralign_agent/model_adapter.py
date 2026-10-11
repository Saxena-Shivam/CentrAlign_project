from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from centralign_agent.config import get_settings

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

        if observation.get("status") == "error":
            reason = observation.get("message", "The last tool action failed.")
            return ToolAction(
                tool="request_human_input",
                arguments={"reason": f"{reason} Inspect the error and current page before continuing."},
                rationale="Pause for human help instead of repeating a failed browser action.",
            )

        page = observation.get("current_page", observation)
        page_text = str(page.get("text", "")).lower() if isinstance(page, dict) else ""
        elements = page.get("interactive_elements", []) if isinstance(page, dict) else []
        if not isinstance(page, dict):
            page = {}

        if "payables dashboard" in page_text or ("payables table" in page_text and "invoice" in page_text):
            options = next(
                (
                    item.get("options", [])
                    for item in elements
                    if isinstance(item, dict) and self._field_key(item) == "vendor"
                ),
                [],
            )
            details = self._payable_details(goal, options)
            if any(word in goal.lower() for word in ("add", "create", "enter", "record")) and details is None:
                return ToolAction(
                    tool="request_human_input",
                    arguments={
                        "reason": (
                            "I can create a payable from a goal when it includes a listed vendor, invoice number, "
                            "amount, and due date in YYYY-MM-DD format. Please provide any missing or unsupported details."
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
                if vendor_field.get("value") != details["vendor"]:
                    return ToolAction(
                        tool="select_option",
                        arguments={"selector": f'[name="{vendor_field["name"]}"]', "value": details["vendor"]},
                        rationale=f"Select the requested vendor {details['vendor']} from the visible options.",
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
                        continue
                    if str(field.get("value", "")) != details[field_name]:
                        if "fill_fields" in tool_names:
                            fields_to_fill = {}
                            for target_name in ("invoice_number", "amount", "due_date"):
                                target_field = next(
                                    (
                                        item
                                        for item in elements
                                        if isinstance(item, dict) and self._field_key(item) == target_name
                                    ),
                                    None,
                                )
                                if target_field and str(target_field.get("value", "")) != details[target_name]:
                                    fields_to_fill[f'[name="{target_field["name"]}"]'] = details[target_name]
                            return ToolAction(
                                tool="fill_fields",
                                arguments={"fields": fields_to_fill},
                                rationale="Fill the requested values into their currently observed form fields.",
                            )
                        return ToolAction(
                            tool="fill",
                            arguments={"selector": f'[name="{field["name"]}"]', "value": details[field_name]},
                            rationale=f"Fill the observed {field_name.replace('_', ' ')} field with the goal value.",
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
                (
                    "system",
                    "Choose exactly one tool from the registry using the newest observation, goal, plan, and memory. "
                    "Provide arguments that match the selected tool's schema. Never invent missing user data. "
                    "Use request_human_input when required information is missing or a safe action is unclear. "
                    "Never use finish until the requested outcome is supported by observable evidence. "
                    "Do not repeat a successful write based on stale page state. "
                    "Browser navigation is restricted to the exact allowed_app_origin in the user payload. "
                    "On the first turn, navigate to allowed_app_origin + '/login'. "
                    "Never invent a URL, use a placeholder such as 'accounting_system_url', "
                    "or navigate outside the allowed origin.",
                ),
                ("user", json.dumps(payload)),
            ]
        )
        return response if isinstance(response, ToolAction) else ToolAction.model_validate(response)
