/**
 * Document Analytics API Service
 * Provides methods for fetching document statistics, file type distributions,
 * processing status, and paginated document lists from the backend.
 */

import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';

// Response Types
export interface FileTypeStats {
  type: string;
  count: number;
  total_size_mb: number;
}

export interface ProcessingStats {
  status: string;
  count: number;
}

export interface FileStatsResponse {
  files_by_type: FileTypeStats[];
  processing_stats: ProcessingStats[];
}

export interface DocumentResponse {
  id: string;
  title: string;
  filename: string;
  document_type: string;
  file_size_bytes: number;
  file_size_mb: number;
  mime_type: string;
  processing_status: string;
  tags: string[];
  is_public: boolean;
  content_preview?: string;
  created_at: string;
  updated_at: string;
  uploaded_by_user_id: string;
  organization_id: string;
}

export interface PaginationInfo {
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

export interface DocumentListResponse {
  documents: DocumentResponse[];
  pagination: PaginationInfo;
}

export interface DocumentAnalytics {
  total_documents: number;
  total_storage_mb: number;
  documents_by_type: Record<string, number>;
  processing_status_counts: Record<string, number>;
  average_file_size_mb: number;
  recent_uploads_count: number;
}

// Pilot consumer of the generated OpenAPI types (audit C5 REST contract
// ratchet). Sourced from backend/openapi.json via `pnpm generate:api-types`
// instead of a hand-mirrored interface, so the shape can never silently drift
// from the FastAPI response model. Note `processing_status` is the backend's
// narrowed ApiDocumentStatus enum ('queued' | 'processing' | 'indexed' |
// 'failed'), not a loose `string` as it was when hand-written. This is the
// proof-of-loop consumer; other types migrate adopt-on-touch, not en masse.
export type DocumentStatusResponse =
  components['schemas']['DocumentStatusResponse'];

// Query parameters
export interface DocumentListParams {
  page?: number;
  size?: number;
  status?: string;
  document_type?: string;
  search?: string;
  sort_by?: string;
  sort_order?: 'asc' | 'desc';
  tags?: string[];
}

/**
 * `/files/stats` reports raw stored statuses (`pending`, `processing`,
 * `retrying`, `completed`, `failed`), not the public `ApiDocumentStatus`
 * vocabulary. These are the ones still being worked on.
 */
const IN_FLIGHT_STATUSES = new Set(['pending', 'processing', 'retrying']);

export function countInFlightDocuments(stats: ProcessingStats[]): number {
  return stats
    .filter((s) => IN_FLIGHT_STATUSES.has(s.status))
    .reduce((sum, s) => sum + s.count, 0);
}

/**
 * Document Analytics API Service
 */
class DocumentAnalyticsApiService {
  private readonly basePath = '/files';
  private readonly documentsPath = '/documents';

  /**
   * Get file statistics including type distribution and processing status
   */
  async getFileStats(): Promise<FileStatsResponse> {
    const response = await api.get<FileStatsResponse>(`${this.basePath}/stats`);
    // `FileStatsResponse` requires both lists. A 200 without them (or with a
    // non-numeric count) is shape drift: reject so callers show the source as
    // unavailable, instead of summing an empty list into "0" (GOO-649).
    const filesByType = requireArray<FileTypeStats>(
      response?.files_by_type,
      'files_by_type'
    );
    const processingStats = requireArray<ProcessingStats>(
      response?.processing_stats,
      'processing_stats'
    );
    filesByType.forEach((item) =>
      requireNumber(item?.count, 'files_by_type.count')
    );
    processingStats.forEach((item) =>
      requireNumber(item?.count, 'processing_stats.count')
    );
    return response;
  }

