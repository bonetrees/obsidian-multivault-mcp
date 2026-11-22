"""End-to-end tool tests via a FastMCP in-process Client.

Each test parameterizes the `vault_handlers` fixture with one or more
MockTransport handlers, then invokes tools through the Client and asserts
on the curated response shape.
"""

import json
import urllib.parse

import httpx
import pytest
from fastmcp.exceptions import ToolError

from obsidian_multivault_mcp.client import ObsidianVaultClient
from tests.plugin_fakes import FakeFrontmatterNote, FakeHeadingNote, FakeVaultNote


# ---------- helpers ----------


def _ok(body):
    return httpx.Response(200, json=body)


def _note_response(content="Body text", frontmatter=None, tags=None):
    return _ok(
        {
            "content": content,
            "frontmatter": frontmatter or {},
            "tags": tags or [],
            "stat": {"ctime": 1700000000000, "mtime": 1700000001000, "size": len(content)},
        }
    )


async def _call(mcp_client, tool, args):
    result = await mcp_client.call_tool(tool, args)
    return result.data


# ---------- registration ----------


class TestToolRegistration:
    async def test_all_tools_registered(self, mcp_client):
        tools = await mcp_client.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "list_vaults",
            "list_directory",
            "read_note",
            "write_note",
            "append_note",
            "patch_note",
            "delete_note",
            "search_vault",
            "search_all_vaults",
        }


# ---------- list_vaults ----------


class TestListVaults:
    @pytest.fixture
    def vault_handlers(self):
        def alpha(request):
            if request.url.path == "/":
                return _ok({"status": "OK", "service": "Obsidian Local REST API"})
            if request.url.path == "/vault/Index.md":
                return _note_response(content="---\nfoo: bar\n---\n# Alpha\n\nDescription.")
            return httpx.Response(404)

        def beta(request):
            if request.url.path == "/":
                return _ok({"status": "OK"})
            if request.url.path == "/vault/Index.md":
                return httpx.Response(404, json={"errorCode": 40400, "message": "missing"})
            return httpx.Response(404)

        def gamma(_request):
            raise httpx.ConnectError("refused")

        return {"alpha": alpha, "beta": beta, "gamma": gamma}

    async def test_returns_all_vaults(self, mcp_client):
        data = await _call(mcp_client, "list_vaults", {})
        by_name = {v["name"]: v for v in data["vaults"]}
        assert set(by_name) == {"alpha", "beta", "gamma"}
        assert by_name["alpha"]["status"] == "online"
        assert by_name["alpha"]["index"].startswith("# Alpha")
        assert by_name["alpha"]["error"] is None
        assert by_name["beta"]["status"] == "online"
        assert by_name["beta"]["index"] is None
        assert by_name["beta"]["error"] is None
        assert by_name["gamma"]["status"] == "unreachable"
        assert by_name["gamma"]["index"] is None
        assert by_name["gamma"]["error"] is None


class TestListVaultsDegradedOnAuthFailure:
    """A vault that passes health check but returns 401 on Index.md must NOT
    be reported as online — that would lie to the LLM about usability. It
    also shouldn't be reported as unreachable (the plugin is responding);
    "degraded" with the underlying error is the honest signal."""

    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.url.path == "/":
                return _ok({"status": "OK"})
            if request.url.path == "/vault/Index.md":
                return httpx.Response(401, json={"errorCode": 40100, "message": "bad key"})
            return httpx.Response(404)

        return {"v": vault}

    async def test_marks_degraded_on_auth_failure(self, mcp_client):
        data = await _call(mcp_client, "list_vaults", {})
        entry = data["vaults"][0]
        assert entry["name"] == "v"
        assert entry["status"] == "degraded"
        assert entry["index"] is None
        assert entry["error"] is not None
        assert "Authentication failed" in entry["error"]


# ---------- list_directory ----------


class TestListDirectory:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.url.path == "/vault/":
                return _ok({"files": ["Index.md", "Folder/", "b.md", "Other Folder/"]})
            if request.url.path == "/vault/notes/":
                return _ok({"files": ["a.md", "sub/"]})
            return httpx.Response(404)

        return {"v": vault}

    async def test_root(self, mcp_client):
        data = await _call(mcp_client, "list_directory", {"vault": "v", "path": ""})
        assert data["files"] == ["Index.md", "b.md"]
        assert data["folders"] == ["Folder", "Other Folder"]

    async def test_subdirectory(self, mcp_client):
        data = await _call(mcp_client, "list_directory", {"vault": "v", "path": "notes"})
        assert data["files"] == ["a.md"]
        assert data["folders"] == ["sub"]


