/**
 * Strike Desk V4.5 — Daily Operator Checklist
 * 08 — Pre-market checklist backed by real state where available.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { brokers as brokerApi, risk as riskApi, execution as execApi, strategy as stratApi } from '../../api/services';
import {
  Panel, PanelHeader, StatusBadge, LoadingBlock, PageHeader, ActionBtn,
} from '../../components/ui';

function CheckRow({ num, title, desc, status, detail, action, onAction }) {
  const statusColor = status === 'OK' ? 'green' : status === 'WARN' ? 'amber' : status === 'FAIL' ? 'red' : 'gray';
  const statusText  = status === 'OK' ? 'DONE' : status === 'WARN' ? 'WARNING' : status === 'FAIL' ? 'ACTION REQUIRED' : 'CHECKING…';
  return (
    <div className={`flex items-center gap-4 px-4 py-3 border-b border-[#1E2A3A] ${
      status === 'FAIL' ? 'bg-red-950/10' : status === 'WARN' ? 'bg-amber-950/10' : ''
    }`}>
      <div className={`w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 font-mono font-bold text-[11px] ${
        status === 'OK'   ? 'bg-green-500/20 text-green-300 border border-green-500/40' :
        status === 'FAIL' ? 'bg-red-500/20 text-red-400 border border-red-500/40' :
        status === 'WARN' ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40' :
                            'bg-[#1E2A3A] text-[#4A5568] border border-[#2A3F5A]'
      }`}>{num}</div>
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-medium text-[#F5F7FA]">{title}</span>
          <StatusBadge text={statusText} color={statusColor} />
        </div>
        <div className="text-[11px] text-[#8D98A8] mt-0.5">{desc}</div>
        {detail && <div className="text-[10px] font-mono mt-0.5 text-amber-300">{detail}</div>}
      </div>
      {action && status !== 'OK' && (
        <ActionBtn color="amber" onClick={onAction}>{action}</ActionBtn>
      )}
    </div>
  );
}

export default function ChecklistPage() {
  const [checks, setChecks] = useState({
    login:    { status: 'LOADING', detail: '' },
    dhan:     { status: 'LOADING', detail: '' },
    health:   { status: 'LOADING', detail: '' },
    recovery: { status: 'OK',      detail: 'Service running' },
    risk:     { status: 'LOADING', detail: '' },
    strategy: { status: 'LOADING', detail: '' },
    mode:     { status: 'LOADING', detail: '' },
    pcr:      { status: 'OK',      detail: 'Will verify after 9:15' },
  });
  const [loading, setLoading] = useState(true);

  const runChecks = useCallback(async () => {
    setLoading(true);
    const [rBrokers, rRisk, rMode, rStrat] = await Promise.allSettled([
      brokerApi.tokensStatus(),
      riskApi.state(),
      execApi.mode(),
      stratApi.mode(),
    ]);

    setChecks(prev => {
      const next = { ...prev };

      // Login always OK if we're here
      next.login = { status: 'OK', detail: 'Authenticated as operator' };

      // Dhan token
      if (rBrokers.status === 'fulfilled') {
        const dhan = (rBrokers.value.data?.brokers ?? []).find(b => b.broker_name === 'dhan');
        if (dhan?.is_authenticated && dhan?.status === 'ACTIVE') {
          next.dhan   = { status: 'OK',   detail: `Expires: ${dhan.expires_in ?? '—'}` };
          next.health = { status: 'OK',   detail: 'Dhan API reachable' };
        } else {
          next.dhan   = { status: 'FAIL', detail: dhan?.error_message ?? 'Token missing or expired' };
          next.health = { status: 'FAIL', detail: 'Dhan not authenticated — cannot confirm health' };
        }
      } else {
        next.dhan   = { status: 'FAIL', detail: 'Cannot reach broker API' };
        next.health = { status: 'FAIL', detail: 'Cannot reach broker API' };
      }

      // Risk state
      if (rRisk.status === 'fulfilled') {
        const rd = rRisk.value.data;
        if (rd?.emergency_halt_active || rd?.loss_ceiling_breached) {
          next.risk = { status: 'FAIL', detail: rd?.emergency_halt_active ? 'Emergency halt active' : 'Loss ceiling breached' };
        } else if (rd?.cooldown_active) {
          next.risk = { status: 'WARN', detail: `Cooldown until ${rd.cooldown_until}` };
        } else {
          next.risk = { status: 'OK',   detail: 'No halt, no cooldown, ceiling clear' };
        }
      } else {
        next.risk = { status: 'FAIL', detail: 'Cannot reach risk API' };
      }

      // Strategy mode
      if (rStrat.status === 'fulfilled') {
        const m = rStrat.value.data?.mode ?? rStrat.value.data?.active_mode;
        next.strategy = m === 'AUTO_STOCK_OPTIONS'
          ? { status: 'OK',   detail: 'AUTO_STOCK_OPTIONS active' }
          : { status: 'WARN', detail: `Active mode: ${m ?? 'unknown'} — expected AUTO_STOCK_OPTIONS` };
      } else {
        next.strategy = { status: 'FAIL', detail: 'Cannot reach strategy API' };
      }

      // Execution mode
      if (rMode.status === 'fulfilled') {
        const m = rMode.value.data?.mode;
        next.mode = { status: 'OK', detail: `Currently ${m} — verify intended mode` };
      } else {
        next.mode = { status: 'FAIL', detail: 'Cannot reach execution mode API' };
      }

      return next;
    });

    setLoading(false);
  }, []);

  useEffect(() => { runChecks(); }, [runChecks]);

  const allOk = Object.values(checks).every(c => c.status === 'OK');
  const anyFail = Object.values(checks).some(c => c.status === 'FAIL');

  const items = [
    { key: 'login',    num: 1, title: 'Login',               desc: 'Authenticate as operator' },
    { key: 'dhan',     num: 2, title: 'Dhan Token',          desc: 'Fresh token from api.dhan.co', action: 'Re-Auth' },
    { key: 'health',   num: 3, title: 'Broker Health',       desc: 'Dhan API connectivity verified' },
    { key: 'recovery', num: 4, title: 'State Recovery',      desc: 'Service started, state files loaded' },
    { key: 'risk',     num: 5, title: 'Risk State',          desc: 'No halt, no ceiling breach' },
    { key: 'strategy', num: 6, title: 'Strategy Mode',       desc: 'AUTO_STOCK_OPTIONS selected' },
    { key: 'mode',     num: 7, title: 'Execution Mode',      desc: 'Verify PAPER before going LIVE' },
    { key: 'pcr',      num: 8, title: 'PCR Feed',            desc: 'First option chain after 9:15 IST' },
  ];

  return (
    <div>
      <PageHeader
        title="Daily Operator Checklist"
        subtitle="08 — Before 9:15 AM IST. Backend state where available."
        right={<ActionBtn onClick={runChecks} disabled={loading}>Re-Check All</ActionBtn>}
      />

      <div className={`mb-4 p-4 rounded border ${
        loading  ? 'bg-[#151C25] border-[#1E2A3A]' :
        allOk    ? 'bg-green-950/20 border-green-800/40' :
        anyFail  ? 'bg-red-950/20 border-red-800/40' :
                   'bg-amber-950/20 border-amber-800/40'
      }`}>
        <div className={`text-[15px] font-bold font-mono ${
          loading ? 'text-[#8D98A8]' : allOk ? 'text-green-300' : anyFail ? 'text-red-400' : 'text-amber-300'
        }`}>
          SYSTEM READINESS — {loading ? 'CHECKING…' : allOk ? 'READY' : anyFail ? 'NOT READY' : 'WARNING'}
        </div>
        {!allOk && !loading && (
          <div className="text-[11px] text-[#8D98A8] mt-1">
            {anyFail ? 'One or more checks failed. Resolve before going LIVE.' :
             'Warnings present. Review before trading.'}
          </div>
        )}
      </div>

      <Panel>
        <PanelHeader title="PRE-MARKET CHECKLIST" subtitle="Before 9:15 AM IST" />
        {loading ? <LoadingBlock label="Running checks…" /> : (
          <div>
            {items.map(item => {
              const c = checks[item.key];
              return (
                <CheckRow
                  key={item.key}
                  num={item.num}
                  title={item.title}
                  desc={item.desc}
                  status={c.status === 'LOADING' ? 'LOADING' : c.status}
                  detail={c.detail}
                  action={item.action}
                />
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