  /**
   * Get paginated list of documents
   */
  async getDocuments(
    params: DocumentListParams = {}
  ): Promise<DocumentListResponse> {
    const queryParams = new URLSearchParams();

    if (params.page) queryParams.append('page', params.page.toString());
    if (params.size) queryParams.append('size', params.size.toString());
    if (params.status) queryParams.append('status', params.status);
    if (params.document_type)
      queryParams.append('document_type', params.document_type);
    if (params.search) queryParams.append('search', params.search);
    if (params.sort_by) queryParams.append('sort_by', params.sort_by);
    if (params.sort_order) queryParams.append('sort_order', params.sort_order);
    if (params.tags?.length) {
      params.tags.forEach((tag) => queryParams.append('tags', tag));
    }

    const queryString = queryParams.toString();
    // Use trailing slash to avoid 307 redirect which drops Authorization header
    const url = queryString
      ? `${this.documentsPath}/?${queryString}`
      : `${this.documentsPath}/`;

    return api.get<DocumentListResponse>(url);
  }

  /**
   * Get single document details
   */
  async getDocument(documentId: string): Promise<DocumentResponse> {
    return api.get<DocumentResponse>(`${this.documentsPath}/${documentId}`);
  }

  /**
   * Get document processing status
   */
  async getDocumentStatus(documentId: string): Promise<DocumentStatusResponse> {
    return api.get<DocumentStatusResponse>(
      `${this.documentsPath}/${documentId}/status`
    );
  }

  /**
   * Get comprehensive document analytics
   * Aggregates data from multiple endpoints for dashboard display
   */
  async getDocumentAnalytics(): Promise<DocumentAnalytics> {
    try {
      // Fetch file stats
      const fileStats = await this.getFileStats();

      // Calculate totals
      const totalDocuments = fileStats.files_by_type.reduce(
        (sum, item) => sum + item.count,
        0
      );
      const totalStorageMb = fileStats.files_by_type.reduce(
        (sum, item) => sum + item.total_size_mb,
        0
      );

      // Transform file types
      const documentsByType: Record<string, number> = {};
      fileStats.files_by_type.forEach((item) => {
        documentsByType[item.type] = item.count;
      });

      // Transform processing status
      const processingStatusCounts: Record<string, number> = {};
      fileStats.processing_stats.forEach((item) => {
        processingStatusCounts[item.status] = item.count;
      });

      // Calculate average file size
      const averageFileSizeMb =
        totalDocuments > 0 ? totalStorageMb / totalDocuments : 0;

      // Get recent uploads (last 24 hours would require a separate endpoint, using completed as proxy)
      const recentUploadsCount = processingStatusCounts['pending'] || 0;

      return {
        total_documents: totalDocuments,
        total_storage_mb: totalStorageMb,
        documents_by_type: documentsByType,
        processing_status_counts: processingStatusCounts,
        average_file_size_mb: averageFileSizeMb,
        recent_uploads_count: recentUploadsCount,
      };
    } catch (error) {
      console.error('Failed to fetch document analytics:', error);
      throw error;
    }
  }

  /**
   * Get chart-ready file type distribution data
   */
  async getFileTypeDistribution(): Promise<
    Array<{ name: string; value: number }>
  > {
    const stats = await this.getFileStats();
    return stats.files_by_type.map((item) => ({
      name: item.type.toUpperCase(),
      value: item.count,
    }));
  }

  /**
   * Get chart-ready processing status data
   */
  async getProcessingStatusDistribution(): Promise<
    Array<{ status: string; count: number }>
  > {
    const stats = await this.getFileStats();
    return stats.processing_stats.map((item) => ({
      status: item.status,
      count: item.count,
    }));
  }
}

// The analytics routes below return untyped dicts (no response_model), so the
// shapes are copied from the backend producers named on each type. Values are
// read through these guards: a missing or non-numeric field throws, so a shape
// drift surfaces as "unavailable" instead of a fabricated zero (GOO-649).
function requireNumber(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`Unexpected analytics response: ${field}`);
  }
  return value;
}

function requireArray<T>(value: unknown, field: string): T[] {
  if (!Array.isArray(value)) {
    throw new Error(`Unexpected analytics response: ${field}`);
  }
  return value as T[];
}

function requireObject(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`Unexpected analytics response: ${field}`);
  }
  return value as Record<string, unknown>;
}

// Search Analytics Types

