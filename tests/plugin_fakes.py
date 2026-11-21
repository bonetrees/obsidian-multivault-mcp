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
import json
import urllib.parse

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


class FakeFrontmatterNote:
    """Stateful fake of the plugin's v2 frontmatter PATCH semantics.

    Mirrors plugin 5.5.2 as probed live, so a client that sends a list as a
    JSON *string* (or the wrong carrier) produces the wrong frontmatter here
    exactly as it would in a vault:

    - The body must be application/json; it is the new value, any JSON type.
    - replace sets the key. append/prepend merge two lists (concatenated,
      duplicates kept), two objects (shallow; new keys win on append,
      existing keys win on prepend) or two strings. Any other pairing is
      400 / 40080 "not mergeable".
    - A missing key is 404 for every operation unless
      Create-Target-If-Missing: true, which sets it to the value. Delete is
      never creatable: a missing key is 404 regardless.
    - delete carries no body. Target-Scope content (the plugin default) sets
      the key to null; markerAndContent removes it; marker is 40081.

    Route PATCHes through markdown_patch_v2_handler so the v2 header contract
    is enforced too; this fake only models the frontmatter itself.
    """

    def __init__(self, path: str, frontmatter: dict | None = None) -> None:
        self.url_path = f"/vault/{path}"
        self.frontmatter: dict = dict(frontmatter or {})
        self.requests: list[httpx.Request] = []

    @staticmethod
    def _merge(operation: str, old, new):
        """Merged value, or None when the plugin would refuse the pair."""
        if isinstance(old, list) and isinstance(new, list):
            return old + new if operation == "append" else new + old
        if isinstance(old, dict) and isinstance(new, dict):
            return {**old, **new} if operation == "append" else {**new, **old}
        if isinstance(old, str) and isinstance(new, str):
            return old + new if operation == "append" else new + old
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != self.url_path:
            return httpx.Response(404, json={"errorCode": 40400, "message": "Not Found"})
        if request.method == "HEAD":  # read_note's etag probe; no tag modelled here
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"content": "# Body\n", "frontmatter": self.frontmatter, "tags": []},
            )
        if request.method != "PATCH" or request.headers.get("target-type") != "frontmatter":
            return httpx.Response(405, json={"errorCode": 40500, "message": "Not modelled"})
        key = urllib.parse.unquote(request.headers["target"])
        if request.headers["operation"] == "delete":
            return self._delete(request, key)
        if request.headers.get("content-type") != "application/json":
            return httpx.Response(
                400, json={"errorCode": 40000, "message": "Expected an application/json value"}
            )
        new = json.loads(request.content)
        operation = request.headers["operation"]
        if key not in self.frontmatter:
            if request.headers.get("create-target-if-missing") != "true":
                return httpx.Response(
                    404,
                    json={
                        "errorCode": 40400,
                        "message": f'Not Found\ncould not resolve frontmatter target "{key}"',
                    },
                )
            self.frontmatter[key] = new
        elif operation == "replace":
            self.frontmatter[key] = new
        else:
            merged = self._merge(operation, self.frontmatter[key], new)
            if merged is None:
                return httpx.Response(
                    400,
                    json={
                        "errorCode": 40080,
                        "message": "The patch you provided could not be applied to the "
                        "target content.\nfrontmatter values are not mergeable (need two "
                        "lists, two dicts, or two strings)",
                    },
                )
            self.frontmatter[key] = merged
        return httpx.Response(200, text="patched")

    def _delete(self, request: httpx.Request, key: str) -> httpx.Response:
        scope = request.headers.get("target-scope", "content")
        if request.content or scope not in ("content", "markerAndContent"):
            return httpx.Response(
                400,
                json={"errorCode": 40081, "message": f"delete @ {scope} is not valid here"},
            )
        if key not in self.frontmatter:
            return httpx.Response(
                404,
                json={
                    "errorCode": 40400,
                    "message": f'Not Found\ncould not resolve frontmatter target "{key}"',
                },
            )
        if scope == "content":
            self.frontmatter[key] = None
        else:
            del self.frontmatter[key]
        return httpx.Response(200, text="patched")