# ---------- read_note ----------


class TestReadNote:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.url.path == "/vault/notes/n.md":
                return _note_response(
                    content="---\ntitle: hi\n---\nThe body.",
                    frontmatter={"title": "hi"},
                    tags=["t1"],
                )
            if request.url.path == "/vault/missing.md":
                return httpx.Response(404, json={"errorCode": 40400, "message": "nope"})
            return httpx.Response(404)

        return {"v": vault}

    async def test_read(self, mcp_client):
        data = await _call(mcp_client, "read_note", {"vault": "v", "path": "notes/n.md"})
        assert data["vault"] == "v"
        assert data["path"] == "notes/n.md"
        assert data["content"] == "The body."
        assert data["frontmatter"] == {"title": "hi"}
        assert data["tags"] == ["t1"]
        assert data["stat"]["size"] == len("---\ntitle: hi\n---\nThe body.")

    async def test_read_unknown_vault_raises(self, mcp_client):
        with pytest.raises(ToolError) as exc_info:
            await _call(mcp_client, "read_note", {"vault": "nope", "path": "x.md"})
        assert "Unknown vault" in str(exc_info.value)

    async def test_read_404_raises(self, mcp_client):
        with pytest.raises(ToolError) as exc_info:
            await _call(mcp_client, "read_note", {"vault": "v", "path": "missing.md"})
        assert "Not found" in str(exc_info.value)


# ---------- write_note / append_note / patch_note / delete_note ----------


class TestWriteNote:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.method == "PUT" and request.url.path == "/vault/new.md":
                return httpx.Response(204)
            return httpx.Response(404)

        return {"v": vault}

    async def test_write(self, mcp_client):
        data = await _call(
            mcp_client, "write_note", {"vault": "v", "path": "new.md", "content": "Hello"}
        )
        assert data == {"vault": "v", "path": "new.md", "status": "written", "etag": None}


class TestAppendNote:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.method == "POST" and request.url.path == "/vault/log.md":
                assert request.content == b"\nentry"
                return httpx.Response(204)
            return httpx.Response(404)

        return {"v": vault}

    async def test_append(self, mcp_client):
        data = await _call(
            mcp_client, "append_note", {"vault": "v", "path": "log.md", "content": "\nentry"}
        )
        assert data == {"vault": "v", "path": "log.md", "status": "appended", "etag": None}


class TestPatchNote:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.method == "PATCH" and request.url.path == "/vault/doc.md":
                assert request.headers["operation"] == "append"
                assert request.headers["target-type"] == "heading"
                # markdown-patch v2: the version header is mandatory for
                # header-based targeting and the heading target is a
                # percent-encoded JSON array, not a '::'-joined string.
                assert request.headers["markdown-patch-version"] == "2"
                assert json.loads(urllib.parse.unquote(request.headers["target"])) == [
                    "Report",
                    "Findings",
                ]
                return httpx.Response(200)
            return httpx.Response(404)

        return {"v": vault}

    async def test_patch_heading(self, mcp_client):
        data = await _call(
            mcp_client,
            "patch_note",
            {
                "vault": "v",
                "path": "doc.md",
                "operation": "append",
                "target_type": "heading",
                "target": "Report::Findings",
                "content": "\nnew row",
            },
        )
        assert data["status"] == "patched"
        assert data["operation"] == "append"
        # The tool API is unchanged by the v2 migration — callers still pass
        # and get back the '::' path; the JSON-array translation is internal.
        assert data["target"] == "Report::Findings"
        assert data["warnings"] == []

    async def test_patch_warnings_reach_the_caller(self, mcp_client, monkeypatch):
        # A patch can succeed and still be worth flagging. The advisory has to
        # survive the whole client → tool → FastMCP path or callers silently
        # lose "applied, but...".
        advisories = [{"code": "heading-depth-overflow", "message": "level 7"}]

        async def fake_patch(*_args, **_kwargs):
            return advisories, '"t1"'

        monkeypatch.setattr(ObsidianVaultClient, "patch_note", fake_patch)
        data = await _call(
            mcp_client,
            "patch_note",
            {
                "vault": "v",
                "path": "doc.md",
                "operation": "append",
                "target_type": "heading",
                "target": "Report::Findings",
                "content": "\nnew row",
            },
        )
        assert data["status"] == "patched"
        assert data["warnings"] == advisories
        assert data["etag"] == '"t1"'


