import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { CheckCircle, XCircle, AlertTriangle, Clock, ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import { formatDistanceToNow, format } from 'date-fns';

interface SyncRun {
  id: number;
  timestamp: string;
  exit_code: number;
  duration_ms: number | null;
  scope: 'personal' | 'team';
  strategy: string | null;
  changes_detected: boolean;
  rows_merged: number;
  conflicts_count: number;
  quarantine_count: number;
}

interface SyncChange {
  change_type: 'knowledge' | 'artifact' | 'lesson' | 'transcript' | 'config';
  action: 'added' | 'updated' | 'deleted' | 'conflict';
  item_id: string | null;
  details: string | null;
}

interface SyncRunDetails extends SyncRun {
  changes: SyncChange[];
}

function RunChanges({ runId }: { runId: number }) {
  const { data, isLoading, isError, refetch } = useQuery<SyncRunDetails>({
    queryKey: ['sync-history-detail', runId],
    queryFn: async () => {
      const response = await fetch(`/api/apps/kirocrew-sync/history/${runId}`);
      if (!response.ok) throw new Error('Failed to fetch run details');
      return response.json();
    },
  });

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground py-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        Loading changes...
      </div>
    );
  }

  if (isError) {
    return (
      <div className="py-2 space-y-2">
        <p className="text-sm text-muted-foreground">Failed to load changes for this run</p>
        <Button size="sm" variant="outline" onClick={() => refetch()}>
          Retry
        </Button>
      </div>
    );
  }

  const changes = data?.changes || [];

  if (changes.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-2">No detailed changes recorded for this run</p>
    );
  }

  return (
    <div className="space-y-2 py-2">
      {changes.map((change, index) => (
        <div
          key={index}
          className="flex items-start gap-2 text-sm border-b border-border last:border-0 pb-2 last:pb-0"
        >
          <Badge variant="outline">{change.change_type}</Badge>
          <Badge variant="outline">{change.action}</Badge>
          {change.item_id && (
            <code className="text-xs bg-muted px-1 py-0.5 rounded">{change.item_id}</code>
          )}
          {change.details && <span className="text-muted-foreground">{change.details}</span>}
        </div>
      ))}
    </div>
  );
}

function HistoryRunCard({ run }: { run: SyncRun }) {
  const [expanded, setExpanded] = useState(false);

  const Icon = run.exit_code === 0 ? CheckCircle : run.exit_code === 3 ? AlertTriangle : XCircle;
  const iconColor =
    run.exit_code === 0
      ? 'text-green-500'
      : run.exit_code === 3
      ? 'text-yellow-500'
      : 'text-red-500';

  return (
    <Card>
      <CardHeader className="pb-3">
        <button
          type="button"
          className="flex items-start justify-between w-full text-left"
          onClick={() => setExpanded((prev) => !prev)}
          aria-expanded={expanded}
        >
          <div className="flex items-center gap-2">
            {expanded ? (
              <ChevronDown className="w-4 h-4 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="w-4 h-4 shrink-0 text-muted-foreground" />
            )}
            <Icon className={`w-5 h-5 ${iconColor}`} />
            <div>
              <p className="text-sm font-medium">
                {format(new Date(run.timestamp), 'MMM d, yyyy HH:mm:ss')}
              </p>
              <p className="text-xs text-muted-foreground">
                {formatDistanceToNow(new Date(run.timestamp), { addSuffix: true })}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="outline">{run.scope}</Badge>
            <Badge variant="outline">
              <Clock className="w-3 h-3 mr-1" />
              {run.duration_ms == null ? '—' : `${(run.duration_ms / 1000).toFixed(1)}s`}
            </Badge>
          </div>
        </button>
      </CardHeader>

      {run.changes_detected && (
        <CardContent className="pt-0">
          <div className="flex flex-wrap gap-4 text-sm">
            {run.rows_merged > 0 && (
              <span className="text-muted-foreground">
                {run.rows_merged} row{run.rows_merged > 1 ? 's' : ''} merged
              </span>
            )}
            {run.conflicts_count > 0 && (
              <span className="text-yellow-600 dark:text-yellow-400">
                {run.conflicts_count} conflict{run.conflicts_count > 1 ? 's' : ''}
              </span>
            )}
            {run.quarantine_count > 0 && (
              <span className="text-orange-600 dark:text-orange-400">
                {run.quarantine_count} machine{run.quarantine_count > 1 ? 's' : ''} quarantined
              </span>
            )}
          </div>
        </CardContent>
      )}

      {expanded && (
        <CardContent className="pt-0 border-t border-border">
          <RunChanges runId={run.id} />
        </CardContent>
      )}
    </Card>
  );
}

export function HistoryTimeline() {
  const { data, isLoading, isError, refetch } = useQuery<{ runs: SyncRun[]; total: number }>({
    queryKey: ['sync-history'],
    queryFn: async () => {
      const response = await fetch('/api/apps/kirocrew-sync/history?limit=50');
      if (!response.ok) throw new Error('Failed to fetch history');
      return response.json();
    },
  });

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">Loading history...</p>
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="py-8 text-center space-y-3">
          <p className="text-sm text-muted-foreground">Failed to load sync history</p>
          <Button size="sm" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const runs = data?.runs || [];

  if (runs.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">No sync history yet</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {runs.map((run) => (
        <HistoryRunCard key={run.id} run={run} />
      ))}

      {data && data.total > 50 && (
        <p className="text-sm text-muted-foreground text-center">
          Showing 50 of {data.total} syncs
        </p>
      )}
    </div>
  );
}
