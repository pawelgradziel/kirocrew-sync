import { Fragment, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardTitle, Btn, Badge, EmptyState } from '@kirocrew/app-sdk/ui';
import lucideIcons from 'lucide-react';
import { CodePill, ErrorBlock, LoadingBlock, Message } from './shared';
import { apiFetchJson } from '../lib/api';

const { ChevronDown, ChevronRight, GitCompare } = lucideIcons;

interface Conflict {
  id: number;
  run_id: number;
  machine: string;
  table_name: string;
  row_id: string;
  local_value: string | null;
  remote_value: string | null;
  resolved: boolean;
}

function DiffValue({ value, emptyLabel }: { value: string | null; emptyLabel: string }) {
  if (value === null) {
    return <p className="text-xs italic text-muted">{emptyLabel}</p>;
  }
  return (
    <pre className="text-xs bg-bg-elevated rounded-md p-2 whitespace-pre-wrap break-words">{value}</pre>
  );
}

export function ConflictPanel() {
  const queryClient = useQueryClient();
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const [resolveError, setResolveError] = useState<string | null>(null);

  const { data, isLoading, isError, error, refetch } = useQuery<{ conflicts: Conflict[]; total: number }>({
    queryKey: ['conflicts'],
    queryFn: () => apiFetchJson('/conflicts'),
  });

  const resolveConflict = useMutation({
    mutationFn: ({ id, resolution }: { id: number; resolution: string }) =>
      apiFetchJson(`/conflicts/${id}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution }),
      }),
    onSuccess: () => {
      setResolveError(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ['sync-status'] });
    },
    onError: (err: Error) => setResolveError(err.message),
  });

  const toggleExpanded = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  if (isLoading) {
    return (
      <Card>
        <LoadingBlock label="Loading conflicts…" />
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <ErrorBlock label="Failed to load conflicts" detail={error?.message} onRetry={() => refetch()} />
      </Card>
    );
  }

  const conflicts = data?.conflicts || [];

  if (conflicts.length === 0) {
    return (
      <Card>
        <EmptyState icon={<GitCompare size={40} />} title="No unresolved conflicts" />
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex items-center justify-between mb-4">
        <CardTitle className="mb-0">Unresolved Conflicts</CardTitle>
        <Badge variant="err">{conflicts.length}</Badge>
      </div>

      {resolveError && (
        <div className="mb-4">
          <Message tone="error">{resolveError}</Message>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left">
              <th className="w-8 py-2" />
              <th className="py-2 text-xs font-medium text-muted">Machine</th>
              <th className="py-2 text-xs font-medium text-muted">Table</th>
              <th className="py-2 text-xs font-medium text-muted">Row ID</th>
              <th className="py-2 text-xs font-medium text-muted text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {conflicts.map((conflict) => {
              const isExpanded = expandedIds.has(conflict.id);

              return (
                <Fragment key={conflict.id}>
                  <tr className="border-b border-border last:border-0">
                    <td className="py-2">
                      <button
                        type="button"
                        aria-label={isExpanded ? 'Hide diff' : 'View diff'}
                        onClick={() => toggleExpanded(conflict.id)}
                        className="p-[4px] rounded bg-transparent border-none text-muted hover:text-text cursor-pointer"
                      >
                        {isExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                      </button>
                    </td>
                    <td className="py-2 font-medium text-text">{conflict.machine}</td>
                    <td className="py-2">
                      <CodePill>{conflict.table_name}</CodePill>
                    </td>
                    <td className="py-2">
                      <CodePill>{conflict.row_id}</CodePill>
                    </td>
                    <td className="py-2 text-right">
                      <div className="flex gap-2 justify-end flex-wrap">
                        <Btn onClick={() => toggleExpanded(conflict.id)}>View Diff</Btn>
                        <Btn
                          onClick={() => resolveConflict.mutate({ id: conflict.id, resolution: 'local-wins' })}
                          disabled={resolveConflict.isPending}
                        >
                          Keep Local
                        </Btn>
                        <Btn
                          onClick={() => resolveConflict.mutate({ id: conflict.id, resolution: 'remote-wins' })}
                          disabled={resolveConflict.isPending}
                        >
                          Keep Remote
                        </Btn>
                      </div>
                    </td>
                  </tr>

                  {isExpanded && (
                    <tr className="border-b border-border last:border-0">
                      <td colSpan={5} className="bg-bg-elevated/30 py-2">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                          <div>
                            <p className="text-xs font-medium text-muted mb-1">Local value</p>
                            <DiffValue value={conflict.local_value} emptyLabel="No local value" />
                          </div>
                          <div>
                            <p className="text-xs font-medium text-muted mb-1">Remote value</p>
                            <DiffValue value={conflict.remote_value} emptyLabel="No remote value" />
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