class FakeHeadingNote:
    """Stateful fake of the plugin's v2 heading PATCH and document map.

    Mirrors plugin 5.5.2 as probed live, so a client that replaces a parent
    heading loses the children here exactly as it would in a vault:

    - A heading's `content` scope is its whole subtree. replace swaps all of
      it, children included; append/prepend insert at the subtree's end/start.
    - ``Within: <int>`` addresses one of the heading's intro blocks: the
      top-level blocks above its first child heading (negative counts from
      the end). replace swaps that block, append/prepend splice onto its
      end/start literally, and children are never touched. Out of range is
      404 / 40400 naming the block count.
    - GET with the document-map Accept type returns the heading tree as
      nested objects, e.g. {"Doc": {"Child": {}}}.

    Not modelled: heading-level rebasing of content, whitespace management,
    duplicate headings. The note is a list of blank-line-separated chunks,
    each either a heading line or a body block, so test notes must use one
    blank line between blocks. Route PATCHes through
    markdown_patch_v2_handler so the v2 header contract is enforced too.
    """

    DOCUMENT_MAP_ACCEPT = "application/vnd.olrapi.document-map+json"

    def __init__(self, path: str, markdown: str) -> None:
        self.url_path = f"/vault/{path}"
        self.items: list[tuple[int, str]] = self._parse(markdown)
        self.requests: list[httpx.Request] = []

    @staticmethod
    def _parse(markdown: str) -> list[tuple[int, str]]:
        """(level, text) per chunk; level 0 is a body block."""
        items = []
        for chunk in (c.strip("\n") for c in markdown.split("\n\n")):
            if not chunk:
                continue
            hashes = len(chunk) - len(chunk.lstrip("#"))
            if 1 <= hashes <= 6 and "\n" not in chunk and chunk[hashes : hashes + 1] == " ":
                items.append((hashes, chunk[hashes + 1 :]))
            else:
                items.append((0, chunk))
        return items

    @property
    def markdown(self) -> str:
        return (
            "\n\n".join(f"{'#' * level} {text}" if level else text for level, text in self.items)
            + "\n"
        )

    def _paths(self) -> dict[int, tuple[str, ...]]:
        """Item index → heading path, for every heading."""
        paths: dict[int, tuple[str, ...]] = {}
        stack: list[tuple[int, str]] = []
        for index, (level, text) in enumerate(self.items):
            if not level:
                continue
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            paths[index] = tuple(t for _, t in stack)
        return paths

    def document_map(self) -> dict:
        tree: dict = {}
        for path in self._paths().values():
            node = tree
            for segment in path:
                node = node.setdefault(segment, {})
        return {"version": "fake", "frontmatterFields": [], "headings": tree, "blocks": []}

    @staticmethod
    def _not_found(message: str) -> httpx.Response:
        return httpx.Response(404, json={"errorCode": 40400, "message": f"Not Found\n{message}"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != self.url_path:
            return self._not_found("no such file")
        if request.method == "GET" and request.headers.get("accept") == self.DOCUMENT_MAP_ACCEPT:
            return httpx.Response(200, json=self.document_map())
        if request.method != "PATCH" or request.headers.get("target-type") != "heading":
            return httpx.Response(405, json={"errorCode": 40500, "message": "Not modelled"})
        segments = tuple(json.loads(urllib.parse.unquote(request.headers["target"])))
        found = [i for i, path in self._paths().items() if path == segments]
        if not found:
            return self._not_found(f"could not resolve heading target {list(segments)}")
        head = found[0]
        level = self.items[head][0]
        end = next(
            (i for i in range(head + 1, len(self.items)) if 0 < self.items[i][0] <= level),
            len(self.items),
        )
        operation = request.headers["operation"]
        content = request.content.decode("utf-8")
        if "within" in request.headers:
            return self._patch_within(request, head, end, operation, content, list(segments))
        new_items = self._parse(content)
        if operation == "replace":
            self.items[head + 1 : end] = new_items
        elif operation == "append":
            self.items[end:end] = new_items
        else:
            self.items[head + 1 : head + 1] = new_items
        return httpx.Response(200, text=self.markdown)

    def _patch_within(self, request, head, end, operation, content, segments):
        intro = []
        for i in range(head + 1, end):
            if self.items[i][0]:
                break
            intro.append(i)
        index = int(request.headers["within"])
        if not -len(intro) <= index < len(intro):
            return self._not_found(
                f"`within` index {index} is out of range: {json.dumps(segments)} has "
                f"{len(intro)} top-level blocks"
            )
        position = intro[index]
        block = self.items[position][1]
        spliced = {"replace": content, "append": block + content, "prepend": content + block}
        self.items[position] = (0, spliced[operation].strip("\n"))
        return httpx.Response(200, text=self.markdown)
