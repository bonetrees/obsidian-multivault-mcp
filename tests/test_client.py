"""Tests for ObsidianVaultClient using httpx.MockTransport."""

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from obsidian_multivault_mcp.client import NotFound, ObsidianVaultClient


def make_client(handler, **overrides) -> ObsidianVaultClient:
    transport = httpx.MockTransport(handler)
    return ObsidianVaultClient(
        name=overrides.pop("name", "test"),
        base_url=overrides.pop("base_url", "https://127.0.0.1:27124"),
        api_key=overrides.pop("api_key", "test-key"),
        verify_ssl=False,
        transport=transport,
        **overrides,
    )


class TestAuthHeader:
    async def test_authorization_bearer_sent(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"status": "OK"})

        async with make_client(handler, api_key="my-secret") as client:
            await client.get_status()
        assert seen["auth"] == "Bearer my-secret"


class TestGetStatus:
    async def test_ok(self):
        def handler(request):
            assert request.url.path == "/"
            return httpx.Response(200, json={"status": "OK"})

        async with make_client(handler) as client:
            assert await client.get_status() is True

    async def test_non_ok_status(self):
        def handler(_request):
            return httpx.Response(200, json={"status": "ERR"})

        async with make_client(handler) as client:
            assert await client.get_status() is False

    async def test_non_200(self):
        def handler(_request):
            return httpx.Response(503)

        async with make_client(handler) as client:
            assert await client.get_status() is False

    async def test_connection_error_returns_false(self):
        def handler(_request):
            raise httpx.ConnectError("refused")

        async with make_client(handler) as client:
            assert await client.get_status() is False

    async def test_timeout_returns_false(self):
        def handler(_request):
            raise httpx.ConnectTimeout("slow")

        async with make_client(handler) as client:
            assert await client.get_status() is False


class TestReadNote:
    async def test_success(self):
        body = {
            "content": "---\ntitle: hi\n---\nbody text",
            "frontmatter": {"title": "hi"},
            "tags": ["a", "b"],
            "stat": {"ctime": 1700000000000, "mtime": 1700000001000, "size": 20},
        }

        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/vault/notes/foo.md"
            assert request.headers["accept"] == "application/vnd.olrapi.note+json"
            return httpx.Response(200, json=body)

        async with make_client(handler) as client:
            result = await client.read_note("notes/foo.md")
        assert result == body

    async def test_path_url_encoded(self):
        seen = {}

        def handler(request):
            seen["raw_path"] = request.url.raw_path.decode("ascii")
            return httpx.Response(200, json={})

        async with make_client(handler) as client:
            await client.read_note("My Folder/My Note.md")
        assert seen["raw_path"] == "/vault/My%20Folder/My%20Note.md"

    async def test_404_raises_notfound(self):
        def handler(_request):
            return httpx.Response(404, json={"errorCode": 40400, "message": "File does not exist."})

        async with make_client(handler) as client:
            with pytest.raises(NotFound) as exc_info:
                await client.read_note("missing.md")
        # NotFound is a ToolError subclass so existing handling still works.
        assert isinstance(exc_info.value, ToolError)
        msg = str(exc_info.value)
        assert "Not found" in msg
        assert "missing.md" in msg
        assert "test" in msg  # vault name in message
        assert "read_note" in msg  # operation context

    async def test_read_note_non_dict_response_raises(self):
        # Plugin spec promises an object for vnd.olrapi.note+json. A list
        # response (e.g. via a confused proxy) would otherwise crash the
        # curator with AttributeError on body.get(...).
        def handler(_request):
            return httpx.Response(200, json=["unexpected", "list"])

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert "Expected JSON object" in msg
        assert "list" in msg

    async def test_401_raises_toolerror_with_api_key_hint(self):
        def handler(_request):
            return httpx.Response(401, json={"errorCode": 40100, "message": "Unauthorized"})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert "Authentication failed" in msg
        assert "API key" in msg
        # 401 messages must carry the same operation/path context as other branches
        # so a per-vault auth failure is diagnosable from the message alone.
        assert "read_note" in msg
        assert "foo.md" in msg


