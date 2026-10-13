import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';
import apiClient from '../api/client';
import StatusPill from '../components/StatusPill';
import { REQ, statusFromAxiosError } from '../utils/requestState';
import { brokerConnectionView, requestStatusText } from '../utils/stateLabels';

const BROKER_META = {
  angelone: { label: 'AngelOne',  icon: '🔄', note: 'SmartAPI · Auto-TOTP refresh, no token needed' },
  dhan:     { label: 'Dhan',      icon: '🔑', note: 'Paste access token from api.dhan.co' },
  fyers:    { label: 'Fyers',     icon: '🔑', note: 'Paste access token from myapi.fyers.in' },
};

export default function BrokerSetup() {
  const navigate   = useNavigate();
  const { role, setSelectedBroker } = useAuthStore();

  const [brokers, setBrokers]       = useState([]);
  const [selected, setSelected]     = useState(null);
  const [tokens, setTokens]         = useState({});       // { dhan: '...', fyers: '...' }
  const [submitting, setSubmitting] = useState({});       // { dhan: bool }
  const [results, setResults]       = useState({});       // { dhan: { ok, msg } }
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [statusError, setStatusError] = useState(null);   // REQ status when the read failed

  useEffect(() => {
    apiClient.get('/api/v1/brokers/tokens/status')
      .then(({ data }) => {
        setBrokers(data.brokers || []);
        setSelected(data.active_broker ?? null);
      })
      .catch((err) => {
        // No placeholder brokers: an invented ACTIVE list would read as real status.
        setBrokers([]);
        setStatusError(statusFromAxiosError(err));
      })
      .finally(() => setLoadingStatus(false));
  }, []);

  const submitToken = async (brokerName) => {
    const token = (tokens[brokerName] || '').trim();
    if (!token) return;
    setSubmitting((p) => ({ ...p, [brokerName]: true }));
    try {
      await apiClient.post(`/api/v1/brokers/${brokerName}/reauth`, { access_token: token });
      setResults((p) => ({ ...p, [brokerName]: { ok: true, msg: 'Authentication successful' } }));
      setTokens((p) => ({ ...p, [brokerName]: '' }));
    } catch (err) {
      const msg = err.response?.data?.message || err.response?.data?.detail || err.message || 'Auth failed';
      setResults((p) => ({ ...p, [brokerName]: { ok: false, msg } }));
    } finally {
      setSubmitting((p) => ({ ...p, [brokerName]: false }));
    }
  };

  const handleContinue = () => {
    if (selected) setSelectedBroker(selected);
    navigate('/dashboard');
  };

  return (
    <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-[520px] bg-slate-900 border border-slate-800 rounded-xl p-8 shadow-2xl">

        {/* Header */}
        <div className="mb-6">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xl">⚡</span>
            <h2 className="text-lg font-bold text-white">Broker Setup</h2>
            {role && (
              <span className={`ml-auto text-[10px] font-mono font-bold px-2 py-0.5 rounded uppercase ${
                role === 'guest'
                  ? 'bg-slate-800 text-slate-400 border border-slate-700'
                  : 'bg-amber-900/30 text-amber-400 border border-amber-700'
              }`}>
                {role}
              </span>
            )}
          </div>
          <p className="text-xs text-slate-400">
            Select your primary execution broker. Paste daily tokens for Dhan and Fyers before continuing.
          </p>
        </div>

        {loadingStatus ? (
          <div className="text-center py-8 text-slate-300 text-sm">Loading broker status…</div>
        ) : statusError ? (
          <div role="alert" className="text-center py-8 text-rose-300 text-sm font-mono">
            Broker status unavailable — {requestStatusText(statusError ?? REQ.API_ERROR)}
          </div>
        ) : (
          <div className="space-y-3 mb-6">
            {brokers.map((b) => {
              const meta      = BROKER_META[b.broker_name] || { label: b.display_name, icon: '🔌', note: '' };
              const isManual  = b.auth_type === 'MANUAL_REAUTH';
              const isSelected = selected === b.broker_name;
              const result    = results[b.broker_name];

              return (
                <div
                  key={b.broker_name}
                  className={`rounded-lg border transition-all duration-150 overflow-hidden ${
                    isSelected
                      ? 'border-amber-500/60 bg-amber-950/10'
                      : 'border-slate-800 bg-slate-950/60 hover:border-slate-700'
                  }`}
                >
                  {/* Broker row — click to select */}
                  <button
                    type="button"
                    onClick={() => setSelected(b.broker_name)}
                    className="w-full flex items-center justify-between px-4 py-3 text-left"
                  >
                    <div className="flex items-center gap-3">
                      <div aria-hidden="true" className={`h-2.5 w-2.5 rounded-full flex-shrink-0 ${
                        b.is_authenticated ? 'bg-emerald-400' : 'bg-red-400'
                      }`} />
                      <div>
                        <span className="text-sm font-semibold text-slate-100">{meta.label}</span>
                        <p className="text-[10px] text-slate-500 mt-0.5">{meta.note}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0 ml-3">
                      <StatusPill
                        className="text-[10px]"
                        tone={brokerConnectionView(b.status).tone}
                        text={brokerConnectionView(b.status).text}
                        ariaLabel={`${meta.label}: ${brokerConnectionView(b.status).text.toLowerCase()}`}
                      />
                      {isSelected && (
                        <span className="text-[9px] bg-amber-500/20 text-amber-400 border border-amber-500/40 px-1.5 py-0.5 rounded font-bold">
                          PRIMARY
                        </span>
                      )}
                    </div>
                  </button>

                  {/* Token input for manual-reauth brokers */}
                  {isManual && (
                    <div className="px-4 pb-3 border-t border-slate-800/60 pt-2.5">
                      {result && (
                        <div className={`text-[11px] mb-2 font-medium ${result.ok ? 'text-emerald-400' : 'text-red-400'}`}>
                          {result.ok ? '✓ ' : '✗ '}{result.msg}
                        </div>
                      )}
                      <div className="flex gap-2">
                        <input
                          type="password"
                          placeholder="Paste access token…"
                          value={tokens[b.broker_name] || ''}
                          onChange={(e) => setTokens((p) => ({ ...p, [b.broker_name]: e.target.value }))}
                          className="flex-1 bg-slate-900 border border-slate-700 rounded px-3 py-1.5 text-xs text-slate-100 placeholder-slate-600 focus:outline-none focus:border-amber-500 font-mono"
                        />
                        <button
                          type="button"
                          onClick={() => submitToken(b.broker_name)}
                          disabled={!tokens[b.broker_name]?.trim() || submitting[b.broker_name]}
                          className="px-3 py-1.5 bg-amber-500/20 text-amber-400 border border-amber-500/40 rounded text-xs font-bold hover:bg-amber-500/30 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                        >
                          {submitting[b.broker_name] ? '…' : 'Auth'}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        <button
          type="button"
          onClick={handleContinue}
          className="w-full py-3 bg-amber-500 hover:bg-amber-400 active:bg-amber-600 text-slate-950 font-bold rounded-lg text-sm transition-colors duration-150 shadow-lg"
        >
          Continue to Dashboard →
        </button>

        <p className="text-center text-[10px] text-slate-600 mt-4">
          You can re-authenticate brokers any time from the dashboard sidebar.
        </p>
      </div>
    </div>
  );
}