class TestPreconditions:
    """read_note's etag round-trips through every write tool as if_match."""

    @pytest.fixture
    def note(self):
        return FakeVaultNote("doc.md", "# Doc\n\nbody\n")

    @pytest.fixture
    def vault_handlers(self, note):
        return {"v": note.handler}

    async def _read_etag(self, mcp_client):
        data = await _call(mcp_client, "read_note", {"vault": "v", "path": "doc.md"})
        return data["etag"]

    @staticmethod
    def _args(tool, etag):
        args = {"vault": "v", "path": "doc.md", "if_match": etag}
        if tool == "write_note":
            args["content"] = "rewritten"
        elif tool == "append_note":
            args["content"] = "\nappended"
        elif tool == "patch_note":
            args.update(
                operation="append", target_type="heading", target="Doc", content="\npatched"
            )
        else:
            args["confirm"] = True
        return args

    async def test_read_note_exposes_file_etag(self, mcp_client, note):
        assert await self._read_etag(mcp_client) == note.etag

    @pytest.mark.parametrize("tool", ["write_note", "append_note", "patch_note"])
    async def test_round_trip_succeeds_and_returns_new_etag(self, mcp_client, note, tool):
        etag = await self._read_etag(mcp_client)
        data = await _call(mcp_client, tool, self._args(tool, etag))
        assert data["etag"] == note.etag != etag

    @pytest.mark.parametrize("tool", ["write_note", "append_note", "patch_note", "delete_note"])
    async def test_interleaved_edit_is_refused_and_preserved(self, mcp_client, note, tool):
        etag = await self._read_etag(mcp_client)
        note.content = "# Doc\n\nuser typed this\n"
        with pytest.raises(ToolError) as exc_info:
            await _call(mcp_client, tool, self._args(tool, etag))
        assert "Precondition failed" in str(exc_info.value)
        assert note.content == "# Doc\n\nuser typed this\n"

    async def test_create_only_on_existing_note_says_it_exists(self, mcp_client, note):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "write_note",
                {"vault": "v", "path": "doc.md", "content": "x", "create_only": True},
            )
        assert "already exists" in str(exc_info.value)
        assert note.content == "# Doc\n\nbody\n"

    async def test_create_only_on_absent_note_reports_created(self, mcp_client, note):
        note.content = None
        data = await _call(
            mcp_client,
            "write_note",
            {"vault": "v", "path": "doc.md", "content": "fresh", "create_only": True},
        )
        assert data["status"] == "created"
        assert data["etag"] == note.etag
        assert note.content == "fresh"

    async def test_create_only_and_if_match_are_exclusive(self, mcp_client, note):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "write_note",
                {
                    "vault": "v",
                    "path": "doc.md",
                    "content": "x",
                    "create_only": True,
                    "if_match": note.etag,
                },
            )
        assert "not both" in str(exc_info.value)
        # Rejected before any request reached the vault.
        assert note.requests == []

    @pytest.mark.parametrize("bad", ["", "   ", '"a"\r\nX-Injected: 1'])
    async def test_unusable_etag_rejected_before_request(self, mcp_client, note, bad):
        with pytest.raises(ToolError):
            await _call(mcp_client, "append_note", self._args("append_note", bad))
        assert note.requests == []