/**
 * GET /analytics/performance/search-performance. Built from
 * SearchPerformanceMetrics in services/quality/performance_dashboard_service.py.
 */
export interface SearchPerformanceResponse {
  total_searches: number;
  avg_response_time: number;
  p95_response_time: number;
  success_rate: number;
  no_results_rate: number;
  top_queries: Array<{ query: string; count: number; avg_response_time: number }>;
  search_types: Array<{ type: string; count: number }>;
  errors: unknown[];
}

/**
 * GET /search/analytics. The `SearchAnalytics` schema in backend/openapi.json,
 * produced in api/search/search.py.
 */
export type SearchAnalyticsResponse = components['schemas']['SearchAnalytics'];

/**
 * Search Analytics API Service
 */
class SearchAnalyticsApiService {
  private readonly searchPath = '/search';
  private readonly qualityPath = '/analytics/quality';
  private readonly performancePath = '/analytics/performance';

  /**
   * Get search analytics from the search service
   */
  async getSearchAnalytics(
    days: number = 30
  ): Promise<SearchAnalyticsResponse> {
    return api.get<SearchAnalyticsResponse>(
      `${this.searchPath}/analytics?days=${days}`
    );
  }

  /**
   * Get search analytics from quality metrics service
   */
  async getQualitySearchAnalytics(): Promise<SearchAnalyticsResponse> {
    return api.get<SearchAnalyticsResponse>(
      `${this.qualityPath}/analytics/search`
    );
  }

  /**
   * Get search performance metrics
   */
  async getSearchPerformance(
    timeRange: string = '7d'
  ): Promise<SearchPerformanceResponse> {
    // Backend MetricTimeRange enum accepts value-form only (`1h`, `24h`, `7d`,
    // `30d`, `90d`). Passing the enum NAME (e.g. `LAST_7D`) returns 422.
    return api.get<SearchPerformanceResponse>(
      `${this.performancePath}/search-performance?time_range=${timeRange}`
    );
  }

  /**
   * Combined search analytics for the last 7 days: the performance endpoint
   * first, then the search analytics endpoint. Rejects when both fail, or when
   * a response lacks the fields it should carry, so callers show the source as
   * unavailable instead of a fabricated "0 searches" (FL2C-2). Per-query values
   * the backend does not send are `null`, never invented.
   */
  async getCombinedSearchAnalytics(): Promise<CombinedSearchAnalytics> {
    const toType = (rawType: unknown, count: unknown) => {
      const type = typeof rawType === 'string' && rawType ? rawType : 'unknown';
      return {
        name: type.charAt(0).toUpperCase() + type.slice(1),
        value: requireNumber(count, 'search type count'),
      };
    };

    let performanceError: unknown;
    try {
      const performance = await this.getSearchPerformance(
        SEARCH_ANALYTICS_WINDOW
      );
      return {
        topQueries: requireArray<SearchPerformanceResponse['top_queries'][number]>(
          performance.top_queries,
          'top_queries'
        ).map((q, idx) => ({
          id: `search_${idx + 1}`,
          query: q.query,
          count: requireNumber(q.count, 'top_queries.count'),
          avgResponseTimeMs:
            typeof q.avg_response_time === 'number' ? q.avg_response_time : null,
        })),
        searchTypes: requireArray<SearchPerformanceResponse['search_types'][number]>(
          performance.search_types,
          'search_types'
        ).map((t) => toType(t.type, t.count)),
        totalSearches: requireNumber(performance.total_searches, 'total_searches'),
        avgResponseTimeMs: requireNumber(
          performance.avg_response_time,
          'avg_response_time'
        ),
      };
    } catch (error) {
      performanceError = error;
    }

    try {
      const analytics = await this.getSearchAnalytics(
        SEARCH_ANALYTICS_WINDOW_DAYS
      );
      return {
        topQueries: requireArray<Record<string, unknown>>(
          analytics.most_common_queries,
          'most_common_queries'
        ).map((q, idx) => ({
          id: `search_${idx + 1}`,
          query: String(q.query ?? ''),
          count: requireNumber(q.count, 'most_common_queries.count'),
          avgResponseTimeMs: null,
        })),
        searchTypes: Object.entries(
          requireObject(
            analytics.search_types_distribution,
            'search_types_distribution'
          )
        ).map(([type, count]) => toType(type, count)),
        totalSearches: requireNumber(analytics.total_searches, 'total_searches'),
        avgResponseTimeMs: requireNumber(
          analytics.average_search_time_ms,
          'average_search_time_ms'
        ),
      };
    } catch (error) {
      console.error('Failed to fetch search analytics:', performanceError, error);
      throw error;
    }
  }
}

