"""The test-only field-encryption scope sets up encryption and leaks nothing (GOO-785)."""

from __future__ import annotations

import os

import pytest

import src.core.encryption as encryption
from tests.utils.field_encryption import (
    ENCRYPTION_GLOBALS,
    TEST_MASTER_KEY,
    scoped_field_encryption,
)

pytestmark = pytest.mark.unit


def test_scope_works_from_an_uninitialized_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ENCRYPTION_GLOBALS:
        monkeypatch.setattr(encryption, name, None)
    monkeypatch.delenv("ENCRYPTION_MASTER_KEY", raising=False)

    with scoped_field_encryption() as field:
        token = field.encrypt_field("Ada", "first_name")
        assert token != "Ada"
        assert field.decrypt_field(token, "first_name") == "Ada"

    # Back to exactly how the test found it: uninitialized, no key set.
    for name in ENCRYPTION_GLOBALS:
        assert getattr(encryption, name) is None, name
    assert "ENCRYPTION_MASTER_KEY" not in os.environ
    with pytest.raises(encryption.EncryptionError, match="not initialized"):
        encryption.get_field_encryption()


def test_scope_restores_previously_initialized_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinels = {name: object() for name in ENCRYPTION_GLOBALS}
    for name, sentinel in sentinels.items():
        monkeypatch.setattr(encryption, name, sentinel)
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", "previous-key")

    with scoped_field_encryption():
        assert encryption._field_encryption is not sentinels["_field_encryption"]
        assert os.environ["ENCRYPTION_MASTER_KEY"] == TEST_MASTER_KEY

    for name, sentinel in sentinels.items():
        assert getattr(encryption, name) is sentinel, name
    assert os.environ["ENCRYPTION_MASTER_KEY"] == "previous-key"
