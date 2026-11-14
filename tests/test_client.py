"""Tests for ObsidianVaultClient using httpx.MockTransport."""

import json
import urllib.parse

import httpx
import pytest
from fastmcp.exceptions import ToolError

from obsidian_multivault_mcp.client import NotFound, ObsidianVaultClient, PreconditionFailed
from tests.plugin_fakes import FakeVaultNote, content_etag


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

        seen = []

        def handler(request):
            assert request.url.path == "/vault/notes/foo.md"
            seen.append((request.method, request.headers["accept"]))
            if request.method == "HEAD":
                return httpx.Response(200, headers={"ETag": '"abc123"'})
            return httpx.Response(200, json=body)

        async with make_client(handler) as client:
            result, etag = await client.read_note("notes/foo.md")
        assert result == body
        assert etag == '"abc123"'
        # HEAD for the file etag strictly before the note+json GET — see
        # TestPreconditions.test_read_note_etag_is_never_newer_than_content.
        assert seen == [
            ("HEAD", "text/markdown"),
            ("GET", "application/vnd.olrapi.note+json"),
        ]

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

    @pytest.mark.parametrize(
        ("field", "bad_value", "type_label"),
        [
            ("content", 42, "string"),
            ("frontmatter", "not-a-dict", "object"),
            ("tags", "single-tag-not-list", "array"),
            ("stat", ["arr", "not", "dict"], "object"),
        ],
    )
    async def test_read_note_field_type_mismatch_raises(self, field, bad_value, type_label):
        # Each field is optional, but if present must be the right type —
        # otherwise the curator hits TypeError / AttributeError / silent
        # garbage. Catch it at the client boundary.
        def handler(_request):
            body = {
                "content": "ok",
                "frontmatter": {},
                "tags": [],
                "stat": {},
            }
            body[field] = bad_value
            return httpx.Response(200, json=body)

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert field in msg
        assert type_label in msg
        assert type(bad_value).__name__ in msg

    async def test_read_note_field_absent_ok(self):
        # Missing optional fields are fine — only wrong-typed presence raises.
        def handler(_request):
            return httpx.Response(200, json={"content": "body"})  # no frontmatter/tags/stat

        async with make_client(handler) as client:
            result, _etag = await client.read_note("foo.md")
        assert result == {"content": "body"}

    @pytest.mark.parametrize("subfield", ["ctime", "mtime", "size"])
    async def test_read_note_stat_subfield_type_mismatch_raises(self, subfield):
        # epoch_ms_to_iso does arithmetic on ctime/mtime; size hits the
        # curator's stat dict. Wrong types here would TypeError out of
        # the curator, breaking the "only ToolError escapes" contract.
        def handler(_request):
            stat = {"ctime": 1700000000000, "mtime": 1700000001000, "size": 100}
            stat[subfield] = "not-a-number"
            return httpx.Response(200, json={"content": "body", "stat": stat})

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        msg = str(exc_info.value)
        assert f"stat.{subfield}" in msg
        assert "number" in msg

    async def test_read_note_stat_subfield_bool_rejected(self):
        # bool is an int subclass; would silently be accepted as 0/1 ms
        # otherwise. Explicit rejection.
        def handler(_request):
            return httpx.Response(
                200,
                json={"content": "body", "stat": {"ctime": True}},
            )

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.read_note("foo.md")
        assert "stat.ctime" in str(exc_info.value)
        assert "bool" in str(exc_info.value)

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


