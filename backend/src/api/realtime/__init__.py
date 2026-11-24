"""
Realtime API routes for WebSocket connections and real-time status updates
"""

from .realtime_document_status import router as realtime_status_router
from .websocket_v2 import router as websocket_v2_router

# The simulated realtime quality-metrics route was unmounted (GOO-653) and
# then deleted with its per-user WebSocket channel (GOO-740).

__all__ = [
    "websocket_v2_router",
    "realtime_status_router",
]