class TestWriteNote:
    async def test_success_204(self):
        def handler(request):
            assert request.method == "PUT"
            assert request.url.path == "/vault/notes/foo.md"
            assert request.headers["content-type"] == "text/markdown"
            assert request.content == b"hello"
            return httpx.Response(204)

        async with make_client(handler) as client:
            await client.write_note("notes/foo.md", "hello")

    async def test_unicode_content(self):
        def handler(request):
            assert request.content == "日本語".encode("utf-8")
            return httpx.Response(204)

        async with make_client(handler) as client:
            await client.write_note("foo.md", "日本語")


class TestAppendNote:
    async def test_success(self):
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/vault/log.md"
            assert request.content == b"\nnew entry"
            return httpx.Response(204)

        async with make_client(handler) as client:
            await client.append_note("log.md", "\nnew entry")


class TestPatchNote:
    async def test_success_heading(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["operation"] = request.headers.get("operation")
            seen["target-type"] = request.headers.get("target-type")
            seen["target"] = request.headers.get("target")
            seen["content"] = request.content
            return httpx.Response(200)

        async with make_client(handler) as client:
            await client.patch_note(
                "doc.md",
                operation="append",
                target_type="heading",
                target="Report::Findings",
                content="\nnew bullet",
            )
        assert seen["method"] == "PATCH"
        assert seen["operation"] == "append"
        assert seen["target-type"] == "heading"
        # '::' encoded as %3A%3A with safe=""
        assert seen["target"] == "Report%3A%3AFindings"
        assert seen["content"] == b"\nnew bullet"

    async def test_invalid_target_raises_specific_error(self):
        def handler(_request):
            return httpx.Response(
                400,
                json={
                    "errorCode": 40080,
                    "message": "Invalid target.",
                },
            )

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.patch_note(
                    "doc.md",
                    operation="append",
                    target_type="heading",
                    target="Findings",
                    content="x",
                )
        msg = str(exc_info.value)
        assert "Invalid PATCH target" in msg
        assert "::" in msg  # hint about delimiter included


class TestDeleteNote:
    async def test_success(self):
        def handler(request):
            assert request.method == "DELETE"
            assert request.url.path == "/vault/old.md"
            return httpx.Response(204)

        async with make_client(handler) as client:
            await client.delete_note("old.md")


class TestListDirectory:
    async def test_root_path(self):
        def handler(request):
            assert request.url.path == "/vault/"
            return httpx.Response(200, json={"files": ["Index.md", "Folder/"]})

        async with make_client(handler) as client:
            result = await client.list_directory("")
        assert result == ["Index.md", "Folder/"]

    async def test_subdirectory(self):
        def handler(request):
            assert request.url.path == "/vault/notes/"
            return httpx.Response(200, json={"files": ["a.md", "b/"]})

        async with make_client(handler) as client:
            result = await client.list_directory("notes")
        assert result == ["a.md", "b/"]


class TestErrorLocationFormatting:
    """Empty-string path (used by list_directory(path="") for root) must still
    show up in error messages as '/' — not be silently dropped."""

    async def test_root_directory_404_shows_root_in_message(self):
        def handler(_request):
            return httpx.Response(404, json={"errorCode": 40400, "message": "no root"})

        async with make_client(handler) as client:
            with pytest.raises(NotFound) as exc_info:
                await client.list_directory("")
        msg = str(exc_info.value)
        assert "at '/'" in msg
        assert "list_directory" in msg

    async def test_no_path_omits_location_segment(self):
        # When path is None (search endpoints), there shouldn't be a stray
        # "at ''" segment — confirm the message just lists vault + operation.
        def handler(_request):
            return httpx.Response(500, text="upstream broken")

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.search_simple("q", context_length=100)
        msg = str(exc_info.value)
        assert "at '" not in msg


class TestListDirectoryShapeValidation:
    """A 2xx response that isn't an object-with-files-array must raise
    ToolError instead of silently returning an empty list — otherwise
    plugin/proxy contract breakage would masquerade as 'empty directory'."""

    async def test_non_dict_body_raises(self):
        def handler(_request):
            return httpx.Response(200, json=[1, 2, 3])

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.list_directory("notes")
        assert "Expected JSON object" in str(exc_info.value)

    async def test_files_not_a_list_raises(self):
        def handler(_request):
            return httpx.Response(200, json={"files": "not-a-list"})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.list_directory("notes")
        assert "files" in str(exc_info.value).lower()
        assert "JSON array" in str(exc_info.value)

    async def test_non_string_element_raises(self):
        # A non-str element would later AttributeError in the curator on
        # entry.endswith("/"). Catch it at the client boundary with the
        # bad entry's index and type for easier diagnosis.
        def handler(_request):
            return httpx.Response(200, json={"files": ["ok.md", 42, "also-ok/"]})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.list_directory("notes")
        msg = str(exc_info.value)
        assert "files[1]" in msg
        assert "int" in msg
        assert "string" in msg


class TestSearchSimple:
    async def test_query_in_url_params(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["query"] = dict(request.url.params)
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "notes/a.md",
                        "score": -0.5,
                        "matches": [{"match": {"start": 0, "end": 5}, "context": "hello world"}],
                    }
                ],
            )

        async with make_client(handler) as client:
            result = await client.search_simple("hello", context_length=50)
        assert seen["path"] == "/search/simple/"
        assert seen["query"] == {"query": "hello", "contextLength": "50"}
        assert len(result) == 1
        assert result[0]["filename"] == "notes/a.md"


