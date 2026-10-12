from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RememberArgs(BaseModel):
    key: str = Field(..., min_length=1)
    value: Any


class RecallArgs(BaseModel):
    key: str = Field(..., min_length=1)


class TableAnalysisArgs(BaseModel):
    operations: list[dict[str, Any]] = Field(..., min_length=1)


def analyze_table(arguments: dict[str, Any], table: dict[str, Any]) -> dict[str, Any]:
    args = TableAnalysisArgs.model_validate(arguments)
    headers = [str(header).strip() for header in table.get("headers", [])]
    rows = table.get("rows", [])
    if not headers or not isinstance(rows, list):
        raise ValueError("No complete observed table is available for analysis.")
    index = {header.lower(): position for position, header in enumerate(headers)}

    def column(name: str) -> int:
        key = name.strip().lower()
        if key not in index:
            raise ValueError(f"Observed table has no column {name!r}.")
        return index[key]

    def number(row: list[Any], name: str) -> Decimal:
        raw = str(row[column(name)]).replace("$", "").replace(",", "").strip()
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(f"Column {name!r} contains a non-numeric value.") from exc

    records = [
        {headers[position]: value for position, value in enumerate(row)}
        for row in rows
        if len(row) == len(headers)
    ]
    if len(records) != len(rows):
        raise ValueError("Every observed row must contain the same fields as the table headers.")

    results: list[dict[str, Any]] = []
    for operation in args.operations:
        kind = str(operation.get("kind", "")).strip().lower()
        amount_column = str(operation.get("amount_column", "AMOUNT"))
        if kind in {"total", "average", "minimum", "maximum", "count"}:
            values = [number(row, amount_column) for row in rows]
            if kind == "total":
                value: Any = f"{sum(values, Decimal('0')):.2f}"
            elif kind == "average":
                value = f"{(sum(values, Decimal('0')) / len(values)):.2f}" if values else None
            elif kind == "minimum":
                value = f"{min(values):.2f}" if values else None
            elif kind == "maximum":
                value = f"{max(values):.2f}" if values else None
            else:
                value = len(values)
            results.append({"kind": kind, "value": value})
        elif kind == "percentage":
            values = [number(row, amount_column) for row in rows]
            total = sum(values, Decimal("0"))
            if total == 0:
                raise ValueError("Cannot calculate percentages from a zero total.")
            name = str(operation.get("output", "percentage"))
            precision = int(operation.get("precision", 2))
            calculated = []
            for record, value in zip(records, values):
                item = dict(record)
                percentage = (value / total * Decimal("100")).quantize(
                    Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP
                )
                item[name] = f"{percentage:.{precision}f}"
                calculated.append(item)
            results.append(
                {"kind": kind, "total": f"{total:.2f}", "rows": calculated, "output": name}
            )
        elif kind == "filter":
            operator = str(operation.get("operator", "")).strip()
            threshold = operation.get("threshold")
            if isinstance(threshold, dict):
                threshold_kind = str(threshold.get("kind", "")).lower()
                values = [number(row, amount_column) for row in rows]
                if threshold_kind == "average":
                    threshold = sum(values, Decimal("0")) / len(values)
                elif threshold_kind == "total":
                    threshold = sum(values, Decimal("0"))
            threshold_value = Decimal(str(threshold))
            values = [number(row, amount_column) for row in rows]
            comparisons = {
                ">": lambda value: value > threshold_value,
                ">=": lambda value: value >= threshold_value,
                "<": lambda value: value < threshold_value,
                "<=": lambda value: value <= threshold_value,
                "==": lambda value: value == threshold_value,
            }
            if operator not in comparisons:
                raise ValueError(f"Unsupported filter operator {operator!r}.")
            results.append(
                {
                    "kind": kind,
                    "operator": operator,
                    "threshold": f"{threshold_value:.2f}",
                    "rows": [record for record, value in zip(records, values) if comparisons[operator](value)],
                }
            )
        elif kind == "group_sum":
            group_column = str(operation.get("group_by", "VENDOR"))
            grouped: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            for row in rows:
                grouped[str(row[column(group_column)])] += number(row, amount_column)
            results.append(
                {
                    "kind": kind,
                    "group_by": group_column,
                    "values": {key: f"{value:.2f}" for key, value in sorted(grouped.items())},
                }
            )
        elif kind == "duplicates":
            values: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record, row in zip(records, rows):
                values[str(row[column(amount_column)])].append(record)
            results.append({"kind": kind, "values": {key: value for key, value in values.items() if len(value) > 1}})
        elif kind == "distinct_rank":
            rank = int(operation.get("rank", 2))
            values = sorted({number(row, amount_column) for row in rows}, reverse=True)
            target = values[rank - 1] if len(values) >= rank else None
            results.append(
                {
                    "kind": kind,
                    "rank": rank,
                    "value": f"{target:.2f}" if target is not None else None,
                    "rows": [
                        record for record, row in zip(records, rows)
                        if target is not None and number(row, amount_column) == target
                    ],
                }
            )
        else:
            raise ValueError(f"Unsupported table analysis operation {kind!r}.")
    return {"status": "ok", "headers": headers, "results": results}

class MemoryToolSet:
    @staticmethod
    def remember(arguments: dict[str, Any], state_path: Path | None = None) -> dict[str, Any]:
        args = RememberArgs.model_validate(arguments).model_dump()
        state_path = state_path or Path("runs") / "latest" / "state.json"
        state_exists = state_path.exists()
        if state_exists:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            payload = {"memory": {}}
        payload.setdefault("memory", {})
        payload["memory"][args["key"]] = args["value"]
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {"status": "ok", "stored": args["key"], "value": args["value"]}

    @staticmethod
    def recall(arguments: dict[str, Any], state_path: Path | None = None) -> dict[str, Any]:
        args = RecallArgs.model_validate(arguments).model_dump()
        state_path = state_path or Path("runs") / "latest" / "state.json"
        if not state_path.exists():
            return {"status": "ok", "key": args["key"], "value": None}
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return {"status": "ok", "key": args["key"], "value": payload.get("memory", {}).get(args["key"])}
