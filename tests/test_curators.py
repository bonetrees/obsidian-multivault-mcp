"""Pure-function tests for tool curators and helpers."""

import pytest

from obsidian_multivault_mcp.tools._helpers import (
    curate_simple_match,
    curate_structured_match,
    epoch_ms_to_iso,
    strip_frontmatter,
)
from obsidian_multivault_mcp.tools.list_directory import curate_directory_listing
from obsidian_multivault_mcp.tools.read_note import curate_note


class TestStripFrontmatter:
    def test_strips_block(self):
        content = "---\ntitle: hi\ntags: [a]\n---\nBody text here."
        assert strip_frontmatter(content) == "Body text here."

    def test_no_frontmatter_passthrough(self):
        assert strip_frontmatter("Just body text.") == "Just body text."

    def test_empty_frontmatter(self):
        content = "---\n---\nBody"
        assert strip_frontmatter(content) == "Body"

    def test_only_frontmatter(self):
        content = "---\nkey: value\n---\n"
        assert strip_frontmatter(content) == ""

    def test_frontmatter_ending_at_eof_without_newline(self):
        # Note ends with the closing fence and no trailing newline. Valid
        # Markdown — common for stub / placeholder notes. The block still
        # needs to be stripped so it doesn't leak into curated `content`.
        content = "---\nkey: value\n---"
        assert strip_frontmatter(content) == ""

    def test_does_not_terminate_at_mid_line_dashes(self):
        # "---" inside a YAML value (here, a quoted string with literal
        # dashes) must NOT count as the closing fence — the closer has to
        # be on its own line. Earlier regex `^---\n.*?---(?:\n|\Z)` would
        # have stopped at the inline "---" and corrupted the content.
        content = '---\ndescription: "test --- here"\nkey: v\n---\nBody'
        assert strip_frontmatter(content) == "Body"

    def test_does_not_terminate_at_mid_line_dashes_only_inline(self):
        # If the only "---" inside the block is mid-line, there's no real
        # closing fence — the block should pass through unmodified rather
        # than half-strip and corrupt the content.
        content = '---\ndescription: "test --- here"\nBody'
        assert strip_frontmatter(content) == content

    def test_malformed_unterminated_passthrough(self):
        content = "---\nkey: value\nno closing fence"
        assert strip_frontmatter(content) == content

    def test_does_not_strip_horizontal_rule_in_body(self):
        content = "First line\n---\nSecond line"
        assert strip_frontmatter(content) == content


class TestEpochMsToIso:
    def test_none_returns_none(self):
        assert epoch_ms_to_iso(None) is None

    def test_converts_with_utc(self):
        # 2023-11-14T22:13:20+00:00
        result = epoch_ms_to_iso(1700000000000)
        assert "2023-11-14" in result
        assert result.endswith("+00:00")

    def test_milliseconds_preserved(self):
        result = epoch_ms_to_iso(1700000000123)
        assert ".123" in result


class TestCurateDirectoryListing:
    def test_splits_files_and_folders(self):
        result = curate_directory_listing(
            "v", "notes", ["Index.md", "Folder/", "a.md", "Other Folder/"]
        )
        assert result["files"] == ["Index.md", "a.md"]
        assert result["folders"] == ["Folder", "Other Folder"]
        assert result["vault"] == "v"
        assert result["path"] == "notes"

    def test_empty_listing(self):
        result = curate_directory_listing("v", "", [])
        assert result["files"] == []
        assert result["folders"] == []

    def test_sorted_output(self):
        result = curate_directory_listing("v", "", ["b.md", "a.md", "z/", "m/"])
        assert result["files"] == ["a.md", "b.md"]
        assert result["folders"] == ["m", "z"]


class TestCurateNote:
    def test_full_shape(self):
        raw = {
            "content": "---\ntitle: hi\n---\nBody",
            "frontmatter": {"title": "hi"},
            "tags": ["a", "b"],
            "stat": {"ctime": 1700000000000, "mtime": 1700000001000, "size": 100},
        }
        result = curate_note("v", "n.md", raw)
        assert result["vault"] == "v"
        assert result["path"] == "n.md"
        assert result["content"] == "Body"
        assert result["frontmatter"] == {"title": "hi"}
        assert result["tags"] == ["a", "b"]
        assert result["stat"]["size"] == 100
        assert result["stat"]["created"].startswith("2023-")
        assert result["stat"]["modified"].startswith("2023-")

    def test_empty_frontmatter_becomes_none(self):
        raw = {"content": "Body", "frontmatter": {}, "tags": [], "stat": {}}
        result = curate_note("v", "n.md", raw)
        assert result["frontmatter"] is None

    def test_missing_frontmatter_becomes_none(self):
        raw = {"content": "Body", "tags": [], "stat": {}}
        result = curate_note("v", "n.md", raw)
        assert result["frontmatter"] is None

    def test_missing_stat_fields_become_none(self):
        raw = {"content": "x", "tags": [], "stat": {}}
        result = curate_note("v", "n.md", raw)
        assert result["stat"] == {"created": None, "modified": None, "size": None}


class TestCurateSimpleMatch:
    def test_renames_filename_to_path(self):
        raw = {
            "filename": "notes/a.md",
            "score": -0.5,
            "matches": [
                {"match": {"start": 0, "end": 5}, "context": "hello there"},
                {"match": {"start": 10, "end": 15}, "context": "world here"},
            ],
        }
        result = curate_simple_match(raw)
        assert result["path"] == "notes/a.md"
        assert result["score"] == -0.5
        assert result["context"] == ["hello there", "world here"]

    def test_handles_missing_keys(self):
        result = curate_simple_match({})
        assert result == {"path": "", "score": None, "context": []}

    def test_handles_null_context(self):
        raw = {"filename": "a.md", "matches": [{"match": {}, "context": None}]}
        result = curate_simple_match(raw)
        assert result["context"] == [""]


class TestCurateStructuredMatch:
    def test_jsonlogic_drops_result(self):
        raw = {"filename": "a.md", "result": True}
        result = curate_structured_match(raw)
        assert result == {"path": "a.md", "score": None, "context": []}

    def test_dataview_drops_result(self):
        raw = {"filename": "a.md", "result": ["row1", "row2"]}
        result = curate_structured_match(raw)
        assert result == {"path": "a.md", "score": None, "context": []}