/** Both search sources are read over the same window so their numbers agree. */
export const SEARCH_ANALYTICS_WINDOW = '7d';
const SEARCH_ANALYTICS_WINDOW_DAYS = 7;

export interface CombinedSearchQueryRow {
  id: string;
  query: string;
  count: number;
  /** Only the performance source reports a per-query time. */
  avgResponseTimeMs: number | null;
}

export interface CombinedSearchAnalytics {
  topQueries: CombinedSearchQueryRow[];
  searchTypes: Array<{ name: string; value: number }>;
  totalSearches: number;
  avgResponseTimeMs: number;
}

/**
 * GET /analytics/behavior/organization/trends (admin only; 403 otherwise).
 * Built by UserBehaviorService.get_behavior_trends in
 * services/quality/user_behavior_service.py.
 */
export interface OrganizationTrendsResponse {
  session_trends: Array<{
    period: string;
    session_count: number;
    avg_searches_per_session: number;
    avg_duration_seconds: number;
  }>;
  search_trends: Array<{
    period: string;
    search_count: number;
    avg_response_time: number;
    avg_results: number;
    total_clicks: number;
  }>;
  engagement_trends: Array<{
    period: string;
    active_users: number;
    power_users: number;
    avg_satisfaction: number;
  }>;
}

/** One point of a daily series, in the shape AnalyticsChart reads. */
export interface DailyPoint {
  name: string;
  date: string;
  value: number;
}

const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;

/**
 * Parse a backend period. The producers emit `datetime.isoformat()`
 * (`2026-11-21T00:00:00+00:00`) or `str(datetime)` (`2026-11-21 00:00:00+00:00`)
 * of a `DATE_TRUNC` over a `timestamptz` column. A value without an offset is
 * UTC, never the viewer's local time, so the day label does not shift with the
 * browser's time zone.
 */
export function parseBackendTimestamp(raw: unknown): Date {
  if (typeof raw !== 'string' || raw.trim() === '') {
    throw new Error('Unexpected analytics response: period');
  }
  const iso = raw.trim().replace(' ', 'T');
  const date = new Date(HAS_ZONE.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(date.getTime())) {
    throw new Error('Unexpected analytics response: period');
  }
  return date;
}

const toDailyPoint = (period: unknown, value: unknown): DailyPoint => {
  const date = parseBackendTimestamp(period);
  return {
    // The UTC calendar day: DATE_TRUNC('day') runs in the database's UTC zone.
    name: date.toISOString().slice(0, 10),
    date: date.toISOString(),
    value: requireNumber(value, 'trend value'),
  };
};

/**
 * User Behavior Analytics API Service
 */
// Every getter below rejects on failure instead of returning zeros, so an
// outage is never shown as genuine zero activity (FL2C-2). Callers record the
// failed source and say so.
class UserBehaviorApiService {
  private readonly basePath = '/analytics/behavior';

  /**
   * Daily active users for the organization. Admin only: the backend answers
   * 403 for other roles, so callers check the role before calling.
   */
  async getOrganizationActiveUserTrend(
    daysBack: number = 30
  ): Promise<DailyPoint[]> {
    const response = await api.get<OrganizationTrendsResponse>(
      `${this.basePath}/organization/trends?days_back=${daysBack}&group_by=day`
    );
    return requireArray<OrganizationTrendsResponse['engagement_trends'][number]>(
      response?.engagement_trends,
      'engagement_trends'
    ).map((row) => toDailyPoint(row.period, row.active_users));
  }
}

