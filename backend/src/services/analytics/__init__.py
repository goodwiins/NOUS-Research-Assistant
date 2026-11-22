"""
Analytics services.

DashboardService, MetricsService and ReportGenerationService backed only the
unmounted analytics dashboards / metrics / reports routers and were retired in
GOO-690.
"""

from .realtime_service import RealtimeAnalyticsService

__all__ = [
    "RealtimeAnalyticsService",
]
