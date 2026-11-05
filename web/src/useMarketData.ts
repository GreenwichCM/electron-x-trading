import { useState } from "react";
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
  time: string;
};

/** Live books for every symbol, from the api's /ws/md WebSocket. */
export function useMarketData(
  token: string,
  onUnauthorized: () => void,
): { books: Record<string, Book>; status: Status } {
  const [books, setBooks] = useState<Record<string, Book>>({});
  const status = useLiveSocket(
    "/ws/md",
    token,
    (msg) => {
      if (msg.type === "book") {
        const book = msg.data as Book;
        setBooks((prev) => ({ ...prev, [book.symbol]: book }));
      }
    },
    onUnauthorized,
    // Prices we hold are no longer live: show them as stale until the server resends them.
    () => setBooks((prev) => Object.fromEntries(Object.entries(prev).map(([s, b]) => [s, { ...b, stale: true }]))),
  );
  return { books, status };
}
