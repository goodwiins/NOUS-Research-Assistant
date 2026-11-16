"""GOO-508: ``setup_observability`` must not register unauthenticated admin routes.

``src.observability.integration`` cannot currently be imported (it imports the
non-existent ``fastapi.middleware.base``), so this guard reads the source
instead of mounting the app. It fails if any route decorator inside the module
targets an ``/admin`` path: such a route would become an unauthenticated admin
surface the moment ``setup_observability(app)`` is wired.
"""

import ast
from pathlib import Path

import pytest

INTEGRATION = (
    Path(__file__).resolve().parents[3] / "src" / "observability" / "integration.py"
)
_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "api_route", "route"}


def _route_paths(tree: ast.AST) -> list[str]:
    paths = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in _ROUTE_METHODS
                and deco.args
                and isinstance(deco.args[0], ast.Constant)
                and isinstance(deco.args[0].value, str)
            ):
                paths.append(deco.args[0].value)
    return paths


@pytest.mark.unit
def test_setup_observability_registers_no_admin_routes() -> None:
    tree = ast.parse(INTEGRATION.read_text(encoding="utf-8"))
    paths = _route_paths(tree)

    assert "/metrics" in paths  # sanity: the parser sees the module's routes
    admin = [p for p in paths if p.startswith("/admin")]
    assert admin == [], f"unauthenticated admin routes in integration.py: {admin}"