class TestSearchJsonLogic:
    async def test_body_content_type(self):
        seen = {}

        def handler(request):
            seen["content-type"] = request.headers["content-type"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=[{"filename": "a.md", "result": True}])

        async with make_client(handler) as client:
            result = await client.search_jsonlogic({"in": ["tag", {"var": "tags"}]})
        assert seen["content-type"] == "application/vnd.olrapi.jsonlogic+json"
        assert seen["body"] == {"in": ["tag", {"var": "tags"}]}
        assert result == [{"filename": "a.md", "result": True}]


class TestSearchShapeValidation:
    """Every search method must raise ToolError on a non-array 2xx body
    instead of silently returning [], which would conflate 'no matches'
    with 'upstream contract breakage'."""

    async def test_simple_non_list_body_raises(self):
        def handler(_request):
            return httpx.Response(200, json={"unexpected": "object"})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.search_simple("q", context_length=100)
        assert "Expected JSON array" in str(exc_info.value)
        assert "search_simple" in str(exc_info.value)

    async def test_jsonlogic_non_list_body_raises(self):
        def handler(_request):
            return httpx.Response(200, json={"unexpected": "object"})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.search_jsonlogic({"in": ["x", {"var": "tags"}]})
        assert "Expected JSON array" in str(exc_info.value)
        assert "search_jsonlogic" in str(exc_info.value)

    async def test_dataview_non_list_body_raises(self):
        def handler(_request):
            return httpx.Response(200, json="unexpected scalar")

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.search_dataview("TABLE FROM #x")
        assert "Expected JSON array" in str(exc_info.value)
        assert "search_dataview" in str(exc_info.value)


class TestSearchDataview:
    async def test_success_returns_no_warning(self):
        seen = {}

        def handler(request):
            seen["content-type"] = request.headers["content-type"]
            seen["body"] = request.content.decode()
            return httpx.Response(200, json=[{"filename": "a.md", "result": ["row1"]}])

        async with make_client(handler) as client:
            results, warning = await client.search_dataview("TABLE FROM #tag")
        assert seen["content-type"] == "application/vnd.olrapi.dataview.dql+txt"
        assert seen["body"] == "TABLE FROM #tag"
        assert warning is None
        assert results == [{"filename": "a.md", "result": ["row1"]}]

    async def test_fallback_when_dataview_unavailable(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path, dict(request.url.params)))
            if request.url.path == "/search/":
                return httpx.Response(
                    400,
                    json={
                        "errorCode": 40070,
                        "message": "Dataview plugin is not enabled.",
                    },
                )
            # fallback hits /search/simple/
            return httpx.Response(
                200,
                json=[{"filename": "a.md", "score": -0.1, "matches": []}],
            )

        async with make_client(handler) as client:
            results, warning = await client.search_dataview("LIST FROM #x", context_length=80)
        assert warning == "Dataview not available, fell back to text search"
        assert len(calls) == 2
        assert calls[0][1] == "/search/"
        assert calls[1][1] == "/search/simple/"
        assert calls[1][2]["query"] == "LIST FROM #x"
        assert calls[1][2]["contextLength"] == "80"
        assert results[0]["filename"] == "a.md"

    async def test_other_400_still_raises(self):
        def handler(_request):
            return httpx.Response(400, json={"errorCode": 40099, "message": "Some other problem"})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.search_dataview("INVALID QUERY")
        assert "Some other problem" in str(exc_info.value)


