"""Async HTTP client for one obsidian-local-rest-api vault.

One ObsidianVaultClient instance per configured vault. Each method maps to a
single REST endpoint and raises ToolError on transport or HTTP failure.
"""

import json
import math
import os
import urllib.parse
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from .logging_config import setup_logging

logger = setup_logging("obsidian-multivault-mcp.client")


class NotFound(ToolError):
    """Raised when an Obsidian REST endpoint returns HTTP 404.

    Subclass of ToolError so FastMCP still surfaces it correctly, but
    callers (e.g. list_vaults) can catch this specific case without
    inspecting error message strings.
    """


class PreconditionFailed(ToolError):
    """Raised when a conditional write is refused with HTTP 412.

    The plugin guarantees nothing was written. Kept distinct from ToolError
    so the tool layer can reword it for create-only writes, where "the
    precondition failed" really means "the note already exists".
    """


class ObsidianVaultClient:
    HEALTH_CHECK_TIMEOUT = 3.0
    DEFAULT_TIMEOUT = 30.0
    CONNECT_TIMEOUT = 10.0

    DATAVIEW_NOT_INSTALLED_CODE = 40070
    INVALID_PATCH_TARGET_CODE = 40080

    # markdown-patch protocol version for header-based PATCH targeting.
    # obsidian-local-rest-api 5.x serves two incompatible patch formats and
    # treats header targeting as ambiguous between them, so it *rejects* a
    # header-targeted PATCH that doesn't name a version (errorCode 40084) —
    # which is why every patch_note call must carry this header. v1 is still
    # accepted but the plugin advertises sunset-version="6.0" against it, so
    # v2 is the only forward-compatible choice.
    MARKDOWN_PATCH_VERSION = "2"
    MARKDOWN_PATCH_VERSION_HEADER = "Markdown-Patch-Version"

    # Advisory "applied, but..." notices returned on the v2 success path.
    PATCH_WARNINGS_HEADER = "Markdown-Patch-Warnings"

    # Protocol-version complaints, as opposed to "your target was wrong":
    # 40082 unparseable version, 40083 v1-only headers sent without
    # "Markdown-Patch-Version: 1", 40084 header targeting with no version at
    # all. All three mean this client and the plugin disagree about the patch
    # format itself, so they share one diagnostic.
    PATCH_VERSION_CODES = frozenset({40082, 40083, 40084})

    # Tool-facing heading paths are delimiter-joined ("A::B"); v2 addresses
    # headings structurally, as a JSON array of segments.
    HEADING_PATH_DELIMITER = "::"

    # Malformed If-Match / If-None-Match (e.g. empty, or two tags without a
    # comma). An unquoted tag is NOT this — the plugin treats it as a tag
    # that never matches, so it comes back as a 412 instead.
    INVALID_PRECONDITION_CODE = 40024

    @classmethod
    def _resolve_timeout_env(cls) -> float:
        raw = os.environ.get("OBSIDIAN_MCP_TIMEOUT")
        if raw is None or raw == "":
            return cls.DEFAULT_TIMEOUT
        try:
            value = float(raw)
        except ValueError:
            # Don't crash the server over a mis-set env var — log and fall back.
            logger.warning(
                "Ignoring invalid OBSIDIAN_MCP_TIMEOUT=%r (expected number); using default %s.",
                raw,
                cls.DEFAULT_TIMEOUT,
            )
            return cls.DEFAULT_TIMEOUT
        # Reject 0, negatives, NaN, ±inf — httpx.Timeout(...) would raise on
        # construction and take the server down at lifespan startup.
        if not math.isfinite(value) or value <= 0:
            logger.warning(
                "Ignoring OBSIDIAN_MCP_TIMEOUT=%r (must be a positive finite number); "
                "using default %s.",
                raw,
                cls.DEFAULT_TIMEOUT,
            )
            return cls.DEFAULT_TIMEOUT
        return value

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # verify_ssl defaults to True so the constructor is safe-by-default.
        # The server lifespan explicitly passes the loopback-derived value
        # from VaultConfig.verify_ssl; a future call site that forgets to
        # plumb it through gets TLS verification on, which is the right
        # failure mode for any non-loopback HTTPS plugin instance.
        self.name = name
        self.base_url = base_url
        self._timeout = timeout if timeout is not None else self._resolve_timeout_env()
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "headers": {"Authorization": f"Bearer {api_key}"},
            "timeout": httpx.Timeout(self._timeout, connect=self.CONNECT_TIMEOUT),
            "verify": verify_ssl,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client_kwargs = kwargs
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ObsidianVaultClient":
        self._http = httpx.AsyncClient(**self._client_kwargs)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- helpers ---

    def _require_http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(
                f"ObsidianVaultClient for '{self.name}' used outside async-with block"
            )
        return self._http

    @staticmethod
    def _encode_path(path: str) -> str:
        return urllib.parse.quote(path, safe="/")

    @staticmethod
    def _safe_json_object(response: httpx.Response) -> dict:
        """Parse the response body as a JSON object; return {} on any failure.

        Used on both error paths (extract message/errorCode) and the
        health-check success path (extract status). Never raises.
        """
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}

    def _parse_json(self, response: httpx.Response, operation: str) -> Any:
        """Parse a success-path response body and re-raise decode errors as ToolError.

        Without this, a malformed 2xx body would surface as a raw
        json.JSONDecodeError to tool callers, breaking the client's
        "only ToolError escapes" contract.
        """
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            snippet = response.text[:120] if response.text else "(empty body)"
            raise ToolError(
                f"Invalid JSON response from vault '{self.name}' during {operation}: "
                f"{type(exc).__name__}: {exc}. Body started with: {snippet!r}"
            ) from exc

    def _require_list_of_dicts(self, body: Any, operation: str) -> list[dict]:
        """Validate that a parsed success-path body is a JSON array of objects.

        Search endpoints return arrays whose elements are dicts (the
        curators access them via ``.get("filename")``, ``.get("score")``,
        etc.). A non-dict element would otherwise raise ``AttributeError``
        in the tool layer and break the "only ToolError escapes" contract.
        Validate both layers and raise ``ToolError`` naming the bad index
        on element-level mismatch.
        """
        if not isinstance(body, list):
            raise ToolError(
                f"Expected JSON array from vault '{self.name}' during {operation}, "
                f"got {type(body).__name__}."
            )
        for i, entry in enumerate(body):
            if not isinstance(entry, dict):
                raise ToolError(
                    f"Expected '[{i}]' to be a JSON object from vault '{self.name}' "
                    f"during {operation}, got {type(entry).__name__}."
                )
        return body

    @staticmethod
    def _precondition_headers(
        if_match: str | None = None, if_none_match: str | None = None
    ) -> dict[str, str]:
        headers = {}
        if if_match is not None:
            headers["If-Match"] = if_match
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        return headers

    @staticmethod
    def _etag(response: httpx.Response) -> str | None:
        """Return the file's entity tag from a successful vault response.

        The plugin sets a strong, content-derived ETag on GET text/markdown
        (and HEAD), PUT, POST and PATCH. Express also stamps its own *weak*
        tag on any JSON body it serialises — the note+json GET and every
        error response — but that tag hashes the response, not the file, and
        the plugin compares If-Match strongly, so it could never match.
        Dropping weak tags means a caller gets None rather than a tag that
        guarantees a 412.
        """
        tag = response.headers.get("ETag")
        if not tag or tag.startswith("W/"):
            return None
        return tag

    @staticmethod
    def _format_location(path: str | None) -> str:
        """Render the ' at <path>' segment used in error messages.

        - `None`         → ''            (path-less endpoints, e.g. search)
        - `''`           → " at '/'"     (root listing — list_directory(path=""))
        - any other str  → f" at '{path}'"
        """
        if path is None:
            return ""
        if path == "":
            return " at '/'"
        return f" at '{path}'"

    def _raise_for_status(
        self,
        response: httpx.Response,
        operation: str,
        path: str | None = None,
        hint: str | None = None,
    ) -> None:
        """Map an error response to ToolError (or NotFound for HTTP 404).

        ``hint`` appends caller-supplied guidance to the branches where the
        caller knows something the status code doesn't — currently
        patch_note's heading-path syntax, which is the likeliest cause of a
        404 or a failed patch but means nothing on a 401. Passing it in beats
        hardcoding it here: the old unconditional '::' advice on every 40080
        told callers patching a *frontmatter key* to go check their heading
        delimiter.
        """
        if response.is_success:
            return
        body = self._safe_json_object(response)
        msg = body.get("message") or (response.text.strip() if response.text else "(no body)")
        code = body.get("errorCode")
        location = self._format_location(path)
        hint_part = f" {hint}" if hint else ""
        if response.status_code == 401:
            raise ToolError(
                f"Authentication failed for vault '{self.name}' during {operation}"
                f"{location} (HTTP 401). Check the API key. ({msg})"
            )
        if response.status_code == 404:
            raise NotFound(
                f"Not found{location} in vault '{self.name}' during {operation}: "
                f"{msg}{hint_part}"
            )
        if response.status_code == 412:
            raise PreconditionFailed(
                f"Precondition failed{location} in vault '{self.name}' during "
                f"{operation} (HTTP 412); nothing was written: {msg}{hint_part}"
            )
        if response.status_code == 405:
            raise ToolError(
                f"Operation not supported{location} in vault '{self.name}' "
                f"during {operation}: {msg}"
            )
        if code in self.PATCH_VERSION_CODES:
            # Not the caller's fault and not fixable by changing the target:
            # this client and the plugin disagree about the patch protocol.
            # Name the header and the version we send so the mismatch is
            # diagnosable from one log line, without reading our source.
            raise ToolError(
                f"PATCH protocol mismatch for vault '{self.name}' during {operation}"
                f"{location} (HTTP {response.status_code}, errorCode {code}): {msg}. "
                f"This client sends '{self.MARKDOWN_PATCH_VERSION_HEADER}: "
                f"{self.MARKDOWN_PATCH_VERSION}'; the installed obsidian-local-rest-api "
                "expects a different patch format. Check the plugin version."
            )
        if code == self.INVALID_PRECONDITION_CODE:
            raise ToolError(
                f"Malformed etag{location} in vault '{self.name}' during "
                f"{operation} (HTTP {response.status_code}, errorCode {code}): {msg}. "
                "Pass the etag exactly as read_note or a previous write returned it, "
                "quotes included."
            )
        if code == self.INVALID_PATCH_TARGET_CODE:
            raise ToolError(
                f"Invalid PATCH target{location} in vault '{self.name}' during "
                f"{operation} (HTTP {response.status_code}, errorCode {code}): "
                f"{msg}{hint_part}"
            )
        code_part = f", errorCode {code}" if code is not None else ""
        raise ToolError(
            f"{operation} failed for vault '{self.name}'{location} "
            f"(HTTP {response.status_code}{code_part}): {msg}{hint_part}"
        )

    async def _request(
        self,
        method: str,
        url: str,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        http = self._require_http()
        try:
            return await http.request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            # Network-level failures (refused, no route, DNS). Note that
            # ConnectTimeout is a TimeoutException, NOT a ConnectError, so it
            # is handled by the more-specific timeout branch below.
            raise ToolError(
                f"Cannot connect to vault '{self.name}' at {self.base_url}. "
                f"Is Obsidian running with the Local REST API plugin enabled? ({exc})"
            ) from exc
        except httpx.ConnectTimeout as exc:
            # Connection establishment timed out — the relevant timeout is the
            # connect timeout, not the overall request timeout. Reporting the
            # wrong number sends operators looking in the wrong place.
            raise ToolError(
                f"Connection to vault '{self.name}' timed out after "
                f"{self.CONNECT_TIMEOUT}s during {operation}. ({exc})"
            ) from exc
        except httpx.TimeoutException as exc:
            # Read / write / pool timeout — bounded by the overall request timeout.
            raise ToolError(
                f"Request to vault '{self.name}' timed out after {self._timeout}s "
                f"during {operation}. ({exc})"
            ) from exc
        except httpx.RequestError as exc:
            # Catch-all for any other transport-layer error (protocol errors,
            # read errors, decoding errors, …) that isn't connect/timeout.
            # Keeps the module's "raises ToolError on transport failure"
            # contract honest — nothing httpx-shaped leaks past _request.
            raise ToolError(
                f"Transport error talking to vault '{self.name}' during {operation}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    # --- public API ---

    async def get_status(self) -> bool:
        """Quick liveness probe. Returns True if the plugin responds with status OK.

        Uses a tight 3 s timeout independent of OBSIDIAN_MCP_TIMEOUT so list_vaults
        does not hang when one vault is offline. Never raises — failures map to False.
        """
        if self._http is None:
            return False
        try:
            response = await self._http.get("/", timeout=self.HEALTH_CHECK_TIMEOUT)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
            return False
        if response.status_code != 200:
            return False
        body = self._safe_json_object(response)
        return body.get("status") == "OK"

    # vnd.olrapi.note+json response shape: each field is optional but if
    # present must match the type listed here. Validated at the client
    # boundary so the curator can rely on shape ("only ToolError escapes").
    _READ_NOTE_FIELDS: tuple[tuple[str, type, str], ...] = (
        ("content", str, "string"),
        ("frontmatter", dict, "object"),
        ("tags", list, "array"),
        ("stat", dict, "object"),
    )

    async def read_note(self, path: str) -> tuple[dict, str | None]:
        """Returns (note+json body, file etag).

        The note+json GET carries no usable file ETag (see ``_etag``), so the
        tag comes from a body-less HEAD issued *first*. The order is the
        safety property: if the note changes between the two requests, the
        tag is older than the content returned, so a write conditioned on it
        fails with 412 instead of silently overwriting a change the caller
        never saw. Reversing the order would hand back a tag newer than the
        content — exactly the lost update this exists to prevent.
        """
        url = f"/vault/{self._encode_path(path)}"
        head = await self._request(
            "HEAD", url, operation="read_note", headers={"Accept": "text/markdown"}
        )
        self._raise_for_status(head, "read_note", path)
        etag = self._etag(head)
        response = await self._request(
            "GET",
            url,
            operation="read_note",
            headers={"Accept": "application/vnd.olrapi.note+json"},
        )
        self._raise_for_status(response, "read_note", path)
        body = self._parse_json(response, "read_note")
        location = self._format_location(path)
        if not isinstance(body, dict):
            # The plugin spec promises an object for the note+json Accept type.
            # Validating here means the curator can rely on `.get()` access
            # instead of crashing with AttributeError on a misbehaving proxy.
            raise ToolError(
                f"Expected JSON object from vault '{self.name}' during read_note"
                f"{location}, got {type(body).__name__}."
            )
        for field_name, expected_type, type_label in self._READ_NOTE_FIELDS:
            value = body.get(field_name)
            if value is None:
                # Field absent — fine, the curator handles missing fields.
                continue
            if not isinstance(value, expected_type):
                raise ToolError(
                    f"Expected '{field_name}' to be a JSON {type_label} from vault "
                    f"'{self.name}' during read_note{location}, got {type(value).__name__}."
                )
        # stat subfields feed arithmetic in epoch_ms_to_iso — wrong types here
        # would raise TypeError out of the curator and break the
        # "only ToolError escapes" contract.
        stat = body.get("stat")
        if isinstance(stat, dict):
            for sub in ("ctime", "mtime", "size"):
                value = stat.get(sub)
                if value is None:
                    continue
                # JSON numbers are int or float; bool is an int subclass but
                # shouldn't be accepted as a timestamp/size.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ToolError(
                        f"Expected 'stat.{sub}' to be a number from vault "
                        f"'{self.name}' during read_note{location}, "
                        f"got {type(value).__name__}."
                    )
        return body, etag

    async def write_note(
        self,
        path: str,
        content: str,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str | None:
        """PUT the whole note; returns its new etag."""
        response = await self._request(
            "PUT",
            f"/vault/{self._encode_path(path)}",
            operation="write_note",
            content=content.encode("utf-8"),
            headers={
                "Content-Type": "text/markdown",
                **self._precondition_headers(if_match, if_none_match),
            },
        )
        self._raise_for_status(response, "write_note", path)
        return self._etag(response)

    async def append_note(self, path: str, content: str, if_match: str | None = None) -> str | None:
        """POST content onto the end of the note; returns its new etag."""
        response = await self._request(
            "POST",
            f"/vault/{self._encode_path(path)}",
            operation="append_note",
            content=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown", **self._precondition_headers(if_match)},
        )
        self._raise_for_status(response, "append_note", path)
        return self._etag(response)

    # Tool-facing enum (PatchTargetType) → obsidian-local-rest-api Target-Type
    # vocabulary. The plugin accepts only "heading", "block", "frontmatter";
    # sending the tool's friendlier names verbatim 400s with errorCode 40054.
    _TARGET_TYPE_WIRE = {
        "heading": "heading",
        "block-reference": "block",
        "frontmatter-key": "frontmatter",
    }

    # Passed to _raise_for_status as `hint` for heading targets only — see the
    # patch_note call site for why the 404 branch needs it.
    HEADING_TARGET_HINT = (
        "Heading targets must be the full path from the document root, with "
        "'::' as the separator (e.g. 'Report::Findings::Critical') — a bare "
        "child heading will not match."
    )

    @classmethod
    def _encode_patch_target(cls, wire_target_type: str, target: str) -> str:
        """Percent-encode the Target header for one markdown-patch v2 request.

        Tool-facing heading paths stay delimiter-joined ('Report::Findings'),
        but v2 addresses headings structurally: the header carries
        percent-encoded JSON — an array of heading texts — so the plugin never
        has to guess where one heading name ends and the next begins. The
        delimiter is therefore a detail of this client's tool API rather than a
        wire concern. It is still a real limit, just a self-imposed one now: a
        heading whose own text contains '::' cannot be addressed, because the
        split here can't tell it apart from a path separator. The wire format
        could express it; the tool API can't.

        Segments are deliberately NOT trimmed. v1 had an opt-in
        Trim-Target-Whitespace header that this client never sent, and sending
        it under v2 is itself an error (40084), so passing the caller's exact
        heading text through keeps behaviour identical across the migration.

        Block and frontmatter targets are opaque strings in both versions.
        """
        if wire_target_type != "heading":
            return urllib.parse.quote(target, safe="")
        segments = target.split(cls.HEADING_PATH_DELIMITER)
        # Compact separators keep the header short; the plugin JSON.parses it.
        return urllib.parse.quote(json.dumps(segments, separators=(",", ":")), safe="")

    def _parse_patch_warnings(self, response: httpx.Response) -> list[dict]:
        """Extract the advisory notices a successful v2 patch can carry.

        The plugin reports "applied, but..." conditions (e.g. a created
        heading nested past Markdown's six levels, which leaves it not
        structurally addressable) as percent-encoded JSON in a response
        header. These are advisory by construction — the write already
        happened — so a header this client can't parse is logged and dropped
        rather than converted into a failure that would wrongly imply nothing
        was written.
        """
        raw = response.headers.get(self.PATCH_WARNINGS_HEADER)
        if not raw:
            return []
        try:
            parsed = json.loads(urllib.parse.unquote(raw))
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if not isinstance(parsed, list):
            logger.warning(
                "Ignoring unparseable %s header from vault '%s': %r",
                self.PATCH_WARNINGS_HEADER,
                self.name,
                raw,
            )
            return []
        # Entries are {"code": ..., "message": ...} objects; wrap anything
        # else so the tool layer always hands back a uniform list of dicts.
        return [entry if isinstance(entry, dict) else {"message": str(entry)} for entry in parsed]

    async def patch_note(
        self,
        path: str,
        operation: str,
        target_type: str,
        target: str,
        content: str,
        if_match: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Apply one surgical edit; returns (advisory warnings, new etag)."""
        wire_target_type = self._TARGET_TYPE_WIRE.get(target_type, target_type)
        headers = {
            "Operation": operation,
            "Target-Type": wire_target_type,
            "Target": self._encode_patch_target(wire_target_type, target),
            # Required: header targeting without an explicit version is
            # rejected outright (40084). Note that Target-Delimiter and
            # Trim-Target-Whitespace must NOT accompany this — the plugin
            # reads either one as a v1 request and 40084s. This client has
            # never sent them; don't start.
            self.MARKDOWN_PATCH_VERSION_HEADER: self.MARKDOWN_PATCH_VERSION,
            **self._precondition_headers(if_match),
        }
        # Frontmatter PATCH bodies are interpreted as JSON by the plugin, which
        # enables intelligent merging of structured values. A scalar string
        # round-trips correctly through json.dumps — e.g. json.dumps("done")
        # yields the 6-byte JSON-string b'"done"' (quotes included), which the
        # plugin then writes as the YAML string "done". Array values are a
        # separate, unsolved case (the tool types content as str).
        if wire_target_type == "frontmatter":
            # Guard B (parse-to-reject): patch_note frontmatter is scalar-only.
            # If content parses as a JSON array/object the caller almost
            # certainly means structured data, which json.dumps(str) would
            # silently stringify into one wrong value. Fail loud rather than
            # silently stringify. NOTE: the parse result is used ONLY as a
            # yes/no classification signal and then discarded — the untouched
            # original string is what gets sent. This is deliberately the
            # opposite data flow from "parse and use the result as the value"
            # (which would silently coerce scalars like "1.0" → 1.0).
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, (list, dict)):
                # Match _raise_for_status' message shape (vault + operation +
                # location) so a Guard B failure is diagnosable from a log
                # line alone — there's no HTTP response to lean on. Don't
                # name a successor tool: pointing callers at something that
                # doesn't exist yet is worse than naming nothing.
                location = self._format_location(path)
                raise ToolError(
                    f"patch_note frontmatter targets are scalar-only for vault "
                    f"'{self.name}'{location}, but content for '{target}' looks like "
                    f"a JSON {type(parsed).__name__}. Array or object frontmatter "
                    f"is not supported by this tool."
                )
            headers["Content-Type"] = "application/json"
            body = json.dumps(content).encode("utf-8")
        else:
            headers["Content-Type"] = "text/markdown"
            body = content.encode("utf-8")
        response = await self._request(
            "PATCH",
            f"/vault/{self._encode_path(path)}",
            operation="patch_note",
            content=body,
            headers=headers,
        )
        # The heading hint rides along only for heading targets. Under v2 an
        # unmatched heading comes back as 404 (TargetNotFoundError) rather
        # than 40080, and a 404 on PATCH is otherwise indistinguishable from
        # "the note itself doesn't exist" — so the hint has to cover both.
        self._raise_for_status(
            response,
            "patch_note",
            path,
            hint=self.HEADING_TARGET_HINT if wire_target_type == "heading" else None,
        )
        return self._parse_patch_warnings(response), self._etag(response)

    async def delete_note(self, path: str, if_match: str | None = None) -> None:
        response = await self._request(
            "DELETE",
            f"/vault/{self._encode_path(path)}",
            operation="delete_note",
            headers=self._precondition_headers(if_match),
        )
        self._raise_for_status(response, "delete_note", path)

    async def list_directory(self, path: str) -> list[str]:
        url = f"/vault/{self._encode_path(path)}/" if path else "/vault/"
        response = await self._request("GET", url, operation="list_directory")
        self._raise_for_status(response, "list_directory", path)
        body = self._parse_json(response, "list_directory")
        # Plugin spec: 2xx body is an object with a `files` array. Validate
        # both layers so an upstream/proxy contract breakage surfaces as a
        # ToolError instead of a misleading "empty directory" result.
        location = self._format_location(path)
        if not isinstance(body, dict):
            raise ToolError(
                f"Expected JSON object from vault '{self.name}' during list_directory"
                f"{location}, got {type(body).__name__}."
            )
        files = body.get("files")
        if not isinstance(files, list):
            raise ToolError(
                f"Expected 'files' to be a JSON array from vault '{self.name}' "
                f"during list_directory{location}, got {type(files).__name__}."
            )
        # Element-level guard so the curator's `entry.endswith("/")` can't blow
        # up with AttributeError on a wrong-shaped element. ToolError fires
        # with the bad entry's type for easier diagnosis.
        for i, entry in enumerate(files):
            if not isinstance(entry, str):
                raise ToolError(
                    f"Expected 'files[{i}]' to be a string from vault '{self.name}' "
                    f"during list_directory{location}, got {type(entry).__name__}."
                )
        return list(files)

    async def search_simple(self, query: str, context_length: int) -> list[dict]:
        response = await self._request(
            "POST",
            "/search/simple/",
            operation="search_simple",
            params={"query": query, "contextLength": context_length},
        )
        self._raise_for_status(response, "search_simple")
        body = self._parse_json(response, "search_simple")
        return self._require_list_of_dicts(body, "search_simple")

    async def search_jsonlogic(self, query: dict) -> list[dict]:
        response = await self._request(
            "POST",
            "/search/",
            operation="search_jsonlogic",
            content=json.dumps(query).encode("utf-8"),
            headers={"Content-Type": "application/vnd.olrapi.jsonlogic+json"},
        )
        self._raise_for_status(response, "search_jsonlogic")
        body = self._parse_json(response, "search_jsonlogic")
        return self._require_list_of_dicts(body, "search_jsonlogic")

    async def search_dataview(
        self, query: str, context_length: int = 100
    ) -> tuple[list[dict], str | None]:
        """Returns (results, warning). On Dataview-not-installed (40070), falls
        back to search_simple and the warning is populated.
        """
        response = await self._request(
            "POST",
            "/search/",
            operation="search_dataview",
            content=query.encode("utf-8"),
            headers={"Content-Type": "application/vnd.olrapi.dataview.dql+txt"},
        )
        if response.status_code == 400:
            body = self._safe_json_object(response)
            if body.get("errorCode") == self.DATAVIEW_NOT_INSTALLED_CODE:
                logger.info(
                    "Dataview not installed in vault '%s'; falling back to text search.",
                    self.name,
                )
                fallback = await self.search_simple(query, context_length)
                return fallback, "Dataview not available, fell back to text search"
        self._raise_for_status(response, "search_dataview")
        body = self._parse_json(response, "search_dataview")
        return self._require_list_of_dicts(body, "search_dataview"), None
