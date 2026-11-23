'use client';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';
import { useAuth } from '@/hooks/useAuth';
import {
  countInFlightDocuments,
  documentAnalyticsApi,
  type CombinedSearchQueryRow,
  type DailyPoint,
  type DocumentResponse,
  type FileTypeStats,
  type PaginationInfo,
  performanceApi,
  type ProcessingStats,
  searchAnalyticsApi,
  userBehaviorApi,
} from '@/services/documentAnalyticsApi';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle,
  Clock,
  Download,
  FileText,
  Info,
  MessageSquare,
  RefreshCw,
  Search,
  TrendingUp,
  Users,
  XCircle,
} from 'lucide-react';
import { motion, MotionConfig } from 'framer-motion';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { AnalyticsChart } from './AnalyticsChart';
import { AnalyticsOverview, type AnalyticsOverviewData } from './AnalyticsOverview';
import {
  AnalyticsTable,
  createDocumentAnalyticsTable,
  createSearchAnalyticsTable,
} from './AnalyticsTable';

interface AnalyticsDashboardProps {
  className?: string;
}

// Transform API document to table format
const transformDocumentToTableRow = (doc: DocumentResponse) => ({
  id: doc.id,
  filename: doc.filename,
  type: doc.document_type,
  size: doc.file_size_bytes,
  uploadedAt: doc.created_at,
  status: doc.processing_status,
});

// Transform file type stats for pie chart
const transformFileTypeForChart = (stats: FileTypeStats[]) =>
  stats.map((item) => ({
    name: item.type.toUpperCase(),
    value: item.count,
  }));

// Transform processing stats for display. Each status pairs an icon with its
// label, so color is never the only signal.
const transformProcessingStats = (stats: ProcessingStats[]) => {
  const statusConfig: Record<string, { icon: any; color: string }> = {
    completed: { icon: CheckCircle, color: 'text-(--nous-terra)' },
    processing: { icon: Clock, color: 'text-primary' },
    failed: { icon: XCircle, color: 'text-(--nous-mars)' },
    pending: { icon: Clock, color: 'text-muted-foreground' },
  };

  return stats.map((item) => ({
    status: item.status.charAt(0).toUpperCase() + item.status.slice(1),
    count: item.count,
    icon: statusConfig[item.status]?.icon || Clock,
    color: statusConfig[item.status]?.color || 'text-muted-foreground',
  }));
};

/** A figure the backend reported, or `null` when its source failed to load. */
type Figure = number | null;

interface DashboardData {
  overview: AnalyticsOverviewData;
  summary: {
    documents: Figure;
    searches7d: Figure;
    inFlight: Figure;
    /** Admin only; `undefined` hides it. */
    activeUsers24h?: Figure;
  };
  fileStatsAvailable: boolean;
  documents: ReturnType<typeof transformDocumentToTableRow>[];
  pagination: PaginationInfo | null;
  searches: CombinedSearchQueryRow[] | null;
  searchTypes: Array<{ name: string; value: number }> | null;
  fileTypeDistribution: ReturnType<typeof transformFileTypeForChart>;
  processingStats: ReturnType<typeof transformProcessingStats>;
  searchVolume: DailyPoint[] | null;
  /** Admin only; `undefined` for other roles. */
  activeUserTrend?: DailyPoint[] | null;
}

const UNAVAILABLE = '—';

function ChartNotice({ title, message }: { title: string; message: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <div
          role="status"
          className="flex h-[300px] items-center justify-center text-center text-sm text-muted-foreground"
        >
          {message}
        </div>
      </CardContent>
    </Card>
  );
}

export interface AnalyticsDashboardResult {
  data: DashboardData;
  /** Names of the sources that failed to load, shown in a note. */
  failed: string[];
}

/**
 * Loads every analytics source in parallel. A failed source becomes `null` and
 * is named in `failed`; it is never replaced by zeros (GOO-649). Organization
 * trends are admin-only on the backend (403 otherwise), so other roles never
 * request them.
 */