class TestPatchNoteStructuredFrontmatter:
    """`value` survives the MCP JSON boundary with its JSON type intact."""

    @pytest.fixture
    def note(self):
        return FakeFrontmatterNote("doc.md", {"tags": ["alpha"], "status": "draft"})

    @pytest.fixture
    def vault_handlers(self, note):
        return {"v": note.handler}

    @staticmethod
    def _args(**kwargs):
        base = {
            "vault": "v",
            "path": "doc.md",
            "operation": "replace",
            "target_type": "frontmatter-key",
            "target": "tags",
        }
        return {**base, **kwargs}

    async def test_list_round_trips_as_a_list(self, mcp_client, note):
        await _call(mcp_client, "patch_note", self._args(operation="append", value=["beta"]))
        data = await _call(mcp_client, "read_note", {"vault": "v", "path": "doc.md"})
        assert data["frontmatter"]["tags"] == ["alpha", "beta"]

    @pytest.mark.parametrize("value", [2, 2.5, True, "1.0", {"owner": "sim", "n": [1, None]}])
    async def test_json_types_preserved_end_to_end(self, mcp_client, note, value):
        await _call(mcp_client, "patch_note", self._args(target="status", value=value))
        assert note.frontmatter["status"] == value
        assert type(note.frontmatter["status"]) is type(value)

    @pytest.mark.parametrize("content", ["1.0", "true", "null"])
    async def test_content_still_writes_strings(self, mcp_client, note, content):
        # The scalar contract is unchanged: `content` never becomes a number,
        # boolean or null, however much it looks like one.
        await _call(mcp_client, "patch_note", self._args(target="status", content=content))
        assert note.frontmatter["status"] == content

    async def test_create_if_missing(self, mcp_client, note):
        await _call(
            mcp_client,
            "patch_note",
            self._args(target="aliases", value=["a"], create_if_missing=True),
        )
        assert note.frontmatter["aliases"] == ["a"]

    async def test_schema_lists_accepted_value_types(self, mcp_client):
        tool = next(t for t in await mcp_client.list_tools() if t.name == "patch_note")
        value_types = {
            option.get("type") for option in tool.inputSchema["properties"]["value"]["anyOf"]
        }
        assert {"array", "object", "string", "boolean", "integer", "number"} <= value_types
        assert "content" not in tool.inputSchema["required"]


class TestPatchNoteDelete:
    """delete and list heading targets, end to end through the MCP boundary."""

    @pytest.fixture
    def note(self):
        return FakeFrontmatterNote("doc.md", {"status": "draft", "tags": ["a"]})

    @pytest.fixture
    def seen(self):
        return []

    @pytest.fixture
    def vault_handlers(self, note, seen):
        def vault(request):
            seen.append(request)
            if request.headers.get("target-type") == "heading":
                return httpx.Response(200)
            return note.handler(request)

        return {"v": vault}

    @staticmethod
    def _args(**kwargs):
        return {"vault": "v", "path": "doc.md", "operation": "delete", **kwargs}

    async def test_frontmatter_delete_removes_the_key_by_default(self, mcp_client, note):
        data = await _call(
            mcp_client, "patch_note", self._args(target_type="frontmatter-key", target="status")
        )
        assert data["operation"] == "delete"
        assert note.frontmatter == {"tags": ["a"]}

    async def test_content_scope_keeps_key_as_null(self, mcp_client, note):
        await _call(
            mcp_client,
            "patch_note",
            self._args(target_type="frontmatter-key", target="status", delete_scope="content"),
        )
        assert note.frontmatter["status"] is None

    async def test_list_heading_target_crosses_mcp_intact(self, mcp_client, seen):
        await _call(
            mcp_client,
            "patch_note",
            self._args(target_type="heading", target=["Report", "Q1::Q2"]),
        )
        assert json.loads(urllib.parse.unquote(seen[-1].headers["target"])) == ["Report", "Q1::Q2"]
        assert seen[-1].headers["target-scope"] == "markerAndContent"
        assert seen[-1].content == b""

    @pytest.mark.parametrize("target", [[], ["A", ""]])
    async def test_unusable_list_rejected_before_request(self, mcp_client, seen, target):
        with pytest.raises(ToolError):
            await _call(mcp_client, "patch_note", self._args(target_type="heading", target=target))
        assert seen == []

    async def test_schema_and_annotations(self, mcp_client):
        tool = next(t for t in await mcp_client.list_tools() if t.name == "patch_note")
        props = tool.inputSchema["properties"]
        assert "delete" in json.dumps(props["operation"])
        assert {"all", "content", "marker"} <= set(json.dumps(props["delete_scope"]).split('"'))
        assert "array" in json.dumps(props["target"])
        assert tool.annotations.destructiveHint is True


