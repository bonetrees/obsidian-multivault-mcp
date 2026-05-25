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
        assert "Not found" in str(exc_info.value)
        assert "missing.md" in str(exc_info.value)
        assert "test" in str(exc_info.value)  # vault name in message

    async def test_401_raises_toolerror_with_api_key_hint(self):
        def handler(_request):
            return httpx.Response(401, json={"errorCode": 40100, "message": "Unauthorized"})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        assert "Authentication failed" in str(exc_info.value)
        assert "API key" in str(exc_info.value)


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