def markdown_patch_v2_handler(response_factory=None):
    """Build a handler that emulates obsidian-local-rest-api's PATCH contract.

    The point is to be *strict where the plugin is strict*. The mocks this
    replaced returned 200 unconditionally and asserted nothing about required
    headers, which is why the suite stayed green while every live patch_note
    call 400'd: nothing in the tests could tell a conformant request from a
    rejected one. This handler refuses exactly what the plugin refuses, so
    dropping the version header or regressing the heading encoding turns red
    here instead of in production.
    """

    def handler(request):
        version = request.headers.get("markdown-patch-version")
        if version is None:
            # errorCode 40084: header targeting is ambiguous between the two
            # patch formats, so the version must be explicit.
            return httpx.Response(
                400,
                json={
                    "errorCode": 40084,
                    "message": "Header-based PATCH targeting requires an explicit "
                    "'Markdown-Patch-Version' header.",
                },
            )
        if version not in ("1", "2"):
            return httpx.Response(
                400,
                json={"errorCode": 40082, "message": "Invalid patch version header."},
            )
        if version == "2":
            # v1-only headers are never processed under v2; their presence is
            # itself the error.
            if request.headers.get("target-delimiter") or request.headers.get(
                "trim-target-whitespace"
            ):
                return httpx.Response(
                    400,
                    json={
                        "errorCode": 40084,
                        "message": "Target-Delimiter and Trim-Target-Whitespace are "
                        "never processed under version 2.",
                    },
                )
            if request.headers.get("target-type") == "heading":
                decoded = urllib.parse.unquote(request.headers.get("target", ""))
                try:
                    parsed = json.loads(decoded)
                except (json.JSONDecodeError, ValueError):
                    parsed = object()
                if not (
                    parsed is None
                    or (isinstance(parsed, list) and all(isinstance(seg, str) for seg in parsed))
                ):
                    return httpx.Response(
                        400,
                        json={
                            "errorCode": 40058,
                            "message": "A heading 'Target' header must be "
                            "percent-encoded JSON: an array of heading texts.",
                        },
                    )
        if response_factory is not None:
            return response_factory(request)
        return httpx.Response(200)

    return handler