class TestPatchNoteChildHeadings:
    """Child-heading protection and `within`, end to end through MCP."""

    @pytest.fixture
    def note(self):
        return FakeHeadingNote("doc.md", "# Doc\n\nintro\n\n- one\n\n## Child\n\nchild body\n")

    @pytest.fixture
    def vault_handlers(self, note):
        return {"v": note.handler}

    @staticmethod
    def _args(**kwargs):
        return {
            "vault": "v",
            "path": "doc.md",
            "operation": "replace",
            "target_type": "heading",
            "target": "Doc",
            "content": "new intro",
            **kwargs,
        }

    async def test_replace_with_children_is_refused(self, mcp_client, note):
        with pytest.raises(ToolError, match="refusing to replace heading 'Doc'"):
            await _call(mcp_client, "patch_note", self._args())
        assert "## Child\n\nchild body" in note.markdown

    async def test_within_reaches_the_plugin(self, mcp_client, note):
        await _call(
            mcp_client,
            "patch_note",
            self._args(operation="append", content="\n- two", within=-1),
        )
        assert note.markdown == "# Doc\n\nintro\n\n- one\n- two\n\n## Child\n\nchild body\n"

    async def test_replace_children_reaches_the_client(self, mcp_client, note):
        await _call(mcp_client, "patch_note", self._args(replace_children=True))
        assert note.markdown == "# Doc\n\nnew intro\n"

    @pytest.mark.parametrize("bad", [{"within": "1"}, {"within": True}, {"within": 1.5}])
    async def test_within_must_be_a_real_int(self, mcp_client, note, bad):
        with pytest.raises(ToolError):
            await _call(mcp_client, "patch_note", self._args(**bad))
        assert note.requests == []

    async def test_schema_lists_new_parameters(self, mcp_client):
        tool = next(t for t in await mcp_client.list_tools() if t.name == "patch_note")
        props = tool.inputSchema["properties"]
        assert "integer" in json.dumps(props["within"])
        assert props["replace_children"]["type"] == "boolean"


class TestPatchNoteSiblingInsert:
    """Sibling inserts and the heading-level refusals, end to end through MCP."""

    LOG = (
        "# Log\n\n## Decisions\n\n### Entry one\n\none body\n\n#### Follow-up\n\n"
        "follow body\n\n### Entry two\n\ntwo body\n"
    )

    @pytest.fixture
    def note(self):
        return FakeHeadingNote("doc.md", self.LOG)

    @pytest.fixture
    def vault_handlers(self, note):
        return {"v": note.handler}

    @staticmethod
    def _args(**kwargs):
        return {
            "vault": "v",
            "path": "doc.md",
            "operation": "insert_after",
            "target_type": "heading",
            "target": "Log::Decisions::Entry one",
            "content": "# Entry new\n\nnew body",
            **kwargs,
        }

    async def test_insert_after_lands_as_sibling_after_children(self, mcp_client, note):
        result = await _call(mcp_client, "patch_note", self._args())
        assert result["operation"] == "insert_after"
        assert "follow body\n\n### Entry new\n\nnew body\n\n### Entry two" in note.markdown

    async def test_insert_before_lands_before_heading(self, mcp_client, note):
        await _call(
            mcp_client,
            "patch_note",
            self._args(operation="insert_before", target="Log::Decisions::Entry two"),
        )
        assert "follow body\n\n### Entry new\n\nnew body\n\n### Entry two" in note.markdown

    async def test_absolute_append_refused_note_untouched(self, mcp_client, note):
        # TS-Westworld regression: '### New entry' copied from the note.
        with pytest.raises(ToolError, match="looks copied from the note"):
            await _call(
                mcp_client,
                "patch_note",
                self._args(operation="append", content="\n### New entry\n\nbody\n"),
            )
        assert note.markdown == self.LOG
        assert not [r for r in note.requests if r.method == "PATCH"]

    async def test_allow_deep_headings_reaches_the_client(self, mcp_client, note):
        await _call(
            mcp_client,
            "patch_note",
            self._args(operation="append", content="\n## Deep\n", allow_deep_headings=True),
        )
        assert "follow body\n\n##### Deep\n\n### Entry two" in note.markdown

    @pytest.mark.parametrize("bad", ["true", 1])
    async def test_allow_deep_headings_must_be_a_real_bool(self, mcp_client, note, bad):
        with pytest.raises(ToolError):
            await _call(mcp_client, "patch_note", self._args(allow_deep_headings=bad))
        assert note.requests == []

    async def test_schema_lists_new_operations(self, mcp_client):
        tool = next(t for t in await mcp_client.list_tools() if t.name == "patch_note")
        props = tool.inputSchema["properties"]
        assert {"insert_after", "insert_before"} <= set(props["operation"]["enum"])
        assert props["allow_deep_headings"]["type"] == "boolean"


