import { useEffect, useRef, useState } from "react";
import { type Status, useLiveSocket } from "./useLiveSocket";

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
  state?: string; // OPEN, EXPIRED, ...
  settlement?: { price: number; final: boolean; flag?: string; time?: string } | null;
  marks?: { dam?: { price: number; time: string } } | null; // the exchange's day-ahead price (Snapshot API)
  time: string;
};

/** Live books for the symbols in `watch`, from the api's /ws/md WebSocket. The server sends only
 *  those, so the screen can show a few contracts out of thousands. Updates are applied once per screen
 *  frame, so a burst of prices redraws the table once, not once per message. */
export function useMarketData(
  token: string,
  onUnauthorized: () => void,
  watch: string[],
): { books: Record<string, Book>; status: Status } {
  const [books, setBooks] = useState<Record<string, Book>>({});
  const pending = useRef<Record<string, Book>>({});
  const frame = useRef<number | null>(null);
  useEffect(() => () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
  }, []);
  const flush = () => {
    frame.current = null;
    const batch = pending.current;
    pending.current = {};
    setBooks((prev) => ({ ...prev, ...batch }));
  };
  const status = useLiveSocket(
    "/ws/md?watch=1",
    token,
    (msg) => {
      if (msg.type === "book") {
        const book = msg.data as Book;
        pending.current[book.symbol] = book; // only the newest per symbol is kept
        if (frame.current === null) frame.current = requestAnimationFrame(flush);
      }
    },
    onUnauthorized,
    // Prices we hold are no longer live: show them as stale until the server resends them.
    () => setBooks((prev) => Object.fromEntries(Object.entries(prev).map(([s, b]) => [s, { ...b, stale: true }]))),
    { type: "watch", symbols: watch },
  );
  return { books, status };
}
