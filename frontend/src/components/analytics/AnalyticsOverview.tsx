'use client';

import { useEffect, useState } from 'react';
import { useReducedMotion } from 'framer-motion';
import { cn } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  NOT_MEASURED,
  NOT_MEASURED_NOTE,
  UNAVAILABLE_NOTE,
  type NotMeasured,
} from '@/utils/analyticsFigure';
import {
  Users,
  FileText,
  Search,
  Clock,
  Activity,
  BarChart3,
} from 'lucide-react';

interface MetricCardProps {
  title: string;
  value: string | number;
  icon: any;
  description?: string;
  loading?: boolean;
  onClick?: () => void;
}

function MetricCard({
  title,
  value,
  icon: Icon,
  description,
  loading = false,
  onClick,
}: MetricCardProps) {
  const reduceMotion = useReducedMotion();
  const targetValue = typeof value === 'number' ? value : 0;
  const [animatedValue, setAnimatedValue] = useState(0);

  // Count up to the real value. When the user prefers reduced motion the final
  // number is shown directly and nothing animates.
  useEffect(() => {
    if (loading || typeof value !== 'number' || reduceMotion) return;

    const duration = 1200;
    const steps = 40;
    const stepDuration = duration / steps;
    let step = 0;

    const timer = setInterval(() => {
      step++;
      const progress = step / steps;
      const easeOut = 1 - Math.pow(1 - progress, 3);
      setAnimatedValue(Math.round(targetValue * easeOut));

      if (step >= steps) {
        setAnimatedValue(targetValue);
        clearInterval(timer);
      }
    }, stepDuration);

    return () => clearInterval(timer);
  }, [value, loading, reduceMotion, targetValue]);

  const displayValue = reduceMotion ? targetValue : animatedValue;

  const isInteractive = Boolean(onClick);

  return (
    <Card
      className={cn(
        'transition-colors',
        isInteractive &&
          'cursor-pointer hover:border-primary/40 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring'
      )}
      onClick={onClick}
      {...(isInteractive
        ? {
            role: 'button',
            tabIndex: 0,
            onKeyDown: (e: React.KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick?.();
              }
            },
            'aria-label': `${title} details`,
          }
        : {})}
    >
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-muted text-primary">
          <Icon aria-hidden="true" className="h-4 w-4" />
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-1">
          <div className="text-2xl font-semibold tabular-nums tracking-tight text-foreground">
            {loading ? (
              <div className="h-8 w-24 animate-pulse rounded bg-muted" />
            ) : typeof value === 'number' ? (
              displayValue.toLocaleString()
            ) : (
              value
            )}
          </div>
          {description && !loading && (
            <p className="text-xs text-muted-foreground">{description}</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * A figure the backend reported, `null` when its source failed to load, or
 * `undefined` when the viewer may not see it (the metric is then hidden).
 */
type Figure = number | null | undefined;

export interface AnalyticsOverviewData {
  documentsIndexed: Figure;
  searches24h: Figure;
  /** `NOT_MEASURED` when no search in the window was timed (GOO-768). */
  avgSearchTimeMs24h: Figure | NotMeasured;
  /** Admin only. */
  activeUsers24h?: Figure;
  /** Admin only. */
  searchSessions24h?: Figure;
}

interface AnalyticsOverviewProps {
  data?: AnalyticsOverviewData | null;
  loading?: boolean;
  onRefresh?: () => void;
  onExport?: () => void;
  onMetricClick?: (metric: string) => void;
}

const UNAVAILABLE = '—';

export function AnalyticsOverview({
  data,
  loading = false,
  onMetricClick,
}: AnalyticsOverviewProps) {
  // Only figures the backend reports. Registered users, page views, chat
  // conversations and error rate have no organization-level source, so they
  // are not shown rather than shown as zero (GOO-649).
  const overviewMetrics = [
    {
      key: 'documents',
      title: 'Documents',
      figure: data?.documentsIndexed,
      icon: FileText,
      description: 'Indexed in your knowledge base',
    },
    {
      key: 'searches',
      title: 'Searches',
      figure: data?.searches24h,
      icon: Search,
      description: 'In the last 24 hours',
    },
    {
      key: 'searchTime',
      title: 'Avg search time',
      figure: data?.avgSearchTimeMs24h,
      icon: Clock,
      description: 'Milliseconds, last 24 hours',
    },
    {
      key: 'activeUsers',
      title: 'Active users',
      figure: data?.activeUsers24h,
      icon: Users,
      description: 'Searched in the last 24 hours',
    },
    {
      key: 'sessions',
      title: 'Search sessions',
      figure: data?.searchSessions24h,
      icon: Activity,
      description: 'In the last 24 hours',
    },
  ].filter((metric) => metric.figure !== undefined);

  // A failed source keeps the cards (with "—"), so an outage never looks
  // like a new org. A zero count or an unmeasured average is no activity
  // (GOO-768).
  const hasAnyData = overviewMetrics.some(
    (metric) =>
      metric.figure === null ||
      (typeof metric.figure === 'number' && metric.figure > 0)
  );

  if (!loading && !hasAnyData) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center justify-center gap-2 py-12 text-center">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted text-muted-foreground">
            <BarChart3 aria-hidden="true" className="h-5 w-5" />
          </div>
          <p className="text-sm font-medium text-foreground">No activity yet</p>
          <p className="max-w-xs text-sm text-muted-foreground">
            Upload documents and run searches to start measuring usage across
            your knowledge base.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {overviewMetrics.map((metric) => (
        <MetricCard
          key={metric.key}
          title={metric.title}
          value={
            typeof metric.figure !== 'number'
              ? UNAVAILABLE
              : metric.key === 'searchTime'
                ? Math.round(metric.figure)
                : metric.figure
          }
          icon={metric.icon}
          description={
            metric.figure === NOT_MEASURED
              ? NOT_MEASURED_NOTE
              : metric.figure == null
                ? UNAVAILABLE_NOTE
                : metric.description
          }
          loading={loading}
          onClick={onMetricClick ? () => onMetricClick(metric.key) : undefined}
        />
      ))}
    </div>
  );
}

export default AnalyticsOverview;