class TestToolDescriptions:
    """Claude Code truncates a tool description at 2KB before the model sees
    it (obsidian-local-rest-api#295). Past the cut, rules are silently lost:
    patch_note's whole operations section once was. Detail that doesn't fit
    belongs in parameter descriptions, which aren't truncated."""

    CLAUDE_CODE_DESCRIPTION_LIMIT = 2048

    async def test_every_tool_description_fits(self, mcp_client):
        too_long = {
            tool.name: len(tool.description or "")
            for tool in await mcp_client.list_tools()
            if len(tool.description or "") > self.CLAUDE_CODE_DESCRIPTION_LIMIT
        }
        assert too_long == {}

    async def test_patch_note_critical_rules_are_in_the_description(self, mcp_client):
        tool = next(t for t in await mcp_client.list_tools() if t.name == "patch_note")
        for rule in ("full path from the document root", "relative to the target", "refused"):
            assert rule in tool.description
        for param in ("within", "replace_children", "insert_after", "insert_before"):
            assert param in tool.description

    async def test_every_patch_note_parameter_is_described(self, mcp_client):
        tool = next(t for t in await mcp_client.list_tools() if t.name == "patch_note")
        props = tool.inputSchema["properties"]
        assert [name for name, schema in props.items() if not schema.get("description")] == []


class TestPatchNoteFrontmatter:
    """End-to-end coverage of the v0.2.0 frontmatter fix: the friendly
    `frontmatter-key` enum must reach the plugin as `frontmatter` over JSON,
    and Guard B (structured-content rejection) must surface as a tool error
    through the FastMCP client — not a silent stringification."""

    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.method == "PATCH" and request.url.path == "/vault/doc.md":
                # Mirror the live plugin: PATCH on frontmatter returns 200.
                # Asserting on header/body shape happens at the client layer
                # (test_client.py) — here we just confirm the in-process
                # FastMCP path delivers the call and curates the response.
                assert request.headers["target-type"] == "frontmatter"
                assert request.headers["content-type"] == "application/json"
                return httpx.Response(200)
            return httpx.Response(404)

        return {"v": vault}

    async def test_scalar_frontmatter_patch_succeeds(self, mcp_client):
        data = await _call(
            mcp_client,
            "patch_note",
            {
                "vault": "v",
                "path": "doc.md",
                "operation": "replace",
                "target_type": "frontmatter-key",
                "target": "status",
                "content": "published",
            },
        )
        assert data["status"] == "patched"
        assert data["target_type"] == "frontmatter-key"  # tool keeps friendly enum
        assert data["target"] == "status"

    async def test_guard_b_list_rejection_surfaces_as_tool_error(self, mcp_client):
        # FastMCP turns a server-side ToolError into an is_error result; the
        # in-process Client re-raises it as ToolError. The caller must see
        # the scalar-only constraint so they can correct the content shape.
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "patch_note",
                {
                    "vault": "v",
                    "path": "doc.md",
                    "operation": "replace",
                    "target_type": "frontmatter-key",
                    "target": "tags",
                    "content": '["a","b"]',
                },
            )
        msg = str(exc_info.value)
        assert "scalar-only" in msg
        assert "list" in msg

    async def test_guard_b_dict_rejection_surfaces_as_tool_error(self, mcp_client):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "patch_note",
                {
                    "vault": "v",
                    "path": "doc.md",
                    "operation": "replace",
                    "target_type": "frontmatter-key",
                    "target": "meta",
                    "content": '{"k":"v"}',
                },
            )
        msg = str(exc_info.value)
        assert "scalar-only" in msg
        assert "dict" in msg


class TestDeleteNote:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.method == "DELETE" and request.url.path == "/vault/old.md":
                return httpx.Response(204)
            return httpx.Response(404)

        return {"v": vault}

    async def test_delete_requires_confirm(self, mcp_client):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "delete_note",
                {"vault": "v", "path": "old.md", "confirm": False},
            )
        assert "confirm=True" in str(exc_info.value)

    async def test_delete_confirm_non_bool_rejected(self, mcp_client):
        # StrictBool: integers and strings must not slip past the safety
        # gate via Pydantic's lax coercion. confirm=1 should fail schema
        # validation, not be coerced to True and delete the note.
        with pytest.raises(ToolError):
            await _call(
                mcp_client,
                "delete_note",
                {"vault": "v", "path": "old.md", "confirm": 1},
            )

    async def test_delete_omitted_confirm_hits_runtime_gate(self, mcp_client):
        # confirm defaults to False so the LLM-friendly runtime gate fires
        # instead of a less-helpful Pydantic schema error.
        with pytest.raises(ToolError) as exc_info:
            await _call(mcp_client, "delete_note", {"vault": "v", "path": "old.md"})
        assert "confirm=True" in str(exc_info.value)

    async def test_delete_with_confirm(self, mcp_client):
        data = await _call(
            mcp_client,
            "delete_note",
            {"vault": "v", "path": "old.md", "confirm": True},
        )
        assert data == {"vault": "v", "path": "old.md", "status": "deleted"}


