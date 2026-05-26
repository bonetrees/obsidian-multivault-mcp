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


class ObsidianVaultClient:
    HEALTH_CHECK_TIMEOUT = 3.0
    DEFAULT_TIMEOUT = 30.0
    CONNECT_TIMEOUT = 10.0

    DATAVIEW_NOT_INSTALLED_CODE = 40070
    INVALID_PATCH_TARGET_CODE = 40080

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
    def _parse_error_body(response: httpx.Response) -> dict:
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

    def _require_list(self, body: Any, operation: str) -> list:
        """Validate that a parsed success-path body is a JSON array.

        Silently returning `[]` on a wrong-shape response would conflate
        "no matches" with "upstream/proxy contract breakage", which makes
        debugging painful. Raise ToolError instead.
        """
        if not isinstance(body, list):
            raise ToolError(
                f"Expected JSON array from vault '{self.name}' during {operation}, "
                f"got {type(body).__name__}."
            )
        return body

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
    ) -> None:
        if response.is_success:
            return
        body = self._parse_error_body(response)
        msg = body.get("message") or (response.text.strip() if response.text else "(no body)")
        code = body.get("errorCode")
        location = self._format_location(path)
        if response.status_code == 401:
            raise ToolError(
                f"Authentication failed for vault '{self.name}' during {operation}"
                f"{location} (HTTP 401). Check the API key. ({msg})"
            )
        if response.status_code == 404:
            raise NotFound(f"Not found{location} in vault '{self.name}' during {operation}: {msg}")
        if response.status_code == 405:
            raise ToolError(
                f"Operation not supported{location} in vault '{self.name}' "
                f"during {operation}: {msg}"
            )
        if code == self.INVALID_PATCH_TARGET_CODE:
            raise ToolError(
                f"Invalid PATCH target{location} in vault '{self.name}': {msg}. "
                "Heading targets must use the full path from the document root, "
                "with '::' as the separator (e.g. 'Report::Findings::Critical')."
            )
        code_part = f", errorCode {code}" if code is not None else ""
        raise ToolError(
            f"{operation} failed for vault '{self.name}'{location} "
            f"(HTTP {response.status_code}{code_part}): {msg}"
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
        body = self._parse_error_body(response)
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

    async def read_note(self, path: str) -> dict:
        response = await self._request(
            "GET",
            f"/vault/{self._encode_path(path)}",
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
        return body

    async def write_note(self, path: str, content: str) -> None:
        response = await self._request(
            "PUT",
            f"/vault/{self._encode_path(path)}",
            operation="write_note",
            content=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown"},
        )
        self._raise_for_status(response, "write_note", path)

    async def append_note(self, path: str, content: str) -> None:
        response = await self._request(
            "POST",
            f"/vault/{self._encode_path(path)}",
            operation="append_note",
            content=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown"},
        )
        self._raise_for_status(response, "append_note", path)

    async def patch_note(
        self,
        path: str,
        operation: str,
        target_type: str,
        target: str,
        content: str,
    ) -> None:
        headers = {
            "Operation": operation,
            "Target-Type": target_type,
            "Target": urllib.parse.quote(target, safe=""),
            "Content-Type": "text/markdown",
        }
        response = await self._request(
            "PATCH",
            f"/vault/{self._encode_path(path)}",
            operation="patch_note",
            content=content.encode("utf-8"),
            headers=headers,
        )
        self._raise_for_status(response, "patch_note", path)

    async def delete_note(self, path: str) -> None:
        response = await self._request(
            "DELETE",
            f"/vault/{self._encode_path(path)}",
            operation="delete_note",
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
        return self._require_list(body, "search_simple")

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
        return self._require_list(body, "search_jsonlogic")

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
            body = self._parse_error_body(response)
            if body.get("errorCode") == self.DATAVIEW_NOT_INSTALLED_CODE:
                logger.info(
                    "Dataview not installed in vault '%s'; falling back to text search.",
                    self.name,
                )
                fallback = await self.search_simple(query, context_length)
                return fallback, "Dataview not available, fell back to text search"
        self._raise_for_status(response, "search_dataview")
        body = self._parse_json(response, "search_dataview")
        return self._require_list(body, "search_dataview"), None
