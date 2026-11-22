"""
Tenant management services for multi-tenancy
Handles organization management, quotas, and tenant operations
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import Depends
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.exceptions.analytics_exceptions import (
    ConfigurationException,
    PermissionDeniedException,
)
from src.middleware.multi_tenancy import get_current_user_role
from src.models.document import Document
from src.models.organization import Organization, StorageTier
from src.models.user import User

logger = logging.getLogger(__name__)


class TenantService:
    """Service for managing tenant operations and quotas.

    Runs on the request's ``AsyncSession``. It used to default to
    ``next(get_db())``, but ``get_db`` is an async generator, so building the
    dependency raised ``TypeError`` and every ``/tenants`` route failed.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _load_organization(self, organization_id: str) -> Optional[Organization]:
        result = await self.db.execute(
            select(Organization).where(Organization.id == organization_id)
        )
        return result.scalar_one_or_none()

    async def create_organization(
        self, name: str, storage_tier: StorageTier = StorageTier.FREE
    ) -> Organization:
        """Create a new organization with default settings"""
        try:
            # Check if organization name already exists
            existing = await self.db.execute(
                select(Organization.id).where(Organization.name == name)
            )
            if existing.first() is not None:
                raise ConfigurationException(
                    config_key="organization_name",
                    config_value=name,
                    reason="Organization name already exists",
                )

            organization = Organization(
                name=name,
                storage_tier=storage_tier,
                storage_limit_bytes=Organization.get_default_storage_limit(
                    storage_tier
                ),
                storage_used_bytes=0,
                is_active=True,
            )

            self.db.add(organization)
            await self.db.commit()
            await self.db.refresh(organization)

            logger.info(
                "Created organization %s with tier %s", name, storage_tier.value
            )
            return organization

        except Exception:
            await self.db.rollback()
            logger.error("Failed to create organization %s", name, exc_info=True)
            raise

    async def get_organization(self, organization_id: str) -> Optional[Organization]:
        """Get organization by ID with tenant validation"""
        from src.middleware.multi_tenancy import validate_tenant_access

        if not validate_tenant_access(organization_id):
            raise PermissionDeniedException(
                required_permission="organization_read",
                user_role=get_current_user_role() or "unknown",
                details={"organization_id": organization_id},
            )

        return await self._load_organization(organization_id)

    async def update_organization(
        self, organization_id: str, **kwargs: Any
    ) -> Organization:
        """Update organization settings with tenant validation"""
        from src.middleware.multi_tenancy import validate_tenant_access

        if not validate_tenant_access(organization_id):
            raise PermissionDeniedException(
                required_permission="organization_update",
                user_role=get_current_user_role() or "unknown",
                details={"organization_id": organization_id},
            )

        organization = await self._load_organization(organization_id)
        if not organization:
            raise ConfigurationException(
                config_key="organization_id",
                config_value=organization_id,
                reason="Organization not found",
            )

        # Update allowed fields
        allowed_fields = ["name", "is_active"]
        for field, value in kwargs.items():
            if field in allowed_fields:
                setattr(organization, field, value)

        # Handle storage tier changes separately
        if "storage_tier" in kwargs:
            new_tier = kwargs["storage_tier"]
            if isinstance(new_tier, str):
                new_tier = StorageTier(new_tier)

            # Check if this would exceed new limits
            current_usage = organization.storage_used_bytes
            new_limit = Organization.get_default_storage_limit(new_tier)

            if current_usage > new_limit:
                raise ConfigurationException(
                    config_key="storage_tier",
                    config_value=new_tier.value,
                    reason=f"Current usage ({current_usage} bytes) exceeds new tier limit ({new_limit} bytes)",
                )

            organization.storage_tier = new_tier
            organization.storage_limit_bytes = new_limit

        await self.db.commit()
        await self.db.refresh(organization)

        logger.info("Updated organization %s", organization_id)
        return organization

    async def upgrade_storage_tier(
        self, organization_id: str, new_tier: StorageTier
    ) -> Organization:
        """Upgrade organization storage tier"""
        organization = await self.get_organization(organization_id)
        if not organization:
            raise ConfigurationException(
                config_key="organization_id",
                config_value=organization_id,
                reason="Organization not found",
            )

        # Only allow upgrades (not downgrades that would exceed limits)
        if (
            StorageTier[new_tier.value].value
            < StorageTier[organization.storage_tier.value].value
        ):
            current_usage = organization.storage_used_bytes
            new_limit = Organization.get_default_storage_limit(new_tier)

            if current_usage > new_limit:
                raise ConfigurationException(
                    config_key="storage_tier_downgrade",
                    config_value=new_tier.value,
                    reason="Current usage exceeds new tier limit",
                )

        return await self.update_organization(organization_id, storage_tier=new_tier)

    async def get_storage_quota_status(self, organization_id: str) -> Dict[str, Any]:
        """Get detailed storage quota status for organization"""
        organization = await self.get_organization(organization_id)
        if not organization:
            raise ConfigurationException(
                config_key="organization_id",
                config_value=organization_id,
                reason="Organization not found",
            )

        document_stats = await self._get_document_storage_stats(organization_id)

        quota_status = organization.check_storage_quota()
        quota_status.update(
            {
                "document_count": document_stats["document_count"],
                "file_type_breakdown": document_stats["file_type_breakdown"],
                "average_file_size_mb": document_stats["average_file_size_mb"],
                "largest_file_size_mb": document_stats["largest_file_size_mb"],
                "storage_efficiency_score": self._calculate_storage_efficiency(
                    organization_id, document_stats
                ),
            }
        )

        return quota_status

    async def _get_document_storage_stats(self, organization_id: str) -> Dict[str, Any]:
        """Get detailed document storage statistics"""
        try:
            doc_count = (
                await self.db.scalar(
                    select(func.count(Document.id)).where(
                        Document.organization_id == organization_id
                    )
                )
            ) or 0

            # File type breakdown (Document has no ``file_type``; MIME type is it)
            file_type_rows = (
                await self.db.execute(
                    select(
                        Document.mime_type,
                        func.count(Document.id).label("count"),
                        func.sum(Document.file_size_bytes).label("total_size"),
                    )
                    .where(Document.organization_id == organization_id)
                    .group_by(Document.mime_type)
                )
            ).all()

            file_type_breakdown = {
                row.mime_type: {
                    "count": row.count,
                    "total_size_mb": (row.total_size or 0) / (1024 * 1024),
                }
                for row in file_type_rows
            }

            size_stats = (
                await self.db.execute(
                    select(
                        func.avg(Document.file_size_bytes).label("avg_size"),
                        func.max(Document.file_size_bytes).label("max_size"),
                    ).where(
                        Document.organization_id == organization_id,
                        Document.file_size_bytes.isnot(None),
                    )
                )
            ).first()

            return {
                "document_count": doc_count,
                "file_type_breakdown": file_type_breakdown,
                "average_file_size_mb": float(size_stats.avg_size or 0) / (1024 * 1024),
                "largest_file_size_mb": (size_stats.max_size or 0) / (1024 * 1024),
            }

        except Exception:
            logger.error("Error getting document storage stats", exc_info=True)
            return {
                "document_count": 0,
                "file_type_breakdown": {},
                "average_file_size_mb": 0,
                "largest_file_size_mb": 0,
            }

    def _calculate_storage_efficiency(
        self, organization_id: str, stats: Dict[str, Any]
    ) -> float:
        """Calculate storage efficiency score (0-100)"""
        try:
            if stats["document_count"] == 0:
                return 100.0

            # Factors for efficiency calculation
            file_type_diversity = (
                len(stats["file_type_breakdown"]) / 10
            )  # Normalize to 0-1
            avg_file_size_score = min(
                stats["average_file_size_mb"] / 10, 1.0
            )  # Optimal around 10MB

            efficiency = (file_type_diversity * 30) + (avg_file_size_score * 70)
            return min(efficiency, 100.0)

        except Exception:
            return 50.0  # Default middle score

    async def get_user_count(self, organization_id: str) -> int:
        """Get number of users in organization"""
        from src.middleware.multi_tenancy import validate_tenant_access

        if not validate_tenant_access(organization_id):
            raise PermissionDeniedException(
                required_permission="organization_users_read",
                user_role=get_current_user_role() or "unknown",
                details={"organization_id": organization_id},
            )

        return (
            await self.db.scalar(
                select(func.count(User.id)).where(
                    User.organization_id == organization_id
                )
            )
        ) or 0

    async def get_organization_users(
        self, organization_id: str, limit: int = 50, offset: int = 0
    ) -> List[User]:
        """Get paginated list of users in organization"""
        from src.middleware.multi_tenancy import validate_tenant_access

        if not validate_tenant_access(organization_id):
            raise PermissionDeniedException(
                required_permission="organization_users_read",
                user_role=get_current_user_role() or "unknown",
                details={"organization_id": organization_id},
            )

        result = await self.db.execute(
            select(User)
            .where(User.organization_id == organization_id)
            .order_by(User.id)
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_organization_analytics(
        self, organization_id: str, days: int = 30
    ) -> Dict[str, Any]:
        """Get organization analytics summary"""
        from src.middleware.multi_tenancy import validate_tenant_access
        from src.models.analytics_event import AnalyticsEvent
        from src.models.user_session import UserSession

        if not validate_tenant_access(organization_id):
            raise PermissionDeniedException(
                required_permission="organization_analytics",
                user_role=get_current_user_role() or "unknown",
                details={"organization_id": organization_id},
            )

        start_date = datetime.utcnow() - timedelta(days=days)

        try:
            events_count = (
                await self.db.scalar(
                    select(func.count(AnalyticsEvent.id)).where(
                        and_(
                            AnalyticsEvent.organization_id == organization_id,
                            AnalyticsEvent.created_at >= start_date,
                        )
                    )
                )
            ) or 0

            active_sessions = (
                await self.db.scalar(
                    select(func.count(UserSession.id)).where(
                        and_(
                            UserSession.organization_id == organization_id,
                            UserSession.created_at >= start_date,
                        )
                    )
                )
            ) or 0

            storage_growth = await self._calculate_storage_growth(organization_id, days)

            return {
                "period_days": days,
                "analytics_events_count": events_count,
                "active_sessions_count": active_sessions,
                "storage_growth_mb": storage_growth,
                "daily_averages": {
                    "events_per_day": events_count / max(days, 1),
                    "sessions_per_day": active_sessions / max(days, 1),
                    "storage_growth_per_day_mb": storage_growth / max(days, 1),
                },
            }

        except Exception:
            logger.error("Error getting organization analytics", exc_info=True)
            return {
                "period_days": days,
                "analytics_events_count": 0,
                "active_sessions_count": 0,
                "storage_growth_mb": 0,
                "daily_averages": {
                    "events_per_day": 0,
                    "sessions_per_day": 0,
                    "storage_growth_per_day_mb": 0,
                },
            }

    async def _calculate_storage_growth(self, organization_id: str, days: int) -> float:
        """Calculate storage growth in MB over specified period"""
        try:
            # Simplified: bytes uploaded within the window.
            recent_uploads = (
                await self.db.scalar(
                    select(func.sum(Document.file_size_bytes)).where(
                        and_(
                            Document.organization_id == organization_id,
                            Document.created_at
                            >= datetime.utcnow() - timedelta(days=days),
                        )
                    )
                )
            ) or 0

            return recent_uploads / (1024 * 1024)  # Convert to MB

        except Exception:
            return 0.0

    async def validate_organization_limits(
        self, organization_id: str
    ) -> Dict[str, Any]:
        """Validate organization against various limits and return status"""
        organization = await self.get_organization(organization_id)
        if not organization:
            raise ConfigurationException(
                config_key="organization_id",
                config_value=organization_id,
                reason="Organization not found",
            )

        user_count = await self.get_user_count(organization_id)
        document_count = (await self._get_document_storage_stats(organization_id))[
            "document_count"
        ]

        # Define limits per tier
        tier_limits = {
            StorageTier.FREE: {
                "max_users": 5,
                "max_documents": 100,
                "api_calls_per_day": 1000,
            },
            StorageTier.PROFESSIONAL: {
                "max_users": 50,
                "max_documents": 5000,
                "api_calls_per_day": 10000,
            },
            StorageTier.ENTERPRISE: {
                "max_users": None,  # Unlimited
                "max_documents": None,  # Unlimited
                "api_calls_per_day": None,  # Unlimited
            },
        }

        limits = tier_limits.get(organization.storage_tier, {})

        validation_result = {
            "organization_id": organization_id,
            "storage_tier": organization.storage_tier.value,
            "limits_status": {
                "storage": {
                    "used": organization.storage_used_gb,
                    "limit": organization.storage_limit_gb,
                    "percentage": organization.storage_percentage_used,
                    "status": (
                        "ok" if organization.storage_percentage_used < 90 else "warning"
                    ),
                },
                "users": {
                    "used": user_count,
                    "limit": limits.get("max_users", "unlimited"),
                    "status": (
                        "ok"
                        if limits.get("max_users") is None
                        or user_count < limits.get("max_users")
                        else "exceeded"
                    ),
                },
                "documents": {
                    "used": document_count,
                    "limit": limits.get("max_documents", "unlimited"),
                    "status": (
                        "ok"
                        if limits.get("max_documents") is None
                        or document_count < limits.get("max_documents")
                        else "exceeded"
                    ),
                },
            },
            "overall_status": "ok",
        }

        # Determine overall status
        for limit_info in validation_result["limits_status"].values():
            if limit_info["status"] == "exceeded":
                validation_result["overall_status"] = "error"
            elif (
                limit_info["status"] == "warning"
                and validation_result["overall_status"] == "ok"
            ):
                validation_result["overall_status"] = "warning"

        return validation_result


# Utility functions


def get_tenant_service(db: AsyncSession = Depends(get_db)) -> TenantService:
    """FastAPI dependency: a ``TenantService`` bound to the request session."""
    return TenantService(db)


def check_tenant_permission(
    required_permission: str, organization_id: str = None
) -> bool:
    """Check if current user has permission for tenant operation"""
    from src.middleware.multi_tenancy import (
        get_current_tenant_id,
        get_current_user_role,
    )

    current_role = get_current_user_role()
    current_tenant = get_current_tenant_id()

    # Super admins have all permissions
    if current_role == "super_admin":
        return True

    # Check organization access if specified
    if organization_id and current_tenant != organization_id:
        return False

    # Role-based permissions
    role_permissions = {
        "admin": [
            "organization_read",
            "organization_update",
            "organization_users_read",
            "organization_analytics",
            "tenant_access",
            "update_access",
            "delete_access",
        ],
        "content_manager": [
            "organization_read",
            "organization_users_read",
            "tenant_access",
            "update_access",
        ],
        "analyst": ["organization_read", "organization_analytics", "tenant_access"],
        "user": ["organization_read", "tenant_access"],
    }

    user_permissions = role_permissions.get(current_role, [])
    return required_permission in user_permissions