# ---------- search_vault ----------


class TestSearchVault:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.url.path == "/search/simple/":
                return _ok(
                    [
                        {
                            "filename": "notes/a.md",
                            "score": -0.5,
                            "matches": [{"match": {"start": 0, "end": 5}, "context": "hello"}],
                        }
                    ]
                )
            if request.url.path == "/search/":
                ctype = request.headers.get("content-type", "")
                if "jsonlogic" in ctype:
                    return _ok([{"filename": "b.md", "result": True}])
                if "dataview" in ctype:
                    return _ok([{"filename": "c.md", "result": ["row"]}])
            return httpx.Response(404)

        return {"v": vault}

    async def test_text_search(self, mcp_client):
        data = await _call(mcp_client, "search_vault", {"vault": "v", "query": "hello"})
        assert data["search_type"] == "text"
        assert data["total_results"] == 1
        assert data["results"][0]["path"] == "notes/a.md"
        assert data["results"][0]["score"] == -0.5
        assert data["results"][0]["context"] == ["hello"]
        assert data["warning"] is None

    async def test_jsonlogic_search(self, mcp_client):
        data = await _call(
            mcp_client,
            "search_vault",
            {
                "vault": "v",
                "query": json.dumps({"in": ["#tag", {"var": "tags"}]}),
                "search_type": "jsonlogic",
            },
        )
        assert data["results"] == [{"path": "b.md", "score": None, "context": []}]

    async def test_jsonlogic_non_object_root_raises(self, mcp_client):
        # JsonLogic expressions are objects; parsing "[]" succeeds but isn't
        # a valid expression — caller should get a clear self-correcting error.
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "search_vault",
                {"vault": "v", "query": "[]", "search_type": "jsonlogic"},
            )
        msg = str(exc_info.value)
        assert "JSON object" in msg
        assert "list" in msg  # type name of the bad root

    async def test_jsonlogic_bad_json_raises(self, mcp_client):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "search_vault",
                {"vault": "v", "query": "not json", "search_type": "jsonlogic"},
            )
        assert "valid JSON" in str(exc_info.value)

    async def test_dataview_success(self, mcp_client):
        data = await _call(
            mcp_client,
            "search_vault",
            {"vault": "v", "query": "TABLE FROM #x", "search_type": "dataview"},
        )
        assert data["results"] == [{"path": "c.md", "score": None, "context": []}]
        assert data["warning"] is None


class TestSearchVaultDataviewFallback:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.url.path == "/search/":
                # Dataview not installed
                return httpx.Response(
                    400,
                    json={
                        "errorCode": 40070,
                        "message": "Dataview plugin is not enabled.",
                    },
                )
            if request.url.path == "/search/simple/":
                return _ok([{"filename": "a.md", "score": -0.1, "matches": []}])
            return httpx.Response(404)

        return {"v": vault}

    async def test_fallback_warning(self, mcp_client):
        data = await _call(
            mcp_client,
            "search_vault",
            {"vault": "v", "query": "LIST FROM #x", "search_type": "dataview"},
        )
        assert data["warning"] == "Dataview not available, fell back to text search"
        assert data["results"][0]["path"] == "a.md"


# ---------- search_all_vaults ----------


class TestSearchAllVaults:
    @pytest.fixture
    def vault_handlers(self):
        def alpha(request):
            if request.url.path == "/search/simple/":
                return _ok([{"filename": "a.md", "score": -0.5, "matches": []}])
            return httpx.Response(404)

        def beta(request):
            if request.url.path == "/search/simple/":
                return _ok([{"filename": "b.md", "score": -0.3, "matches": []}])
            return httpx.Response(404)

        def broken(_request):
            return httpx.Response(500, text="internal server error")

        return {"alpha": alpha, "beta": beta, "broken": broken}

    async def test_results_grouped_by_vault(self, mcp_client):
        data = await _call(mcp_client, "search_all_vaults", {"query": "foo"})
        assert data["total_results_all"] == 2
        rbv = data["results_by_vault"]
        assert rbv["alpha"]["status"] == "ok"
        assert rbv["alpha"]["total_results"] == 1
        assert rbv["beta"]["status"] == "ok"
        assert rbv["broken"]["status"] == "error"
        assert rbv["broken"]["total_results"] == 0
        assert rbv["broken"]["error"]


