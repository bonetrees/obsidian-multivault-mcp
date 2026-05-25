"""Pydantic-annotated types used for tool input validation."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator


def validate_vault_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("vault name must be a string")
    if not value or not value.strip():
        raise ValueError("vault name must be non-empty")
    if value.strip() != value:
        raise ValueError("vault name must not have leading or trailing whitespace")
    return value


def validate_vault_path(value: str) -> str:
    """Vault path that allows the root (empty string / single slash).

    Used for directory-oriented tools like list_directory where the root
    is a meaningful target. Note tools should use VaultFilePath instead.
    """
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    if "\x00" in value:
        raise ValueError("path must not contain null bytes")
    if value.strip() != value:
        raise ValueError(f"path must not have leading or trailing whitespace: {value!r}")
    cleaned = value.strip("/")
    if not cleaned:
        return ""
    for seg in cleaned.split("/"):
        if seg in ("", "..", "."):
            raise ValueError(f"path must not contain empty, '.' or '..' segments: {value!r}")
    return cleaned


def validate_vault_file_path(value: str) -> str:
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


VaultName = Annotated[str, AfterValidator(validate_vault_name)]
VaultPath = Annotated[str, AfterValidator(validate_vault_path)]
VaultFilePath = Annotated[str, AfterValidator(validate_vault_file_path)]
PatchOperation = Literal["append", "prepend", "replace"]
PatchTargetType = Literal["heading", "block-reference", "frontmatter-key"]
SearchType = Literal["text", "jsonlogic", "dataview"]
ClampedContextLength = Annotated[
    int,
    BeforeValidator(_reject_non_int_context_length),
    AfterValidator(clamp_context_length),
]
