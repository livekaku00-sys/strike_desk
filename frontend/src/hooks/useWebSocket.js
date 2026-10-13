import { useState, useEffect } from 'react';
import webSocketService from '../services/websocket';

export const useWebSocket = (channels = ['ticks', 'positions', 'orders', 'risk', 'signals']) => {
  const [isConnected, setIsConnected] = useState(webSocketService.isConnected);
  const [lastMessage, setLastMessage] = useState(null);
  const [channelData, setChannelData] = useState({});

  useEffect(() => {
    webSocketService.connect();

    const unsubConn = webSocketService.subscribe('connection', (statusData) => {
      setIsConnected(statusData.status === 'CONNECTED');
    });

    const unsubs = channels.map((ch) =>
      webSocketService.subscribe(ch, (data) => {
        setLastMessage({ channel: ch, data, timestamp: new Date().toISOString() });
        setChannelData((prev) => ({ ...prev, [ch]: data }));
      })
    );

    return () => {
      unsubConn();
      unsubs.forEach((unsub) => unsub());
    };
  }, [channels]);

  return {
    isConnected,
    lastMessage,
    channelData,
    send: (data) => webSocketService.send(data),
    subscribe: (ch, cb) => webSocketService.subscribe(ch, cb),
  };
};

export default useWebSocket;
