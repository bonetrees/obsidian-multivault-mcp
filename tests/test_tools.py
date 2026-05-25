"""End-to-end tool tests via a FastMCP in-process Client.

Each test parameterizes the `vault_handlers` fixture with one or more
MockTransport handlers, then invokes tools through the Client and asserts
on the curated response shape.
"""

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError


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
        assert by_name["beta"]["status"] == "online"
        assert by_name["beta"]["index"] is None
        assert by_name["gamma"]["status"] == "unreachable"
        assert by_name["gamma"]["index"] is None


class TestListVaultsAuthFailureNotSwallowed:
    """A vault that passes health check but returns 401 on Index.md must NOT
    be reported as online — that would lie to the LLM about usability."""

    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.url.path == "/":
                return _ok({"status": "OK"})
            if request.url.path == "/vault/Index.md":
                return httpx.Response(401, json={"errorCode": 40100, "message": "bad key"})
            return httpx.Response(404)

        return {"v": vault}

    async def test_marks_unreachable_on_auth_failure(self, mcp_client):
        data = await _call(mcp_client, "list_vaults", {})
        entry = data["vaults"][0]
        assert entry["name"] == "v"
        assert entry["status"] == "unreachable"
        assert entry["index"] is None


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
        assert data == {"vault": "v", "path": "new.md", "status": "written"}


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
        assert data == {"vault": "v", "path": "log.md", "status": "appended"}


class TestPatchNote:
    @pytest.fixture
    def vault_handlers(self):
        def vault(request):
            if request.method == "PATCH" and request.url.path == "/vault/doc.md":
                assert request.headers["operation"] == "append"
                assert request.headers["target-type"] == "heading"
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
        assert data["target"] == "Report::Findings"


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
