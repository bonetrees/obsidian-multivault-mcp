"""Pydantic-annotated types used for tool input validation."""

from typing import Annotated, Literal

from pydantic import AfterValidator


def validate_vault_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("vault name must be a string")
    if not value or not value.strip():
        raise ValueError("vault name must be non-empty")
    if value.strip() != value:
        raise ValueError("vault name must not have leading or trailing whitespace")
    return value


def validate_vault_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("path must be a string")
    if "\x00" in value:
        raise ValueError("path must not contain null bytes")
    cleaned = value.strip().strip("/")
    if not cleaned:
        return ""
    for seg in cleaned.split("/"):
        if seg in ("", "..", "."):
            raise ValueError(f"path must not contain empty, '.' or '..' segments: {value!r}")
    return cleaned


def clamp_context_length(value: int) -> int:
    return max(20, min(500, value))


VaultName = Annotated[str, AfterValidator(validate_vault_name)]
VaultPath = Annotated[str, AfterValidator(validate_vault_path)]
PatchOperation = Literal["append", "prepend", "replace"]
PatchTargetType = Literal["heading", "block-reference", "frontmatter-key"]
SearchType = Literal["text", "jsonlogic", "dataview"]
ClampedContextLength = Annotated[int, AfterValidator(clamp_context_length)]
