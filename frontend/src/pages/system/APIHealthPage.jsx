/**
 * Strike Desk V4.5 — API Health Page
 */
import React, { useState, useCallback, useEffect } from 'react';
import { system as sysApi, brokers as brokerApi, risk as riskApi, execution as execApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, KV, LoadingBlock, PageHeader, ActionBtn,
} from '../../components/ui';

const SERVICES = [
  { key: 'backend',    label: 'Backend API',      endpoint: () => sysApi.health() },
  { key: 'risk',       label: 'Risk Engine',       endpoint: () => riskApi.state() },
  { key: 'exec',       label: 'Execution Engine',  endpoint: () => execApi.mode() },
  { key: 'dhan',       label: 'Dhan Broker',       endpoint: () => brokerApi.tokensStatus() },
  { key: 'brokers',    label: 'Broker API',        endpoint: () => brokerApi.all() },
];

export default function APIHealthPage() {
  const [results, setResults] = useState({});
  const [checking, setChecking] = useState(false);

  const runChecks = useCallback(async () => {
    setChecking(true);
    const entries = await Promise.allSettled(
      SERVICES.map(async svc => {
        const start = Date.now();
        try {
          await svc.endpoint();
          return { key: svc.key, status: 'HEALTHY', latencyMs: Date.now() - start };
        } catch(e) {
          return { key: svc.key, status: e.response?.status >= 500 ? 'DEGRADED' : 'OFFLINE', latencyMs: Date.now() - start, error: e.response?.data?.detail || e.message };
        }
      })
    );
    const r = {};
    entries.forEach(e => { if (e.status === 'fulfilled') r[e.value.key] = e.value; });
    setResults(r);
    setChecking(false);
  }, []);

  useEffect(() => { runChecks(); const t = setInterval(runChecks, 30000); return () => clearInterval(t); }, [runChecks]);

  const statusColor = { HEALTHY: 'green', DEGRADED: 'amber', OFFLINE: 'red' };

  return (
    <div>
      <PageHeader
        title="API Health"
        subtitle="Backend service connectivity. Never faked."
        right={<ActionBtn onClick={runChecks} disabled={checking}>{checking ? 'Checking…' : 'Check All'}</ActionBtn>}
      />
      <Panel>
        <PanelHeader title="SERVICE STATUS" />
        <div className="divide-y divide-[#1E2A3A]">
          {SERVICES.map(svc => {
            const r = results[svc.key];
            return (
              <div key={svc.key} className="flex items-center gap-4 px-4 py-3">
                <span className="text-[13px] text-[#F5F7FA] flex-1">{svc.label}</span>
                {checking && !r ? (
                  <span className="text-[11px] font-mono text-[#4A5568] animate-pulse">checking…</span>
                ) : r ? (
                  <>
                    <StatusBadge text={r.status} color={statusColor[r.status]} />
                    <span className="text-[11px] font-mono text-[#4A5568] min-w-[60px] text-right">{r.latencyMs}ms</span>
                    {r.error && <span className="text-[10px] font-mono text-red-400/70 max-w-[200px] truncate">{r.error}</span>}
                  </>
                ) : (
                  <StatusBadge text="UNKNOWN" color="gray" />
                )}
              </div>
            );
          })}
        </div>
      </Panel>
    </div>
  );
}
