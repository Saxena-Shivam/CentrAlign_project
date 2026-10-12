from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class Verifier:
    def verify(self, goal: str, run_state: dict[str, Any]) -> tuple[bool, str]:
        memory = run_state.get("memory", {})
        evidence = run_state.get("evidence", [])
        if not evidence:
            return False, "No evidence was collected for the task."

        normalized = goal.lower()
        asks_for_report = bool(
            re.search(r"\b(?:write|save|create|generate|export)\b", normalized)
            and any(term in normalized for term in ("report", "markdown", "observation"))
        )
        if asks_for_report:
            report_path = memory.get("task_report_path")
            report_file = Path(report_path) if report_path else None
            if report_file and not report_file.is_absolute():
                candidates = [Path.cwd() / report_file]
                run_dir = run_state.get("run_dir")
                if run_dir:
                    candidates.append(Path(run_dir) / report_file)
                report_file = next(
                    (candidate for candidate in candidates if candidate.is_file()),
                    candidates[0],
                )
            if (
                not memory.get("task_report_verified")
                or not memory.get("task_report_read_back")
                or not report_path
                or not report_file
                or not report_file.is_file()
            ):
                return False, "The requested report was not written and verified."

        if re.search(r"\b(?:add|create|enter|record)\b", normalized) and (
            "payable" in normalized or "invoice" in normalized
        ) and not _goal_forbids_payable_write(normalized):
            if memory.get("payable_submission_seen") and memory.get("target_invoice_verified"):
                return True, "The app confirmed the payable submission and the invoice was found in a fresh table observation."
            return False, "The payable was not both confirmed by the app and independently found in the table."

        if "activity" in normalized or "audit trail" in normalized:
            if memory.get("login_success") and memory.get("activity_inspected"):
                return True, "Login succeeded and the visible activity entries were independently inspected."
            return False, "The Activity page and its entries were not verified."

        if (
            "vendor directory" in normalized
            or "vendors page" in normalized
            or re.search(r"\b(?:inspect|review|show|list)\s+(?:the\s+)?vendors\b", normalized)
        ):
            if memory.get("login_success") and memory.get("vendors_inspected"):
                return True, "Login succeeded and the visible vendor directory was independently inspected."
            return False, "The requested Vendors page and directory contents were not verified."

        if re.search(r"\b(?:latest|newest|most recent)\b.{0,35}\binvoice\b", normalized):
            if memory.get("latest_invoice_found"):
                return True, "The requested latest invoice was selected from the verified visible register."
            return False, "The latest invoice was not identified using a confirmed date definition."

        if re.search(r"\b(?:highest|largest|maximum|max)\b", normalized) and re.search(
            r"\b(?:amount|invoice|payable)\b", normalized
        ):
            invoices = memory.get("highest_amount_invoices")
            if memory.get("highest_amount_verified") and isinstance(invoices, list) and invoices:
                tie_message = (
                    f"{len(invoices)} invoices were tied"
                    if len(invoices) > 1
                    else "The highest-amount invoice was identified"
                )
                return True, f"{tie_message} from the verified visible payables register."
            return False, "The highest amount and all invoices tied for it were not verified from observed rows."

        if re.search(r"\b(?:smallest|lowest|minimum|min)\b", normalized) and re.search(
            r"\b(?:amount|invoice|payable)\b", normalized
        ):
            invoices = memory.get("amount_extreme_invoices")
            if (
                memory.get("amount_extreme_verified")
                and memory.get("amount_extreme_direction") == "smallest"
                and isinstance(invoices, list)
                and invoices
            ):
                tie_message = (
                    f"{len(invoices)} invoices were tied"
                    if len(invoices) > 1
                    else "The smallest-amount invoice was identified"
                )
                return True, f"{tie_message} from the verified visible payables register."
            return False, "The smallest amount and its invoice details were not verified from observed rows."

        if re.search(r"\b(?:mark|set)\b.{0,20}\b(?:paid|payment)\b", normalized):
            proposal = memory.get("payment_proposal")
            if proposal and run_state.get("status") == "waiting_for_human":
                return True, (
                    f"Approval was requested for marking {proposal.get('invoice_number')} as paid; "
                    "no financial record was changed because the operation is not available."
                )
            return False, "The proposed payment action was not presented for explicit approval."

        if (
            re.search(r"\b(?:update|edit|modify|change)\b", normalized)
            and re.search(r"\b(?:payable|invoice|record)\b", normalized)
            and not _goal_forbids_payable_write(normalized)
        ):
            if memory.get("payable_update_verified"):
                return True, "The requested payable update was approved and independently verified."
            return False, "No payable update was approved and independently verified."

        if any(term in normalized for term in ("total", "sum", "average", "mean")) and any(
            term in normalized for term in ("payable", "invoice")
        ):
            if re.search(
                r"\b(?:greater|more|above|over|higher)\b.{0,30}\b(?:average|mean)\b"
                r"|\b(?:average|mean)\b.{0,30}\b(?:greater|more|above|over|higher)\b",
                normalized,
            ):
                if memory.get("payables_average_comparison_verified"):
                    return True, "The average and strict greater-than invoice comparison were derived from observed rows."
                return False, "The invoices strictly greater than the observed average were not verified."
            if memory.get("payables_calculation_completed") or (
                memory.get("payables_inspected") and memory.get("finish_requested")
            ):
                return True, "The requested calculation was derived from the visible payables table."
            return False, "The requested payable calculation was not completed from observed rows."

        goal_criteria = memory.get("goal_criteria")
        if isinstance(goal_criteria, dict) and goal_criteria:
            unresolved = [
                item.get("description", item.get("criterion_id", key))
                for key, item in goal_criteria.items()
                if item.get("status") != "completed"
            ]
            if unresolved:
                return False, (
                    "Mandatory goal criteria remain unresolved: "
                    + "; ".join(str(item) for item in unresolved)
                )
            return True, "All LLM-derived goal criteria were completed and evaluated against run evidence."

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


def _goal_forbids_payable_write(normalized_goal: str) -> bool:
    return bool(
        re.search(
            r"\b(?:do not|don't|never)\s+(?:save|submit|create|add|record|change|update|edit|modify)\b",
            normalized_goal,
        )
        or re.search(r"\bwithout\s+(?:saving|submitting|creating|adding|recording|changing|updating|editing|modifying)\b", normalized_goal)
        or "read-only" in normalized_goal
    )
