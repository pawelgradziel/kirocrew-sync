import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { ChevronDown, ChevronRight } from 'lucide-react';

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
    return <p className="text-xs italic text-muted-foreground">{emptyLabel}</p>;
  }
  return (
    <pre className="text-xs bg-muted rounded-md p-2 whitespace-pre-wrap break-words">{value}</pre>
  );
}

export function ConflictPanel() {
  const queryClient = useQueryClient();
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());

  const { data, isLoading, isError, refetch } = useQuery<{ conflicts: Conflict[]; total: number }>({
    queryKey: ['conflicts'],
    queryFn: async () => {
      const response = await fetch('/api/apps/kirocrew-sync/conflicts');
      if (!response.ok) throw new Error('Failed to fetch conflicts');
      return response.json();
    },
  });

  const resolveConflict = useMutation({
    mutationFn: async ({ id, resolution }: { id: number; resolution: string }) => {
      const response = await fetch(`/api/apps/kirocrew-sync/conflicts/${id}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution }),
      });
      if (!response.ok) throw new Error('Failed to resolve conflict');
      return response.json();
    },
    onSuccess: () => {
      refetch();
      queryClient.invalidateQueries({ queryKey: ['sync-status'] });
    },
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
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">Loading conflicts...</p>
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="py-8 text-center space-y-3">
          <p className="text-sm text-muted-foreground">Failed to load conflicts</p>
          <Button size="sm" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const conflicts = data?.conflicts || [];

  if (conflicts.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">No unresolved conflicts</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">Unresolved Conflicts</h3>
          <Badge variant="destructive">{conflicts.length}</Badge>
        </div>
      </CardHeader>

      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8" />
              <TableHead>Machine</TableHead>
              <TableHead>Table</TableHead>
              <TableHead>Row ID</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {conflicts.map((conflict) => {
              const isExpanded = expandedIds.has(conflict.id);

              return (
                <React.Fragment key={conflict.id}>
                  <TableRow>
                    <TableCell>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 w-6 p-0"
                        aria-label={isExpanded ? 'Hide diff' : 'View diff'}
                        onClick={() => toggleExpanded(conflict.id)}
                      >
                        {isExpanded ? (
                          <ChevronDown className="w-4 h-4" />
                        ) : (
                          <ChevronRight className="w-4 h-4" />
                        )}
                      </Button>
                    </TableCell>
                    <TableCell className="font-medium">{conflict.machine}</TableCell>
                    <TableCell>
                      <code className="text-xs bg-muted px-1 py-0.5 rounded">{conflict.table_name}</code>
                    </TableCell>
                    <TableCell>
                      <code className="text-xs bg-muted px-1 py-0.5 rounded">{conflict.row_id}</code>
                    </TableCell>
                    <TableCell className="text-right space-x-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => toggleExpanded(conflict.id)}
                      >
                        View Diff
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          resolveConflict.mutate({ id: conflict.id, resolution: 'local-wins' })
                        }
                        disabled={resolveConflict.isPending}
                      >
                        Keep Local
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          resolveConflict.mutate({ id: conflict.id, resolution: 'remote-wins' })
                        }
                        disabled={resolveConflict.isPending}
                      >
                        Keep Remote
                      </Button>
                    </TableCell>
                  </TableRow>

                  {isExpanded && (
                    <TableRow>
                      <TableCell colSpan={5} className="bg-muted/30">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 py-2">
                          <div>
                            <p className="text-xs font-medium text-muted-foreground mb-1">
                              Local value
                            </p>
                            <DiffValue value={conflict.local_value} emptyLabel="No local value" />
                          </div>
                          <div>
                            <p className="text-xs font-medium text-muted-foreground mb-1">
                              Remote value
                            </p>
                            <DiffValue value={conflict.remote_value} emptyLabel="No remote value" />
                          </div>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </React.Fragment>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
