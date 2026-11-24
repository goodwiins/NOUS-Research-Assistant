"""
Analytics models.

The analytics dashboard / metric / KPI / report models (the ``analytics_*``
tables) backed only unmounted routers and were retired in GOO-690. The graph
analytics and realtime models below are still not registered in
``src.models`` (see ``KNOWN_UNREGISTERED`` in
``tests/unit/architecture/test_orm_tables_reach_alembic.py``).
"""

# AnalyticsEvent is re-exported from the canonical model module
# (src/models/analytics_event.py) to avoid a duplicate ORM mapping of the
# `analytics_events` table.
from ..analytics_event import AnalyticsEvent
from .graph_analytics import (
    CommunityMetrics,
    EdgeMetrics,
    GraphAnalyticsResult,
    GraphMetrics,
    NodeMetrics,
    PathAnalytics,
)
from .realtime_models import (
    EventStream,
    LiveMetric,
    RealtimeSubscription,
    WebSocketMessage,
)

__all__ = [
    "AnalyticsEvent",
    # Graph analytics models
    "GraphAnalyticsResult",
    "GraphMetrics",
    "NodeMetrics",
    "EdgeMetrics",
    "CommunityMetrics",
    "PathAnalytics",
    # Realtime models
    "RealtimeSubscription",
    "WebSocketMessage",
    "LiveMetric",
    "EventStream",
]
