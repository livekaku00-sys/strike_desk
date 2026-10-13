import { REQ } from './requestState';

// Every operational indicator is a (text, tone) pair. The tone only picks a
// colour; the text alone must carry the meaning (WCAG 1.4.1).

const REQ_TEXT = {
  [REQ.LOADING]: 'LOADING…',
  [REQ.API_ERROR]: 'API ERROR',
  [REQ.TIMEOUT]: 'TIMEOUT',
  [REQ.UNAVAILABLE]: 'UNAVAILABLE',
};

export function requestStatusText(status) {
  return status === REQ.OK ? 'OK' : (REQ_TEXT[status] || 'UNKNOWN');
}

const errorTone = (status) => (status === REQ.LOADING ? 'muted' : 'error');

// Execution mode comes only from GET /trading/execution-mode. Anything other than
// a successful read of exactly PAPER or LIVE is shown as not known.
export function executionModeView(execMode) {
  const { status, value } = execMode || { status: REQ.LOADING };
  if (status !== REQ.OK) {
    const s = requestStatusText(status);
    return { mode: null, tone: errorTone(status), text: `EXECUTION MODE: ${s}`, aria: `Execution mode: ${s.toLowerCase()}` };
  }
  if (value === 'PAPER') {
    return { mode: 'PAPER', tone: 'paper', text: 'PAPER — VIRTUAL MONEY', aria: 'Execution mode: Paper, virtual money' };
  }
  if (value === 'LIVE') {
    return { mode: 'LIVE', tone: 'live', text: 'LIVE — REAL MONEY', aria: 'Execution mode: Live, real money' };
  }
  return { mode: null, tone: 'error', text: 'EXECUTION MODE: UNRECOGNISED', aria: 'Execution mode: unrecognised value' };
}

// The active execution class runs in whatever mode the backend reports; it says
// LIVE EXECUTION only when the backend positively reports LIVE.
export function activeClassExecutionText(execMode) {
  const { mode } = executionModeView(execMode);
  if (mode === 'LIVE') return 'LIVE EXECUTION';
  if (mode === 'PAPER') return 'PAPER EXECUTION';
  return 'EXECUTION MODE UNKNOWN';
}

// Broker token/feed status from GET /brokers/tokens/status. The backend already
// downgrades ACTIVE to ERROR when the adapter's feed is down.
const BROKER_STATUS = {
  ACTIVE: { text: 'CONNECTED', tone: 'ok' },
  EXPIRING: { text: 'CONNECTED · TOKEN EXPIRING', tone: 'warn' },
  EXPIRED: { text: 'TOKEN EXPIRED', tone: 'error' },
  ERROR: { text: 'DISCONNECTED', tone: 'error' },
  UNAUTHENTICATED: { text: 'AUTHENTICATION REQUIRED', tone: 'muted' },
};

export function brokerConnectionView(status) {
  return BROKER_STATUS[status] || { text: 'STATUS UNKNOWN', tone: 'muted' };
}

const BROKER_NAMES = { angelone: 'AngelOne', dhan: 'Dhan', fyers: 'Fyers' };
export const brokerDisplayName = (id) => BROKER_NAMES[id] || id || '—';

// Badge for a panel fed by one API call: says whether the call worked, nothing more.
export function apiBadgeView(status) {
  if (status === REQ.OK) return { text: 'API OK', tone: 'ok' };
  return { text: requestStatusText(status), tone: errorTone(status) };
}

export function formatClock(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}
