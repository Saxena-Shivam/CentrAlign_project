from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit


def ensure_allowed_origin(url: str, allowed_origin: str) -> None:
    target = urlsplit(url)
    allowed = urlsplit(allowed_origin)
    if (
        target.scheme not in {"http", "https"}
        or target.scheme != allowed.scheme
        or target.netloc != allowed.netloc
        or target.username is not None
        or target.password is not None
    ):
        raise ValueError(
            f"Navigation rejected: {url!r} is outside the allowed origin {allowed_origin!r}"
        )


def safe_inbox_path(inbox_root: Path, requested: str) -> Path:
    root = inbox_root.resolve()
    candidate = (root / requested).resolve()
    candidate.relative_to(root)
    return candidate
