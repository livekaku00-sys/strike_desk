/**
 * Strike Desk V4 - Institutional WebSocket Real-Time Service
 * Manages subscriptions for: ticks, positions, orders, risk, signals.
 * Implements exponential backoff auto-reconnect and state broadcast.
 */

class WebSocketService {
  constructor() {
    this.ws = null;
    this.subscribers = new Map();
    this.isConnected = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 10;
    this.baseDelay = 1000;
    this.channels = ['ticks', 'positions', 'orders', 'risk', 'signals'];
    this.heartbeatInterval = null;
  }

  getWebSocketUrl() {
    if (import.meta.env?.VITE_WS_URL) {
      return import.meta.env.VITE_WS_URL;
    }
    const host = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
    const protocol = typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Defaults to FastAPI port (8000 or 8765)
    return `${protocol}//${host}:8765/ws`;
  }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const url = this.getWebSocketUrl();
    try {
      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        this.isConnected = true;
        this.reconnectAttempts = 0;
        this.emit('connection', { status: 'CONNECTED', url });
        this.subscribeToChannels();
        this.startHeartbeat();
      };

      this.ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          const channel = payload.channel || payload.type?.toLowerCase() || 'general';
          this.emit(channel, payload.data || payload);
          this.emit('*', payload);
        } catch (err) {
          console.warn('[WS] Failed to parse message:', event.data);
        }
      };

      this.ws.onerror = () => {
        this.isConnected = false;
      };

      this.ws.onclose = () => {
        this.isConnected = false;
        this.cleanup();
        this.emit('connection', { status: 'DISCONNECTED' });
        this.scheduleReconnect();
      };
    } catch (err) {
      this.isConnected = false;
      this.scheduleReconnect();
    }
  }

  subscribeToChannels() {
    if (!this.isConnected || !this.ws) return;
    this.channels.forEach((channel) => {
      const msg = JSON.stringify({ action: 'subscribe', channel });
      this.ws.send(msg);
    });
  }

  scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      // Give up quietly. Emitting synthetic ticks here would put fabricated prices
      // on the dashboard that are indistinguishable from the live feed.
      this.isConnected = false;
      this.emit('status', { connected: false, reason: 'reconnect_attempts_exhausted' });
      return;
    }

    const delay = Math.min(this.baseDelay * 2 ** this.reconnectAttempts, 15000);
    this.reconnectAttempts += 1;
    setTimeout(() => {
      this.connect();
    }, delay);
  }

  startHeartbeat() {
    this.stopHeartbeat();
    this.heartbeatInterval = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ action: 'ping', timestamp: Date.now() }));
      }
    }, 15000);
  }

  stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  cleanup() {
    this.stopHeartbeat();
  }

  // Event Pub/Sub API
  subscribe(channel, callback) {
    if (!this.subscribers.has(channel)) {
      this.subscribers.set(channel, new Set());
    }
    this.subscribers.get(channel).add(callback);

    return () => {
      const subs = this.subscribers.get(channel);
      if (subs) {
        subs.delete(callback);
        if (subs.size === 0) this.subscribers.delete(channel);
      }
    };
  }

  emit(channel, data) {
    if (this.subscribers.has(channel)) {
      this.subscribers.get(channel).forEach((cb) => {
        try {
          cb(data);
        } catch (e) {
          console.error(`[WS Error in ${channel}]:`, e);
        }
      });
    }
  }

  send(data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(typeof data === 'string' ? data : JSON.stringify(data));
      return true;
    }
    return false;
  }
}

export const webSocketService = new WebSocketService();
export default webSocketService;
