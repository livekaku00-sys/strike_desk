// Outcome of the most recent request for a piece of backend state. The dashboard
// shows these explicitly instead of falling back to a value that looks like real
// state — a failed request must never read as PAPER, LIVE, CONNECTED, etc.
export const REQ = Object.freeze({
  LOADING: 'LOADING',
  OK: 'OK',
  API_ERROR: 'API_ERROR',
  TIMEOUT: 'TIMEOUT',
  UNAVAILABLE: 'UNAVAILABLE',
});

// apiClient (axios, 15s timeout) failure -> request status.
export function statusFromAxiosError(err) {
  if (err?.code === 'ECONNABORTED' || err?.code === 'ETIMEDOUT' || /timeout/i.test(err?.message || '')) {
    return REQ.TIMEOUT;
  }
  if (err?.response) return REQ.API_ERROR;
  return REQ.UNAVAILABLE; // no response at all: server unreachable or request blocked
}

// fetch() rejection (see fetchJson) -> request status.
export function statusFromFetchFailure(reason) {
  if (reason?.name === 'AbortError') return REQ.TIMEOUT;
  if (reason?.name === 'TypeError') return REQ.UNAVAILABLE; // network failure
  return REQ.API_ERROR; // non-2xx response or unparseable body
}

// fetch() that rejects on non-2xx, so an error body is never mistaken for state.
export function fetchJson(url, options) {
  return fetch(url, options).then((r) => {
    if (!r.ok) {
      const e = new Error(`HTTP ${r.status}`);
      e.name = 'HttpError';
      throw e;
    }
    return r.json();
  });
}