class TestSearchAllVaultsValidatesJsonLogicEagerly:
    """Malformed jsonlogic input should raise a single ToolError before
    fan-out, not N identical {"status": "error"} per-vault entries — that
    asymmetry with search_vault was confusing and forced the caller to
    deduplicate per-vault errors to recognise a single user-input bug."""

    @pytest.fixture
    def vault_handlers(self):
        # The handlers should never actually be hit because validation
        # fails before fan-out. Return 500 so the test fails loudly if
        # they ever do get called.
        def vault(_request):
            return httpx.Response(500, text="should not be reached")

        return {"alpha": vault, "beta": vault, "gamma": vault}

    async def test_malformed_json_raises_single_toolerror(self, mcp_client):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "search_all_vaults",
                {"query": "not json", "search_type": "jsonlogic"},
            )
        msg = str(exc_info.value)
        assert "valid JSON" in msg
        # No mention of per-vault structure — it's a single failure mode.
        assert "results_by_vault" not in msg

    async def test_non_object_root_raises_single_toolerror(self, mcp_client):
        with pytest.raises(ToolError) as exc_info:
            await _call(
                mcp_client,
                "search_all_vaults",
                {"query": "[]", "search_type": "jsonlogic"},
            )
        msg = str(exc_info.value)
        assert "JSON object" in msg


class TestSearchAllVaultsPropagatesCancellation:
    """asyncio.CancelledError must propagate out of _one_vault, not be turned
    into a per-vault 'error' result — otherwise cooperative cancellation
    (request abort, server shutdown) would stall."""

    @pytest.fixture
    def vault_handlers(self):
        # Need at least one configured vault for the fixture machinery.
        def vault(_request):
            return httpx.Response(200, json=[])

        return {"v": vault}

    async def test_cancellederror_propagates(self):
        # Call _one_vault directly with a client that awaits an
        # already-cancelled future, so the body of run_search raises
        # CancelledError. The wrapper must let it through.
        import asyncio  # noqa: PLC0415

        from obsidian_multivault_mcp.tools.search_all_vaults import (  # noqa: PLC0415
            _one_vault,
        )

        class StubClient:
            name = "v"

            async def search_simple(self, *_args, **_kwargs):
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await _one_vault("v", StubClient(), "text", "q", 100)


class TestSearchAllVaultsIsolatesUnexpectedExceptions:
    """A vault that triggers a non-ToolError must not fail the whole fan-out."""

    @pytest.fixture
    def vault_handlers(self):
        def good(request):
            if request.url.path == "/search/simple/":
                return _ok([{"filename": "a.md", "score": -0.5, "matches": []}])
            return httpx.Response(404)

        def exploding(_request):
            # A handler that raises an unexpected (non-httpx, non-ToolError) exception.
            raise RuntimeError("oh no")

        return {"good": good, "bad": exploding}

    async def test_one_vault_runtimeerror_does_not_kill_fanout(self, mcp_client):
        data = await _call(mcp_client, "search_all_vaults", {"query": "x"})
        rbv = data["results_by_vault"]
        assert rbv["good"]["status"] == "ok"
        assert rbv["good"]["total_results"] == 1
        assert rbv["bad"]["status"] == "error"
        assert "RuntimeError" in rbv["bad"]["error"] or "oh no" in rbv["bad"]["error"]


class TestSearchAllVaultsPerVaultDataviewFallback:
    @pytest.fixture
    def vault_handlers(self):
        # alpha has Dataview, beta does not
        def alpha(request):
            if request.url.path == "/search/" and "dataview" in request.headers.get(
                "content-type", ""
            ):
                return _ok([{"filename": "a.md", "result": ["x"]}])
            return httpx.Response(404)

        def beta(request):
            if request.url.path == "/search/":
                return httpx.Response(400, json={"errorCode": 40070, "message": "no dataview"})
            if request.url.path == "/search/simple/":
                return _ok([{"filename": "b.md", "score": -0.1, "matches": []}])
            return httpx.Response(404)

        return {"alpha": alpha, "beta": beta}

    async def test_warning_only_for_falling_back_vault(self, mcp_client):
        data = await _call(
            mcp_client,
            "search_all_vaults",
            {"query": "LIST FROM #x", "search_type": "dataview"},
        )
        rbv = data["results_by_vault"]
        assert rbv["alpha"]["warning"] is None
        assert rbv["beta"]["warning"] == "Dataview not available, fell back to text search"
