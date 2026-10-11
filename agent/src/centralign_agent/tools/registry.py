from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    read_only: bool
    execution: Callable[[dict[str, Any]], dict[str, Any]]
    approval: str | None = None


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def list_tools(self) -> list[dict[str, Any]]:
        result = []
        for tool in self._tools.values():
            result.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "read_only": tool.read_only,
                    "approval": tool.approval,
                    "schema": tool.input_model.model_json_schema(),
                }
            )
        return result

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.get_tool(name)
        validated = spec.input_model.model_validate(arguments)
        return spec.execution(validated.model_dump())
