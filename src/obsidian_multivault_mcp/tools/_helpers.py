"""Internal curator helpers shared across tools."""

import re
from datetime import datetime, timezone

_FRONTMATTER_RE = re.compile(r"^---\n.*?---\n", re.DOTALL)


def strip_frontmatter(content: str) -> str:
    """Strip a leading YAML frontmatter block (if any) from raw markdown."""
    return _FRONTMATTER_RE.sub("", content, count=1)


def epoch_ms_to_iso(ms: int | None) -> str | None:
    """Convert epoch milliseconds to a UTC ISO 8601 string. None → None."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def curate_simple_match(raw: dict) -> dict:
    """Curate one result entry from POST /search/simple/."""
    return {
        "path": raw.get("filename", ""),
        "score": raw.get("score"),
        "context": [(m.get("context") or "") for m in (raw.get("matches") or [])],
    }


def curate_structured_match(raw: dict) -> dict:
    """Curate one result entry from POST /search/ (JsonLogic or Dataview).

    JsonLogic returns `{filename, result: bool}`; Dataview returns
    `{filename, result: [...]}` (list rows). Neither carries scores or
    context strings, so we drop the `result` field and emit a uniform
    shape with empty context.
    """
    return {
        "path": raw.get("filename", ""),
        "score": None,
        "context": [],
    }
