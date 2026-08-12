import type { ReactNode } from 'react';
import { CheckCircle, XCircle, Loader2 } from 'lucide-react';
import { Btn } from '@kirocrew/ui';

export type MessageTone = 'success' | 'error';

/**
 * Inline success/error banner. Every panel below surfaces mutation results
 * (save, resolve, dismiss, switch...) the same way, so this is factored out
 * once rather than re-styled per component.
 */
export function Message({ tone, children }: { tone: MessageTone; children: ReactNode }) {
  const Icon = tone === 'success' ? CheckCircle : XCircle;
  const cls = tone === 'success' ? 'bg-ok-subtle text-ok' : 'bg-danger-subtle text-danger';
  return (
    <div className={`${cls} rounded-md p-2 flex items-center gap-1.5 text-sm`}>
      <Icon size={14} className="shrink-0" />
      <span>{children}</span>
    </div>
  );
}

/** Loading placeholder for a panel/card body. */
export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted">
      <Loader2 size={16} className="animate-spin" />
      {label}
    </div>
  );
}

/**
 * Failed-to-load placeholder with a retry action. Kept visually and
 * semantically distinct from "loaded successfully, nothing here" empty
 * states — a fetch failure must never read as "there is nothing to see".
 */
export function ErrorBlock({ label, onRetry }: { label: string; onRetry: () => void }) {
  return (
    <div className="py-8 text-center space-y-3">
      <p className="text-sm text-muted">{label}</p>
      <Btn onClick={onRetry}>Retry</Btn>
    </div>
  );
}

/** Small code-style pill for ids, table names, etc. */
export function CodePill({ children }: { children: ReactNode }) {
  return (
    <code className="text-[11px] font-mono bg-bg-elevated px-1 py-0.5 rounded">{children}</code>
  );
}
