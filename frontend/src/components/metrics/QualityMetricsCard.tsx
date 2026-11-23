import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import {
  ChartBarIcon,
  ClockIcon,
  DocumentTextIcon,
} from '@heroicons/react/24/outline';
import React from 'react';

interface QualityMetricsCardProps {
  className?: string;
  query?: string;
  sessionId?: string;
}

export const UNAVAILABLE_LABEL = 'Unavailable';

const QUALITY_METRICS = [
  { key: 'answerRelevancy', label: 'Answer relevancy' },
  { key: 'faithfulness', label: 'Faithfulness' },
  { key: 'contextualRelevancy', label: 'Context relevancy' },
  { key: 'safety', label: 'Safety score' },
] as const;

const PERFORMANCE_METRICS = [
  { key: 'latency', label: 'Latency', Icon: ClockIcon },
  { key: 'documents', label: 'Documents', Icon: DocumentTextIcon },
  { key: 'entities', label: 'Entities', Icon: ChartBarIcon },
] as const;

/**
 * Retrieval quality for a query.
 *
 * There is no live quality-metrics backend for this card (the WebSocket
 * subscription it once had never worked - R4-H1/H2/H3). It used to fill the
 * gap with Math.random() values refreshed every 2s, which read as real
 * measurements (GOO-724). It now says plainly that the metrics are
 * unavailable instead of inventing them.
 */
export const QualityMetricsCard: React.FC<QualityMetricsCardProps> = ({
  className,
  query,
}) => {
  return (
    <Card className={cn('overflow-hidden', className)}>
      <CardContent className="p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="p-2 rounded-lg bg-primary/10 text-primary">
            <ChartBarIcon aria-hidden="true" className="h-5 w-5" />
          </div>
          <div>
            <h3 className="text-base font-semibold text-foreground">
              Retrieval quality
            </h3>
            <p
              role="status"
              className="flex items-center gap-1.5 text-xs text-muted-foreground mt-0.5"
            >
              <span
                aria-hidden="true"
                className="inline-flex h-1.5 w-1.5 rounded-full bg-muted-foreground"
              />
              Metrics unavailable: no live quality measurements are connected.
            </p>
          </div>
        </div>

        {query ? (
          <div className="mb-6 p-3 rounded-lg border border-border bg-muted/30">
            <span className="text-sm text-foreground truncate max-w-md block">
              <span className="text-muted-foreground">Query: </span>
              {query}
            </span>
          </div>
        ) : null}

        <div className="grid grid-cols-2 gap-4 mb-6">
          {QUALITY_METRICS.map((metric) => (
            <div
              key={metric.key}
              data-testid={`quality-metric-${metric.key}`}
              className="p-4 rounded-xl border border-border bg-card"
            >
              <div className="text-sm font-medium text-foreground mb-3">
                {metric.label}
              </div>
              <div className="text-sm text-muted-foreground">
                {UNAVAILABLE_LABEL}
              </div>
            </div>
          ))}
        </div>

        <div className="border-t border-border pt-4">
          <h4 className="text-sm font-medium text-foreground mb-3">
            Performance
          </h4>
          <div className="grid grid-cols-3 gap-4">
            {PERFORMANCE_METRICS.map(({ key, label, Icon }) => (
              <div
                key={key}
                data-testid={`performance-metric-${key}`}
                className="flex items-center gap-2"
              >
                <Icon
                  aria-hidden="true"
                  className="h-4 w-4 text-muted-foreground shrink-0"
                />
                <div>
                  <div className="text-sm text-muted-foreground">
                    {UNAVAILABLE_LABEL}
                  </div>
                  <div className="text-xs text-muted-foreground">{label}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  );
};

export default QualityMetricsCard;
