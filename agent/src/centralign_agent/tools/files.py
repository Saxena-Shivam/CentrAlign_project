from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from centralign_agent.config import get_settings
from centralign_agent.safety import safe_inbox_path


class ReadFileArgs(BaseModel):
    path: str = Field(..., min_length=1)
    kind: str = Field(default="plain")


class FileToolSet:
    @staticmethod
    def read_file(arguments: dict[str, Any]) -> dict[str, Any]:
        args = ReadFileArgs.model_validate(arguments).model_dump()
        settings = get_settings()
        root = settings.inbox_dir.resolve()
        if not root.exists():
            return {"status": "error", "message": f"Inbox directory not found: {root}"}
        try:
            resolved = safe_inbox_path(root, args["path"])
        except Exception as exc:
            return {"status": "error", "message": f"Denied file read: {exc}"}

        if not resolved.exists():
            return {"status": "error", "message": f"File not found: {resolved}"}

        if args["kind"] == "html":
            return {"status": "ok", "path": str(resolved), "content": resolved.read_text(encoding="utf-8", errors="replace")}

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
            return {"status": "ok", "path": str(resolved), "content": text}
        except Exception as exc:
            return {"status": "error", "message": f"Unsupported or unreadable file: {exc}"}
