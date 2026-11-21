/**
 * GraphHealthMonitor Component
 * Monitors and displays the health status of the knowledge graph.
 *
 * Renders only what `GET /knowledge-graph/health` returns (`GraphHealthStatus`
 * in the generated API types). It used to invent a health score (defaulting
 * to 100), quality metrics and a `last_check` the backend never sends, so a
 * degraded graph looked healthy (FR-3).
 */

import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Database,
  Zap,
  HelpCircle,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';
import toast from 'react-hot-toast';

type BackendGraphHealthStatus = components['schemas']['GraphHealthStatus'];

type GraphStatus = 'healthy' | 'degraded' | 'critical' | 'unknown';

// Map the backend's free-form status string. Anything unrecognised is shown
// as "unknown", never as healthy.
export const toGraphStatus = (status: string | null | undefined): GraphStatus => {
  switch (status) {
    case 'healthy':
      return 'healthy';
    case 'degraded':
      return 'degraded';
    case 'unhealthy':
    case 'critical':
      return 'critical';
    default:
      return 'unknown';
  }
};

const NOT_REPORTED = '—';

const getStatusColor = (status: GraphStatus): string => {
  switch (status) {
    case 'healthy':
      return 'var(--nous-sol)';
    case 'degraded':
      return 'var(--nous-helios)';
    case 'critical':
      return 'var(--nous-mars)';
    default:
      return 'var(--nous-fg-3)';
  }
};

const getStatusIcon = (status: GraphStatus): React.ReactNode => {
  switch (status) {
    case 'healthy':
      return <CheckCircle2 className="w-6 h-6" />;
    case 'degraded':
      return <AlertTriangle className="w-6 h-6" />;
    case 'critical':
      return <XCircle className="w-6 h-6" />;
    default:
      return <HelpCircle className="w-6 h-6" />;
  }
};

