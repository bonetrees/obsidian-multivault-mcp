"""Stateful fake of obsidian-local-rest-api's ETag / precondition contract.

Like ``markdown_patch_v2_handler`` in test_client.py, this is strict where
the plugin is strict: a mock that answers 2xx to every conditional write
can't tell a request that would clobber a concurrent edit from one that
wouldn't. Behaviour mirrors plugin 5.5.2 as probed live:

- The file ETag is strong and derived from content alone (rewriting the
  same bytes keeps it; any change moves it). GET/HEAD ``text/markdown``,
  PUT, POST and PATCH return it.
- The note+json GET does NOT carry it — Express stamps a weak tag hashed
  from the JSON response body instead.
- If-Match / If-None-Match accept ``*`` or a comma-separated tag list and
  compare strongly, so a ``W/`` tag never matches. Empty or
  space-separated lists are malformed (400 / 40024). A failed
  precondition is 412 / 41200, and nothing is written.
"""

import hashlib

import httpx

PRECONDITION_FAILED_MESSAGE = (
    "A precondition on this write did not hold, so nothing was written: the file "
    "has changed since the version If-Match named, or If-None-Match ruled out what "
    "is there now. Read the file again for its current ETag, and retry against that."
)


def content_etag(content: str) -> str:
    return '"' + hashlib.sha1(content.encode("utf-8")).hexdigest()[:6] + '"'


class FakeVaultNote:
    """One note's worth of plugin state, served by ``handler``.

    ``content`` is None while the note doesn't exist. Tests mutate it
    directly to simulate an out-of-band edit (the user typing in Obsidian).
    PATCH bodies are appended verbatim — targeting is covered elsewhere;
    this fake is about version checks.
    """

    def __init__(self, path: str, content: str | None = None) -> None:
        self.url_path = f"/vault/{path}"
        self.content = content
        self.requests: list[httpx.Request] = []

    @property
    def etag(self) -> str | None:
        return None if self.content is None else content_etag(self.content)

    @staticmethod
    def _parse(header: str) -> list[str] | None:
        """Tag list for a precondition header, or None if malformed."""
        if header.strip() == "*":
            return ["*"]
        tags = [t.strip() for t in header.split(",")]
        if any(not t or " " in t for t in tags):
            return None
        return tags

    def _precondition_error(self, request: httpx.Request) -> httpx.Response | None:
        for name in ("if-match", "if-none-match"):
            raw = request.headers.get(name)
            if raw is None:
                continue
            tags = self._parse(raw)
            if tags is None:
                return httpx.Response(
                    400,
                    json={
                        "errorCode": 40024,
                        "message": "An 'If-Match' or 'If-None-Match' header must be "
                        "'*' or a comma-separated list of entity tags.",
                    },
                )
            current = self.etag
            matches = current is not None and ("*" in tags or current in tags)
            if (name == "if-match") != matches:
                return httpx.Response(
                    412,
                    json={
                        "errorCode": 41200,
                        "message": f"{PRECONDITION_FAILED_MESSAGE}\n"
                        f"{name} named {raw}, but the file is now at version {current}.",
                    },
                    # Express's weak tag over the error body — must never be
                    # mistaken for the file's version.
                    headers={"ETag": 'W/"168-errorbody"'},
                )
        return None

    def _ok(self, status: int, **kwargs) -> httpx.Response:
        return httpx.Response(status, headers={"ETag": self.etag}, **kwargs)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != self.url_path:
            return httpx.Response(404, json={"errorCode": 40400, "message": "Not Found"})
        method = request.method
        if method in ("GET", "HEAD"):
            if self.content is None:
                return httpx.Response(404, json={"errorCode": 40400, "message": "Not Found"})
            if request.headers.get("accept") == "application/vnd.olrapi.note+json":
                return httpx.Response(
                    200,
                    json={
                        "content": self.content,
                        "frontmatter": {},
                        "tags": [],
                        "stat": {"ctime": 0, "mtime": 0, "size": len(self.content)},
                        "path": self.url_path.removeprefix("/vault/"),
                    },
                    headers={"ETag": 'W/"109-jsonbody"'},
                )
            if method == "HEAD":
                return self._ok(200)
            return self._ok(200, text=self.content)
        error = self._precondition_error(request)
        if error is not None:
            return error
        body = request.content.decode("utf-8")
        if method == "PUT":
            self.content = body
            return self._ok(204)
        if self.content is None:
            return httpx.Response(404, json={"errorCode": 40400, "message": "Not Found"})
        if method == "POST":
            self.content += body
            return self._ok(204)
        if method == "PATCH":
            self.content += body
            return self._ok(200, text=self.content)
        if method == "DELETE":
            self.content = None
            return httpx.Response(204)
        return httpx.Response(405, json={"errorCode": 40500, "message": "Not allowed"})