/**
 * GET /analytics/performance/overview. Built by
 * PerformanceDashboardService.get_dashboard_overview (each section is a
 * dataclass passed through `asdict`), then scoped per caller by
 * `_scope_overview_for` in api/quality/performance_dashboard.py (GOO-633):
 * `user_engagement` is sent only to org admins, and `system_health` (host CPU,
 * memory and disk) only to platform operators. `system_health` is deliberately
 * not typed or read here.
 */
export interface DashboardOverviewResponse {
  timestamp: string;
  time_range: string;
  search_performance: SearchPerformanceResponse;
  /** Org admins only; absent for other roles. */
  user_engagement?: {
    active_users: number;
    total_sessions: number;
    avg_session_duration: number;
    searches_per_user: number;
    top_users: unknown[];
    engagement_trend: string;
  };
}

export interface DashboardOverview {
  /** Backend time range of the figures, e.g. `24h`. */
  timeRange: string;
  totalSearches: number;
  avgSearchTimeMs: number;
  /**
   * `null` when the backend left `user_engagement` out, which it does by design
   * for callers who are not org admins.
   */
  engagement: {
    /** Distinct users who searched in the window. */
    activeUsers: number;
    /** Distinct search sessions in the window (not chat conversations). */
    searchSessions: number;
  } | null;
}

/**
 * Performance Dashboard API Service
 */
class PerformanceApiService {
  private readonly basePath = '/analytics/performance';

  /**
   * Get the organization overview for the last 24 hours. Reads the nested
   * `search_performance` section, which every caller gets and which must be
   * well formed, and `user_engagement` when the backend includes it (org
   * admins only). A missing `user_engagement` is not an error; a malformed one
   * is.
   */
  async getDashboardOverview(): Promise<DashboardOverview> {
    const response = await api.get<DashboardOverviewResponse>(
      `${this.basePath}/overview?time_range=24h`
    );
    const search = requireObject(response?.search_performance, 'search_performance');
    const rawEngagement = response.user_engagement;
    // `_scope_overview_for` pops the key for non-admins, so only a missing key
    // means "not sent to you". Anything else present must be the real section.
    const engagement =
      rawEngagement === undefined
        ? null
        : requireObject(rawEngagement, 'user_engagement');
    return {
      timeRange: String(response.time_range ?? '24h'),
      totalSearches: requireNumber(search.total_searches, 'total_searches'),
      avgSearchTimeMs: requireNumber(search.avg_response_time, 'avg_response_time'),
      engagement: engagement
        ? {
            activeUsers: requireNumber(engagement.active_users, 'active_users'),
            searchSessions: requireNumber(
              engagement.total_sessions,
              'total_sessions'
            ),
          }
        : null,
    };
  }

  /**
   * Daily search volume for the organization over the last 7 days, from
   * `/charts/search_volume` (`{metric_name, time_range, granularity, data}`).
   * `get_metric_chart_data` picks the bucket from the range and ignores the
   * requested granularity: 1h/24h are hourly, 7d is daily, and anything longer
   * is weekly (while the route still echoes `granularity`). So 7d is the only
   * range that yields per-day points. Days without searches are absent.
   */
  async getSearchVolumeTrend(): Promise<DailyPoint[]> {
    const response = await api.get<{
      data: Array<{ timestamp: string; value: number }>;
    }>(`${this.basePath}/charts/search_volume?time_range=7d&granularity=day`);
    return requireArray<{ timestamp: string; value: number }>(
      response?.data,
      'data'
    ).map((point) => toDailyPoint(point.timestamp, point.value));
  }
}

// Export singleton instances
export const documentAnalyticsApi = new DocumentAnalyticsApiService();
export const searchAnalyticsApi = new SearchAnalyticsApiService();
export const userBehaviorApi = new UserBehaviorApiService();
export const performanceApi = new PerformanceApiService();
export default documentAnalyticsApi;
