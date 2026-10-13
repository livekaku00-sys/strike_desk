/**
 * Strike Desk V4.5 — Risk Management Page
 *
 * Data flow:
 *   GET /api/v1/risk/state           → riskData  (5s poll)
 *   GET /api/v1/risk/operator-status → opStatus  (5s poll, parallel, supplementary)
 *   GET /api/v1/risk/audit-log       → auditLog  (on-demand)
 *
 * Backend field names from get_risk_summary():
 *   daily_loss_ceiling  — NEGATIVE float, e.g. -2500.0
 *   max_daily_trades    — int, e.g. 5
 *   total_risk_pnl      — float (realized + unrealized − charges); negative = loss
 *   loss_ceiling_breached, cooldown_active, cooldown_until,
 *   emergency_halt_active, emergency_halt_reason
 *
 * Halt reset flow (two-step):
 *   POST /acknowledge/request-token → { token }
 *   POST /acknowledge/reset         { token, operator_id, resolution_notes }
 *
 * Flatten All flow (two-step):
 *   POST /flatten-all/request-token → { token }
 *   POST /flatten-all/confirm        { token, operator_id, reason }
 *
 * STALE_WARN_MS / STALE_ERR_MS: frontend thresholds — backend does not expose one.
 * BACKEND CONTRACT GAP: consecutive_loss_threshold (2) and cooldown_duration_minutes (45)
 * are not returned by get_risk_summary(); values are from RiskLimitsConfig defaults.
 */
import React, { useState, useCallback, useEffect, useRef } from 'react';
import { risk as riskApi } from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, StatusBadge, KV, Meter,
  LoadingBlock, ErrorBlock, EmptyBlock, PageHeader, ActionBtn, Freshness,
} from '../../components/ui';

const POLL          = 5_000;
const STALE_WARN_MS = 15_000;
const STALE_ERR_MS  = 30_000;
const OPERATOR_ID   = 'MONUJ';

// ── ConfirmDialog ─────────────────────────────────────────────────────────────

