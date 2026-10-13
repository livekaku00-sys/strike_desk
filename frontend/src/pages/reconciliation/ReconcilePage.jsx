/**
 * Strike Desk V4.5 — Reconciliation Page
 * Internal vs broker position & order compare.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { risk as riskApi, execution as execApi } from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, StatusBadge, LoadingBlock, EmptyBlock, PageHeader, ActionBtn, ErrorBlock, BackendRequired,
} from '../../components/ui';

const POLL = 30000;

function DiffRow({ symbol, internal, broker, match }) {
  return (
    <div className={`grid grid-cols-[1fr_120px_120px_80px] gap-2 px-4 py-2.5 border-b border-[#1E2A3A]/40 items-center ${
      !match ? 'bg-red-950/15' : 'hover:bg-[#111821]/30'
    }`}>
      <span className="text-[12px] font-mono font-bold text-[#F5F7FA]">{symbol}</span>
      <span className="text-[11px] font-mono text-[#8D98A8]">{internal ?? '—'}</span>
      <span className="text-[11px] font-mono text-[#8D98A8]">{broker ?? '—'}</span>
      <StatusBadge text={match ? 'MATCH' : 'MISMATCH'} color={match ? 'green' : 'red'} />
    </div>
  );
}

export default function ReconcilePage() {
  const { role } = useAuthStore();
  const [recon, setRecon]         = useState(null);
  const [loading, setLoading]     = useState(true);
  const [err, setErr]             = useState(null);
  const [running, setRunning]     = useState(false);

  const fetch = useCallback(async () => {
    try {
      const r = await riskApi.reconcile();
      setRecon(r.data);
      setErr(null);
    } catch(e) {
      if (e.response?.status === 404 || e.response?.status === 501) {
        setRecon(null);
      } else {
        setErr(e.response?.data?.detail || e.message);
      }
    } finally { setLoading(false); }
  }, []);

  const runReconcile = useCallback(async () => {
    setRunning(true);
    try { await fetch(); }
    finally { setRunning(false); }
  }, [fetch]);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  const mismatches = recon?.mismatches ?? recon?.differences ?? [];
  const allMatch   = recon && mismatches.length === 0;

  return (
    <div>
      <PageHeader
        title="Reconciliation"
        subtitle="Internal book vs broker. Detects phantom fills and missed exits."
        right={
          role === 'operator'
            ? <ActionBtn onClick={runReconcile} disabled={running} color="amber">
                {running ? 'Running…' : 'Reconcile Now'}
              </ActionBtn>
            : null
        }
      />

      {/* Summary banner */}
      {!loading && recon && (
        <div className={`mb-4 p-4 rounded border ${
          allMatch ? 'bg-green-950/20 border-green-800/40' : 'bg-red-950/20 border-red-800/40'
        }`}>
          <div className={`text-[15px] font-bold font-mono ${allMatch ? 'text-green-300' : 'text-red-400'}`}>
            {allMatch ? '✓ RECONCILED — Internal book matches broker' : `⚠ ${mismatches.length} MISMATCH${mismatches.length > 1 ? 'ES' : ''} DETECTED`}
          </div>
          {recon.last_run && (
            <div className="text-[10px] text-[#4A5568] mt-1 font-mono">Last run: {recon.last_run}</div>
          )}
        </div>
      )}

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="RECONCILIATION UNAVAILABLE" detail={err} /> :
       !recon  ? <BackendRequired endpoint="GET /api/v1/risk/reconcile" /> : (
        <>
          {/* Positions diff */}
          <Panel>
            <PanelHeader title="POSITION COMPARISON" subtitle="Internal vs broker state" />
            <div className="grid grid-cols-[1fr_120px_120px_80px] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase tracking-wider text-[#4A5568]">
              <span>Symbol</span><span>Internal</span><span>Broker</span><span>Status</span>
            </div>
            {(recon.positions ?? []).length === 0 ? (
              <EmptyBlock label="NO POSITIONS" sub="No positions to compare" />
            ) : (
              (recon.positions ?? []).map((p, i) => (
                <DiffRow
                  key={i}
                  symbol={p.symbol}
                  internal={p.internal_qty != null ? `${p.internal_qty} @ ${p.internal_entry ?? '?'}` : p.internal}
                  broker={p.broker_qty != null ? `${p.broker_qty} @ ${p.broker_entry ?? '?'}` : p.broker}
                  match={p.match ?? p.status === 'MATCH'}
                />
              ))
            )}
          </Panel>

          {/* Mismatches detail */}
          {mismatches.length > 0 && (
            <Panel className="mt-4">
              <PanelHeader title="MISMATCHES" subtitle="Manual review required" />
              {mismatches.map((m, i) => (
                <div key={i} className="px-4 py-3 border-b border-[#1E2A3A]/40">
                  <div className="flex items-center gap-2 mb-1">
                    <StatusBadge text="MISMATCH" color="red" />
                    <span className="text-[12px] font-mono font-bold text-[#F5F7FA]">{m.symbol ?? m.field ?? `item ${i+1}`}</span>
                  </div>
                  <div className="text-[11px] text-[#8D98A8]">Internal: <span className="text-[#F5F7FA] font-mono">{String(m.internal ?? '—')}</span></div>
                  <div className="text-[11px] text-[#8D98A8]">Broker:   <span className="text-[#F5F7FA] font-mono">{String(m.broker   ?? '—')}</span></div>
                  {m.reason && <div className="text-[10px] text-amber-300/70 mt-1">{m.reason}</div>}
                </div>
              ))}
              <div className="px-4 py-3 text-[11px] font-mono text-amber-300/80">
                Mismatches require manual review. Do NOT flatten positions based on frontend data alone — verify via broker portal before acting.
              </div>
            </Panel>
          )}

          {/* Stats */}
          {recon.summary && (
            <Panel className="mt-4">
              <PanelHeader title="RECONCILIATION SUMMARY" />
              <div className="p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
                {Object.entries(recon.summary).map(([k, v]) => (
                  <div key={k} className="text-center">
                    <div className="text-[20px] font-mono font-bold text-[#F5F7FA]">{String(v)}</div>
                    <div className="text-[9px] uppercase tracking-wider text-[#4A5568] mt-0.5">{k.replace(/_/g, ' ')}</div>
                  </div>
                ))}
              </div>
            </Panel>
          )}
        </>
      )}
    </div>
  );
}
