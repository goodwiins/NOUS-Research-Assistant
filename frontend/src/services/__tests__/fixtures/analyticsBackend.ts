/**
 * Success-path response bodies for the analytics routes, copied from the
 * backend producers at origin/develop 9cb10c452 (these routes have no
 * response_model, so openapi.json types them as `{}`). Keep each in step with
 * the file named above it.
 */

// backend/src/services/quality/performance_dashboard_service.py:
// PerformanceDashboardService.get_dashboard_overview, each section `asdict()`
// of its dataclass. GET /analytics/performance/overview then passes it through
// `_scope_overview_for` (backend/src/api/quality/performance_dashboard.py),
// which drops `system_health` unless the caller is a platform operator and
// `user_engagement` unless the caller is an org admin. The three scoped
// shapes a caller can receive are exported below.
const unscopedOverviewBody = {
  timestamp: '2026-11-23T05:00:00',
  time_range: '24h',
  system_health: {
    cpu_usage: 41.5,
    memory_usage: 63.2,
    disk_usage: 70.1,
    network_io: {
      bytes_sent: 1.0,
      bytes_recv: 2.0,
      packets_sent: 3.0,
      packets_recv: 4.0,
    },
    response_time_p50: 120.0,
    response_time_p95: 480.0,
    error_rate: 0.02,
    active_connections: 9,
    uptime: 86400.0,
    timestamp: '2026-11-23T05:00:00',
  },
  search_performance: {
    total_searches: 37,
    avg_response_time: 212.4,
    p95_response_time: 640.0,
    success_rate: 1.0,
    no_results_rate: 0.08,
    top_queries: [
      { query: 'graph neural networks', count: 9, avg_response_time: 180.5 },
    ],
    search_types: [{ type: 'hybrid', count: 30 }],
    errors: [],
  },
  quality_metrics: {
    overall_score: 0.8,
    precision_avg: 0.7,
    recall_avg: 0.6,
    relevance_avg: 0.75,
    user_satisfaction: 0.9,
    active_alerts: 0,
    trends: {},
    top_issues: [],
  },
  user_engagement: {
    active_users: 5,
    total_sessions: 11,
    avg_session_duration: 0.0,
    searches_per_user: 7.4,
    top_users: [],
    engagement_trend: 'stable',
  },
  alerts: [],
};

const {
  system_health: _systemHealth,
  user_engagement: _userEngagement,
  ...memberSections
} = unscopedOverviewBody;

/** A member: neither `system_health` nor `user_engagement`. */
export const memberOverviewBody = memberSections;

/** An org admin who is not a platform operator: `user_engagement` only. */
export const adminOverviewBody = {
  ...memberSections,
  user_engagement: _userEngagement,
};

/** A platform operator who is also an org admin: both sections. */
export const operatorOverviewBody = unscopedOverviewBody;

// backend/src/api/quality/performance_dashboard.py: get_search_performance_metrics
// (GET /analytics/performance/search-performance).
export const searchPerformanceBody = {
  total_searches: 120,
  avg_response_time: 230.6,
  p95_response_time: 700.0,
  success_rate: 1.0,
  no_results_rate: 0.1,
  top_queries: [
    { query: 'transformers', count: 14, avg_response_time: 199.2 },
    { query: 'diffusion', count: 6, avg_response_time: 250.0 },
  ],
  search_types: [
    { type: 'hybrid', count: 100 },
    { type: 'keyword', count: 20 },
  ],
  errors: [],
};

// backend/src/api/search/search.py (the SearchAnalytics schema in
// backend/openapi.json), GET /search/analytics.
export const searchAnalyticsBody = {
  total_searches: 42,
  average_search_time_ms: 310.0,
  most_common_queries: [{ query: 'protein folding', count: 8 }],
  search_types_distribution: { hybrid: 30, unknown: 12 },
  zero_result_queries: ['zzz'],
  average_results_per_search: 6.5,
};

// GET /analytics/performance/charts/search_volume?time_range=7d&granularity=day.
// `data` is what PerformanceDashboardService.get_metric_chart_data
// (backend/src/services/quality/performance_dashboard_service.py) returns for
// LAST_7D: it forces `day` buckets for 7d (`hour` for 1h/24h, `week` for
// anything longer, whatever `granularity` says), one row per day that had
// searches, `{"timestamp": period.isoformat(), "value": float(count)}`.
// `period` is DATE_TRUNC over `search_queries.created_at`, a timestamptz, so
// the timestamp carries an offset. The route wrapper
// (backend/src/api/quality/performance_dashboard.py: get_metric_chart_data)
// adds the other three keys and echoes the requested granularity.
export const searchVolumeChartBody = {
  metric_name: 'search_volume',
  time_range: '7d',
  granularity: 'day',
  data: [
    { timestamp: '2026-11-17T00:00:00+00:00', value: 3.0 },
    { timestamp: '2026-11-21T00:00:00+00:00', value: 4.0 },
    { timestamp: '2026-11-22T00:00:00+00:00', value: 9.0 },
  ],
};

// backend/src/services/quality/user_behavior_service.py:
// UserBehaviorService.get_behavior_trends
// (GET /analytics/behavior/organization/trends, admin only). `period` is
// `str()` of a DATE_TRUNC over timestamptz columns, hence the space and offset.
export const organizationTrendsBody = {
  session_trends: [
    {
      period: '2026-11-22 00:00:00+00:00',
      session_count: 3,
      avg_searches_per_session: 2.0,
      avg_duration_seconds: 60.0,
    },
  ],
  search_trends: [
    {
      period: '2026-11-22 00:00:00+00:00',
      search_count: 6,
      avg_response_time: 200.0,
      avg_results: 5.0,
      total_clicks: 2,
    },
  ],
  engagement_trends: [
    {
      period: '2026-11-21 00:00:00+00:00',
      active_users: 2,
      power_users: 0,
      avg_satisfaction: 0.0,
    },
    {
      period: '2026-11-22 00:00:00+00:00',
      active_users: 4,
      power_users: 1,
      avg_satisfaction: 0.5,
    },
  ],
};
