import { useState } from 'react';
import { SegmentedControl, type Segment } from '@kirocrew/app-sdk/ui';
import lucideIcons from 'lucide-react';
import { StatusWidget } from './StatusWidget';
import { DaemonControl } from './DaemonControl';
import { HistoryTimeline } from './HistoryTimeline';
import { ConflictPanel } from './ConflictPanel';
import { QuarantinePanel } from './QuarantinePanel';
import { BackendConfig } from './BackendConfig';

const { History, GitCompare, ShieldAlert, Server } = lucideIcons;

type TabKey = 'history' | 'conflicts' | 'quarantine' | 'backend';

const TABS: Segment<TabKey>[] = [
  { key: 'history', label: 'History', icon: <History size={14} /> },
  { key: 'conflicts', label: 'Conflicts', icon: <GitCompare size={14} /> },
  { key: 'quarantine', label: 'Quarantine', icon: <ShieldAlert size={14} /> },
  { key: 'backend', label: 'Backend', icon: <Server size={14} /> },
];

export function SyncDashboard() {
  const [tab, setTab] = useState<TabKey>('history');

  return (
    <div className="px-6 pt-4 pb-8 space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <StatusWidget />
        <DaemonControl />
      </div>

      <div>
        <SegmentedControl segments={TABS} value={tab} onChange={setTab} layoutId="sync-dashboard-tab" />

        <div className="mt-6">
          {tab === 'history' && <HistoryTimeline />}
          {tab === 'conflicts' && <ConflictPanel />}
          {tab === 'quarantine' && <QuarantinePanel />}
          {tab === 'backend' && <BackendConfig />}
        </div>
      </div>
    </div>
  );
}

export default SyncDashboard;
