"""Internal curator helpers shared across tools."""

import re
from datetime import datetime, timezone

# Frontmatter starts at line 1 with "---\n" and ends with a "---" line of
# its own. Requiring "\n---" (not just "---") for the closing fence prevents
# a mid-line "---" inside a YAML value (e.g. ``description: "test --- here"``)
# from prematurely terminating the block and corrupting the curated content.
# The body+newline is wrapped in an optional non-capturing group so the
# empty-frontmatter case "---\n---\n" also matches. End-of-string (\Z) is
# accepted in place of the trailing newline for notes that end with the
# closing fence (valid Markdown for stub notes). `\r?\n` rather than `\n`
# at each line break so notes edited on Windows (CRLF) strip correctly too.
_FRONTMATTER_RE = re.compile(r"^---\r?\n(?:.*?\r?\n)?---(?:\r?\n|\Z)", re.DOTALL)


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
