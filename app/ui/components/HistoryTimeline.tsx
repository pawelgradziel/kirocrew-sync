import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, Btn, Badge, EmptyState } from '@kirocrew/app-sdk/ui';
import lucideIcons from 'lucide-react';
import { CodePill, ErrorBlock, LoadingBlock } from './shared';
import { formatRelativeTime, formatDateTime } from '../lib/time';
import { API_BASE } from '../lib/api';

const { CheckCircle, XCircle, AlertTriangle, Clock, ChevronDown, ChevronRight, History } = lucideIcons;

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
      const response = await fetch(`${API_BASE}/history/${runId}`);
      if (!response.ok) throw new Error('Failed to fetch run details');
      return response.json();
    },
  });

  if (isLoading) return <LoadingBlock label="Loading changes…" />;
  if (isError) return <ErrorBlock label="Failed to load changes for this run" onRetry={() => refetch()} />;

  const changes = data?.changes || [];
  if (changes.length === 0) {
    return <p className="text-sm text-muted py-2">No detailed changes recorded for this run</p>;
  }

  return (
    <div className="space-y-2 py-2">
      {changes.map((change, index) => (
        <div
          key={index}
          className="flex items-start gap-2 text-sm border-b border-border last:border-0 pb-2 last:pb-0"
        >
          <Badge variant="muted">{change.change_type}</Badge>
          <Badge variant="muted">{change.action}</Badge>
          {change.item_id && <CodePill>{change.item_id}</CodePill>}
          {change.details && <span className="text-muted">{change.details}</span>}
        </div>
      ))}
    </div>
  );
}

function HistoryRunCard({ run }: { run: SyncRun }) {
  const [expanded, setExpanded] = useState(false);

  const Icon = run.exit_code === 0 ? CheckCircle : run.exit_code === 3 ? AlertTriangle : XCircle;
  const iconColor = run.exit_code === 0 ? 'text-ok' : run.exit_code === 3 ? 'text-warn' : 'text-danger';

  return (
    <Card>
      <button
        type="button"
        className="flex items-start justify-between w-full text-left bg-transparent border-none p-0 cursor-pointer"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
      >
        <div className="flex items-center gap-2">
          {expanded ? (
            <ChevronDown size={16} className="shrink-0 text-muted" />
          ) : (
            <ChevronRight size={16} className="shrink-0 text-muted" />
          )}
          <Icon size={18} className={iconColor} />
          <div>
            <p className="text-sm font-medium text-text">{formatDateTime(run.timestamp)}</p>
            <p className="text-xs text-muted">{formatRelativeTime(run.timestamp)}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="muted">{run.scope}</Badge>
          <Badge variant="muted">
            <Clock size={12} />
            {run.duration_ms == null ? '—' : `${(run.duration_ms / 1000).toFixed(1)}s`}
          </Badge>
        </div>
      </button>

      {run.changes_detected && (
        <div className="flex flex-wrap gap-4 text-sm mt-3">
          {run.rows_merged > 0 && (
            <span className="text-muted">
              {run.rows_merged} row{run.rows_merged > 1 ? 's' : ''} merged
            </span>
          )}
          {run.conflicts_count > 0 && (
            <span className="text-warn">
              {run.conflicts_count} conflict{run.conflicts_count > 1 ? 's' : ''}
            </span>
          )}
          {run.quarantine_count > 0 && (
            <span className="text-danger">
              {run.quarantine_count} machine{run.quarantine_count > 1 ? 's' : ''} quarantined
            </span>
          )}
        </div>
      )}

      {expanded && (
        <div className="mt-3 pt-3 border-t border-border">
          <RunChanges runId={run.id} />
        </div>
      )}
    </Card>
  );
}

export function HistoryTimeline() {
  const { data, isLoading, isError, refetch } = useQuery<{ runs: SyncRun[]; total: number }>({
    queryKey: ['sync-history'],
    queryFn: async () => {
      const response = await fetch(`${API_BASE}/history?limit=50`);
      if (!response.ok) throw new Error('Failed to fetch history');
      return response.json();
    },
  });

  if (isLoading) {
    return (
      <Card>
        <LoadingBlock label="Loading history…" />
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <ErrorBlock label="Failed to load sync history" onRetry={() => refetch()} />
      </Card>
    );
  }

  const runs = data?.runs || [];

  if (runs.length === 0) {
    return (
      <Card>
        <EmptyState icon={<History size={40} />} title="No sync history yet" />
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {runs.map((run) => (
        <HistoryRunCard key={run.id} run={run} />
      ))}

      {data && data.total > 50 && (
        <p className="text-sm text-muted text-center">Showing 50 of {data.total} syncs</p>
      )}
    </div>
  );
}
