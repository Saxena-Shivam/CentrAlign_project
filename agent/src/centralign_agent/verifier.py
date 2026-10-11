from __future__ import annotations

import re
from typing import Any


class Verifier:
    def verify(self, goal: str, run_state: dict[str, Any]) -> tuple[bool, str]:
        memory = run_state.get("memory", {})
        evidence = run_state.get("evidence", [])
        if not evidence:
            return False, "No evidence was collected for the task."

        normalized = goal.lower()
        if re.search(r"\b(?:add|create|enter|record)\b", normalized) and (
            "payable" in normalized or "invoice" in normalized
        ):
            if memory.get("payable_submission_seen") and memory.get("target_invoice_verified"):
                return True, "The app confirmed the payable submission and the invoice was found in a fresh table observation."
            return False, "The payable was not both confirmed by the app and independently found in the table."

        if re.search(r"\blog\s*in\b", normalized) and any(
            word in normalized for word in ("inspect", "view", "check")
        ):
            if memory.get("login_success") and memory.get("payables_inspected"):
                return True, "Login succeeded and the rendered payables table was independently inspected."
            return False, "Login and the payables table were not both verified."

        if "invoice" in normalized and "latest" in normalized:
            if memory.get("latest_invoice_found"):
                return True, "Latest invoice was discovered and recorded in the run evidence."
            return False, "The task requires a latest invoice, but no matching invoice was found."

        if "login" in normalized or "app" in normalized:
            if memory.get("login_success"):
                return True, "The login-related task was verified through the app state and evidence."

        return True, "The task appears complete based on collected evidence."
