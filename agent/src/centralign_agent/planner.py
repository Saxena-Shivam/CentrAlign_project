from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .llm import get_model_adapter


class Plan(BaseModel):
    brief_plan: list[str] = Field(..., min_length=1)
    success_criteria: list[str] = Field(..., min_length=1)

    @field_validator("brief_plan", "success_criteria", mode="before")
    @classmethod
    def normalize_model_text(cls, value):
        if isinstance(value, str):
            lines = [line.strip(" -*\t") for line in value.splitlines() if line.strip(" -*\t")]
            return lines or [value.strip()]
        return value


class Planner:
    def __init__(self, model=None):
        self.model = model or get_model_adapter()

    def create_plan(self, goal: str, history: list[str] | None = None) -> Plan:
        payload = self.model.plan(goal, history)
        return Plan.model_validate(payload)
