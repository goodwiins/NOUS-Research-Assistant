"""The per-user ``quality_metrics_<user_id>`` channel is gone (GOO-740).

Its only producer was the simulated realtime quality service behind the
``/realtime/quality`` route, which GOO-653 unmounted. A mid-session
WebSocket subscribe accepts any channel name, so while a producer could
publish ``quality_metrics_<id>``, any user could subscribe to another user's
channel. With no producer left, there is nothing to subscribe to; this test
keeps one from coming back unnoticed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

SRC = Path(__file__).resolve().parents[3] / "src"


@pytest.mark.parametrize(
    "module",
    [
        "src.api.realtime.realtime_quality_metrics",
        "src.services.quality.realtime_quality_metrics",
    ],
)
def test_simulated_realtime_quality_modules_are_deleted(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_quality_package_no_longer_exports_realtime_names() -> None:
    import src.services.quality as quality

    for name in ("RealTimeQualityMetricsService", "RealTimeQualityMetrics"):
        assert name not in quality.__all__


def test_no_source_publishes_a_per_user_quality_metrics_channel() -> None:
    pattern = re.compile(r"quality_metrics_\{")
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
