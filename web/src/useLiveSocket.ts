import { useEffect, useRef, useState } from "react";

export type Status = "connecting" | "live" | "disconnected";

const UNAUTHORIZED = 4401;

/** A WebSocket to the api that signs in with the first message and reconnects on its own.
 *  `onMessage` gets every message after the server's welcome. `outgoing`, if given, is sent after
 *  every welcome and again whenever it changes (for example, which symbols the screen shows). */
export function useLiveSocket(
  path: string,
  token: string,
  onMessage: (msg: { type: string; data: unknown }) => void,
  onUnauthorized: () => void,
  onDisconnect?: () => void,
  outgoing?: unknown,
): Status {
  const [status, setStatus] = useState<Status>("connecting");
  // Keep the latest callbacks without reconnecting every time the component re-renders.
  const handlers = useRef({ onMessage, onUnauthorized, onDisconnect });
  handlers.current = { onMessage, onUnauthorized, onDisconnect };
  const out = outgoing === undefined ? null : JSON.stringify(outgoing);
  const outRef = useRef(out);
  outRef.current = out;
  const socket = useRef<WebSocket | null>(null);
  const welcomed = useRef(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let closedByUs = false;

    const connect = () => {
      setStatus("connecting");
      welcomed.current = false;
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${scheme}://${window.location.host}${path}`);
      socket.current = ws;
      ws.onopen = () => ws?.send(JSON.stringify({ type: "auth", token }));
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "welcome") {
          retry = 0;
          welcomed.current = true;
          if (outRef.current !== null) ws?.send(outRef.current);
          setStatus("live");
        } else {
          handlers.current.onMessage(msg);
        }
      };
      ws.onclose = (event) => {
        welcomed.current = false;
        if (closedByUs) return;
        if (event.code === UNAUTHORIZED) {
          handlers.current.onUnauthorized();
          return;
        }
        setStatus("disconnected");
        handlers.current.onDisconnect?.();
        timer = window.setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
      };
    };

    connect();
    return () => {
      closedByUs = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [path, token]);

  useEffect(() => {
    const ws = socket.current;
    if (out !== null && ws && ws.readyState === WebSocket.OPEN && welcomed.current) ws.send(out);
  }, [out]);

  return status;
}
