"""Pydantic-annotated types used for tool input validation.

The string validators below run as BeforeValidators so they see the raw
input *before* Pydantic's lax-mode str coercion — without that, an int
like ``123`` would get silently coerced to ``"123"`` and slip past the
``isinstance(value, str)`` check.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator


def _require_str(label: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string, got {type(value).__name__}: {value!r}")
    return value


def validate_vault_name(value: object) -> str:
    name = _require_str("vault name", value)
    if not name or not name.strip():
        raise ValueError("vault name must be non-empty")
    if name.strip() != name:
        raise ValueError("vault name must not have leading or trailing whitespace")
    return name


def validate_vault_path(value: object) -> str:
    """Vault path that allows the root (empty string / single slash).

    Used for directory-oriented tools like list_directory where the root
    is a meaningful target. Note tools should use VaultFilePath instead.
    """
    path = _require_str("path", value)
    if "\x00" in path:
        raise ValueError("path must not contain null bytes")
    if path.strip() != path:
        raise ValueError(f"path must not have leading or trailing whitespace: {path!r}")
    cleaned = path.strip("/")
    if not cleaned:
        return ""
    for seg in cleaned.split("/"):
        if seg in ("", "..", "."):
            raise ValueError(f"path must not contain empty, '.' or '..' segments: {path!r}")
    return cleaned


def validate_vault_file_path(value: object) -> str:
    """Vault path that must point to a file (root not allowed).

    Used for read/write/append/patch/delete note tools — calling them
    with an empty path would hit /vault/ which is the directory-listing
    endpoint and would either 405 or behave unexpectedly.
    """
    cleaned = validate_vault_path(value)
    if not cleaned:
        raise ValueError(
            "note path must not be empty — root '/' is not a valid file path "
            "(use list_directory for directory operations)"
        )
    return cleaned


def _reject_non_int_context_length(value):
    """Pydantic BeforeValidator: reject non-int (incl. bool) before coercion.

    Without this, JSON ``true`` / Python ``True`` would coerce to 1 and then
    silently get clamped to 20 — surprising behavior for callers. Strings,
    floats, etc. are also rejected so the type signature stays honest.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"context_length must be an integer, got {type(value).__name__}: {value!r}"
        )
    return value


def clamp_context_length(value: int) -> int:
    return max(20, min(500, value))


# BeforeValidator on the str validators so non-str inputs (e.g. 123) are
# rejected before Pydantic coerces them to str. AfterValidator would receive
# the already-coerced "123" and pass the isinstance check incorrectly.
VaultName = Annotated[str, BeforeValidator(validate_vault_name)]
VaultPath = Annotated[str, BeforeValidator(validate_vault_path)]
VaultFilePath = Annotated[str, BeforeValidator(validate_vault_file_path)]
PatchOperation = Literal["append", "prepend", "replace"]
PatchTargetType = Literal["heading", "block-reference", "frontmatter-key"]
SearchType = Literal["text", "jsonlogic", "dataview"]
ClampedContextLength = Annotated[
    int,
    BeforeValidator(_reject_non_int_context_length),
    AfterValidator(clamp_context_length),
]
