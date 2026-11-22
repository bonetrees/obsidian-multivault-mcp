"""Async HTTP client for one obsidian-local-rest-api vault.

One ObsidianVaultClient instance per configured vault. Each method maps to a
single REST endpoint and raises ToolError on transport or HTTP failure.
"""

# One class holds every endpoint by design (see CLAUDE.md), and most of its
# length is the protocol notes beside the code.
# pylint: disable=too-many-lines

import json
import math
import os
import re
import urllib.parse
from collections import Counter
from datetime import date
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
        404 or a failed patch but means nothing on a 401 or a 412. Passing it in beats
        hardcoding it here: the old unconditional '::' advice on every 40080
        told callers patching a *frontmatter key* to go check their heading
        delimiter.
        """
        if response.is_success:
            return
        body = self._safe_json_object(response)
        msg = body.get("message") or (response.text.strip() if response.text else "(no body)")
        msg = msg.rstrip()
        code = body.get("errorCode")
        location = self._format_location(path)
        # Plugin messages often end without a full stop ('…has 2 top-level
        # blocks'), which would run straight into the hint's first sentence.
        hint_part = ""
        if hint:
            hint_part = f" {hint}" if msg.endswith((".", "!", "?")) else f". {hint}"
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
            # No hint: a stale version has nothing to do with how the target
            # was addressed, and target advice would send the caller chasing
            # the wrong fix.
            raise PreconditionFailed(
                f"Precondition failed{location} in vault '{self.name}' during "
                f"{operation} (HTTP 412); nothing was written: {msg}"
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

    # Frontmatter counterpart to HEADING_TARGET_HINT. It rides on both the
    # 404 branch (key absent — under v2 that's "could not resolve frontmatter
    # target", which otherwise reads like the note is missing) and the 40080
    # branch (append/prepend across mismatched types, "not mergeable").
    FRONTMATTER_TARGET_HINT = (
        "If the note exists but the frontmatter key doesn't, pass "
        "create_if_missing=True to create it. append/prepend only merge two "
        "lists, two objects, or two strings; use replace to change a value's type."
    )

    # Opt-in key creation. v1 header that v2 still honours for frontmatter
    # targets (verified against 5.5.2, including on a note with no
    # frontmatter block at all, which it creates).
    CREATE_TARGET_HEADER = "Create-Target-If-Missing"

    # Tool-facing delete scope → v2 Target-Scope. Sent only for deletes and
    # the sibling inserts below, so every other operation keeps its exact
    # pre-delete wire shape. "all" is
    # the tool default because the plugin's own default (content) is the
    # surprising one: it leaves `key: null` behind on frontmatter and, on a
    # heading, keeps the heading line while deleting its body AND children.
    TARGET_SCOPE_HEADER = "Target-Scope"
    _DELETE_SCOPE_WIRE = {"all": "markerAndContent", "content": "content", "marker": "marker"}

    # Sibling inserts → v2 append/prepend at markerAndContent scope, which
    # inserts a new section after the target's whole subtree (append) or
    # before its heading line (prepend). The content is rebased by the
    # *parent's* level, so `#` lands at the target's own level. A heading
    # append/replace can only ever produce children, so this is the one way
    # to add a sibling section without rewriting the note.
    _INSERT_WIRE = {"insert_after": "append", "insert_before": "prepend"}

    # Splices the edit into one of a heading's intro blocks: the top-level
    # blocks above its first child heading (0 = first, -1 = last). Verified
    # live with header targeting on 5.5.2: children are never touched, and an
    # out-of-range index is 404/40400 naming how many blocks there are.
    WITHIN_HEADER = "Within"
    WITHIN_HINT = (
        "'within' counts the top-level blocks (paragraphs, lists, tables…) "
        "above the heading's first child heading: 0 is the first, -1 the last. "
        "A heading with no text above its first child has none."
    )
    # A 404 on a `within` PATCH can mean any of three misses, and the
    # plugin's message only names the last two (a missing note is a bare
    # "Not Found"), so the hint covers all three rather than guessing.
    WITHIN_TARGET_HINT = (
        "With 'within', a 404 means the note doesn't exist, the heading path "
        "didn't match, or the block index is out of range (the plugin's message "
        f"names the last two). {HEADING_TARGET_HINT} {WITHIN_HINT}"
    )

    # v2 document map: the note's heading tree as nested objects, e.g.
    # {"Doc": {"Child": {}, "Sib": {}}}. Used to find a heading's children.
    DOCUMENT_MAP_ACCEPT = "application/vnd.olrapi.document-map+json"

    @classmethod
    def _heading_segments(cls, target: str | list[str]) -> list[str]:
        """A heading target as the list of heading texts v2 addresses it by."""
        if isinstance(target, list):
            return target
        return target.split(cls.HEADING_PATH_DELIMITER)

    @classmethod
    def _encode_patch_target(cls, wire_target_type: str, target: str | list[str]) -> str:
        """Percent-encode the Target header for one markdown-patch v2 request.

        Tool-facing heading paths stay delimiter-joined ('Report::Findings'),
        but v2 addresses headings structurally: the header carries
        percent-encoded JSON — an array of heading texts — so the plugin never
        has to guess where one heading name ends and the next begins. The
        delimiter is therefore a detail of this client's tool API rather than a
        wire concern. It is still a real limit, just a self-imposed one now: a
        heading whose own text contains '::' can't be addressed as a string,
        because the split here can't tell it apart from a path separator. So
        heading targets may also be a list of heading texts, which is sent as
        the JSON array verbatim with no splitting.

        A list must never be empty: `[]` addresses the document *root*, where
        a delete wipes the entire body. The tool layer's HeadingSegments type
        already refuses it; _check_patch_args refuses it again so no future
        caller of this client can reach that by accident.

        Segments are deliberately NOT trimmed. v1 had an opt-in
        Trim-Target-Whitespace header that this client never sent, and sending
        it under v2 is itself an error (40084), so passing the caller's exact
        heading text through keeps behaviour identical across the migration.

        Block and frontmatter targets are opaque strings in both versions.
        """
        if wire_target_type != "heading":
            return urllib.parse.quote(target, safe="")
        segments = cls._heading_segments(target)
        # Compact separators keep the header short; the plugin JSON.parses it.
        return urllib.parse.quote(json.dumps(segments, separators=(",", ":")), safe="")

    async def _heading_children(self, path: str, target: str | list[str]) -> list[str]:
        """Direct child heading texts of a heading target, from the document map.

        Returns [] when the heading isn't in the map, so the PATCH itself
        reports the miss with its usual 404 and hint. A repeated heading's
        map key carries an opaque non-printable suffix; that is stripped so
        the names read cleanly in an error message.
        """
        response = await self._request(
            "GET",
            f"/vault/{self._encode_path(path)}",
            operation="patch_note",
            headers={
                "Accept": self.DOCUMENT_MAP_ACCEPT,
                self.MARKDOWN_PATCH_VERSION_HEADER: self.MARKDOWN_PATCH_VERSION,
            },
        )
        self._raise_for_status(response, "patch_note", path)
        body = self._parse_json(response, "patch_note")
        node = body.get("headings") if isinstance(body, dict) else None
        for segment in self._heading_segments(target):
            node = node.get(segment) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return []
        return ["".join(ch for ch in key if ch.isprintable()) for key in node]

    async def _refuse_child_loss(self, location: str, path: str, target: str | list[str]) -> None:
        """Refuse a heading replace that would silently delete child headings.

        v2's `content` scope on a heading is its whole subtree, so a replace
        meant for a parent's intro text also deletes every child section,
        with no warning. The plugin will never refuse that, so this does,
        unless the caller passed replace_children=True. One extra GET; there
        is a small window between it and the PATCH, which is fine because
        this guards against caller mistakes, not concurrent edits (if_match
        covers those).
        """
        children = await self._heading_children(path, target)
        if not children:
            return
        joined = self.HEADING_PATH_DELIMITER.join(self._heading_segments(target))
        # Repeated names are listed once with a count. A path can only reach
        # the first of them, so the example prefers a uniquely named child.
        counts = Counter(children)
        names = ", ".join(
            f'"{name}"' if count == 1 else f'"{name}" ({count} headings with this name)'
            for name, count in counts.items()
        )
        example = next((name for name, count in counts.items() if count == 1), None)
        if example is None:
            example = children[0]
            reach = f" A path to a repeated name reaches only the first '{example}'."
        else:
            reach = ""
        raise ToolError(
            f"patch_note for vault '{self.name}'{location}: refusing to replace heading "
            f"'{joined}', because a heading replace also deletes all of its child "
            f"headings ({names}) and their contents. Nothing was written. To edit only "
            "the text above the first child, pass within=<block index> (0 = first "
            "block, -1 = last). To change one child, target it directly (e.g. "
            f"'{joined}::{example}').{reach} To replace the whole section including "
            "its children, pass replace_children=True."
        )

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

    _ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$")
    _CODE_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
    # An ATX closing sequence is a '#' run with whitespace before it (or the
    # whole text), so 'C#' and 'F# and C#' keep their '#'s, as in the plugin.
    _CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
    # Same shape the read_note curator strips: a leading YAML block, whose
    # `# comment` lines must not be mistaken for headings.
    _FRONTMATTER = re.compile(r"^---\r?\n(?:.*?\r?\n)?---(?:\r?\n|\Z)", re.DOTALL)
    MAX_HEADING_LEVEL = 6

    @classmethod
    def _atx_headings(cls, markdown: str) -> list[tuple[int, str]]:
        """(level, text) of each ATX heading outside fenced code, in order.

        Setext headings aren't recognised. Callers treat a miss as "unknown"
        and let the plugin decide, never as a reason to refuse.
        """
        headings = []
        fence: str | None = None
        for line in markdown.splitlines():
            fence_match = cls._CODE_FENCE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
                continue
            if fence is not None:
                continue
            heading = cls._ATX_HEADING.match(line)
            if heading:
                text = cls._CLOSING_HASHES.sub("", heading.group(2))
                headings.append((len(heading.group(1)), text))
        return headings

    async def _target_levels(self, path: str, target: str | list[str]) -> tuple[int, int] | None:
        """(target level, parent level) of a heading target, read from the note.

        The document map carries no levels, and segment count is only a
        lower bound because notes skip levels, so this reads the markdown.
        None when the path doesn't match an ATX heading (a setext heading,
        say): the PATCH then goes ahead and the plugin's own
        heading-depth-overflow warning is the backstop.
        """
        response = await self._request(
            "GET",
            f"/vault/{self._encode_path(path)}",
            operation="patch_note",
            headers={"Accept": "text/markdown"},
        )
        self._raise_for_status(response, "patch_note", path)
        segments = self._heading_segments(target)
        stack: list[tuple[int, str]] = []
        for level, text in self._atx_headings(self._FRONTMATTER.sub("", response.text, count=1)):
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            if [t for _, t in stack] == segments:
                return level, stack[-2][0] if len(stack) > 1 else 0
        return None

    async def _check_heading_patch(
        self,
        path: str,
        operation: str,
        target: str | list[str],
        content: str,
        replace_children: bool,
        allow_deep_headings: bool,
    ) -> None:
        """Client-side refusals for a heading write with content, before any PATCH.

        v2 rebases every heading in a heading target's content (verified
        live): by the target's level for append/prepend/replace, so `#` is
        a direct child, and by the parent's level for the sibling inserts,
        so `#` is a section beside the target. Content whose levels look
        absolute, or would land past h6, used to get an advisory warning,
        but that arrived after the bad heading was written and the repair
        was a whole-note rewrite. So both are refused instead, unless
        allow_deep_headings is set. A heading replace that would delete
        children is refused too — see _refuse_child_loss.
        """
        location = self._format_location(path)
        prefix = f"patch_note for vault '{self.name}'{location}:"
        joined = self.HEADING_PATH_DELIMITER.join(self._heading_segments(target))
        headings = self._atx_headings(content)
        if not allow_deep_headings:
            self._refuse_absolute_levels(prefix, joined, operation, headings)
        if operation in self._INSERT_WIRE:
            self._refuse_headless_insert(prefix, joined, operation, content)
            await self._refuse_duplicate_sibling(prefix, path, target, content)
        if not allow_deep_headings and headings:
            await self._refuse_heading_overflow(prefix, joined, path, operation, target, headings)
        if operation == "replace" and not replace_children:
            await self._refuse_child_loss(location, path, target)

    def _refuse_absolute_levels(
        self, prefix: str, joined: str, operation: str, headings: list[tuple[int, str]]
    ) -> None:
        """Refuse content whose shallowest heading has 2+ `#`s.

        A correct relative fragment starts at `#`, while a `#` count copied
        from the note is always 2 or more, since nothing below the root is
        shallower than h2. That needs no knowledge of the target's level,
        which the path can't give: notes skip levels. A relative `##` would
        be a grandchild with no child between, a level skip that's rarely
        meant; allow_deep_headings covers it when it is.
        """
        if not headings:
            return
        level, text = min(headings, key=lambda heading: heading[0])
        if level < 2:
            return
        insert = operation in self._INSERT_WIRE
        relation = "a section beside" if insert else "a direct child of"
        sibling = (
            ""
            if insert
            else f" To add a section at the same level as '{joined}', use "
            "operation='insert_after' (or 'insert_before') with '# Title'."
        )
        raise ToolError(
            f"{prefix} refusing: heading \"{text}\" in content has {level} '#'s. "
            f"Heading levels in content are relative: '#' is {relation} '{joined}' "
            "and each extra '#' goes one level deeper, so this looks copied from the "
            "note's absolute levels. Nothing was written. Start the content's headings "
            f"at '#'.{sibling} If the deeper level is intended, pass "
            "allow_deep_headings=True."
        )

    def _refuse_headless_insert(
        self, prefix: str, joined: str, operation: str, content: str
    ) -> None:
        """Refuse a sibling insert that doesn't open with a `#` heading line.

        Without one, the text lands after the target's subtree (or before
        its heading line) as plain body, silently joining whichever section
        precedes it.
        """
        first = next((line for line in content.splitlines() if line.strip()), "")
        opening = self._ATX_HEADING.match(first)
        if opening is not None and len(opening.group(1)) == 1:
            return
        raise ToolError(
            f"{prefix} {operation} adds a new section beside '{joined}', so content "
            "must open with that section's heading line at '#' (e.g. "
            "'# New entry\\n\\nbody'). Nothing was written. To add text inside "
            f"'{joined}' instead, use append or prepend."
        )

    async def _refuse_duplicate_sibling(
        self, prefix: str, path: str, target: str | list[str], content: str
    ) -> None:
        """Refuse a sibling insert whose title the parent already has.

        Retrying an insert adds a second copy: the REST API doesn't enable
        the plugin's idempotency guard by default, and with it enabled the
        guard only searches the target's own subtree, which a sibling lands
        outside (both verified live on 5.5.2). A repeated name under one
        parent is also unreachable by path, which only ever finds the first.
        So a clash is refused, using the same document map as the child
        check. One GET. There is deliberately no override (see CLAUDE.md,
        *Sibling inserts and heading levels*): unique titles keep every
        section addressable, so the message suggests one.
        """
        segments = self._heading_segments(target)
        title = self._atx_headings(content)[0][1]
        copies = (await self._heading_children(path, segments[:-1])).count(title)
        if not copies:
            return
        parent = (
            f"'{self.HEADING_PATH_DELIMITER.join(segments[:-1])}'"
            if len(segments) > 1
            else "the document root"
        )
        raise ToolError(
            f'{prefix} refusing: {parent} already has a heading "{title}". Inserting '
            "another would duplicate it, and a path to a repeated name reaches only the "
            "first. Nothing was written. If this is a retry of an insert that already "
            "succeeded, there is nothing to do. Otherwise give the new section a title "
            "that's unique under its parent, e.g. "
            f"'# {title} ({copies + 1})' or '# {date.today().isoformat()} {title}'."
        )

    async def _refuse_heading_overflow(
        self,
        prefix: str,
        joined: str,
        path: str,
        operation: str,
        target: str | list[str],
        headings: list[tuple[int, str]],
    ) -> None:
        """Refuse content with a heading that would land past h6.

        Past h6 the line stops being a heading, so it can't be addressed
        afterwards; the plugin only warns, after writing. This needs the
        target's real level, so it costs one GET.
        """
        levels = await self._target_levels(path, target)
        if levels is None:
            return
        deepest, text = max(headings, key=lambda heading: heading[0])
        landed = (levels[1] if operation in self._INSERT_WIRE else levels[0]) + deepest
        if landed <= self.MAX_HEADING_LEVEL:
            return
        raise ToolError(
            f'{prefix} refusing: heading "{text}" in content would land at level '
            f"{landed} ('{joined}' is an h{levels[0]}), past Markdown's maximum of "
            f"{self.MAX_HEADING_LEVEL}, where it stops being a heading. Nothing was "
            "written. Flatten the content's headings, or pass allow_deep_headings=True "
            "to write it anyway."
        )

    def _patch_body(
        self,
        headers: dict[str, str],
        wire_target_type: str,
        location: str,
        target: str | list[str],
        content: str | None,
        value: Any,
    ) -> bytes | None:
        """Return the body for one PATCH and set its Content-Type in `headers`.

        Enforces Guard B. Returns None, leaving Content-Type unset, for a
        delete: v2 treats any payload on a delete as an error (40081), and
        only a body with no bytes counts as absent, so no body and no
        Content-Type at all is the one unambiguous shape.
        """
        if content is None and value is None:
            return None
        content_type, body = self._patch_payload(wire_target_type, location, target, content, value)
        headers["Content-Type"] = content_type
        return body

    def _patch_payload(
        self,
        wire_target_type: str,
        location: str,
        target: str | list[str],
        content: str | None,
        value: Any,
    ) -> tuple[str, bytes]:
        """(Content-Type, body) for a PATCH that carries a payload."""
        # Frontmatter PATCH bodies are interpreted as JSON by the plugin, which
        # enables intelligent merging of structured values. A scalar string
        # round-trips correctly through json.dumps — e.g. json.dumps("done")
        # yields the 6-byte JSON-string b'"done"' (quotes included), which the
        # plugin then writes as the YAML string "done". Lists and objects take
        # a separate path: the caller declares them by passing `value`.
        if value is not None:
            # Already a JSON value — no parsing, no guessing. application/json
            # selects v2's `value` carrier, which takes any JSON (the plugin
            # parses with express.json({strict: false})).
            return "application/json", json.dumps(value).encode("utf-8")
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
                # line alone — there's no HTTP response to lean on. Point at
                # `value`: the caller almost certainly meant structure, and
                # that's the parameter that can say so unambiguously.
                raise ToolError(
                    f"patch_note frontmatter 'content' is scalar-only for vault "
                    f"'{self.name}'{location}, but content for '{target}' looks like "
                    f"a JSON {type(parsed).__name__}. To write a real "
                    f"{type(parsed).__name__}, pass it as 'value' (a JSON "
                    f"{type(parsed).__name__}, not a string) instead of 'content'."
                )
            return "application/json", json.dumps(content).encode("utf-8")
        return "text/markdown", content.encode("utf-8")

    def _patch_request(
        self,
        operation: str,
        wire_target_type: str,
        location: str,
        target: str | list[str],
        content: str | None,
        value: Any,
        if_match: str | None,
        create_if_missing: bool,
        delete_scope: str | None,
        within: int | None,
    ) -> dict[str, Any]:
        """Headers and body for one PATCH, as keyword arguments for _request."""
        headers = {
            "Operation": self._INSERT_WIRE.get(operation, operation),
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
        if create_if_missing:
            headers[self.CREATE_TARGET_HEADER] = "true"
        if operation == "delete":
            headers[self.TARGET_SCOPE_HEADER] = self._DELETE_SCOPE_WIRE[delete_scope or "all"]
        elif operation in self._INSERT_WIRE:
            headers[self.TARGET_SCOPE_HEADER] = "markerAndContent"
        if within is not None:
            headers[self.WITHIN_HEADER] = str(within)
        body = self._patch_body(headers, wire_target_type, location, target, content, value)
        return {"headers": headers, "content": body}

    def _check_patch_args(
        self,
        location: str,
        operation: str,
        target_type: str,
        target: str | list[str],
        content: str | None,
        value: Any,
        create_if_missing: bool,
        delete_scope: str | None,
        within: int | None = None,
        replace_children: bool = False,
        allow_deep_headings: bool = False,
    ) -> None:
        """Refuse argument combinations the plugin would reject or misread.

        Everything here fails before any request is sent, with a message that
        says what to pass instead.
        """
        wire_target_type = self._TARGET_TYPE_WIRE.get(target_type, target_type)
        prefix = f"patch_note for vault '{self.name}'{location}:"
        if isinstance(target, list) and (wire_target_type != "heading" or not target):
            raise ToolError(
                f"{prefix} a list target is only for heading targets, and must name at "
                "least one heading (an empty list would address the whole document)."
            )
        if operation in self._INSERT_WIRE and (wire_target_type != "heading" or within is not None):
            raise ToolError(
                f"{prefix} {operation} applies only to heading targets, without 'within'."
            )
        if allow_deep_headings and (
            wire_target_type != "heading" or operation == "delete" or within is not None
        ):
            raise ToolError(
                f"{prefix} allow_deep_headings applies only to heading targets with "
                "content, without 'within' (a 'within' edit is never rebased)."
            )
        if within is not None and (wire_target_type != "heading" or operation == "delete"):
            raise ToolError(
                f"{prefix} 'within' applies only to append, prepend or replace on "
                "heading targets."
            )
        if replace_children and (
            wire_target_type != "heading" or operation != "replace" or within is not None
        ):
            raise ToolError(
                f"{prefix} replace_children applies only to operation='replace' on a "
                "heading target without 'within' (a 'within' edit never touches children)."
            )
        if operation == "delete":
            if content is not None or value is not None or create_if_missing:
                raise ToolError(
                    f"{prefix} delete takes no 'content', 'value' or create_if_missing."
                )
            if wire_target_type == "frontmatter" and delete_scope == "marker":
                raise ToolError(
                    f"{prefix} delete_scope='marker' doesn't apply to frontmatter; use "
                    "'all' to remove the key or 'content' to set it to null."
                )
            return
        if delete_scope is not None:
            raise ToolError(f"{prefix} delete_scope applies only to operation='delete'.")
        if (content is None) == (value is None):
            raise ToolError(f"{prefix} pass exactly one of 'content' or 'value'.")
        if wire_target_type != "frontmatter" and (value is not None or create_if_missing):
            raise ToolError(
                f"{prefix} 'value' and create_if_missing apply only to frontmatter-key "
                f"targets; use 'content' for {target_type} targets."
            )

    async def patch_note(
        self,
        path: str,
        operation: str,
        target_type: str,
        target: str | list[str],
        content: str | None = None,
        if_match: str | None = None,
        value: Any = None,
        create_if_missing: bool = False,
        delete_scope: str | None = None,
        within: int | None = None,
        replace_children: bool = False,
        allow_deep_headings: bool = False,
    ) -> tuple[list[dict], str | None]:
        """Apply one surgical edit; returns (advisory warnings, new etag).

        For append/prepend/replace, exactly one of ``content`` (text) or
        ``value`` (any JSON value except null, frontmatter targets only) must
        be given. ``value`` is how a caller says "this is structured": it is
        serialised as-is, so a list lands as a YAML list. ``content`` is never
        parsed into structure — see Guard B for why that direction is unsafe.

        ``operation="delete"`` takes neither, and ``delete_scope`` (default
        "all") picks how much of the target goes — see _DELETE_SCOPE_WIRE.

        A heading ``replace`` is refused when the heading has children, unless
        ``replace_children`` is set — see _refuse_child_loss. ``within`` edits
        one intro block instead and never touches children — see WITHIN_HEADER.

        ``insert_after``/``insert_before`` add a sibling section — see
        _INSERT_WIRE. Heading content whose levels look absolute, or would
        land past h6, is refused unless ``allow_deep_headings`` is set — see
        _check_heading_patch.
        """
        wire_target_type = self._TARGET_TYPE_WIRE.get(target_type, target_type)
        self._check_patch_args(
            self._format_location(path),
            operation,
            target_type,
            target,
            content,
            value,
            create_if_missing,
            delete_scope,
            within,
            replace_children,
            allow_deep_headings,
        )
        if wire_target_type == "heading" and within is None and content is not None:
            await self._check_heading_patch(
                path, operation, target, content, replace_children, allow_deep_headings
            )
        response = await self._request(
            "PATCH",
            f"/vault/{self._encode_path(path)}",
            operation="patch_note",
            **self._patch_request(
                operation,
                wire_target_type,
                self._format_location(path),
                target,
                content,
                value,
                if_match,
                create_if_missing,
                delete_scope,
                within,
            ),
        )
        # Hints are per target type. Under v2 an unmatched heading or a missing
        # frontmatter key comes back as 404 rather than 40080, and a 404 on
        # PATCH is otherwise indistinguishable from "the note itself doesn't
        # exist" — so each hint has to cover both readings.
        self._raise_for_status(
            response,
            "patch_note",
            path,
            hint={
                "heading": (
                    self.HEADING_TARGET_HINT if within is None else self.WITHIN_TARGET_HINT
                ),
                "frontmatter": self.FRONTMATTER_TARGET_HINT,
            }.get(wire_target_type),
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