export async function fetchAnalyticsDashboard(
  isAdmin: boolean
): Promise<AnalyticsDashboardResult> {
  const failed: string[] = [];
  const settle = <T,>(label: string, request: Promise<T>) =>
    request.catch((): null => {
      failed.push(label);
      return null;
    });

  const [
    fileStats,
    documentsResponse,
    searchAnalytics,
    overview,
    searchVolume,
    activeUserTrend,
  ] = await Promise.all([
    settle('Documents', documentAnalyticsApi.getFileStats()),
    settle(
      'Document list',
      documentAnalyticsApi.getDocuments({ page: 1, size: 25 })
    ),
    settle('Search', searchAnalyticsApi.getCombinedSearchAnalytics()),
    settle('Overview', performanceApi.getDashboardOverview()),
    settle('Search volume', performanceApi.getSearchVolumeTrend()),
    isAdmin
      ? settle(
          'Active users trend',
          userBehaviorApi.getOrganizationActiveUserTrend(30)
        )
      : Promise.resolve(undefined),
  ]);

  const documentsIndexed = fileStats
    ? fileStats.files_by_type.reduce((sum, item) => sum + item.count, 0)
    : null;
  // Admin-only figures: `undefined` hides them for other roles.
  const adminFigure = (value: number | undefined): Figure | undefined =>
    isAdmin ? (value ?? null) : undefined;

  return {
    data: {
      overview: {
        documentsIndexed,
        searches24h: overview ? overview.totalSearches : null,
        avgSearchTimeMs24h: overview ? overview.avgSearchTimeMs : null,
        activeUsers24h: adminFigure(overview?.activeUsers),
        searchSessions24h: adminFigure(overview?.searchSessions),
      },
      summary: {
        documents: documentsIndexed,
        searches7d: searchAnalytics ? searchAnalytics.totalSearches : null,
        inFlight: fileStats
          ? countInFlightDocuments(fileStats.processing_stats)
          : null,
        activeUsers24h: adminFigure(overview?.activeUsers),
      },
      fileStatsAvailable: fileStats !== null,
      documents: documentsResponse
        ? documentsResponse.documents.map(transformDocumentToTableRow)
        : [],
      pagination: documentsResponse ? documentsResponse.pagination : null,
      searches: searchAnalytics ? searchAnalytics.topQueries : null,
      searchTypes: searchAnalytics ? searchAnalytics.searchTypes : null,
      fileTypeDistribution: transformFileTypeForChart(
        fileStats?.files_by_type ?? []
      ),
      processingStats: transformProcessingStats(
        fileStats?.processing_stats ?? []
      ),
      searchVolume,
      activeUserTrend,
    },
    failed: Array.from(new Set(failed)),
  };
}