class TestPatchNote:
    async def test_success_heading(self):
        seen = {}

        def record(request):
            seen["method"] = request.method
            seen["operation"] = request.headers.get("operation")
            seen["target-type"] = request.headers.get("target-type")
            seen["target"] = request.headers.get("target")
            seen["version"] = request.headers.get("markdown-patch-version")
            seen["content"] = request.content
            return httpx.Response(200)

        async with make_client(markdown_patch_v2_handler(record)) as client:
            warnings, _etag = await client.patch_note(
                "doc.md",
                operation="append",
                target_type="heading",
                target="Report::Findings",
                content="\nnew bullet",
            )
        assert seen["method"] == "PATCH"
        assert seen["operation"] == "append"
        assert seen["target-type"] == "heading"
        assert seen["version"] == "2"
        # v2 heading targets are percent-encoded JSON arrays, NOT the
        # delimiter-joined string v1 took: '["Report","Findings"]'.
        assert seen["target"] == "%5B%22Report%22%2C%22Findings%22%5D"
        assert json.loads(urllib.parse.unquote(seen["target"])) == ["Report", "Findings"]
        assert seen["content"] == b"\nnew bullet"
        assert warnings == []

    @pytest.mark.parametrize(
        ("target", "expected_segments"),
        [
            ("Report", ["Report"]),
            ("Report::Findings", ["Report", "Findings"]),
            ("Report::Findings::Critical", ["Report", "Findings", "Critical"]),
            # Whitespace is preserved, not trimmed: v1's opt-in
            # Trim-Target-Whitespace was never sent, and sending it under v2
            # is an error, so the caller's exact heading text must survive.
            ("Report:: Findings ", ["Report", " Findings "]),
            # Non-ASCII and header-hostile characters must survive the
            # JSON + percent-encoding round trip.
            ("Rapport::Findings & Co", ["Rapport", "Findings & Co"]),
            ("日本語::セクション", ["日本語", "セクション"]),
        ],
    )
    async def test_heading_target_encoded_as_json_array(self, target, expected_segments):
        seen = {}

        def record(request):
            seen["target"] = request.headers.get("target")
            return httpx.Response(200)

        async with make_client(markdown_patch_v2_handler(record)) as client:
            await client.patch_note(
                "doc.md",
                operation="replace",
                target_type="heading",
                target=target,
                content="x",
            )
        assert json.loads(urllib.parse.unquote(seen["target"])) == expected_segments
        # The header itself must be pure ASCII — httpx would reject raw
        # non-ASCII header bytes, and the plugin percent-decodes before
        # parsing.
        assert seen["target"].isascii()

    async def test_v1_only_headers_never_sent(self):
        # Under v2 the mere presence of Target-Delimiter or
        # Trim-Target-Whitespace makes the plugin reject the request. This
        # client must not send them.
        seen = {}

        def record(request):
            seen["headers"] = dict(request.headers)
            return httpx.Response(200)

        async with make_client(markdown_patch_v2_handler(record)) as client:
            await client.patch_note(
                "doc.md",
                operation="append",
                target_type="heading",
                target="Report::Findings",
                content="x",
            )
        assert "target-delimiter" not in seen["headers"]
        assert "trim-target-whitespace" not in seen["headers"]

    @pytest.mark.parametrize(
        ("headers", "expected_code"),
        [
            # The pre-fix request shape: correct in every respect except that
            # it names no patch version.
            (
                {"Operation": "append", "Target-Type": "heading", "Target": "Report%3A%3AFindings"},
                40084,
            ),
            # v1's delimiter-joined heading target, now that a version is
            # declared — the second half of the migration.
            (
                {
                    "Operation": "append",
                    "Target-Type": "heading",
                    "Target": "Report%3A%3AFindings",
                    "Markdown-Patch-Version": "2",
                },
                40058,
            ),
            # v1-only headers alongside v2.
            (
                {
                    "Operation": "append",
                    "Target-Type": "heading",
                    "Target": "%5B%22Report%22%5D",
                    "Markdown-Patch-Version": "2",
                    "Target-Delimiter": "::",
                },
                40084,
            ),
        ],
    )
    def test_contract_handler_rejects_nonconformant_requests(self, headers, expected_code):
        # Guards the guard. A mock that returns 200 for everything is what let
        # this bug ship green, so assert directly that the contract handler
        # refuses each shape the live plugin refuses — otherwise the tests
        # above could pass for the wrong reason.
        handler = markdown_patch_v2_handler()
        response = handler(
            httpx.Request(
                "PATCH",
                "https://127.0.0.1:27124/vault/doc.md",
                headers=headers,
                content=b"x",
            )
        )
        assert response.status_code == 400
        assert json.loads(response.content)["errorCode"] == expected_code

    def test_contract_handler_accepts_the_conformant_request(self):
        # The positive control for the parametrized rejections above.
        handler = markdown_patch_v2_handler()
        response = handler(
            httpx.Request(
                "PATCH",
                "https://127.0.0.1:27124/vault/doc.md",
                headers={
                    "Operation": "append",
                    "Target-Type": "heading",
                    "Target": "%5B%22Report%22%2C%22Findings%22%5D",
                    "Markdown-Patch-Version": "2",
                },
                content=b"x",
            )
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("code", [40082, 40083, 40084])
    async def test_patch_version_codes_raise_protocol_mismatch(self, code):
        # All three version complaints share one diagnostic: the client and
        # the plugin disagree about the patch format itself, which is neither
        # a bad target nor a caller error.
        def handler(_request):
            return httpx.Response(
                400,
                json={"errorCode": code, "message": "Patch version problem."},
            )

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.patch_note(
                    "doc.md",
                    operation="append",
                    target_type="heading",
                    target="Report::Findings",
                    content="x",
                )
        msg = str(exc_info.value)
        assert "PATCH protocol mismatch" in msg
        # Names the header and the version we send, so the mismatch is
        # diagnosable without reading the client source.
        assert "Markdown-Patch-Version: 2" in msg
        assert "plugin version" in msg
        assert f"errorCode {code}" in msg
        assert "doc.md" in msg

    async def test_warnings_surfaced_from_response_header(self):
        advisories = [
            {
                "code": "heading-depth-overflow",
                "message": 'Created heading "Deep" resolves to level 7.',
            }
        ]

        def record(_request):
            return httpx.Response(
                200,
                headers={"Markdown-Patch-Warnings": urllib.parse.quote(json.dumps(advisories))},
            )

        async with make_client(markdown_patch_v2_handler(record)) as client:
            warnings, _etag = await client.patch_note(
                "doc.md",
                operation="append",
                target_type="heading",
                target="Report::Findings",
                content="x",
            )
        assert warnings == advisories

    @pytest.mark.parametrize("raw", ["not-json", "%7Bnope", urllib.parse.quote('{"a":1}')])
    async def test_unparseable_warnings_header_is_dropped_not_raised(self, raw):
        # Warnings are advisory and arrive on the success path — the write
        # already happened. A header we can't parse must not be promoted into
        # a failure that would wrongly suggest nothing was written.
        def record(_request):
            return httpx.Response(200, headers={"Markdown-Patch-Warnings": raw})

        async with make_client(markdown_patch_v2_handler(record)) as client:
            warnings, _etag = await client.patch_note(
                "doc.md",
                operation="append",
                target_type="heading",
                target="Report::Findings",
                content="x",
            )
        assert warnings == []

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
        # Same operation/status/errorCode context as the other error branches
        # — so an operator who sees this in a log can locate it.
        assert "patch_note" in msg
        assert "doc.md" in msg
        assert "HTTP 400" in msg
        assert "errorCode 40080" in msg

    async def test_heading_hint_appears_on_404_target_not_found(self):
        # Under v2 an unmatched heading is a 404 (TargetNotFoundError), not a
        # 40080 — and a 404 on PATCH otherwise reads identically to "the note
        # doesn't exist". The hint has to reach this branch or the commonest
        # caller mistake loses its only clue.
        def handler(_request):
            return httpx.Response(404, json={"message": "No heading matched that address."})

        async with make_client(handler) as client:
            with pytest.raises(NotFound) as exc_info:
                await client.patch_note(
                    "doc.md",
                    operation="append",
                    target_type="heading",
                    target="Critical",
                    content="x",
                )
        msg = str(exc_info.value)
        assert "Not found" in msg
        assert "full path from the document root" in msg

    async def test_no_heading_hint_for_frontmatter_targets(self):
        # The old code appended '::' heading advice to *every* 40080,
        # including frontmatter-key patches where the delimiter is
        # meaningless. The hint is now opt-in per target type.
        def handler(_request):
            return httpx.Response(
                400, json={"errorCode": 40080, "message": "Could not apply patch."}
            )

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.patch_note(
                    "doc.md",
                    operation="replace",
                    target_type="frontmatter-key",
                    target="status",
                    content="draft",
                )
        msg = str(exc_info.value)
        assert "Invalid PATCH target" in msg
        assert "document root" not in msg
        assert "::" not in msg

    @pytest.mark.parametrize(
        ("tool_enum", "wire_value"),
        [
            ("heading", "heading"),
            ("block-reference", "block"),
            ("frontmatter-key", "frontmatter"),
        ],
    )
    async def test_target_type_enum_translation(self, tool_enum, wire_value):
        # The tool exposes a friendly enum (heading | block-reference |
        # frontmatter-key); the plugin's Target-Type header accepts only
        # heading | block | frontmatter. Passing the tool enum through
        # untranslated 400s with errorCode 40054 — that was the original
        # bug. Each tool enum must map to the right wire value.
        seen = {}

        def record(request):
            seen["target-type"] = request.headers.get("target-type")
            return httpx.Response(200)

        # Use a JSON-scalar-looking string so frontmatter passes Guard B.
        async with make_client(markdown_patch_v2_handler(record)) as client:
            await client.patch_note(
                "doc.md",
                operation="append",
                target_type=tool_enum,
                target="some-target",
                content="draft",
            )
        assert seen["target-type"] == wire_value

    async def test_frontmatter_sends_json_content_type(self):
        # The plugin interprets PATCH bodies for frontmatter targets as JSON.
        # text/markdown (the default for heading/block) would mishandle the
        # value even with a correct Target-Type.
        seen = {}

        def record(request):
            seen["content-type"] = request.headers.get("content-type")
            seen["body"] = request.content
            return httpx.Response(200)

        async with make_client(markdown_patch_v2_handler(record)) as client:
            await client.patch_note(
                "doc.md",
                operation="replace",
                target_type="frontmatter-key",
                target="status",
                content="published",
            )
        # Still application/json under v2, where it selects the instruction's
        # `value` carrier (vs `content` for a text/* body) — which is exactly
        # the right carrier for a frontmatter value, so the body needed no
        # change in the migration.
        assert seen["content-type"] == "application/json"
        # Body is JSON-encoded — json.dumps("published") wraps the string
        # in quotes so the plugin sees a JSON string, not a bare token.
        assert seen["body"] == b'"published"'

    async def test_non_frontmatter_keeps_text_markdown(self):
        # Sanity check that the JSON branch is gated on frontmatter and
        # doesn't leak into heading/block. text/markdown + raw body.
        seen = {}

        def record(request):
            seen["content-type"] = request.headers.get("content-type")
            seen["body"] = request.content
            seen["target"] = request.headers.get("target")
            return httpx.Response(200)

        async with make_client(markdown_patch_v2_handler(record)) as client:
            await client.patch_note(
                "doc.md",
                operation="append",
                target_type="block-reference",
                target="findings-summary",
                content="\nnew bullet",
            )
        assert seen["content-type"] == "text/markdown"
        assert seen["body"] == b"\nnew bullet"
        # Only *heading* targets became JSON arrays in v2. Block (and
        # frontmatter) targets stay opaque percent-encoded strings; wrapping
        # them in an array would be a second, invented breakage.
        assert seen["target"] == "findings-summary"

    @pytest.mark.parametrize(
        ("content", "kind_label"),
        [
            ('["a","b","c"]', "list"),
            ('{"k":"v"}', "dict"),
        ],
    )
    async def test_frontmatter_guard_b_rejects_structured(self, content, kind_label):
        # Guard B fires at the client boundary BEFORE any HTTP call when
        # frontmatter content parses as JSON list/dict — silently
        # stringifying structured data into a single frontmatter value is
        # the exact footgun the guard exists to prevent.
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200)

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.patch_note(
                    "doc.md",
                    operation="replace",
                    target_type="frontmatter-key",
                    target="tags",
                    content=content,
                )
        msg = str(exc_info.value)
        assert "scalar-only" in msg
        assert kind_label in msg  # mentions "list" or "dict" in the error
        assert "tags" in msg  # echoes the target so the caller can locate it
        assert not calls, "Guard B must reject before any HTTP request"

    @pytest.mark.parametrize("content", ["1.0", "true", "null", "42"])
    async def test_frontmatter_guard_b_passes_json_scalar_looking_strings(self, content):
        # The whole point of parse-to-reject (as opposed to parse-to-coerce):
        # strings that *look* like JSON scalars must be sent as JSON STRINGS
        # ("1.0" → b'"1.0"'), NOT coerced to their parsed value (1.0 float,
        # True bool, None, etc.). A caller wanting version: "1.0" should get
        # the literal string back, not silently lose precision/type.
        seen = {}

        def record(request):
            seen["body"] = request.content
            return httpx.Response(200)

        async with make_client(markdown_patch_v2_handler(record)) as client:
            await client.patch_note(
                "doc.md",
                operation="replace",
                target_type="frontmatter-key",
                target="version",
                content=content,
            )
        # json.dumps("1.0") → '"1.0"' (a JSON-encoded string), not '1.0'.
        assert seen["body"] == json.dumps(content).encode("utf-8")

    async def test_frontmatter_guard_b_passes_plain_scalar(self):
        # Bare scalar like "draft" doesn't parse as JSON (invalid token).
        # The parse exception path leaves parsed=None and the guard lets
        # it through unchanged.
        seen = {}

        def record(request):
            seen["body"] = request.content
            return httpx.Response(200)

        async with make_client(markdown_patch_v2_handler(record)) as client:
            await client.patch_note(
                "doc.md",
                operation="replace",
                target_type="frontmatter-key",
                target="status",
                content="draft",
            )
        assert seen["body"] == b'"draft"'