function ConfirmDialog({ title, message, dangerous, onConfirm, onCancel, loading }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <div className="bg-[#111821] border border-[#2A3F5A] rounded-lg p-6 max-w-md w-full mx-4">
        <div className={`text-[15px] font-bold mb-2 ${dangerous ? 'text-red-400' : 'text-[#F5F7FA]'}`}>{title}</div>
        <div className="text-[12px] text-[#8D98A8] mb-5 leading-relaxed">{message}</div>
        <div className="flex gap-3 justify-end">
          <ActionBtn onClick={onCancel} disabled={loading}>Cancel</ActionBtn>
          <ActionBtn onClick={onConfirm} color={dangerous ? 'danger' : 'amber'} disabled={loading}>
            {loading ? 'Processing…' : 'Confirm'}
          </ActionBtn>
        </div>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function RiskPage() {
  const { role } = useAuthStore();
  const isOperator = role === 'operator';

  const [initialLoad, setInitialLoad] = useState(true);
  const [refreshing,  setRefreshing]  = useState(false);
  const [riskData,    setRiskData]    = useState(null);
  const [opStatus,    setOpStatus]    = useState(null);
  const [err,         setErr]         = useState(null);
  const [ageMs,       setAgeMs]       = useState(null);
  const reqId = useRef(0);

  // Self-updating ticker for stale detection (cannot rely on poll to re-render during errors)
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 2000);
    return () => clearInterval(t);
  }, []);

  const [auditLog,     setAuditLog]     = useState([]);
  const [auditLoading, setAuditLoading] = useState(false);

  const [confirm,   setConfirm]   = useState(null);
  const [mutating,  setMutating]  = useState(false);
  const [mutateMsg, setMutateMsg] = useState(null);

  // ── Fetch (initialLoad / refreshing split + race protection) ─────────────

  const fetchRisk = useCallback(async (isManual = false) => {
    if (isManual) setRefreshing(true);
    const id = ++reqId.current;

    const [rState, rOp] = await Promise.allSettled([
      riskApi.state(),
      riskApi.operatorStatus(),
    ]);

    if (id !== reqId.current) return; // superseded by a newer request

    if (rState.status === 'fulfilled') {
      setRiskData(rState.value.data ?? null);
      setAgeMs(Date.now());
      setErr(null);
    } else {
      const msg = rState.reason?.response?.data?.detail
                || rState.reason?.message
                || 'Risk state unavailable';
      setErr(msg);
      // Keep existing riskData — show stale indicator rather than blank the page
    }

    if (rOp.status === 'fulfilled') {
      setOpStatus(rOp.value.data ?? null);
    }
    // opStatus failure is non-fatal — supplementary halt-type info only

    setInitialLoad(false);
    if (isManual) setRefreshing(false);
  }, []);

  const fetchAudit = useCallback(async () => {
    setAuditLoading(true);
    try {
      const r = await riskApi.auditLog({ limit: 20 });
      // Backend returns { events: [...], count: N } — not "entries" or "log"
      const rows = r.data?.events ?? [];
      setAuditLog(Array.isArray(rows) ? rows : []);
    } catch {
      setAuditLog([]);
    } finally {
      setAuditLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRisk();
    fetchAudit();
    const t = setInterval(fetchRisk, POLL);
    return () => clearInterval(t);
  }, [fetchRisk, fetchAudit]);

  // ── Mutations ─────────────────────────────────────────────────────────────

  const doMutate = async (action) => {
    setMutating(true);
    setMutateMsg(null);
    try {
      let r;
      if (action === 'halt') {
        r = await riskApi.halt({ reason: 'Manual halt from dashboard', level: 'HARD_HALT', operator_id: OPERATOR_ID });
      } else if (action === 'stopEntry') {
        r = await riskApi.stopEntry({ reason: 'Manual stop entry from dashboard', operator_id: OPERATOR_ID });
      } else if (action === 'resetHalt') {
        // Step 1: get a signed acknowledgment token from the backend
        const tokenRes = await riskApi.requestAckToken({ operator_id: OPERATOR_ID });
        const token = tokenRes.data?.token;
        if (!token) throw new Error('Backend did not return a reset token');
        // Step 2: reset with that token
        r = await riskApi.resetHalt({ token, operator_id: OPERATOR_ID, resolution_notes: '' });
      } else if (action === 'flatten') {
        // Step 1: get a signed flatten confirmation token from the backend
        const tokenRes = await riskApi.requestFlattenToken({ operator_id: OPERATOR_ID });
        const token = tokenRes.data?.token;
        if (!token) throw new Error('Backend did not return a flatten token');
        // Step 2: confirm within the 60-second window
        r = await riskApi.flattenAll({ token, operator_id: OPERATOR_ID, reason: 'Operator FLATTEN ALL from dashboard' });
      }
      setMutateMsg({ ok: true, text: r?.data?.message || `${action} executed successfully` });
      await fetchRisk(false);
      await fetchAudit();
    } catch (e) {
      setMutateMsg({ ok: false, text: e.response?.data?.detail || e.message || 'Action failed' });
    } finally {
      setMutating(false);
      setConfirm(null);
    }
  };

  // ── Derived values ─────────────────────────────────────────────────────────

  const rd = riskData;

  // daily_loss_ceiling from backend is NEGATIVE (e.g. -2500.0); take abs for display math
  const ceiling  = rd?.daily_loss_ceiling != null ? Math.abs(rd.daily_loss_ceiling) : 2500;
  // backend field is max_daily_trades (not max_trade_count / daily_trade_limit)
  const tradeMax = rd?.max_daily_trades ?? 5;
  // total_risk_pnl is what the backend checks against the ceiling (realized + unrealized - charges)
  const lossUsed = rd ? Math.abs(Math.min(0, rd.total_risk_pnl ?? 0)) : 0;
  const tradePct = tradeMax > 0 ? ((rd?.trade_count ?? 0) / tradeMax) * 100 : 0;
  const lossPct  = ceiling > 0 ? (lossUsed / ceiling) * 100 : 0;
  const lossColor = lossPct > 80 ? 'red' : lossPct > 50 ? 'amber' : 'green';

  const pnlColor = (v) => (v == null ? '' : v >= 0 ? 'text-green-300' : 'text-red-400');

  // Stale detection (frontend thresholds — backend contract gap, see file header)
  const isStale         = ageMs != null && (now - ageMs) > STALE_WARN_MS;
  const isCriticalStale = ageMs != null && (now - ageMs) > STALE_ERR_MS;

  // Halt type from operator_status: 'STOP_ENTRY' | 'FLATTEN_ALL' | null (HARD_HALT = internal)
  const haltType = opStatus?.halt_type ?? null;

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div>
      <PageHeader
        title="Risk Engine"
        subtitle={rd ? `Trading date: ${rd.ist_date ?? '—'}` : 'Always on — backend state authoritative'}
        right={
          <div className="flex items-center gap-2">
            {refreshing && <span className="text-[10px] font-mono text-[#4A5568]">refreshing…</span>}
            {ageMs && <Freshness ageMs={ageMs} warnMs={STALE_WARN_MS} errorMs={STALE_ERR_MS} />}
            <ActionBtn onClick={() => fetchRisk(true)} disabled={refreshing || mutating}>Refresh</ActionBtn>
          </div>
        }
      />

      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          message={confirm.message}
          dangerous={confirm.dangerous}
          loading={mutating}
          onConfirm={() => doMutate(confirm.action)}
          onCancel={() => setConfirm(null)}
        />
      )}

      {mutateMsg && (
        <div className={`mb-4 px-4 py-2 rounded border text-[12px] font-mono ${
          mutateMsg.ok
            ? 'bg-green-950/20 border-green-800/30 text-green-300'
            : 'bg-red-950/20 border-red-800/40 text-red-300'
        }`}>
          {mutateMsg.text}
        </div>
      )}

      {/* Stale banners — safety-critical: never silently show stale state as current */}
      {!initialLoad && isCriticalStale && (
        <div className="mb-4 p-3 bg-red-950/30 border border-red-700/40 rounded text-[12px] font-mono text-red-300">
          ⚠ RISK STATE CRITICALLY STALE — Backend unreachable for {Math.round((now - ageMs) / 1000)}s. Do not rely on displayed authorization state.
        </div>
      )}
      {!initialLoad && !isCriticalStale && isStale && (
        <div className="mb-4 p-3 bg-amber-950/20 border border-amber-700/30 rounded text-[12px] font-mono text-amber-300">
          ⚠ RISK STATE STALE — Last updated {Math.round((now - ageMs) / 1000)}s ago. Attempting to reconnect.
        </div>
      )}

      {/* Refresh error with existing data — keep data visible */}
      {!initialLoad && err && rd && (
        <div className="mb-4 p-3 bg-red-950/20 border border-red-800/30 rounded text-[12px] font-mono text-red-300">
          Refresh error: {err} — Showing last known state
        </div>
      )}

      {initialLoad ? (
        <LoadingBlock label="Loading risk state…" />
      ) : !rd && err ? (
        <ErrorBlock label="RISK STATE UNAVAILABLE" detail={`${err} — Risk authorization cannot be confirmed`} />
      ) : !rd ? (
        <ErrorBlock label="RISK STATE UNKNOWN" detail="No data received from backend. Risk authorization cannot be confirmed." />
      ) : (
        <div className="space-y-4">

          {/* Emergency Halt Banner — highest priority, most visible */}
          {rd.emergency_halt_active && (
            <div className="flex items-center justify-between p-4 bg-red-950/50 border-2 border-red-500/70 rounded-lg">
              <div>
                <div className="text-[15px] font-bold text-red-300 font-mono">
                  ⚠{' '}
                  {haltType === 'STOP_ENTRY'   ? 'STOP ENTRY ACTIVE'
                   : haltType === 'FLATTEN_ALL' ? 'FLATTEN ALL ACTIVE'
                   : 'EMERGENCY HALT ACTIVE'}
                </div>
                <div className="text-[11px] text-red-400/80 mt-1">
                  {rd.emergency_halt_reason || 'All new entries are blocked.'}
                </div>
                {opStatus?.active_event_id && (
                  <div className="text-[10px] text-red-500/60 mt-0.5 font-mono">
                    Event: {opStatus.active_event_id}
                  </div>
                )}
              </div>
              {isOperator && (
                <ActionBtn color="amber" onClick={() => setConfirm({
                  action: 'resetHalt',
                  title: 'Clear Emergency Halt',
                  message: 'This requests a signed acknowledgment token from the backend, then resets the halt in a second call. Only clear after confirming the system is safe to resume.',
                  dangerous: false,
                })}>
                  Clear Halt
                </ActionBtn>
              )}
            </div>
          )}

          {/* Loss ceiling breached banner */}
          {rd.loss_ceiling_breached && (
            <div className="p-3 bg-red-950/30 border border-red-700/40 rounded text-[12px] font-mono text-red-300">
              DAILY LOSS CEILING BREACHED — Trading halted automatically.
              {' '}Total Risk P&L: ₹{(rd.total_risk_pnl ?? 0).toFixed(2)} ≤ ₹{(rd.daily_loss_ceiling ?? -2500).toFixed(2)}
            </div>
          )}

          {/* Cooldown banner */}
          {rd.cooldown_active && (
            <div className="p-3 bg-amber-950/20 border border-amber-700/30 rounded text-[12px] font-mono text-amber-300">
              COOLDOWN ACTIVE — {rd.consecutive_losses} consecutive {rd.consecutive_losses === 1 ? 'loss' : 'losses'}.
              {rd.cooldown_until ? ` Ends: ${rd.cooldown_until}` : ''}
            </div>
          )}

          {/* Hard limits + raw risk state */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Panel>
              <PanelHeader title="HARD LIMITS" subtitle="Source: backend — daily_loss_ceiling, max_daily_trades" />
              <div className="p-4 space-y-4">
                {/* Daily Loss */}
                <div>
                  <div className="flex justify-between text-[11px] mb-1">
                    <span className="text-[#8D98A8]">Daily Loss Ceiling</span>
                    <span className="font-mono text-red-400">₹{ceiling.toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between items-center text-[10px] mb-1.5">
                    <span className="text-[#4A5568]">
                      Used ₹{lossUsed.toFixed(0)} · Remaining ₹{Math.max(0, ceiling - lossUsed).toFixed(0)}
                    </span>
                    <StatusBadge
                      text={rd.loss_ceiling_breached ? 'BREACHED' : lossPct > 80 ? 'CRITICAL' : lossPct > 50 ? 'WARNING' : 'SAFE'}
                      color={rd.loss_ceiling_breached ? 'red' : lossColor}
                    />
                  </div>
                  <Meter pct={lossPct} color={lossColor} value={`₹${lossUsed.toFixed(0)} / ₹${ceiling}`} />
                </div>
                {/* Trade Count */}
                <div>
                  <div className="flex justify-between text-[11px] mb-1">
                    <span className="text-[#8D98A8]">Trade Count</span>
                    <span className="font-mono text-amber-300">{rd.trade_count ?? 0} / {tradeMax}</span>
                  </div>
                  <div className="flex justify-between items-center text-[10px] mb-1.5">
                    <span className="text-[#4A5568]">Remaining: {Math.max(0, tradeMax - (rd.trade_count ?? 0))}</span>
                    <StatusBadge
                      text={tradePct >= 100 ? 'BLOCKED' : tradePct >= 80 ? 'WARNING' : 'OK'}
                      color={tradePct >= 100 ? 'red' : tradePct >= 80 ? 'amber' : 'green'}
                    />
                  </div>
                  <Meter pct={tradePct} color={tradePct >= 100 ? 'red' : tradePct >= 60 ? 'amber' : 'green'} value={`${rd.trade_count ?? 0}/${tradeMax}`} />
                </div>
                {/* Consecutive Losses — threshold=2 from RiskLimitsConfig; not in API response */}
                <div>
                  <div className="flex justify-between text-[11px] mb-1">
                    <span className="text-[#8D98A8]">Consecutive Losses</span>
                    <span className="font-mono">{rd.consecutive_losses ?? 0} / 2</span>
                  </div>
                  <Meter
                    pct={((rd.consecutive_losses ?? 0) / 2) * 100}
                    color={(rd.consecutive_losses ?? 0) >= 2 ? 'red' : (rd.consecutive_losses ?? 0) >= 1 ? 'amber' : 'green'}
                  />
                </div>
              </div>
            </Panel>

            <Panel>
              <PanelHeader title="RISK STATE" subtitle="GET /api/v1/risk/state — raw backend fields" />
              <div className="p-4">
                <KV label="ist_date"              value={rd.ist_date} />
                <KV label="realized_pnl"          value={rd.realized_pnl != null ? `₹ ${rd.realized_pnl.toFixed(2)}` : null} valueColor={pnlColor(rd.realized_pnl)} />
                <KV label="total_risk_pnl"        value={rd.total_risk_pnl != null ? `₹ ${rd.total_risk_pnl.toFixed(2)}` : null} valueColor={pnlColor(rd.total_risk_pnl)} />
                <KV label="daily_loss_ceiling"    value={rd.daily_loss_ceiling != null ? `₹ ${rd.daily_loss_ceiling.toFixed(2)}` : null} />
                <KV label="trade_count"           value={rd.trade_count} />
                <KV label="max_daily_trades"      value={rd.max_daily_trades} />
                <KV label="open_positions_count"  value={rd.open_positions_count} />
                <KV label="consecutive_losses"    value={rd.consecutive_losses} valueColor={(rd.consecutive_losses ?? 0) >= 2 ? 'text-red-400' : ''} />
                <KV label="cooldown_active"       value={rd.cooldown_active ? 'YES' : 'NO'} valueColor={rd.cooldown_active ? 'text-amber-300' : 'text-green-300'} />
                <KV label="cooldown_until"        value={rd.cooldown_until ?? '—'} />
                <KV label="emergency_halt_active" value={rd.emergency_halt_active ? 'YES' : 'NO'} valueColor={rd.emergency_halt_active ? 'text-red-400 font-bold' : 'text-green-300'} />
                <KV label="emergency_halt_reason" value={rd.emergency_halt_reason ?? '—'} />
                <KV label="loss_ceiling_breached" value={rd.loss_ceiling_breached ? 'YES' : 'NO'} valueColor={rd.loss_ceiling_breached ? 'text-red-400 font-bold' : 'text-green-300'} />
              </div>
            </Panel>
          </div>

          {/* Operator Controls */}
          {isOperator ? (
            <Panel>
              <PanelHeader title="OPERATOR CONTROLS" subtitle="All mutations require confirmation and are backend-audited" />
              <div className="p-4 grid grid-cols-2 md:grid-cols-4 gap-3">
                <div>
                  <ActionBtn color="danger" className="w-full"
                    onClick={() => setConfirm({
                      action: 'halt',
                      title: 'EMERGENCY HALT',
                      message: 'Block ALL new entries immediately. Existing positions continue their exit logic. Triggers HARD_HALT at backend.',
                      dangerous: true,
                    })}
                    disabled={rd.emergency_halt_active || mutating}
                  >Emergency Halt</ActionBtn>
                  <div className="text-[10px] text-[#4A5568] mt-1 font-mono">HARD_HALT</div>
                </div>
                <div>
                  <ActionBtn color="amber" className="w-full"
                    onClick={() => setConfirm({
                      action: 'stopEntry',
                      title: 'Stop New Entries',
                      message: 'Block all new order entry. Open positions continue their exit logic normally. Maps to SOFT_HALT internally.',
                      dangerous: false,
                    })}
                    disabled={mutating}
                  >Stop Entry</ActionBtn>
                  <div className="text-[10px] text-[#4A5568] mt-1 font-mono">SOFT_HALT</div>
                </div>
                <div>
                  <ActionBtn color="red" className="w-full"
                    onClick={() => setConfirm({
                      action: 'flatten',
                      title: 'CLOSE ALL POSITIONS',
                      message: '⚠ Cancel all pending orders and close ALL open positions at market. Requires a backend confirmation token. This cannot be undone.',
                      dangerous: true,
                    })}
                    disabled={mutating}
                  >Flatten All</ActionBtn>
                  <div className="text-[10px] text-[#4A5568] mt-1 font-mono">FULL_KILL_SWITCH</div>
                </div>
                <div>
                  <ActionBtn color="green" className="w-full"
                    onClick={() => setConfirm({
                      action: 'resetHalt',
                      title: 'Clear Halt',
                      message: 'Request a signed acknowledgment token from the backend, then clear the active halt. Only do this after confirming the system is safe to resume.',
                      dangerous: false,
                    })}
                    disabled={!rd.emergency_halt_active || mutating}
                  >Clear Halt</ActionBtn>
                  <div className="text-[10px] text-[#4A5568] mt-1 font-mono">two-step token reset</div>
                </div>
              </div>
            </Panel>
          ) : (
            <div className="p-3 bg-[#151C25] border border-[#1E2A3A] rounded text-[11px] font-mono text-[#4A5568]">
              GUEST — Risk controls require Operator role
            </div>
          )}

          {/* Recent Audit */}
          <Panel>
            <PanelHeader
              title="RECENT AUDIT"
              subtitle="GET /api/v1/risk/audit-log — last 20 events"
              right={<ActionBtn onClick={fetchAudit} disabled={auditLoading}>Refresh</ActionBtn>}
            />
            <div className="p-0">
              {auditLoading ? <LoadingBlock /> :
               auditLog.length === 0 ? <EmptyBlock label="No audit entries" /> : (
                <div className="divide-y divide-[#1E2A3A]">
                  {auditLog.map((e, i) => (
                    <div key={e.event_id ?? i} className="flex items-center gap-3 px-4 py-2.5 hover:bg-[#111821]/50">
                      <span className="text-[10px] font-mono text-[#4A5568] min-w-[130px]">{e.timestamp ?? '—'}</span>
                      <span className={`text-[11px] font-mono font-bold min-w-[130px] ${
                        e.action?.includes('HALT')    ? 'text-red-400'
                        : e.action?.includes('FLATTEN') ? 'text-red-300'
                        : e.action?.includes('STOP')    ? 'text-amber-300'
                        : e.action?.includes('RESET')   ? 'text-green-300'
                        : 'text-[#F5F7FA]'
                      }`}>{e.action ?? '—'}</span>
                      <span className="text-[11px] text-[#8D98A8] flex-1 truncate">
                        {/* backend audit log field is 'reason', not 'detail'/'message' */}
                        {e.reason ?? e.message ?? '—'}
                      </span>
                      {e.operator && (
                        <span className="text-[10px] font-mono text-[#4A5568]">{e.operator}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Panel>

        </div>
      )}
    </div>
  );
}