export function AnalyticsDashboard({ className }: AnalyticsDashboardProps) {
  const { user, isLoading: authLoading } = useAuth();
  const isAdmin = user?.role === 'admin';
  const [activeTab, setActiveTab] = useState('overview');

  // Keyed on the role so an admin whose session resolves after mount still
  // gets the admin-only trend.
  const query = useQuery({
    queryKey: ['analytics-dashboard', isAdmin],
    queryFn: () => fetchAnalyticsDashboard(isAdmin),
    enabled: !authLoading,
  });
  const data = query.data?.data ?? null;
  const partialFailures = query.data?.failed ?? [];
  const loading = query.isFetching;
  const error = query.isError
    ? 'Failed to load analytics data. Please try again.'
    : null;

  const handleRefresh = () => {
    void query.refetch();
  };

  // Serialize the currently loaded document rows to CSV and trigger a download.
  // This exports exactly the rows on screen, not an invented full-corpus export.
  const handleExport = () => {
    const rows = data?.documents ?? [];
    if (rows.length === 0) return;

    const headers = ['Filename', 'Type', 'Size (bytes)', 'Uploaded', 'Status'];
    const escape = (value: unknown) => {
      const str = String(value ?? '');
      return /[",\n]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
    };
    const lines = [
      headers.join(','),
      ...rows.map((row) =>
        [row.filename, row.type, row.size, row.uploadedAt, row.status]
          .map(escape)
          .join(',')
      ),
    ];

    const blob = new Blob([lines.join('\n')], {
      type: 'text/csv;charset=utf-8;',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `analytics-documents-${new Date()
      .toISOString()
      .slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const handleMetricClick = (metric: string) => {
    // Surface the selected metric's tab so the click resolves to something real
    // rather than a no-op. Document and search metrics map to their own tabs.
    const tabForMetric: Record<string, string> = {
      documents: 'documents',
      searches: 'search',
      activeUsers: 'activity',
      sessions: 'activity',
    };
    setActiveTab(tabForMetric[metric] ?? 'overview');
  };

  const documentTableConfig = useMemo(() => createDocumentAnalyticsTable(), []);
  const searchTableConfig = useMemo(() => createSearchAnalyticsTable(), []);

  // Summary stats shown in the top row. Consistent neutral cards with a single
  // warm-gold accent on the icon, matching the dashboard vocabulary.
  const summaryStats: Array<{
    label: string;
    value: Figure | undefined;
    icon: typeof Users;
  }> = [
    { label: 'Documents', value: data?.summary.documents, icon: FileText },
    {
      label: 'Searches, last 7 days',
      value: data?.summary.searches7d,
      icon: Search,
    },
    { label: 'In processing', value: data?.summary.inFlight, icon: Clock },
    {
      label: 'Active users, last 24 hours',
      value: data?.summary.activeUsers24h,
      icon: Users,
    },
  ].filter((stat) => stat.value !== undefined);

  if (!data && !error) {
    return (
      <div className={cn('w-full space-y-6', className)}>
        {/* Header skeleton */}
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <div className="h-7 w-40 rounded-md bg-muted animate-pulse" />
            <div className="h-4 w-72 rounded-md bg-muted/60 animate-pulse" />
          </div>
          <div className="h-9 w-44 rounded-md bg-muted animate-pulse" />
        </div>

        {/* Stat card skeletons */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className="rounded-xl border border-border bg-card p-5 shadow-xs"
            >
              <div className="h-9 w-9 rounded-lg bg-muted animate-pulse" />
              <div className="mt-4 h-7 w-16 rounded-md bg-muted animate-pulse" />
              <div className="mt-2 h-3 w-20 rounded-md bg-muted/60 animate-pulse" />
            </div>
          ))}
        </div>

        {/* Body skeleton */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="rounded-xl border border-border bg-card p-6 shadow-xs"
            >
              <div className="h-4 w-32 rounded-md bg-muted animate-pulse" />
              <div className="mt-4 h-[260px] rounded-lg bg-muted/40 animate-pulse" />
            </div>
          ))}
        </div>
        <span className="sr-only" role="status">
          Loading analytics
        </span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className={cn('w-full', className)}>
        <div
          role="alert"
          className="mx-auto flex max-w-md flex-col items-center gap-4 rounded-xl border border-border bg-card p-8 text-center shadow-xs"
        >
          <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-(--nous-mars)/10">
            <AlertTriangle
              aria-hidden="true"
              className="h-6 w-6 text-(--nous-mars)"
            />
          </div>
          <div className="space-y-1">
            <p className="text-base font-semibold text-foreground">
              Couldn&apos;t load analytics
            </p>
            <p className="text-sm text-muted-foreground">{error}</p>
          </div>
          <Button variant="outline" size="sm" onClick={handleRefresh}>
            <RefreshCw aria-hidden="true" className="mr-2 h-4 w-4" />
            Try again
          </Button>
        </div>
      </div>
    );
  }

  return (
    <MotionConfig reducedMotion="user">
      <div className={cn('w-full space-y-6', className)}>
        {/* Header */}
        <motion.div
          initial={{ opacity: 0, y: -8 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between"
        >
          <div className="flex items-center gap-4">
            <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-primary/10">
              <BarChart3 aria-hidden="true" className="h-6 w-6 text-primary" />
            </div>
            <div>
              <h1 className="text-xl font-semibold tracking-tight text-foreground">
                Analytics
              </h1>
              <p className="mt-0.5 text-sm text-muted-foreground">
                Pipeline throughput and activity across your knowledge base
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleRefresh}
              disabled={loading}
              className="min-h-[44px] sm:min-h-0"
            >
              <RefreshCw
                aria-hidden="true"
                className={cn('mr-2 h-4 w-4', loading && 'animate-spin')}
              />
              Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleExport}
              disabled={loading || (data.documents?.length ?? 0) === 0}
              className="min-h-[44px] sm:min-h-0"
            >
              <Download aria-hidden="true" className="mr-2 h-4 w-4" />
              Export CSV
            </Button>
          </div>
        </motion.div>

        {/* Quick stats */}
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
          className="grid grid-cols-2 gap-4 md:grid-cols-4"
        >
          {summaryStats.map((stat) => (
            <div
              key={stat.label}
              className="rounded-xl border border-border bg-card p-5 shadow-xs"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-muted text-primary">
                <stat.icon aria-hidden="true" className="h-4 w-4" />
              </div>
              <p className="mt-4 text-2xl font-semibold tabular-nums text-foreground">
                {stat.value == null ? UNAVAILABLE : stat.value.toLocaleString()}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {stat.label}
                {stat.value == null && (
                  <span className="block">Unavailable right now</span>
                )}
              </p>
            </div>
          ))}
        </motion.div>

        {/* Partial failure note. Non-blocking: the rest of the page still
            renders, but we don't pretend the missing numbers are real zeros. */}
        {partialFailures.length > 0 && (
          <div
            role="status"
            className="flex items-start gap-3 rounded-xl border border-(--nous-corona)/30 bg-(--nous-corona)/10 p-4"
          >
            <Info
              aria-hidden="true"
              className="mt-0.5 h-4 w-4 shrink-0 text-(--nous-corona)"
            />
            <p className="text-sm text-foreground">
              Some data couldn&apos;t be loaded ({partialFailures.join(', ')}).
              Those sections may be incomplete. Try refreshing to load them
              again.
            </p>
          </div>
        )}

        {/* Main Content Tabs */}
        <Tabs
          value={activeTab}
          onValueChange={setActiveTab}
          className="space-y-6"
        >
          <TabsList className="grid w-full grid-cols-2 sm:grid-cols-4">
            <TabsTrigger
              value="overview"
              className="flex min-h-[44px] items-center gap-2 sm:min-h-0"
            >
              <BarChart3 aria-hidden="true" className="h-4 w-4 shrink-0" />
              Overview
            </TabsTrigger>
            <TabsTrigger
              value="documents"
              className="flex min-h-[44px] items-center gap-2 sm:min-h-0"
            >
              <FileText aria-hidden="true" className="h-4 w-4 shrink-0" />
              Documents
            </TabsTrigger>
            <TabsTrigger
              value="search"
              className="flex min-h-[44px] items-center gap-2 sm:min-h-0"
            >
              <Search aria-hidden="true" className="h-4 w-4 shrink-0" />
              Search
            </TabsTrigger>
            <TabsTrigger
              value="activity"
              className="flex min-h-[44px] items-center gap-2 sm:min-h-0"
            >
              <Activity aria-hidden="true" className="h-4 w-4 shrink-0" />
              Activity
            </TabsTrigger>
          </TabsList>

          {/* Overview Tab */}
          <TabsContent value="overview" className="space-y-6">
            <AnalyticsOverview
              data={data.overview}
              loading={loading}
              onRefresh={handleRefresh}
              onExport={handleExport}
              onMetricClick={handleMetricClick}
            />

            {/* Charts Grid. Each chart reads a real daily series; a failed or
                admin-only source says so instead of drawing zeros. */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {data.searchVolume === null ? (
                <ChartNotice
                  title="Search volume"
                  message="Search volume is unavailable right now."
                />
              ) : data.searchVolume.length === 0 ? (
                <ChartNotice
                  title="Search volume"
                  message="No searches in the last 30 days."
                />
              ) : (
                <AnalyticsChart
                  title="Search volume"
                  description="Searches per day over the last 30 days"
                  data={data.searchVolume}
                  labels={{ value: 'Searches' }}
                  height={300}
                />
              )}

              {data.activeUserTrend === undefined ? (
                <ChartNotice
                  title="Active users"
                  message="Active-user trends are available to organization admins."
                />
              ) : data.activeUserTrend === null ? (
                <ChartNotice
                  title="Active users"
                  message="Active-user trends are unavailable right now."
                />
              ) : data.activeUserTrend.length === 0 ? (
                <ChartNotice
                  title="Active users"
                  message="No user activity in the last 30 days."
                />
              ) : (
                <AnalyticsChart
                  title="Active users"
                  description="Users active per day over the last 30 days"
                  data={data.activeUserTrend}
                  type="bar"
                  labels={{ value: 'Active users' }}
                  height={300}
                />
              )}
            </div>
          </TabsContent>

          {/* Documents Tab */}
          <TabsContent value="documents" className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <div className="lg:col-span-2">
                <AnalyticsTable
                  title="Document analytics"
                  description="Your most recent document uploads and their processing status"
                  data={data.documents}
                  columns={documentTableConfig.columns}
                  loading={loading}
                />
              </div>

              <div className="space-y-6">
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base font-medium">
                      File type distribution
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    {data.fileTypeDistribution.length > 0 ? (
                      <AnalyticsChart
                        title="File types"
                        data={data.fileTypeDistribution}
                        type="pie"
                        height={250}
                        showLegend={true}
                        showGrid={false}
                      />
                    ) : (
                      <div className="flex h-[250px] items-center justify-center text-center text-muted-foreground">
                        <p className="text-sm">
                          {data.fileStatsAvailable
                            ? 'No documents indexed yet. Upload a document to see the breakdown here.'
                            : 'File statistics are unavailable right now.'}
                        </p>
                      </div>
                    )}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className="text-base font-medium">
                      Processing status
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    {data.processingStats.length > 0 ? (
                      data.processingStats.map((item) => (
                        <div
                          key={item.status}
                          className="flex items-center justify-between"
                        >
                          <div className="flex items-center gap-2">
                            <item.icon
                              aria-hidden="true"
                              className={cn('h-4 w-4', item.color)}
                            />
                            <span className="text-sm font-medium text-foreground">
                              {item.status}
                            </span>
                          </div>
                          <Badge variant="secondary">{item.count}</Badge>
                        </div>
                      ))
                    ) : (
                      <div className="flex items-center justify-center py-4 text-muted-foreground">
                        <p className="text-sm">
                          {data.fileStatsAvailable
                            ? 'No processing data yet.'
                            : 'Processing status is unavailable right now.'}
                        </p>
                      </div>
                    )}
                  </CardContent>
                </Card>
              </div>
            </div>
          </TabsContent>

          {/* Search Tab */}
          <TabsContent value="search" className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <div className="lg:col-span-2">
                {data.searches === null ? (
                  <ChartNotice
                    title="Search analytics"
                    message="Search analytics are unavailable right now."
                  />
                ) : (
                  <AnalyticsTable
                    title="Search analytics"
                    description="Your most frequent search queries over the last 7 days"
                    data={data.searches}
                    columns={searchTableConfig.columns}
                    loading={loading}
                  />
                )}
              </div>

              <div className="space-y-6">
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base font-medium">
                      Search types
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    {data.searchTypes && data.searchTypes.length > 0 ? (
                      <AnalyticsChart
                        title="Search types"
                        data={data.searchTypes}
                        type="donut"
                        height={250}
                        showLegend={true}
                        showGrid={false}
                      />
                    ) : (
                      <div className="flex h-[250px] items-center justify-center text-center text-muted-foreground">
                        <p className="text-sm">
                          {data.searchTypes === null
                            ? 'Search types are unavailable right now.'
                            : 'No search data yet. Run a few searches to see how they break down.'}
                        </p>
                      </div>
                    )}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2 text-base font-medium">
                      <TrendingUp
                        aria-hidden="true"
                        className="h-4 w-4 text-primary"
                      />
                      Search quality
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm leading-relaxed text-muted-foreground">
                      Result relevance and click-through metrics appear here
                      once enough queries have run to measure them reliably.
                    </p>
                  </CardContent>
                </Card>
              </div>
            </div>
          </TabsContent>

          {/* Activity Tab. Only figures the backend reports: there is no
              source for live searches or request rates, so none are shown. */}
          <TabsContent value="activity" className="space-y-6">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
              {(
                [
                  {
                    label: 'Documents in processing',
                    value: data.summary.inFlight,
                    icon: FileText,
                  },
                  {
                    label: 'Active users, last 24 hours',
                    value: data.overview.activeUsers24h,
                    icon: Users,
                  },
                  {
                    label: 'Search sessions, last 24 hours',
                    value: data.overview.searchSessions24h,
                    icon: Search,
                  },
                ] as Array<{
                  label: string;
                  value: Figure | undefined;
                  icon: typeof Users;
                }>
              )
                .filter((metric) => metric.value !== undefined)
                .map((metric) => (
                  <Card key={metric.label}>
                    <CardContent className="p-5">
                      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-muted text-primary">
                        <metric.icon aria-hidden="true" className="h-4 w-4" />
                      </div>
                      <p className="mt-4 text-2xl font-semibold tabular-nums text-foreground">
                        {metric.value == null
                          ? UNAVAILABLE
                          : metric.value.toLocaleString()}
                      </p>
                      <p className="mt-1 text-sm text-muted-foreground">
                        {metric.label}
                        {metric.value == null && (
                          <span className="block text-xs">
                            Unavailable right now
                          </span>
                        )}
                      </p>
                    </CardContent>
                  </Card>
                ))}
            </div>

            <Card>
              <CardHeader>
                <CardTitle className="text-base font-medium">
                  Activity feed
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ScrollArea className="h-[400px] pr-4">
                  <div className="flex h-[360px] flex-col items-center justify-center gap-2 text-center">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                      <Activity aria-hidden="true" className="h-5 w-5" />
                    </div>
                    <p className="text-sm font-medium text-foreground">
                      Activity feed not available yet
                    </p>
                    <p className="max-w-xs text-sm text-muted-foreground">
                      There is no activity source to read from yet, so no
                      events are listed here.
                    </p>
                  </div>
                </ScrollArea>
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </MotionConfig>
  );
}

export default AnalyticsDashboard;
