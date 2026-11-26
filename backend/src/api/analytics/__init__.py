"""
Analytics API routes.

The dashboards / metrics / reports routers (and their ``analytics_*`` tables)
were never mounted and were retired in GOO-690. Only the (also unmounted)
realtime router remains.
"""

from .realtime import router as realtime_router

__all__ = [
    "realtime_router",
]
