"""Smoke tests for the ``/tenants`` routes (audit API-3, API2A-14).

``TenantService`` used to build itself with ``next(get_db())``. ``get_db`` is
an async generator, so constructing the dependency raised ``TypeError`` and
every route failed before its body ran. The service now runs on the request's
``AsyncSession``. These tests drive the routes against a real aiosqlite
database so a sync-only query API (``db.query``) would fail them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.auth import tenant_management as tm
from src.core.database import get_db
from src.models.document import Document, DocumentType
from src.models.organization import Organization
from src.models.user import User
from src.services.security.tenant_service import TenantService, get_tenant_service

pytestmark = pytest.mark.unit

ORG = uuid4()
MISSING_ORG = uuid4()


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tenants.db'}")
    async with engine.begin() as conn:
        tables: list[Any] = [Organization, User, Document]
        for model in tables:
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seed:
        seed.add(Organization(id=ORG, name="Acme", storage_limit_bytes=10**9))
        await seed.flush()
        seed.add(
            Document(
                organization_id=ORG,
                uploaded_by_user_id=uuid4(),
                title="paper",
                filename="paper.pdf",
                file_path="/unused/paper.pdf",
                file_size_bytes=2 * 1024 * 1024,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
            )
        )
        await seed.commit()

    app = FastAPI()
    app.include_router(tm.router)
    async with factory() as db:
        app.dependency_overrides[get_db] = lambda: db
        tenant_ctx = {"tenant": None}
        with (
            patch.object(tm, "get_current_tenant_id", lambda: tenant_ctx["tenant"]),
            patch.object(tm, "get_current_user_role", lambda: "admin"),
            patch.object(tm, "check_tenant_permission", lambda perm: True),
            patch(
                "src.middleware.multi_tenancy.validate_tenant_access",
                lambda org_id: True,
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as http:
                http.tenant_ctx = tenant_ctx  # type: ignore[attr-defined]
                yield http
    await engine.dispose()


def _as(http: AsyncClient, org: object) -> None:
    http.tenant_ctx["tenant"] = str(org)  # type: ignore[attr-defined]


async def test_dependency_binds_the_request_session() -> None:
    sentinel = object()
    service = get_tenant_service(db=sentinel)  # type: ignore[arg-type]
    assert isinstance(service, TenantService)
    assert service.db is sentinel


async def test_get_organization_returns_the_org(client: AsyncClient) -> None:
    _as(client, ORG)
    resp = await client.get(f"/tenants/organizations/{ORG}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Acme"


async def test_missing_organization_is_404_not_500(client: AsyncClient) -> None:
    # API2A-14: the 404 HTTPException used to be caught by ``except Exception``.
    _as(client, MISSING_ORG)
    resp = await client.get(f"/tenants/organizations/{MISSING_ORG}")
    assert resp.status_code == 404


async def test_storage_quota_counts_documents(client: AsyncClient) -> None:
    _as(client, ORG)
    resp = await client.get(f"/tenants/organizations/{ORG}/storage-quota")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_count"] == 1
    assert body["file_type_breakdown"]["application/pdf"]["count"] == 1
    assert body["largest_file_size_mb"] == pytest.approx(2.0)


async def test_user_count(client: AsyncClient) -> None:
    _as(client, ORG)
    resp = await client.get(f"/tenants/organizations/{ORG}/users/count")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_count"] == 0


async def test_limits(client: AsyncClient) -> None:
    _as(client, ORG)
    resp = await client.get(f"/tenants/organizations/{ORG}/limits")
    assert resp.status_code == 200, resp.text
    assert resp.json()["limits_status"]["documents"]["used"] == 1