export const GraphHealthMonitor: React.FC = () => {
  const [autoRefresh, setAutoRefresh] = useState(false);

  // TanStack Query owns this request-backed state (one cache owner per server
  // entity).
  const {
    data: health = null,
    error,
    isFetching: loading,
    dataUpdatedAt,
    refetch,
  } = useQuery({
    queryKey: ['knowledge-graph', 'health'],
    queryFn: () => api.get<BackendGraphHealthStatus>('/knowledge-graph/health'),
    refetchInterval: autoRefresh ? 30000 : false, // Refresh every 30s
    // A health monitor reports current state: fetch on every mount (the app
    // default caches for 5 min) and surface a failure at once instead of
    // after three backoff retries.
    staleTime: 0,
    retry: false,
  });

  useEffect(() => {
    if (error) {
      console.error('Error fetching health:', error);
      toast.error('Failed to fetch graph health');
    }
  }, [error]);

  if (loading && !health) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="flex flex-col items-center gap-4">
          <Activity className="w-8 h-8 text-(--nous-sol) animate-pulse" />
          <p className="font-mono text-sm text-(--nous-fg-1)">
            CHECKING_GRAPH_HEALTH...
          </p>
        </div>
      </div>
    );
  }

  if (!health) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 h-96">
        <p
          role={error ? 'alert' : 'status'}
          className="font-mono text-sm text-(--nous-fg-3)"
        >
          {error ? 'Graph health is unavailable' : 'No health data available'}
        </p>
        {error && (
          <Button
            onClick={() => void refetch()}
            disabled={loading}
            variant="outline"
            size="sm"
            className="font-mono text-xs"
          >
            RETRY
          </Button>
        )}
      </div>
    );
  }

  const status = toGraphStatus(health.status);
  const details: Array<{ label: string; value: string }> = [
    { label: 'Neo4j Version', value: health.neo4j_version || NOT_REPORTED },
    {
      label: 'Response Time',
      value: `${Math.round(health.response_time_ms)} ms`,
    },
    { label: 'Indexes', value: health.index_count.toLocaleString() },
    { label: 'Constraints', value: health.constraint_count.toLocaleString() },
    { label: 'Database Size', value: health.database_size || NOT_REPORTED },
    { label: 'Uptime', value: health.uptime || NOT_REPORTED },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-mono font-bold text-(--nous-fg-1) uppercase tracking-wider">
            Graph Health Monitor
          </h2>
          {dataUpdatedAt > 0 && (
            <p className="text-xs font-mono text-(--nous-fg-3) mt-1">
              Fetched: {new Date(dataUpdatedAt).toLocaleString()}
            </p>
          )}
          {error && (
            <p role="alert" className="text-xs font-mono text-(--nous-mars) mt-1">
              Latest refresh failed; showing the last successful check.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => setAutoRefresh(!autoRefresh)}
            variant="outline"
            size="sm"
            className="font-mono text-xs"
          >
            {autoRefresh ? 'Auto-refresh: ON' : 'Auto-refresh: OFF'}
          </Button>
          <Button
            onClick={() => void refetch()}
            disabled={loading}
            variant="outline"
            size="sm"
            className="font-mono text-xs"
          >
            <RefreshCw
              className={`w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`}
            />
            REFRESH
          </Button>
        </div>
      </div>

      {/* Overall Status */}
      <Card className="border-(--nous-border-1) bg-(--nous-bg-2) shadow-lg">
        <CardContent className="p-6">
          <div className="flex items-center gap-4">
            <div
              className="p-4 rounded-lg"
              style={{ backgroundColor: `color-mix(in srgb, ${getStatusColor(status)} 12%, transparent)` }}
            >
              <div style={{ color: getStatusColor(status) }}>
                {getStatusIcon(status)}
              </div>
            </div>
            <div>
              <p className="text-xs font-mono text-(--nous-fg-3) uppercase tracking-widest">
                Overall Status
              </p>
              <p
                className="text-3xl font-mono font-bold uppercase mt-1"
                style={{ color: getStatusColor(status) }}
              >
                {status}
              </p>
            </div>
          </div>
          {health.last_error && (
            <div className="flex items-start gap-2 mt-4 p-3 rounded-lg border border-(--nous-border-1) bg-(--nous-bg-1)">
              <AlertTriangle className="w-4 h-4 text-(--nous-helios) shrink-0" />
              <p className="text-xs font-mono text-(--nous-fg-1)">
                Last error: {health.last_error}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Counts */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card className="border-(--nous-border-1) bg-(--nous-bg-2)">
          <CardContent className="p-4">
            <div className="flex items-center gap-2 mb-2">
              <Database className="w-4 h-4 text-(--nous-sol)" />
              <span className="text-xs font-mono text-(--nous-fg-3)">
                TOTAL ENTITIES
              </span>
            </div>
            <p className="text-2xl font-mono font-bold text-(--nous-fg-1)">
              {health.node_count.toLocaleString()}
            </p>
          </CardContent>
        </Card>

        <Card className="border-(--nous-border-1) bg-(--nous-bg-2)">
          <CardContent className="p-4">
            <div className="flex items-center gap-2 mb-2">
              <Zap className="w-4 h-4 text-(--nous-helios)" />
              <span className="text-xs font-mono text-(--nous-fg-3)">
                RELATIONSHIPS
              </span>
            </div>
            <p className="text-2xl font-mono font-bold text-(--nous-fg-1)">
              {health.relationship_count.toLocaleString()}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Database details */}
      <Card className="border-(--nous-border-1) bg-(--nous-bg-2)">
        <CardContent className="p-4">
          <dl className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {details.map((item) => (
              <div
                key={item.label}
                className="flex items-center justify-between p-2 rounded bg-(--nous-bg-1)"
              >
                <dt className="text-xs font-mono text-(--nous-fg-3)">
                  {item.label}
                </dt>
                <dd className="text-sm font-mono font-bold text-(--nous-fg-1)">
                  {item.value}
                </dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
    </div>
  );
};
