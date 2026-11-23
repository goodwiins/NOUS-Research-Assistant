import { api } from '@/services/api-client';
import {
  RAGTriadMetrics,
  SystemPerformanceMetrics,
  SystemUsageMetrics,
  PerformanceAnalytics,
  UsageAnalytics,
  TimeRange,
  CustomTimeRange,
  AnalyticsFilters,
} from '@/types';

// Type guard for CustomTimeRange
const isCustomTimeRange = (
  timeRange: TimeRange
): timeRange is CustomTimeRange => {
  return (
    typeof timeRange === 'object' && 'start' in timeRange && 'end' in timeRange
  );
};

export interface QualityMetric {
  id: string;
  name: string;
  current_value: number;
  target_value: number;
  threshold: number;
  unit: string;
  trend: 'improving' | 'declining' | 'stable';
  status: 'good' | 'warning' | 'critical';
  last_updated: string;
  description: string;
}

export interface QualityAlert {
  id: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  title: string;
  description: string;
  metric_name: string;
  recommendation: string;
  created_at: string;
}

export interface UserBehaviorStats {
  total_users: number;
  active_users_today: number;
  active_users_week: number;
  active_users_month: number;
  total_sessions: number;
  avg_session_duration: number;
  bounce_rate: number;
  search_volume_today: number;
  search_volume_week: number;
  user_satisfaction_score: number;
  returning_users: number;
  new_users: number;
}

export interface SearchQuery {
  id: string;
  query: string;
  user_id: string;
  user_email: string;
  timestamp: string;
  results_count: number;
  response_time_ms: number;
  user_rating: 'up' | 'down' | null;
  clicked_results: number;
  filters_used: string[];
  content_types: string[];
}

export interface UserSession {
  id: string;
  user_id: string;
  user_email: string;
  start_time: string;
  duration_seconds: number;
  search_count: number;
  document_views: number;
  session_quality: 'high' | 'medium' | 'low';
  satisfaction_score: number | null;
}

export interface Recommendation {
  id: string;
  title: string;
  description: string;
  category:
    | 'content'
    | 'search_algorithm'
    | 'infrastructure'
    | 'user_experience'
    | 'indexing'
    | 'monitoring';
  priority: 'critical' | 'high' | 'medium' | 'low';
  impact_assessment: string;
  effort_required: 'low' | 'medium' | 'high';
  actionable_steps: string[];
  expected_outcome: string;
  estimated_improvement: number;
  status: 'pending' | 'in_progress' | 'completed' | 'rejected' | 'deferred';
  created_at: string;
  updated_at: string;
  votes: {
    up: number;
    down: number;
  };
  implemented_at?: string;
  results_observed?: string;
}

export interface RecommendationRule {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  category: string;
  condition: string;
  recommendation_template: string;
  last_triggered: string;
  trigger_count: number;
}

class AnalyticsService {
  // These legacy getters have no mock fallback: a failed request rejects so
  // callers render an explicit "unavailable" state instead of fabricated
  // health, latency or usage numbers (FL2C-1). `api.get` resolves to the
  // response body, so it is returned as-is.

  // Quality Metrics
  async getQualityMetrics(): Promise<{
    metrics: QualityMetric[];
    alerts: QualityAlert[];
  }> {
    return api.get('/analytics/quality/metrics');
  }

  // User Behavior Analytics
  async getUserBehaviorStats(timeRange: string = '7d'): Promise<{
    stats: UserBehaviorStats;
    queries: SearchQuery[];
    sessions: UserSession[];
  }> {
    return api.get(
      `/analytics/user-behavior?timeRange=${encodeURIComponent(timeRange)}`
    );
  }

  // Recommendations Engine
  async getRecommendations(): Promise<{
    recommendations: Recommendation[];
    rules: RecommendationRule[];
    improvements: unknown[];
  }> {
    return api.get('/analytics/recommendations');
  }

  // Update recommendation status
  async updateRecommendationStatus(
    recommendationId: string,
    status: Recommendation['status']
  ): Promise<void> {
    try {
      await api.patch(
        `/analytics/recommendations/${recommendationId}`,
        { status }
      );
    } catch (error) {
      console.error('Failed to update recommendation status:', error);
      throw error;
    }
  }

  // Vote on recommendation
  async voteOnRecommendation(
    recommendationId: string,
    voteType: 'up' | 'down'
  ): Promise<void> {
    try {
      await api.post(
        `/analytics/recommendations/${recommendationId}/vote`,
        { voteType }
      );
    } catch (error) {
      console.error('Failed to vote on recommendation:', error);
      throw error;
    }
  }

  // RAG Triad Metrics (New architecture)
  async getRAGTriadMetrics(
    timeRange: TimeRange
  ): Promise<{ metrics: RAGTriadMetrics }> {
    try {
      const params = isCustomTimeRange(timeRange)
        ? new URLSearchParams({ start: timeRange.start, end: timeRange.end })
        : new URLSearchParams({ preset: timeRange });
      return await api.get(`/analytics/rag-triad?${params}`);
    } catch (error) {
      console.error('Failed to fetch RAG triad metrics:', error);
      throw error;
    }
  }

  // Performance Analytics (New architecture)
  async getPerformanceAnalytics(
    filters: AnalyticsFilters,
    timeRange: TimeRange
  ): Promise<PerformanceAnalytics> {
    try {
      return await api.post('/analytics/performance', {
        filters,
        timeRange,
      });
    } catch (error) {
      console.error('Failed to fetch performance analytics:', error);
      throw error;
    }
  }

  // Usage Analytics (New architecture)
  async getUsageAnalytics(timeRange: TimeRange): Promise<UsageAnalytics> {
    try {
      const params = isCustomTimeRange(timeRange)
        ? new URLSearchParams({ start: timeRange.start, end: timeRange.end })
        : new URLSearchParams({ preset: timeRange });
      return await api.get(`/analytics/usage?${params}`);
    } catch (error) {
      console.error('Failed to fetch usage analytics:', error);
      throw error;
    }
  }

  // Real-time Metrics (New architecture)
  async getRealTimeMetrics(): Promise<{ metrics: RAGTriadMetrics }> {
    try {
      return await api.get('/analytics/real-time');
    } catch (error) {
      console.error('Failed to fetch real-time metrics:', error);
      throw error;
    }
  }

  // Historical Trends (New architecture)
  async getHistoricalTrends(
    metric: string,
    timeRange: TimeRange
  ): Promise<any> {
    try {
      const params = isCustomTimeRange(timeRange)
        ? new URLSearchParams({ start: timeRange.start, end: timeRange.end })
        : new URLSearchParams({ preset: timeRange });
      return await api.get(`/analytics/trends/${metric}?${params}`);
    } catch (error) {
      console.error('Failed to fetch historical trends:', error);
      throw error;
    }
  }

  // Comparison Data (New architecture)
  async getComparisonData(timeRanges: TimeRange[]): Promise<any> {
    try {
      return await api.post('/analytics/compare', {
        timeRanges,
      });
    } catch (error) {
      console.error('Failed to fetch comparison data:', error);
      throw error;
    }
  }

  // Export Analytics (New architecture)
  async exportAnalytics(
    format: 'csv' | 'json' | 'pdf',
    filters: AnalyticsFilters,
    timeRange: TimeRange
  ): Promise<void> {
    try {
      await api.download(
        `/analytics/export?format=${format}`,
        `analytics-${Date.now()}.${format}`
      );
    } catch (error) {
      console.error('Failed to export analytics:', error);
      throw error;
    }
  }
}

// Create singleton instance
const analyticsService = new AnalyticsService();

export default analyticsService;
