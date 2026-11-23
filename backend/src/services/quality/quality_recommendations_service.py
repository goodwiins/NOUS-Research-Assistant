"""
Quality Improvement Recommendations Service
Provides AI-powered recommendations for improving search quality and user experience
"""

import json
import logging
import statistics
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, asc, desc, func, or_, text
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.database import get_db_sync
from src.models.document import Document
from src.models.quality import QualityMetric
from src.models.quality_metrics import MetricAggregation, QualityAlert, QualityThreshold
from src.models.search import SearchQuery, SearchResult
from src.services.quality.performance_dashboard_service import (
    performance_dashboard_service,
)
from src.services.quality.user_behavior_service import user_behavior_service

logger = logging.getLogger(__name__)


class RecommendationCategory(Enum):
    """Categories of quality improvement recommendations"""

    CONTENT = "content"  # Document content improvements
    SEARCH_ALGORITHM = "search_algorithm"  # Search algorithm tuning
    INDEXING = "indexing"  # Index optimization
    USER_EXPERIENCE = "user_experience"  # UI/UX improvements
    INFRASTRUCTURE = "infrastructure"  # System performance
    MONITORING = "monitoring"  # Better monitoring/alerting


class RecommendationPriority(Enum):
    """Priority levels for recommendations"""

    CRITICAL = "critical"  # Immediate action required
    HIGH = "high"  # Should be addressed soon
    MEDIUM = "medium"  # Nice to have
    LOW = "low"  # Minor improvement


