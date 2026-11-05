import { useEffect, useState } from "react";

export type Trade = {
  price: number | null;
  qty: number;
  aggressor: "BUY" | "SELL" | "";
  trade_id: string;
  time: string;
};

export type Book = {
  symbol: string;
  stale: boolean;
  bid: number | null;
  bid_qty: number | null;
  ask: number | null;
  ask_qty: number | null;
  mid: number | null;
  bids: [number, number][];
  asks: [number, number][];
  last_trade: Trade | null;
  time: string;
};

export type Status = "connecting" | "live" | "disconnected";

/** Live books for every symbol, from the api's /ws/md WebSocket. Reconnects on its own. */
export function useMarketData(): { books: Record<string, Book>; status: Status } {
  const [books, setBooks] = useState<Record<string, Book>>({});
  const [status, setStatus] = useState<Status>("connecting");

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let closedByUs = false;

    const connect = () => {
      setStatus("connecting");
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${scheme}://${window.location.host}/ws/md`);
      ws.onopen = () => {
        retry = 0;
        setStatus("live");
      };
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "book") {
          setBooks((prev) => ({ ...prev, [msg.data.symbol]: msg.data as Book }));
        }
      };
      ws.onclose = () => {
        if (closedByUs) return;
        setStatus("disconnected");
        // Prices we hold are no longer live: show them as stale until the server resends them.
        setBooks((prev) => Object.fromEntries(Object.entries(prev).map(([s, b]) => [s, { ...b, stale: true }])));
        timer = window.setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
      };
    };

    connect();
    return () => {
      closedByUs = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, []);

  return { books, status };
}