def precondition_handler(note: FakeVaultNote):
    """FakeVaultNote, with PATCH also held to the markdown-patch v2 contract."""
    patch_contract = markdown_patch_v2_handler(note.handler)
    return lambda request: (
        patch_contract(request) if request.method == "PATCH" else note.handler(request)
    )


async def _write(client, op, etag, path="doc.md"):
    """Run one conditional write by name; returns its result."""
    if op == "write":
        return await client.write_note(path, "# Doc\n\nrewritten\n", if_match=etag)
    if op == "append":
        return await client.append_note(path, "\nappended", if_match=etag)
    if op == "patch":
        return await client.patch_note(
            path,
            operation="append",
            target_type="heading",
            target="Doc",
            content="\npatched",
            if_match=etag,
        )
    return await client.delete_note(path, if_match=etag)


WRITE_OPS = ["write", "append", "patch", "delete"]


class TestPreconditionFake:
    """Guard the guard: if FakeVaultNote quietly went permissive, every
    precondition test below would pass against a client that sends nothing."""

    @staticmethod
    def _send(note, method, **headers):
        with httpx.Client(transport=httpx.MockTransport(note.handler)) as http:
            return http.request(method, "http://x/vault/doc.md", content=b"new", headers=headers)

    @pytest.mark.parametrize("method", ["PUT", "POST", "PATCH", "DELETE"])
    def test_stale_if_match_is_412_and_writes_nothing(self, method):
        note = FakeVaultNote("doc.md", "original")
        response = self._send(note, method, **{"If-Match": '"stale"'})
        assert response.status_code == 412
        assert response.json()["errorCode"] == 41200
        assert note.content == "original"

    @pytest.mark.parametrize("header", ["", '"a" "b"', '"a",,"b"'])
    def test_malformed_precondition_is_40024(self, header):
        note = FakeVaultNote("doc.md", "original")
        response = self._send(note, "PUT", **{"If-Match": header})
        assert (response.status_code, response.json()["errorCode"]) == (400, 40024)
        assert note.content == "original"

    def test_weak_form_of_current_tag_does_not_match(self):
        # The plugin compares strongly, as probed live.
        note = FakeVaultNote("doc.md", "original")
        response = self._send(note, "PUT", **{"If-Match": "W/" + note.etag})
        assert response.status_code == 412

    def test_if_none_match_star_refuses_existing_note(self):
        note = FakeVaultNote("doc.md", "original")
        assert self._send(note, "PUT", **{"If-None-Match": "*"}).status_code == 412
        assert note.content == "original"

    def test_note_json_get_carries_only_a_weak_tag(self):
        note = FakeVaultNote("doc.md", "original")
        with httpx.Client(transport=httpx.MockTransport(note.handler)) as http:
            response = http.get(
                "http://x/vault/doc.md",
                headers={"Accept": "application/vnd.olrapi.note+json"},
            )
        assert response.headers["etag"].startswith("W/")

    def test_tag_tracks_content_only(self):
        assert content_etag("a") == content_etag("a") != content_etag("b")


