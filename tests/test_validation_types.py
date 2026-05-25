"""Tests for pydantic validation types."""

import pytest
from pydantic import TypeAdapter, ValidationError

from obsidian_multivault_mcp.validation_types import (
    ClampedContextLength,
    PatchOperation,
    PatchTargetType,
    SearchType,
    VaultFilePath,
    VaultName,
    VaultPath,
)


class TestVaultName:
    adapter = TypeAdapter(VaultName)

    def test_valid_simple(self):
        assert self.adapter.validate_python("devprojects") == "devprojects"

    def test_valid_hyphens_and_caps(self):
        assert self.adapter.validate_python("Personal-Vault_2") == "Personal-Vault_2"

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("   ")

    def test_leading_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python(" devprojects")

    def test_trailing_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("devprojects ")


class TestVaultPath:
    adapter = TypeAdapter(VaultPath)

    def test_strip_leading_and_trailing_slashes(self):
        assert self.adapter.validate_python("/notes/foo.md/") == "notes/foo.md"

    def test_no_slashes_passthrough(self):
        assert self.adapter.validate_python("notes/foo.md") == "notes/foo.md"

    def test_root_empty_string(self):
        assert self.adapter.validate_python("") == ""

    def test_root_single_slash(self):
        assert self.adapter.validate_python("/") == ""

    def test_path_traversal_absolute_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("../etc/passwd")

    def test_path_traversal_inline_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("notes/../secrets")

    def test_dot_segment_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("notes/./foo")

    def test_null_byte_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("notes/\x00file.md")

    def test_double_slash_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("notes//foo.md")

    def test_unicode_filename_allowed(self):
        assert self.adapter.validate_python("notes/日本語.md") == "notes/日本語.md"

    def test_spaces_in_filename_allowed(self):
        assert self.adapter.validate_python("My Folder/My Note.md") == "My Folder/My Note.md"

    def test_leading_whitespace_rejected(self):
        # Don't silently mutate user input.
        with pytest.raises(ValidationError):
            self.adapter.validate_python(" notes/foo.md")

    def test_trailing_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("notes/foo.md ")


class TestVaultFilePath:
    """File-only variant: same rules as VaultPath but root is rejected."""

    adapter = TypeAdapter(VaultFilePath)

    def test_normal_path_allowed(self):
        assert self.adapter.validate_python("notes/foo.md") == "notes/foo.md"

    def test_strips_surrounding_slashes(self):
        assert self.adapter.validate_python("/notes/foo.md/") == "notes/foo.md"

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("")

    def test_single_slash_rejected(self):
        # "/" normalises to "" — note tools must not accept root.
        with pytest.raises(ValidationError):
            self.adapter.validate_python("/")


class TestClampedContextLength:
    adapter = TypeAdapter(ClampedContextLength)

    def test_in_range_passthrough(self):
        assert self.adapter.validate_python(100) == 100

    def test_lower_bound(self):
        assert self.adapter.validate_python(20) == 20

    def test_upper_bound(self):
        assert self.adapter.validate_python(500) == 500

    def test_clamp_below_lower(self):
        assert self.adapter.validate_python(0) == 20
        assert self.adapter.validate_python(-100) == 20

    def test_clamp_above_upper(self):
        assert self.adapter.validate_python(10000) == 500


class TestPatchOperation:
    adapter = TypeAdapter(PatchOperation)

    @pytest.mark.parametrize("op", ["append", "prepend", "replace"])
    def test_valid(self, op):
        assert self.adapter.validate_python(op) == op

    def test_invalid_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("delete")


class TestPatchTargetType:
    adapter = TypeAdapter(PatchTargetType)

    @pytest.mark.parametrize("t", ["heading", "block-reference", "frontmatter-key"])
    def test_valid(self, t):
        assert self.adapter.validate_python(t) == t

    def test_invalid_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("body")


class TestSearchType:
    adapter = TypeAdapter(SearchType)

    @pytest.mark.parametrize("st", ["text", "jsonlogic", "dataview"])
    def test_valid(self, st):
        assert self.adapter.validate_python(st) == st

    def test_invalid_rejected(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python("regex")
