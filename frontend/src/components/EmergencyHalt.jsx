import React, { useState } from 'react';
import useEmergencyHalt from '../hooks/useEmergencyHalt';
import { useCanTrade } from '../store/authStore';

export const EmergencyHalt = () => {
  const canTrade = useCanTrade();
  const {
    isHalted,
    haltLevel,
    haltReason,
    loading,
    error,
    issuedToken,
    triggerHalt,
    requestToken,
    resetHalt,
  } = useEmergencyHalt();

  const [showHaltModal, setShowHaltModal] = useState(false);
  const [showResetModal, setShowResetModal] = useState(false);
  const [reasonInput, setReasonInput] = useState('Manual circuit breaker triggered by operator');
  const [levelInput, setLevelInput] = useState('HARD_HALT');
  const [tokenInput, setTokenInput] = useState('');
  const [notesInput, setNotesInput] = useState('Risk limits inspected and verified normal');

  const handleConfirmHalt = async () => {
    const res = await triggerHalt(reasonInput, levelInput);
    if (res.success) {
      setShowHaltModal(false);
    }
  };

  const handleRequestToken = async () => {
    const res = await requestToken('MONUJ');
    if (res.success && res.token) {
      setTokenInput(res.token);
    }
  };

  const handleConfirmReset = async () => {
    if (!tokenInput.trim()) return;
    const res = await resetHalt(tokenInput, notesInput);
    if (res.success) {
      setShowResetModal(false);
      setTokenInput('');
    }
  };

  return (
    <>
      {/* ------------------------------------------------------------- */}
      {/* Full-Screen Dashboard Overlay when Emergency Halt is Active   */}
      {/* ------------------------------------------------------------- */}
      {isHalted && (
        <div className="fixed top-0 left-0 w-full bg-rose-600/95 text-white z-50 py-2.5 px-4 shadow-2xl flex items-center justify-between animate-pulse">
          <div className="flex items-center gap-2 font-mono text-xs md:text-sm font-black tracking-wider">
            <span className="text-base">🚨</span>
            <span>EMERGENCY HALT ACTIVE [{haltLevel}]</span>
            <span className="hidden sm:inline font-normal text-rose-100 text-xs">— ALL ORDER EXECUTION IS BLOCKED</span>
          </div>
          {canTrade && (
            <button
              onClick={() => setShowResetModal(true)}
              className="px-3 py-1 bg-white text-rose-700 font-bold text-xs rounded hover:bg-rose-100 shadow transition font-mono uppercase"
            >
              Acknowledge & Reset
            </button>
          )}
        </div>
      )}

      {/* ------------------------------------------------------------- */}
      {/* Action Buttons (Footer Display)                               */}
      {/* ------------------------------------------------------------- */}
      <div className="flex items-center gap-2">
        {!canTrade ? (
          <span className="text-[10px] font-mono text-slate-500 uppercase tracking-wider">
            Read-only session
          </span>
        ) : isHalted ? (
          <button
            onClick={() => setShowResetModal(true)}
            className="px-3 py-1.5 rounded text-xs font-black uppercase font-mono tracking-wider bg-amber-500 hover:bg-amber-600 text-slate-950 transition shadow-md flex items-center gap-1.5"
          >
            <span>🔓</span> Acknowledge & Reset
          </button>
        ) : (
          <button
            onClick={() => setShowHaltModal(true)}
            className="px-3.5 py-1.5 rounded text-xs font-black uppercase font-mono tracking-wider bg-red-600 hover:bg-red-700 text-white transition shadow-lg flex items-center gap-1.5 border border-red-500"
          >
            <span>⚠️</span> Emergency Halt
          </button>
        )}
      </div>

      {/* ------------------------------------------------------------- */}
      {/* Modal 1: Trigger Halt Confirmation                            */}
      {/* ------------------------------------------------------------- */}
      {showHaltModal && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-[#11151F] border-2 border-red-500 rounded-xl p-6 max-w-md w-full shadow-2xl text-slate-100">
            <div className="flex items-center gap-2.5 text-red-500 mb-2">
              <span className="text-2xl">⚠️</span>
              <h3 className="text-lg font-black font-mono">CONFIRM EMERGENCY HALT</h3>
            </div>

            <div className="bg-red-950/40 border border-red-800/60 p-3 rounded text-xs text-red-200 mb-4 leading-relaxed font-mono">
              <b>CRITICAL:</b> This will block ALL orders immediately across all asset classes and engines.
            </div>

            {error && (
              <div className="mb-3 text-xs text-rose-400 bg-rose-950/50 p-2 rounded border border-rose-800">
                {error}
              </div>
            )}

            <div className="space-y-3 text-xs">
              <div>
                <label className="block text-slate-400 font-mono mb-1">Severity Level</label>
                <select
                  value={levelInput}
                  onChange={(e) => setLevelInput(e.target.value)}
                  className="w-full bg-[#161D2B] border border-slate-700 p-2 rounded text-slate-200 font-mono outline-none"
                >
                  <option value="HARD_HALT">HARD_HALT (Freeze all routing)</option>
                  <option value="SOFT_HALT">SOFT_HALT (Allow square-offs only)</option>
                </select>
              </div>

              <div>
                <label className="block text-slate-400 font-mono mb-1">Reason / Audit Trail Note</label>
                <input
                  type="text"
                  value={reasonInput}
                  onChange={(e) => setReasonInput(e.target.value)}
                  className="w-full bg-[#161D2B] border border-slate-700 p-2 rounded text-slate-200 outline-none"
                />
              </div>
            </div>

            <div className="mt-6 flex justify-end gap-2.5 font-mono text-xs">
              <button
                onClick={() => setShowHaltModal(false)}
                disabled={loading}
                className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 font-semibold"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmHalt}
                disabled={loading}
                className="px-4 py-2 rounded bg-red-600 hover:bg-red-700 text-white font-bold"
              >
                {loading ? 'HALTING...' : 'YES, ENGAGE HALT'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------- */}
      {/* Modal 2: Authenticated Token Reset Workflow                   */}
      {/* ------------------------------------------------------------- */}
      {showResetModal && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-[#11151F] border-2 border-sky-500 rounded-xl p-6 max-w-md w-full shadow-2xl text-slate-100">
            <div className="flex items-center gap-2.5 text-sky-400 mb-2">
              <span className="text-2xl">🛡️</span>
              <h3 className="text-lg font-black font-mono">AUTHENTICATE & RESET HALT</h3>
            </div>

            <p className="text-xs text-slate-400 mb-4 leading-relaxed font-mono">
              Requires valid operator acknowledgment token to resume normal trading operations.
            </p>

            {error && (
              <div className="mb-3 text-xs text-rose-400 bg-rose-950/50 p-2 rounded border border-rose-800">
                {error}
              </div>
            )}

            <div className="space-y-3.5 text-xs font-mono">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="text-slate-400">Operator Token</label>
                  <button
                    type="button"
                    onClick={handleRequestToken}
                    className="text-sky-400 hover:underline text-[11px]"
                  >
                    Request Token (Monuj)
                  </button>
                </div>
                <input
                  type="text"
                  placeholder="e.g. ACK-MONUJ-7729"
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  className="w-full bg-[#161D2B] border border-slate-700 p-2 rounded text-slate-200 outline-none font-mono"
                />
                {issuedToken && (
                  <div className="text-[10px] text-emerald-400 mt-1">Generated: {issuedToken}</div>
                )}
              </div>

              <div>
                <label className="block text-slate-400 mb-1">Resolution & Audit Notes</label>
                <input
                  type="text"
                  value={notesInput}
                  onChange={(e) => setNotesInput(e.target.value)}
                  className="w-full bg-[#161D2B] border border-slate-700 p-2 rounded text-slate-200 outline-none"
                />
              </div>
            </div>

            <div className="mt-6 flex justify-end gap-2.5 font-mono text-xs">
              <button
                onClick={() => setShowResetModal(false)}
                disabled={loading}
                className="px-4 py-2 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 font-semibold"
              >
                Close
              </button>
              <button
                onClick={handleConfirmReset}
                disabled={loading || !tokenInput.trim()}
                className="px-4 py-2 rounded bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white font-bold"
              >
                {loading ? 'RESETTING...' : 'VALIDATE & RESUME'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};

export default EmergencyHalt;