class TestPreconditions:
    async def test_read_note_returns_file_etag_not_json_body_tag(self):
        note = FakeVaultNote("doc.md", "# Doc\n")
        async with make_client(note.handler) as client:
            _body, etag = await client.read_note("doc.md")
        assert etag == note.etag

    async def test_read_note_etag_is_never_newer_than_content(self):
        # The note changes between read_note's two requests. The returned
        # tag must then be the OLDER version, so a write based on what the
        # caller actually saw fails safe instead of clobbering the change.
        note = FakeVaultNote("doc.md", "# Doc\n\nv1\n")

        def racy(request):
            response = note.handler(request)
            if request.method == "HEAD":
                note.content = "# Doc\n\nuser edit\n"
            return response

        async with make_client(racy) as client:
            body, etag = await client.read_note("doc.md")
            assert body["content"] == "# Doc\n\nuser edit\n"
            with pytest.raises(PreconditionFailed):
                await client.write_note("doc.md", "agent rewrite", if_match=etag)
        assert note.content == "# Doc\n\nuser edit\n"

    @pytest.mark.parametrize("op", WRITE_OPS)
    async def test_current_etag_succeeds(self, op):
        note = FakeVaultNote("doc.md", "# Doc\n\nbody\n")
        async with make_client(precondition_handler(note)) as client:
            _body, etag = await client.read_note("doc.md")
            result = await _write(client, op, etag)
        if op == "delete":
            assert note.content is None
            return
        new_etag = result[1] if op == "patch" else result
        # The returned tag is the note's new version, ready for the next write.
        assert new_etag == note.etag != etag

    @pytest.mark.parametrize("op", WRITE_OPS)
    async def test_out_of_band_edit_makes_write_fail_and_preserves_it(self, op):
        # Acceptance criterion: read → someone else edits → conditional write
        # is refused with a distinct error and the edit survives.
        note = FakeVaultNote("doc.md", "# Doc\n\nbody\n")
        async with make_client(precondition_handler(note)) as client:
            _body, etag = await client.read_note("doc.md")
            note.content = "# Doc\n\nuser typed this\n"
            with pytest.raises(PreconditionFailed) as exc_info:
                await _write(client, op, etag)
        assert note.content == "# Doc\n\nuser typed this\n"
        msg = str(exc_info.value)
        assert "Precondition failed" in msg
        assert "nothing was written" in msg
        assert "doc.md" in msg

    @pytest.mark.parametrize("op", WRITE_OPS)
    async def test_no_precondition_headers_without_if_match(self, op):
        # Unconditional writes must stay exactly as before.
        note = FakeVaultNote("doc.md", "# Doc\n")
        async with make_client(precondition_handler(note)) as client:
            await _write(client, op, None)
        write = note.requests[-1]
        assert "if-match" not in write.headers
        assert "if-none-match" not in write.headers

    async def test_create_only_writes_absent_note(self):
        note = FakeVaultNote("doc.md")
        async with make_client(note.handler) as client:
            etag = await client.write_note("doc.md", "fresh", if_none_match="*")
        assert note.content == "fresh"
        assert etag == note.etag

    async def test_create_only_refuses_existing_note(self):
        note = FakeVaultNote("doc.md", "keep me")
        async with make_client(note.handler) as client:
            with pytest.raises(PreconditionFailed):
                await client.write_note("doc.md", "clobber", if_none_match="*")
        assert note.content == "keep me"

    async def test_malformed_etag_is_not_reported_as_a_conflict(self):
        # 40024 means the caller mangled the tag, not that the note moved —
        # telling them to re-read and retry would loop forever.
        note = FakeVaultNote("doc.md", "x")
        async with make_client(note.handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.write_note("doc.md", "y", if_match='"a" "b"')
        assert not isinstance(exc_info.value, PreconditionFailed)
        assert "Malformed etag" in str(exc_info.value)
        assert note.content == "x"

    @pytest.mark.parametrize("header", [None, "", 'W/"109-abc"'])
    def test_etag_drops_missing_and_weak_tags(self, header):
        headers = {} if header is None else {"ETag": header}
        assert ObsidianVaultClient._etag(httpx.Response(200, headers=headers)) is None


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

    async def test_root_path_in_shape_error_renders_as_slash(self):
        # path="" must render as "at '/'", consistent with _raise_for_status,
        # rather than the bare "at ''" the inline f-string used to produce.
        def handler(_request):
            return httpx.Response(200, json=[1, 2, 3])

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.list_directory("")
        msg = str(exc_info.value)
        assert "at '/'" in msg
        assert "at ''" not in msg


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

    async def test_search_simple_non_dict_element_raises(self):
        # Search curators do .get("filename"); a non-dict element would
        # AttributeError out of the tool layer and break the
        # "only ToolError escapes" contract.
        def handler(_request):
            return httpx.Response(
                200, json=[{"filename": "a.md", "score": -0.5, "matches": []}, "stray-string"]
            )

        async with make_client(handler) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.search_simple("q", context_length=100)
        msg = str(exc_info.value)
        assert "[1]" in msg
        assert "JSON object" in msg
        assert "str" in msg


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

    @pytest.mark.parametrize("bad", ["0", "-1", "-0.5", "nan", "inf", "-inf"])
    def test_non_positive_or_non_finite_falls_back(self, monkeypatch, bad):
        # httpx.Timeout(0) or Timeout(-1) or Timeout(nan) all raise at
        # client construction time — bringing down the server at lifespan
        # startup. _resolve_timeout_env must fall back to DEFAULT_TIMEOUT.
        monkeypatch.setenv("OBSIDIAN_MCP_TIMEOUT", bad)
        client = ObsidianVaultClient(
            name="t", base_url="https://127.0.0.1:27124", api_key="k", verify_ssl=False
        )
        assert (
            client._timeout == ObsidianVaultClient.DEFAULT_TIMEOUT
        )  # pylint: disable=protected-access


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
