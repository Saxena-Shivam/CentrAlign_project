from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HumanInputArgs(BaseModel):
    reason: str = Field(..., min_length=1)


class FinishArgs(BaseModel):
    message: str = Field(..., min_length=1)


class InteractionToolSet:
    @staticmethod
    def request_human_input(arguments: dict[str, Any]) -> dict[str, Any]:
        args = HumanInputArgs.model_validate(arguments).model_dump()
        return {
            "status": "needs_human_input",
            "reason": args["reason"],
            "message": "The agent paused and needs explicit user input before continuing."
        }

    @staticmethod
    def finish(arguments: dict[str, Any]) -> dict[str, Any]:
        args = FinishArgs.model_validate(arguments).model_dump()
        return {
            "status": "finished",
            "message": args["message"],
            "final": True,
        }
