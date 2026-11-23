"""
Realtime API routes for WebSocket connections and real-time status updates
"""

from .realtime_document_status import router as realtime_status_router
from .websocket_v2 import router as websocket_v2_router

# realtime_quality_metrics is intentionally not exported or mounted: it
# streamed simulated scores (GOO-653).

__all__ = [
    "websocket_v2_router",
    "realtime_status_router",
]
