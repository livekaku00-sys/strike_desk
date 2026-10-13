/**
 * Strike Desk V4.5 — Configuration Page
 * Read-only system config + operator-editable fields where backend supports it.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { system as sysApi } from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, StatusBadge, KV, LoadingBlock, PageHeader, ActionBtn, ErrorBlock, BackendRequired,
} from '../../components/ui';

const POLL = 60000;

const RISK_LIMITS = [
  { key: 'MAX_DAILY_LOSS',          label: 'Max Daily Loss',            unit: '₹' },
  { key: 'MAX_TRADE_COUNT',         label: 'Max Trades Per Day',        unit: ''  },
  { key: 'MAX_CONSECUTIVE_LOSSES',  label: 'Max Consecutive Losses',    unit: ''  },
  { key: 'COOLDOWN_MINUTES',        label: 'Cooldown After 2 Losses',   unit: 'min'},
  { key: 'POSITION_SIZING_LOTS',    label: 'Default Lot Size',          unit: 'lot'},
  { key: 'SL_PCT',                  label: 'Stop Loss %',               unit: '%' },
  { key: 'TARGET_PCT',              label: 'Target %',                  unit: '%' },
  { key: 'TRAILING_SL_PCT',         label: 'Trailing SL %',             unit: '%' },
];

export default function ConfigPage() {
  const { role } = useAuthStore();
  const [config, setConfig]   = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr]         = useState(null);

  const fetch = useCallback(async () => {
    try {
      const r = await sysApi.config();
      setConfig(r.data);
      setErr(null);
    } catch(e) {
      if (e.response?.status === 404) { setConfig(null); }
      else { setErr(e.response?.data?.detail || e.message); }
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetch(); const t = setInterval(fetch, POLL); return () => clearInterval(t); }, [fetch]);

  return (
    <div>
      <PageHeader
        title="Configuration"
        subtitle="System config. Read-only — edit via backend .env or operator API."
        right={<ActionBtn onClick={fetch} disabled={loading}>Refresh</ActionBtn>}
      />

      {role !== 'operator' && (
        <div className="mb-4 px-4 py-2 bg-amber-950/20 border border-amber-800/40 rounded text-[11px] font-mono text-amber-300">
          Read-only view — operator login required to see sensitive values.
        </div>
      )}

      {loading ? <LoadingBlock /> :
       err     ? <ErrorBlock label="CONFIG UNAVAILABLE" detail={err} /> :
       !config  ? <BackendRequired endpoint="GET /api/v1/system/config" /> : (
        <div className="space-y-4">
          {/* Risk limits */}
          <Panel>
            <PanelHeader title="RISK LIMITS" subtitle="Hard-coded in backend — not editable from UI" />
            <div className="p-4">
              {RISK_LIMITS.map(({ key, label, unit }) => {
                const val = config[key] ?? config?.risk?.[key] ?? config?.limits?.[key];
                return (
                  <KV key={key} label={label} value={
                    val != null
                      ? <span className="font-mono text-[#F5F7FA]">{unit}{val}</span>
                      : <span className="text-[#4A5568]">—</span>
                  } />
                );
              })}
            </div>
          </Panel>

          {/* Execution */}
          <Panel>
            <PanelHeader title="EXECUTION CONFIG" />
            <div className="p-4">
              {[
                ['EXECUTION_MODE',      config?.execution_mode      ?? config?.mode        ?? '—'],
                ['DEFAULT_BROKER',      config?.default_broker      ?? '—'],
                ['ORDER_TYPE',          config?.order_type          ?? '—'],
                ['PRODUCT_TYPE',        config?.product_type        ?? '—'],
                ['EXCHANGE',            config?.exchange            ?? '—'],
                ['MARKET_OPEN',         config?.market_open         ?? '09:15'],
                ['MARKET_CLOSE',        config?.market_close        ?? '15:30'],
                ['EOD_SQUARE_OFF',      config?.eod_square_off      ?? '15:10'],
              ].map(([k, v]) => (
                <KV key={k} label={k} value={<span className="font-mono text-[#8D98A8]">{String(v)}</span>} />
              ))}
            </div>
          </Panel>

          {/* Strategy */}
          <Panel>
            <PanelHeader title="STRATEGY CONFIG" />
            <div className="p-4">
              {[
                ['ACTIVE_MODE',         config?.active_mode         ?? config?.strategy_mode ?? '—'],
                ['OPTION_STRIKE_SEL',   config?.option_strike_selection ?? 'ATM+1 CE / ATM-1 PE'],
                ['EXPIRY_TYPE',         config?.expiry_type         ?? 'WEEKLY'],
                ['PCR_STALE_LIMIT_S',   config?.pcr_stale_limit_seconds ?? '—'],
                ['INDICATOR_FAST_POLL', config?.indicator_fast_poll ?? '—'],
                ['SIGNAL_TTL_S',        config?.signal_ttl_seconds  ?? '—'],
                ['QUEUE_MAX',           config?.queue_max           ?? '—'],
              ].map(([k, v]) => (
                <KV key={k} label={k} value={<span className="font-mono text-[#8D98A8]">{String(v)}</span>} />
              ))}
            </div>
          </Panel>

          {/* Raw dump for operator */}
          {role === 'operator' && (
            <Panel>
              <PanelHeader title="FULL CONFIG DUMP" subtitle="Operator only" />
              <pre className="p-4 text-[10px] font-mono text-[#4A5568] overflow-x-auto whitespace-pre-wrap break-all">
                {JSON.stringify(config, null, 2)}
              </pre>
            </Panel>
          )}

          <div className="px-1 text-[10px] font-mono text-[#4A5568]">
            Config is authoritative from the backend .env file. This view is read-only.
            Changes require a server restart. JWT secrets and API keys are never shown.
          </div>
        </div>
      )}
    </div>
  );
}
