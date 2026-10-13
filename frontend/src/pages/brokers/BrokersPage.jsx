/**
 * Strike Desk V4.5 — Broker Connection Page
 *
 * Data flow:
 *   GET /api/v1/brokers/tokens/status  → full broker list with feed_alive, is_active_routing (30s poll)
 *   GET /api/v1/brokers/health         → token scheduler health, live_mode_blocked (on-demand)
 *   POST /api/v1/brokers/dhan/reauth   → paste new Dhan access token
 *   POST /api/v1/brokers/fyers/reauth  → paste new Fyers access token
 *   POST /api/v1/brokers/angelone/refresh → trigger AngelOne refresh_token renewal
 *
 * tokensStatus() response fields used:
 *   broker_name, display_name, auth_type, auto_refresh_supported, is_authenticated,
 *   status (ACTIVE|EXPIRING|EXPIRED|UNAUTHENTICATED|ERROR), client_id, authenticated_at,
 *   expires_at, last_refreshed_at, expires_in, latency_ms, error_message,
 *   is_active_routing, feed_alive
 *
 * The separate brokerApi.active() call was removed — tokensStatus() already returns
 * active_broker at the top level and is_active_routing per broker.
 */
import React, { useState, useCallback, useEffect, useRef } from 'react';
import { brokers as brokerApi } from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, KV, StatusBadge, LoadingBlock, ErrorBlock,
  PageHeader, ActionBtn, Freshness,
} from '../../components/ui';

const POLL = 30_000;

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