class RecommendationStatus(Enum):
    """Status of recommendation implementation"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"
    DEFERRED = "deferred"


@dataclass
class QualityRecommendation:
    """Quality improvement recommendation"""

    id: str
    category: RecommendationCategory
    priority: RecommendationPriority
    title: str
    description: str
    impact_assessment: str
    effort_required: str
    actionable_steps: List[str]
    expected_outcome: str
    metrics_to_track: List[str]
    supporting_data: Dict[str, Any]
    estimated_improvement: float  # Percentage improvement expected
    due_date: Optional[datetime] = None
    status: RecommendationStatus = RecommendationStatus.PENDING
    assigned_to: Optional[str] = None
    created_at: datetime = None
    updated_at: datetime = None


# The only database failures an analyser may treat as "metrics unavailable":
# the database could not be reached or a connection broke. ProgrammingError
# (a wrong column, the original PL-2 bug class), DataError, IntegrityError
# and every non-database exception are bugs and propagate.
_TRANSIENT_DB_ERRORS = (
    OperationalError,
    InterfaceError,
    DisconnectionError,
    PoolTimeoutError,
)


class _MetricsUnavailable(Exception):
    """An analyser could not read its metrics; the result is incomplete."""


class RecommendationTrackingUnavailable(RuntimeError):
    """Raised when a write the service cannot persist is requested."""


@dataclass
class QualityInsight:
    """Insight about quality metrics"""

    metric_name: str
    current_value: float
    target_value: float
    gap: float
    trend: str  # improving, declining, stable
    impact_area: str
    root_causes: List[str]
    related_metrics: List[str]
    supporting_data: Dict[str, Any] = field(default_factory=dict)


class QualityRecommendationsService:
    """
    Service for generating and managing quality improvement recommendations
    """

    def __init__(self):
        self.recommendation_cache = {}
        self.insight_cache = {}
        self.cache_ttl = 3600  # 1 hour

    async def generate_recommendations(
        self,
        organization_id: str,
        focus_areas: Optional[List[RecommendationCategory]] = None,
        priority_filter: Optional[RecommendationPriority] = None,
        days_back: int = 30,
        *,
        include_host_metrics: bool = False,
    ) -> List[QualityRecommendation]:
        """Generate quality improvement recommendations.

        ``include_host_metrics`` adds the host CPU/memory recommendations.
        Those describe the shared deployment, not the tenant, so callers must
        pass it only for platform operators (GOO-672, same gate as #2197).
        """

        cache_key = (
            f"{organization_id}_{focus_areas}_{priority_filter}_{days_back}"
            f"_host={include_host_metrics}"
        )
        if cache_key in self.recommendation_cache:
            cached_data = self.recommendation_cache[cache_key]
            if (datetime.utcnow() - cached_data["timestamp"]).seconds < self.cache_ttl:
                return cached_data["recommendations"]

        recommendations = []

        # Get quality insights
        insights, complete = await self._collect_insights(organization_id, days_back)

        # Generate recommendations based on insights
        if not focus_areas or RecommendationCategory.CONTENT in focus_areas:
            recommendations.extend(
                await self._generate_content_recommendations(insights, organization_id)
            )

        if not focus_areas or RecommendationCategory.SEARCH_ALGORITHM in focus_areas:
            recommendations.extend(
                await self._generate_search_algorithm_recommendations(
                    insights, organization_id
                )
            )

        if not focus_areas or RecommendationCategory.INDEXING in focus_areas:
            recommendations.extend(
                await self._generate_indexing_recommendations(insights, organization_id)
            )

        if not focus_areas or RecommendationCategory.USER_EXPERIENCE in focus_areas:
            recommendations.extend(
                await self._generate_user_experience_recommendations(
                    insights, organization_id
                )
            )

        if include_host_metrics and (
            not focus_areas or RecommendationCategory.INFRASTRUCTURE in focus_areas
        ):
            recommendations.extend(
                await self._generate_infrastructure_recommendations(
                    insights, organization_id
                )
            )

        if not focus_areas or RecommendationCategory.MONITORING in focus_areas:
            recommendations.extend(
                await self._generate_monitoring_recommendations(
                    insights, organization_id
                )
            )

        # Filter by priority if specified
        if priority_filter:
            recommendations = [
                r for r in recommendations if r.priority == priority_filter
            ]

        # Sort by priority and estimated impact
        priority_order = {
            RecommendationPriority.CRITICAL: 0,
            RecommendationPriority.HIGH: 1,
            RecommendationPriority.MEDIUM: 2,
            RecommendationPriority.LOW: 3,
        }

        recommendations.sort(
            key=lambda r: (priority_order[r.priority], -r.estimated_improvement)
        )

        # Cache the results, unless they rest on incomplete insights
        if complete:
            self.recommendation_cache[cache_key] = {
                "recommendations": recommendations,
                "timestamp": datetime.utcnow(),
            }

        return recommendations

    async def analyze_quality_insights(
        self, organization_id: str, days_back: int = 30
    ) -> List[QualityInsight]:
        """Analyze quality metrics to generate insights"""

        insights, _complete = await self._collect_insights(organization_id, days_back)
        return insights

    async def _collect_insights(
        self, organization_id: str, days_back: int
    ) -> Tuple[List[QualityInsight], bool]:
        """Insights plus whether every analyser could read its metrics.

        An incomplete result (a database outage) is returned but never
        cached, so the outage does not pin empty insights for an hour.
        """
        cache_key = f"insights_{organization_id}_{days_back}"
        if cache_key in self.insight_cache:
            cached_data = self.insight_cache[cache_key]
            if (datetime.utcnow() - cached_data["timestamp"]).seconds < self.cache_ttl:
                return cached_data["insights"], True

        insights: List[QualityInsight] = []
        complete = True
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        for analyse in (
            self._analyze_relevance_insights,
            self._analyze_precision_insights,
            self._analyze_recall_insights,
            self._analyze_response_time_insights,
            self._analyze_user_satisfaction_insights,
        ):
            try:
                insights.extend(await analyse(organization_id, cutoff_date))
            except _MetricsUnavailable:
                complete = False

        if complete:
            self.insight_cache[cache_key] = {
                "insights": insights,
                "timestamp": datetime.utcnow(),
            }
        return insights, complete

    async def track_recommendation_progress(
        self,
        recommendation_id: str,
        status: RecommendationStatus,
        notes: Optional[str] = None,
        actual_improvement: Optional[float] = None,
    ) -> bool:
        """Track implementation progress of a recommendation"""

        # Nothing stores recommendation progress. Reporting success here told
        # clients a status change was saved when it was not, so say so instead.
        raise RecommendationTrackingUnavailable(
            "Recommendation progress tracking is not available"
        )

    async def get_recommendation_effectiveness(
        self, organization_id: str, completed_days: int = 30
    ) -> Dict[str, Any]:
        """Analyze effectiveness of implemented recommendations"""

        # No baseline is recorded when a recommendation is implemented, so a
        # before/after comparison cannot be measured. The previous version
        # compared two hard-coded dictionaries and served the difference as
        # the organization's improvement; report "not available" instead.
        return {
            "organization_id": organization_id,
            "period_days": completed_days,
            "available": False,
            "reason": "Recommendation effectiveness is not measured yet",
            "effectiveness": {},
            "overall_improvement": None,
        }

    async def _generate_content_recommendations(
        self, insights: List[QualityInsight], organization_id: str
    ) -> List[QualityRecommendation]:
        """Generate content-related recommendations"""
        recommendations = []

        # Low relevance insights
        relevance_insights = [
            i for i in insights if i.metric_name == "relevance" and i.gap > 0.2
        ]

        for insight in relevance_insights:
            if insight.gap > 0.3:
                recommendations.append(
                    QualityRecommendation(
                        id=str(uuid.uuid4()),
                        category=RecommendationCategory.CONTENT,
                        priority=RecommendationPriority.HIGH,
                        title="Improve Document Content Quality",
                        description=f"Documents are showing low relevance scores ({insight.current_value:.2f} vs target {insight.target_value:.2f})",
                        impact_assessment="High impact on user satisfaction and search effectiveness",
                        effort_required="Medium - requires content review and updates",
                        actionable_steps=[
                            "Review top 50 low-performing documents",
                            "Update document metadata and descriptions",
                            "Add relevant keywords and tags",
                            "Improve document structure and readability",
                            "Consider consolidating similar content",
                        ],
                        expected_outcome="Improved relevance scores and user satisfaction",
                        metrics_to_track=[
                            "relevance",
                            "user_satisfaction",
                            "click_through_rate",
                        ],
                        supporting_data=insight.supporting_data,
                        estimated_improvement=25.0,
                        due_date=datetime.utcnow() + timedelta(days=14),
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                )

        return recommendations

    async def _generate_search_algorithm_recommendations(
        self, insights: List[QualityInsight], organization_id: str
    ) -> List[QualityRecommendation]:
        """Generate search algorithm-related recommendations"""
        recommendations = []

        # Precision insights
        precision_insights = [
            i for i in insights if i.metric_name == "precision" and i.gap > 0.15
        ]

        for insight in precision_insights:
            if insight.gap > 0.25:
                recommendations.append(
                    QualityRecommendation(
                        id=str(uuid.uuid4()),
                        category=RecommendationCategory.SEARCH_ALGORITHM,
                        priority=RecommendationPriority.MEDIUM,
                        title="Optimize Search Ranking Algorithm",
                        description=f"Search precision is below target ({insight.current_value:.2f} vs target {insight.target_value:.2f})",
                        impact_assessment="Medium impact on search result quality",
                        effort_required="High - requires algorithm tuning and testing",
                        actionable_steps=[
                            "Analyze current ranking factors and weights",
                            "Implement relevance feedback loop",
                            "Tune BM25 parameters for your domain",
                            "Add semantic search capabilities",
                            "A/B test different ranking strategies",
                        ],
                        expected_outcome="Better search result precision and relevance",
                        metrics_to_track=[
                            "precision",
                            "relevance",
                            "click_through_rate",
                        ],
                        supporting_data=insight.supporting_data,
                        estimated_improvement=20.0,
                        due_date=datetime.utcnow() + timedelta(days=30),
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                )

        return recommendations

    async def _generate_indexing_recommendations(
        self, insights: List[QualityInsight], organization_id: str
    ) -> List[QualityRecommendation]:
        """Generate indexing-related recommendations"""
        recommendations = []

        # Response time insights
        response_time_insights = [
            i for i in insights if i.metric_name == "response_time" and i.gap > 1000
        ]  # 1 second

        for insight in response_time_insights:
            if insight.gap > 2000:  # 2 seconds
                recommendations.append(
                    QualityRecommendation(
                        id=str(uuid.uuid4()),
                        category=RecommendationCategory.INDEXING,
                        priority=RecommendationPriority.HIGH,
                        title="Optimize Search Index Performance",
                        description=f"Search response times are slow ({insight.current_value:.0f}ms vs target {insight.target_value:.0f}ms)",
                        impact_assessment="High impact on user experience and system performance",
                        effort_required="Medium - requires index optimization",
                        actionable_steps=[
                            "Review and optimize index mappings",
                            "Implement query result caching",
                            "Add search result pagination",
                            "Optimize index shard configuration",
                            "Consider index warmup strategies",
                        ],
                        expected_outcome="Faster search response times",
                        metrics_to_track=[
                            "response_time",
                            "throughput",
                            "cache_hit_rate",
                        ],
                        supporting_data=insight.supporting_data,
                        estimated_improvement=40.0,
                        due_date=datetime.utcnow() + timedelta(days=21),
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                )

        return recommendations

    async def _generate_user_experience_recommendations(
        self, insights: List[QualityInsight], organization_id: str
    ) -> List[QualityRecommendation]:
        """Generate user experience-related recommendations"""
        recommendations = []

        # User satisfaction insights
        satisfaction_insights = [
            i for i in insights if i.metric_name == "user_satisfaction" and i.gap > 0.3
        ]

        for insight in satisfaction_insights:
            recommendations.append(
                QualityRecommendation(
                    id=str(uuid.uuid4()),
                    category=RecommendationCategory.USER_EXPERIENCE,
                    priority=RecommendationPriority.MEDIUM,
                    title="Enhance Search User Experience",
                    description=f"User satisfaction is below target ({insight.current_value:.2f} vs target {insight.target_value:.2f})",
                    impact_assessment="Medium impact on user engagement and retention",
                    effort_required="Low to Medium - UI/UX improvements",
                    actionable_steps=[
                        "Implement search suggestions and autocomplete",
                        "Add advanced filtering options",
                        "Improve search result presentation",
                        "Add search result preview capabilities",
                        "Implement user feedback mechanisms",
                    ],
                    expected_outcome="Improved user satisfaction and engagement",
                    metrics_to_track=[
                        "user_satisfaction",
                        "session_duration",
                        "return_user_rate",
                    ],
                    supporting_data=insight.supporting_data,
                    estimated_improvement=15.0,
                    due_date=datetime.utcnow() + timedelta(days=45),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )

        return recommendations

    async def _generate_infrastructure_recommendations(
        self, insights: List[QualityInsight], organization_id: str
    ) -> List[QualityRecommendation]:
        """Generate infrastructure-related recommendations"""
        recommendations = []

        # Get system performance data. Only the dependency call is tolerated;
        # a bug while building a recommendation must surface.
        try:
            system_health = (
                await performance_dashboard_service.get_system_health_metrics()
            )
        except Exception as e:
            logger.warning(
                "Could not get system health for infrastructure recommendations: "
                f"{type(e).__name__}",
                exc_info=True,
            )
            return recommendations

        if system_health.cpu_usage > 80:
            recommendations.append(
                QualityRecommendation(
                    id=str(uuid.uuid4()),
                    category=RecommendationCategory.INFRASTRUCTURE,
                    priority=RecommendationPriority.CRITICAL,
                    title="Scale System Resources - High CPU Usage",
                    description=f"System CPU usage is critical ({system_health.cpu_usage:.1f}%)",
                    impact_assessment="Critical impact on all system operations",
                    effort_required="High - infrastructure scaling",
                    actionable_steps=[
                        "Scale up compute resources immediately",
                        "Implement horizontal scaling for search services",
                        "Add load balancing for search queries",
                        "Optimize resource-intensive operations",
                        "Consider auto-scaling configuration",
                    ],
                    expected_outcome="Improved system performance and reliability",
                    metrics_to_track=["cpu_usage", "response_time", "error_rate"],
                    supporting_data={"cpu_usage": system_health.cpu_usage},
                    estimated_improvement=50.0,
                    due_date=datetime.utcnow() + timedelta(days=7),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )

        if system_health.memory_usage > 85:
            recommendations.append(
                QualityRecommendation(
                    id=str(uuid.uuid4()),
                    category=RecommendationCategory.INFRASTRUCTURE,
                    priority=RecommendationPriority.HIGH,
                    title="Optimize Memory Usage",
                    description=f"System memory usage is high ({system_health.memory_usage:.1f}%)",
                    impact_assessment="High impact on system stability and performance",
                    effort_required="Medium - memory optimization",
                    actionable_steps=[
                        "Analyze memory usage patterns",
                        "Implement memory-efficient algorithms",
                        "Add memory monitoring and alerts",
                        "Optimize data structures and caching",
                        "Consider memory scaling",
                    ],
                    expected_outcome="Reduced memory usage and improved stability",
                    metrics_to_track=["memory_usage", "error_rate", "performance"],
                    supporting_data={"memory_usage": system_health.memory_usage},
                    estimated_improvement=30.0,
                    due_date=datetime.utcnow() + timedelta(days=14),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )

        return recommendations

    async def _generate_monitoring_recommendations(
        self, insights: List[QualityInsight], organization_id: str
    ) -> List[QualityRecommendation]:
        """Generate monitoring-related recommendations"""
        recommendations = []

        # Check for active alerts. Only the dependency call is tolerated.
        try:
            alerts = await performance_dashboard_service.get_active_alerts(
                organization_id
            )
        except Exception as e:
            logger.warning(
                "Could not get alerts for monitoring recommendations: "
                f"{type(e).__name__}",
                exc_info=True,
            )
            return recommendations

        if len(alerts) > 10:
            recommendations.append(
                QualityRecommendation(
                    id=str(uuid.uuid4()),
                    category=RecommendationCategory.MONITORING,
                    priority=RecommendationPriority.MEDIUM,
                    title="Improve Alert Management - Too Many Active Alerts",
                    description=f"Too many active alerts ({len(alerts)}) may indicate alert fatigue",
                    impact_assessment="Medium impact on operational efficiency",
                    effort_required="Low - alert configuration optimization",
                    actionable_steps=[
                        "Review and consolidate alert rules",
                        "Implement alert correlation and grouping",
                        "Add alert severity classification",
                        "Implement alert acknowledgment workflows",
                        "Schedule regular alert rule reviews",
                    ],
                    expected_outcome="Reduced alert noise and improved response times",
                    metrics_to_track=[
                        "alert_count",
                        "mean_time_to_resolution",
                        "false_positive_rate",
                    ],
                    supporting_data={"active_alerts": len(alerts)},
                    estimated_improvement=25.0,
                    due_date=datetime.utcnow() + timedelta(days=30),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )

        return recommendations

    async def _analyze_relevance_insights(
        self, organization_id: str, cutoff_date: datetime
    ) -> List[QualityInsight]:
        """Analyze relevance metric insights"""
        db = next(get_db_sync())
        try:

            # Get relevance metrics
            relevance_data = db.execute(
                text("""
                SELECT
                    AVG(qm.value) as current_value,
                    MIN(qt.threshold_target) as target_value,
                    COUNT(*) as sample_count,
                    STDDEV(qm.value) as std_dev
                FROM quality_metrics qm
                LEFT JOIN quality_thresholds qt ON qm.metric_type = qt.metric_type
                    AND qt.organization_id = qm.organization_id
                WHERE qm.organization_id = :org_id
                    AND qm.metric_type = 'relevance'
                    AND qm.measured_at >= :cutoff_date
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchone()

            if relevance_data and relevance_data.current_value:
                current_value = float(relevance_data.current_value)
                target_value = float(relevance_data.target_value or 0.8)
                gap = target_value - current_value

                # Analyze trend by comparing to the previous equivalent period
                period_length = datetime.utcnow() - cutoff_date
                prev_cutoff = cutoff_date - period_length
                prev_data = db.execute(
                    text("""
                    SELECT AVG(qm.value) as prev_value
                    FROM quality_metrics qm
                    WHERE qm.organization_id = :org_id
                        AND qm.metric_type = 'relevance'
                        AND qm.measured_at >= :prev_cutoff
                        AND qm.measured_at < :cutoff_date
                """),
                    {
                        "org_id": organization_id,
                        "prev_cutoff": prev_cutoff,
                        "cutoff_date": cutoff_date,
                    },
                ).fetchone()

                trend = "stable"
                if prev_data and prev_data.prev_value:
                    prev_value = float(prev_data.prev_value)
                    # Relevance is higher-is-better; 5% relative change threshold
                    if current_value > prev_value * 1.05:
                        trend = "improving"
                    elif current_value < prev_value * 0.95:
                        trend = "declining"

                return [
                    QualityInsight(
                        metric_name="relevance",
                        current_value=current_value,
                        target_value=target_value,
                        gap=gap,
                        trend=trend,
                        impact_area="search_quality",
                        root_causes=self._identify_relevance_root_causes(
                            current_value, target_value
                        ),
                        related_metrics=[
                            "precision",
                            "user_satisfaction",
                            "click_through_rate",
                        ],
                        supporting_data={
                            "sample_count": relevance_data.sample_count,
                            "std_dev": float(relevance_data.std_dev or 0),
                        },
                    )
                ]

        except _TRANSIENT_DB_ERRORS as exc:
            # Database unreachable: report incomplete (never cached). Any
            # other error, including ProgrammingError, is a bug and surfaces.
            logger.warning(
                "Failed to analyze relevance insights: database unavailable",
                exc_info=True,
            )
            raise _MetricsUnavailable() from exc
        finally:
            db.close()

        return []

    async def _analyze_precision_insights(
        self, organization_id: str, cutoff_date: datetime
    ) -> List[QualityInsight]:
        """Analyze precision metric insights"""
        # Similar implementation to relevance analysis
        return []

    async def _analyze_recall_insights(
        self, organization_id: str, cutoff_date: datetime
    ) -> List[QualityInsight]:
        """Analyze recall metric insights"""
        # Similar implementation to relevance analysis
        return []

    async def _analyze_response_time_insights(
        self, organization_id: str, cutoff_date: datetime
    ) -> List[QualityInsight]:
        """Analyze response time insights"""
        db = next(get_db_sync())
        try:

            # Get response time from search queries
            response_time_data = db.execute(
                text("""
                SELECT
                    AVG(sq.search_duration_ms) as current_value,
                    COUNT(*) as sample_count,
                    PERCENTILE_CONT(0.95) WITHIN GROUP
                        (ORDER BY sq.search_duration_ms) as p95
                FROM search_queries sq
                WHERE sq.organization_id = :org_id
                    AND sq.created_at >= :cutoff_date
                    AND sq.search_duration_ms IS NOT NULL
            """),
                {"org_id": organization_id, "cutoff_date": cutoff_date},
            ).fetchone()

            if response_time_data and response_time_data.current_value:
                current_value = float(response_time_data.current_value)
                target_value = 1000.0  # 1 second target
                gap = current_value - target_value

                # Analyze trend by comparing to the previous equivalent period
                period_length = datetime.utcnow() - cutoff_date
                prev_cutoff = cutoff_date - period_length
                prev_rt_data = db.execute(
                    text("""
                    SELECT AVG(sq.search_duration_ms) as prev_value
                    FROM search_queries sq
                    WHERE sq.organization_id = :org_id
                        AND sq.created_at >= :prev_cutoff
                        AND sq.created_at < :cutoff_date
                        AND sq.search_duration_ms IS NOT NULL
                """),
                    {
                        "org_id": organization_id,
                        "prev_cutoff": prev_cutoff,
                        "cutoff_date": cutoff_date,
                    },
                ).fetchone()

                trend = "stable"
                if prev_rt_data and prev_rt_data.prev_value:
                    prev_value = float(prev_rt_data.prev_value)
                    # Response time is lower-is-better; 5% relative change threshold
                    if current_value < prev_value * 0.95:
                        trend = "improving"
                    elif current_value > prev_value * 1.05:
                        trend = "declining"

                return [
                    QualityInsight(
                        metric_name="response_time",
                        current_value=current_value,
                        target_value=target_value,
                        gap=gap,
                        trend=trend,
                        impact_area="user_experience",
                        root_causes=self._identify_response_time_root_causes(
                            current_value, target_value
                        ),
                        related_metrics=["throughput", "system_load", "error_rate"],
                        supporting_data={
                            "sample_count": response_time_data.sample_count,
                            "p95": float(response_time_data.p95 or 0),
                        },
                    )
                ]

        except _TRANSIENT_DB_ERRORS as exc:
            # Database unreachable: report incomplete (never cached). Any
            # other error, including ProgrammingError, is a bug and surfaces.
            logger.warning(
                "Failed to analyze response time insights: database unavailable",
                exc_info=True,
            )
            raise _MetricsUnavailable() from exc
        finally:
            db.close()

        return []

    async def _analyze_user_satisfaction_insights(
        self, organization_id: str, cutoff_date: datetime
    ) -> List[QualityInsight]:
        """Analyze user satisfaction insights"""
        # Similar implementation to other metrics
        return []

    def _identify_relevance_root_causes(
        self, current: float, target: float
    ) -> List[str]:
        """Identify potential root causes for low relevance"""
        causes = []

        if current < target * 0.7:
            causes.extend(
                [
                    "Poor document quality or outdated content",
                    "Inadequate indexing of key terms",
                    "Missing semantic understanding",
                    "Insufficient content metadata",
                ]
            )
        elif current < target * 0.85:
            causes.extend(
                [
                    "Suboptimal ranking algorithm parameters",
                    "Limited query understanding",
                    "Content gaps in knowledge base",
                ]
            )

        return causes

    def _identify_response_time_root_causes(
        self, current: float, target: float
    ) -> List[str]:
        """Identify potential root causes for slow response times"""
        causes = []

        if current > target * 3:
            causes.extend(
                [
                    "Insufficient system resources",
                    "Unoptimized database queries",
                    "Large result sets without pagination",
                    "Network latency issues",
                ]
            )
        elif current > target * 1.5:
            causes.extend(
                [
                    "Inefficient search algorithms",
                    "Missing query result caching",
                    "Index optimization needed",
                ]
            )

        return causes


# Global service instance
quality_recommendations_service = QualityRecommendationsService()
