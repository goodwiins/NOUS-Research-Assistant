"""Scoped field encryption for tests that write encrypted columns (GOO-785)."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import src.core.encryption as encryption

# Module globals that initialize_encryption() rebinds.
ENCRYPTION_GLOBALS = (
    "_key_manager",
    "_aes_encryption",
    "_field_encryption",
    "_file_encryption",
)
TEST_MASTER_KEY = base64.b64encode(b"goo-785-test-master-key-32bytes!").decode()


@contextmanager
def scoped_field_encryption() -> Iterator[encryption.FieldEncryption]:
    """Initialize encryption with a fixed test key, then restore the previous
    globals and ``ENCRYPTION_MASTER_KEY`` on exit, so nothing leaks to the
    tests that run after."""
    with pytest.MonkeyPatch.context() as patch:
        for name in ENCRYPTION_GLOBALS:
            patch.setattr(encryption, name, getattr(encryption, name))
        patch.setenv("ENCRYPTION_MASTER_KEY", TEST_MASTER_KEY)
        encryption.initialize_encryption()
        yield encryption.get_field_encryption()