class TestConnectAndTimeoutErrors:
    async def test_connect_error_maps_to_toolerror(self):
        def handler(_request):
            raise httpx.ConnectError("refused")

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        assert "Cannot connect" in str(exc_info.value)
        assert "Local REST API" in str(exc_info.value)

    async def test_read_timeout_reports_request_timeout(self):
        def handler(_request):
            raise httpx.ReadTimeout("slow")

        async with make_client(handler, timeout=30.0) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert "timed out after 30" in msg
        assert "Request" in msg

    async def test_connect_timeout_reports_connect_timeout(self):
        # ConnectTimeout is a TimeoutException, not a ConnectError. The
        # error message should reference CONNECT_TIMEOUT (10s), not the
        # overall request timeout.
        def handler(_request):
            raise httpx.ConnectTimeout("slow connect")

        async with make_client(handler, timeout=30.0) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert "Connection" in msg
        assert "10" in msg  # the CONNECT_TIMEOUT
        assert "30" not in msg  # not the wrong overall timeout

    async def test_other_request_error_maps_to_toolerror(self):
        # A non-connect / non-timeout RequestError must not leak past _request —
        # otherwise raw httpx exceptions break tool callers.
        def handler(_request):
            raise httpx.ReadError("connection reset")

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert "Transport error" in msg
        assert "ReadError" in msg


class TestMalformedJsonSuccessResponse:
    """A 2xx body that isn't valid JSON must map to ToolError, not a raw
    JSONDecodeError. This keeps the client's "only ToolError escapes"
    contract honest end-to-end."""

    async def test_read_note_malformed_json(self):
        def handler(_request):
            return httpx.Response(
                200,
                text="<html>upstream proxy ate the body</html>",
                headers={"content-type": "text/html"},
            )

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert "Invalid JSON response" in msg
        assert "read_note" in msg
        assert "test" in msg  # vault name in message


class TestVerifySslDefault:
    """Constructor must be safe-by-default — a caller that forgets to pass
    verify_ssl ends up with TLS verification ON, not OFF."""

    def test_default_is_true(self):
        client = ObsidianVaultClient(name="t", base_url="https://example.com:27124", api_key="k")
        # The flag is plumbed into httpx as `verify`; we read back the kwarg
        # we stored so we don't have to crack open httpx internals.
        assert client._client_kwargs["verify"] is True  # pylint: disable=protected-access


class TestEnvTimeoutFallback:
    """OBSIDIAN_MCP_TIMEOUT should not crash startup on garbage values."""

    def test_invalid_value_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("OBSIDIAN_MCP_TIMEOUT", "not-a-number")
        # No transport — we only need to verify the construct doesn't raise.
        client = ObsidianVaultClient(
            name="t",
            base_url="https://127.0.0.1:27124",
            api_key="k",
            verify_ssl=False,
        )
        assert (
            client._timeout == ObsidianVaultClient.DEFAULT_TIMEOUT
        )  # pylint: disable=protected-access

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("OBSIDIAN_MCP_TIMEOUT", raising=False)
        client = ObsidianVaultClient(
            name="t", base_url="https://127.0.0.1:27124", api_key="k", verify_ssl=False
        )
        assert (
            client._timeout == ObsidianVaultClient.DEFAULT_TIMEOUT
        )  # pylint: disable=protected-access

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_MCP_TIMEOUT", "5")
        client = ObsidianVaultClient(
            name="t", base_url="https://127.0.0.1:27124", api_key="k", verify_ssl=False
        )
        assert client._timeout == 5.0  # pylint: disable=protected-access


class TestLifecycle:
    async def test_use_outside_async_with_raises(self):
        client = make_client(lambda req: httpx.Response(200))
        with pytest.raises(RuntimeError) as exc_info:
            await client.read_note("foo.md")
        assert "outside async-with block" in str(exc_info.value)

    async def test_aexit_closes_client(self):
        client = make_client(lambda req: httpx.Response(200, json={"status": "OK"}))
        async with client:
            assert client._http is not None  # pylint: disable=protected-access
        assert client._http is None  # pylint: disable=protected-access
