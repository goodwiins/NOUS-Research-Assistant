"""Importing the security package must not write key material (GOO-640, IS-2).

``src/services/security/__init__`` re-exported ``EnhancedSecurityService``,
whose module built a global instance at import. That instance created
``$SECURITY_DIR/master.key`` (default ``./security``, the process working
directory) in every API and worker process, although nothing used the
service, and its ``mask_pii_data`` regex never compiled. The service is
removed; importing the package must leave the filesystem alone.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[4]


def test_importing_security_package_writes_no_master_key(tmp_path: Path) -> None:
    # Fresh interpreter so an already-imported module can't hide the side effect.
    env = {**os.environ, "SECURITY_DIR": str(tmp_path / "security")}
    subprocess.run(
        [sys.executable, "-c", "import src.services.security"],
        cwd=_BACKEND,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert not (tmp_path / "security" / "master.key").exists()


def test_enhanced_security_service_is_gone() -> None:
    package = importlib.import_module("src.services.security")
    assert not hasattr(package, "EnhancedSecurityService")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.services.security.enhanced_security_service")