function fmtSeconds(sec) {
  if (sec == null) return '—';
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function brokerBadge(b) {
  if (!b.is_authenticated)         return { text: 'NO TOKEN',   color: 'red'   };
  if (b.status === 'EXPIRED')      return { text: 'EXPIRED',    color: 'red'   };
  if (b.status === 'ERROR')        return { text: 'FEED ERROR', color: 'red'   };
  if (b.feed_alive === false)      return { text: 'FEED DOWN',  color: 'amber' };
  if (b.status === 'EXPIRING')     return { text: 'EXPIRING',   color: 'amber' };
  return                                  { text: 'AUTHENTICATED', color: 'green' };
}

// ──────────────────────────────────────────────────────────────────────────────
// Health Panel — displays GET /api/v1/brokers/health response
// ──────────────────────────────────────────────────────────────────────────────

function HealthPanel({ health, onDismiss }) {
  if (!health) return null;

  const isErr = !!health.error;
  const statusColor = health.any_live_mode_blocked ? 'amber' : 'green';

  return (
    <Panel className="mb-4">
      <PanelHeader
        title="BROKER HEALTH"
        subtitle={`Checked: ${health.checked_at ? new Date(health.checked_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) + ' IST' : '—'}`}
        right={<ActionBtn onClick={onDismiss}>Dismiss</ActionBtn>}
      />
      <div className="p-4">
        {isErr ? (
          <div className="text-red-400 font-mono text-[11px]">{health.error}</div>
        ) : (
          <>
            {health.any_live_mode_blocked && (
              <div className="mb-3 px-3 py-2 bg-red-950/30 border border-red-800/30 rounded text-[11px] font-mono text-red-300">
                ⚠ LIVE MODE BLOCKED — one or more brokers have blocked LIVE execution due to token expiry.
              </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-3 gap-x-8 mb-3">
              <KV label="Any LIVE blocked"   value={<StatusBadge text={health.any_live_mode_blocked ? 'YES' : 'NO'} color={statusColor} />} mono={false} />
              <KV label="Last daily check"   value={health.last_daily_check ? new Date(health.last_daily_check).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) + ' IST' : '—'} />
              <KV label="Next check in"      value={health.next_check_in_seconds != null ? fmtSeconds(health.next_check_in_seconds) : '—'} />
            </div>
            {Array.isArray(health.brokers) && health.brokers.length > 0 && (
              <div className="border border-[#1E2A3A] rounded overflow-hidden">
                <div className="grid grid-cols-[100px_90px_80px_80px_90px_1fr] gap-2 px-4 py-2 border-b border-[#1E2A3A] text-[9px] font-bold uppercase text-[#4A5568]">
                  <span>Broker</span>
                  <span>Token</span>
                  <span>Age</span>
                  <span>Remaining</span>
                  <span>LIVE Block</span>
                  <span>Block Reason</span>
                </div>
                {health.brokers.map((b) => (
                  <div key={b.broker} className="grid grid-cols-[100px_90px_80px_80px_90px_1fr] gap-2 px-4 py-2 border-b border-[#1E2A3A]/40 hover:bg-[#111821]/50 text-[11px] font-mono">
                    <span className="text-[#F5F7FA] uppercase">{b.broker}</span>
                    <span className={
                      b.token_status === 'ACTIVE'   ? 'text-green-300' :
                      b.token_status === 'EXPIRING' ? 'text-amber-300' : 'text-red-400'
                    }>{b.token_status ?? '—'}</span>
                    <span className="text-[#8D98A8]">{fmtSeconds(b.token_age_seconds)}</span>
                    <span className={b.token_remaining_seconds != null && b.token_remaining_seconds < 3600 ? 'text-amber-300' : 'text-[#8D98A8]'}>
                      {fmtSeconds(b.token_remaining_seconds)}
                    </span>
                    <span>{b.live_mode_blocked ? <StatusBadge text="BLOCKED" color="red" /> : <StatusBadge text="CLEAR" color="green" />}</span>
                    <span className="text-[#4A5568]">{b.block_reason ?? '—'}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}

// ──────────────────────────────────────────────────────────────────────────────
// Broker Card
// ──────────────────────────────────────────────────────────────────────────────

function BrokerCard({ broker, isActive, isOperator, onReauth, onHealthCheck }) {
  const [expanded,   setExpanded]   = useState(false);
  const [token,      setToken]      = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [msg,        setMsg]        = useState(null);

  const b = broker;
  const badge = brokerBadge(b);

  const borderColor = !b.is_authenticated ? 'border-red-800/40' :
                       isActive            ? 'border-amber-500/40' :
                                             'border-[#1E2A3A]';
  const bgColor = isActive ? 'bg-[#151C25]' : 'bg-[#111821]';

  const isManual = b.auth_type === 'MANUAL_REAUTH';

  // Auth type human label — correct description of the actual mechanism
  const authTypeLabel = b.auto_refresh_supported
    ? 'Auto-refresh (programmatic, ~06:00 IST)'
    : 'Manual token required daily';

  // Feed status derived from feed_alive field
  const feedStatus = b.feed_alive === true   ? { text: 'LIVE',    color: 'green' }
                   : b.feed_alive === false   ? { text: 'DOWN',    color: 'red'   }
                   :                            { text: 'UNKNOWN', color: 'gray'  };

  const handleSubmit = async () => {
    if (!token.trim()) return;
    setSubmitting(true);
    setMsg(null);
    try {
      await onReauth(b.broker_name, token.trim());
      setMsg({ ok: true, text: 'Token accepted — broker re-authenticated' });
      setToken('');
      setExpanded(false);
    } catch (e) {
      setMsg({ ok: false, text: e.response?.data?.detail || e.message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={`${bgColor} border ${borderColor} rounded-lg overflow-hidden`}>
      {/* Header */}
      <div className="px-4 py-3 border-b border-[#1E2A3A] flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-bold text-[#F5F7FA]">
              {b.display_name ?? b.broker_name?.toUpperCase()}
            </span>
            {isActive && <StatusBadge text="PRIMARY" color="amber" />}
          </div>
          {b.broker_name === 'dhan' && (
            <div className="text-[10px] font-mono text-amber-400 mt-0.5">
              PRIMARY LIVE OPTIONS BROKER
            </div>
          )}
        </div>
        <StatusBadge text={badge.text} color={badge.color} />
      </div>

      {/* Body */}
      <div className="p-4">
        {/* Critical alerts */}
        {!b.is_authenticated && (
          <div className="mb-3 px-3 py-2 bg-red-950/30 border border-red-800/30 rounded text-[11px] font-mono text-red-300">
            {b.broker_name === 'dhan'    ? '🔴 DHAN TOKEN MISSING — LIVE EXECUTION BLOCKED' :
             b.broker_name === 'fyers'   ? '🔴 FYERS TOKEN MISSING' :
                                           '🔴 AUTHENTICATION REQUIRED'}
          </div>
        )}
        {b.is_authenticated && b.feed_alive === false && (
          <div className="mb-3 px-3 py-2 bg-amber-950/20 border border-amber-800/30 rounded text-[11px] font-mono text-amber-300">
            ⚠ Token valid but feed is disconnected — adapter not streaming
          </div>
        )}
        {b.status === 'EXPIRING' && (
          <div className="mb-3 px-3 py-2 bg-amber-950/20 border border-amber-800/30 rounded text-[11px] font-mono text-amber-300">
            ⚠ Token expiring soon — {b.expires_in ?? 'check expiry'}
          </div>
        )}

        {/* Status details */}
        <div className="space-y-0">
          <KV label="Authentication"
              value={<StatusBadge
                text={b.is_authenticated ? 'AUTHENTICATED' : 'NOT AUTHENTICATED'}
                color={b.is_authenticated ? 'green' : 'red'}
              />} mono={false} />
          <KV label="Token status"
              value={b.status ?? '—'}
              valueColor={
                b.status === 'ACTIVE'   ? 'text-green-300' :
                b.status === 'EXPIRING' ? 'text-amber-300' : 'text-red-400'
              } />
          <KV label="Feed (adapter)"
              value={<StatusBadge text={feedStatus.text} color={feedStatus.color} />}
              mono={false} />
          <KV label="Auth mechanism" value={authTypeLabel} />
          <KV label="Expires in"     value={b.expires_in ?? '—'} />
          <KV label="Client ID"      value={b.client_id ? `••••${b.client_id.slice(-4)}` : '—'} />
          <KV label="Authenticated"  value={b.authenticated_at ? new Date(b.authenticated_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '—'} />
          <KV label="Last refreshed" value={b.last_refreshed_at ? new Date(b.last_refreshed_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '—'} />
          {b.error_message && (
            <KV label="Last error" value={b.error_message} valueColor="text-red-400" />
          )}
          {b.broker_name === 'dhan' && (
            <>
              <KV label="Options routing" value="YES — sole F&O broker"      valueColor="text-green-300" />
              <KV label="Market data"     value="WebSocket primary feed" />
            </>
          )}
          {b.broker_name === 'angelone' && (
            <KV label="Options routing" value="NO — supplemental data feed" valueColor="text-[#8D98A8]" />
          )}
          {b.broker_name === 'fyers' && (
            <KV label="Options routing" value="NO — supplemental data feed" valueColor="text-[#8D98A8]" />
          )}
        </div>

        {/* Operator actions */}
        {isOperator && (
          <div className="mt-4 flex gap-2 flex-wrap">
            {isManual && (
              <ActionBtn color="amber" onClick={() => setExpanded(v => !v)}>
                {expanded ? 'Cancel' : 'Re-Authenticate'}
              </ActionBtn>
            )}
            {b.auto_refresh_supported && (
              <ActionBtn color="default" onClick={() => onReauth(b.broker_name, null)}>
                Force Refresh
              </ActionBtn>
            )}
            <ActionBtn color="default" onClick={() => onHealthCheck(b.broker_name)}>
              Health Check
            </ActionBtn>
          </div>
        )}

        {/* Token paste panel */}
        {expanded && isManual && (
          <div className="mt-3 p-3 bg-[#0B0F14] border border-[#2A3F5A] rounded">
            <div className="text-[10px] text-[#8D98A8] mb-2">
              {b.broker_name === 'dhan'
                ? 'Generate token at api.dhan.co → Access Token'
                : 'Generate token at myapi.fyers.in → Access Token'}
            </div>
            <div className="flex gap-2">
              <input
                type="password"
                value={token}
                onChange={e => setToken(e.target.value)}
                placeholder="Paste access token…"
                className="flex-1 bg-[#0B0F14] border border-[#2A3F5A] rounded px-3 py-1.5 text-[12px] font-mono text-[#F5F7FA] placeholder-[#4A5568] focus:outline-none focus:border-amber-500/60"
              />
              <ActionBtn color="amber" onClick={handleSubmit} disabled={!token.trim() || submitting}>
                {submitting ? '…' : 'Submit'}
              </ActionBtn>
            </div>
            {msg && (
              <div className={`mt-2 text-[11px] font-mono ${msg.ok ? 'text-green-300' : 'text-red-300'}`}>
                {msg.text}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────────
// Main Page
// ──────────────────────────────────────────────────────────────────────────────

export default function BrokersPage() {
  const { role } = useAuthStore();
  const isOperator = role === 'operator';

  const [bList,       setBList]       = useState([]);
  const [activeB,     setActiveB]     = useState(null);
  const [initialLoad, setInitialLoad] = useState(true);
  const [refreshing,  setRefreshing]  = useState(false);
  const [listErr,     setListErr]     = useState(null);
  const [ageMs,       setAgeMs]       = useState(null);
  const [healthResult,setHealthResult]= useState(null);
  const [healthLoading, setHealthLoading] = useState(false);

  const reqId = useRef(0);

  const fetchAll = useCallback(async (isManual = false) => {
    if (isManual) setRefreshing(true);
    const id = ++reqId.current;

    const [rList] = await Promise.allSettled([
      brokerApi.tokensStatus(),
    ]);

    if (id !== reqId.current) return; // superseded

    if (rList.status === 'fulfilled') {
      const data = rList.value.data ?? {};
      setBList(data.brokers ?? []);
      // active_broker is included in tokensStatus response — no separate call needed
      setActiveB(data.active_broker ?? null);
      setAgeMs(Date.now());
      setListErr(null);
    } else {
      const msg = rList.reason?.response?.data?.detail || rList.reason?.message || 'Broker status unavailable';
      setListErr(msg);
      // Do NOT update ageMs — old timestamp stays correct (data not refreshed)
    }

    setInitialLoad(false);
    if (isManual) setRefreshing(false);
  }, []);

  useEffect(() => {
    fetchAll();
    const t = setInterval(fetchAll, POLL);
    return () => clearInterval(t);
  }, [fetchAll]);

  const handleReauth = async (brokerName, token) => {
    if (brokerName === 'angelone') {
      await brokerApi.refreshAngel();
    } else if (brokerName === 'dhan') {
      await brokerApi.reauthDhan(token);
    } else if (brokerName === 'fyers') {
      await brokerApi.reauthFyers(token);
    }
    await fetchAll(true);
  };

  const handleHealthCheck = async () => {
    setHealthLoading(true);
    try {
      const r = await brokerApi.health();
      setHealthResult(r.data ?? null);
    } catch (e) {
      setHealthResult({ error: e.response?.data?.detail || e.message });
    } finally {
      setHealthLoading(false);
    }
  };

  // Sort brokers: dhan → angelone → fyers, then any others
  const brokerOrder = ['dhan', 'angelone', 'fyers'];
  const sortedBrokers = [...bList].sort((a, b) => {
    const ai = brokerOrder.indexOf(a.broker_name);
    const bi = brokerOrder.indexOf(b.broker_name);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  // Derive live_mode_blocked state from broker list statuses
  const anyLiveModeBlocked = healthResult?.any_live_mode_blocked === true;

  return (
    <div>
      <PageHeader
        title="Broker Connection"
        subtitle="02 — Token status, authentication, feed health. Backend authoritative."
        right={
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono text-[#4A5568]">
              <Freshness ageMs={ageMs} warnMs={45_000} errorMs={120_000} />
            </span>
            <ActionBtn onClick={() => fetchAll(true)} disabled={refreshing || initialLoad}>
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </ActionBtn>
            <ActionBtn onClick={handleHealthCheck} disabled={healthLoading}>
              {healthLoading ? '…' : 'Health Check'}
            </ActionBtn>
          </div>
        }
      />

      {/* Health panel */}
      {healthResult && (
        <HealthPanel health={healthResult} onDismiss={() => setHealthResult(null)} />
      )}

      {/* Live mode blocked warning */}
      {anyLiveModeBlocked && (
        <div className="mb-4 px-4 py-3 bg-red-950/30 border border-red-800/40 rounded text-[12px] font-mono text-red-300">
          ⚠ LIVE MODE BLOCKED — one or more broker tokens have expired. Re-authenticate before LIVE trading.
        </div>
      )}

      {initialLoad ? (
        <LoadingBlock label="Loading broker status…" />
      ) : listErr ? (
        <ErrorBlock label="BROKER STATUS UNAVAILABLE" detail={listErr} />
      ) : (
        <div className="space-y-4">

          {/* Safety notice */}
          <div className="p-3 bg-[#151C25] border border-[#1E2A3A] rounded text-[11px] font-mono text-[#8D98A8] leading-relaxed">
            ⚠ LIVE mode requires: token ACTIVE + feed LIVE + broker health GREEN.
            Token valid at 09:00 IST does NOT imply valid at 11:00 — every order revalidates.
          </div>

          {sortedBrokers.length === 0 ? (
            <div className="p-8 text-center text-[#4A5568] font-mono text-[11px]">
              No broker data returned from backend
            </div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
              {sortedBrokers.map(b => (
                <BrokerCard
                  key={b.broker_name}
                  broker={b}
                  isActive={b.broker_name === activeB}
                  isOperator={isOperator}
                  onReauth={handleReauth}
                  onHealthCheck={handleHealthCheck}
                />
              ))}
            </div>
          )}

          {refreshing && (
            <div className="text-[10px] font-mono text-[#4A5568] animate-pulse text-center">
              re-checking broker status…
            </div>
          )}
        </div>
      )}
    </div>
  );
}
