import { useCallback, useEffect, useRef, useState } from "react";
import { api, clearToken, getToken, Unauthorized, type User } from "./auth";
import Blotter from "./Blotter";
import KillSwitch from "./KillSwitch";
import Login from "./Login";
import OrderTicket, { type TicketPreset } from "./OrderTicket";
import Positions from "./Positions";
import { type Book, useMarketData } from "./useMarketData";
import { useOrders } from "./useOrders";

const fmtPrice = (v: number | null | undefined) => (v == null ? "" : v.toFixed(2));
const fmtQty = (v: number | null | undefined) => (v == null ? "" : String(v));
const fmtTime = (iso: string | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "");

/** A price cell that flashes green when the value rises and red when it falls. Clickable to trade. */
function PriceCell({ value, onClick, title }: { value: number | null; onClick?: () => void; title?: string }) {
  const previous = useRef(value);
  const [flash, setFlash] = useState("");
  useEffect(() => {
    const before = previous.current;
    previous.current = value;
    if (value == null || before == null || value === before) return;
    setFlash(value > before ? "up" : "down");
    const t = window.setTimeout(() => setFlash(""), 600);
    return () => window.clearTimeout(t);
  }, [value]);
  const clickable = onClick && value != null;
  return (
    <td
      className={`num ${flash} ${clickable ? "clickable" : ""}`}
      onClick={clickable ? onClick : undefined}
      title={clickable ? title : undefined}
    >
      {fmtPrice(value)}
    </td>
  );
}

function Row({ book, onPick }: { book: Book; onPick: (side: "BUY" | "SELL", price: number) => void }) {
  const trade = book.last_trade;
  return (
    <tr className={book.stale ? "stale" : ""}>
      <td className="symbol">{book.symbol}</td>
      <td className="num qty">{fmtQty(book.bid_qty)}</td>
      <PriceCell value={book.bid} onClick={() => onPick("SELL", book.bid!)} title="Sell at the bid" />
      <PriceCell value={book.ask} onClick={() => onPick("BUY", book.ask!)} title="Buy at the ask" />
      <td className="num qty">{fmtQty(book.ask_qty)}</td>
      <td className={`num ${trade?.aggressor === "BUY" ? "buy" : trade?.aggressor === "SELL" ? "sell" : ""}`}>
        {fmtPrice(trade?.price)}
      </td>
      <td className="num qty">{fmtQty(trade?.qty)}</td>
      <td className="time">{fmtTime(book.time)}</td>
    </tr>
  );
}

function Workspace({ user, token, onSignOut }: { user: User; token: string; onSignOut: () => void }) {
  const { books, status } = useMarketData(token, onSignOut);
  const { orders } = useOrders(token, onSignOut);
  const [preset, setPreset] = useState<TicketPreset | null>(null);
  const rows = Object.values(books).sort((a, b) => a.symbol.localeCompare(b.symbol));
  const orderList = Object.values(orders).sort((a, b) => b.created.localeCompare(a.created));
  const canTrade = user.permissions.includes("orders.trade");

  return (
    <main>
      <header>
        <h1>ElectronX trading</h1>
        <span className={`status ${status}`}>{status}</span>
        <div className="spacer" />
        <div className="user">
          <strong>{user.name}</strong>
          <span>{user.roles.join(", ")}</span>
        </div>
        <button className="link" onClick={onSignOut}>
          Sign out
        </button>
      </header>
      <KillSwitch user={user} />
      <div className="workspace">
        <table className="prices">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="num">Bid qty</th>
              <th className="num">Bid</th>
              <th className="num">Ask</th>
              <th className="num">Ask qty</th>
              <th className="num">Last</th>
              <th className="num">Last qty</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((book) => (
              <Row
                key={book.symbol}
                book={book}
                onPick={(side, price) => setPreset({ symbol: book.symbol, side, price, nonce: Date.now() })}
              />
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={8} className="empty">
                  Waiting for market data…
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <OrderTicket symbols={rows.map((b) => b.symbol)} canTrade={canTrade} preset={preset} />
      </div>
      <Blotter orders={orderList} user={user} />
      {user.permissions.includes("risk.view.all") && <Positions />}
      <section className="permissions">
        <h2>Your permissions</h2>
        <div className="chips">
          {user.permissions.map((p) => (
            <code key={p}>{p}</code>
          ))}
        </div>
      </section>
    </main>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);

  // A token left in this tab from before: check it is still valid.
  useEffect(() => {
    if (!getToken()) {
      setChecking(false);
      return;
    }
    api<User>("/api/me")
      .then(setUser)
      .catch((e) => {
        if (e instanceof Unauthorized) clearToken();
      })
      .finally(() => setChecking(false));
  }, []);

  const signOut = useCallback(() => {
    clearToken();
    setUser(null);
  }, []);

  if (checking) return null;
  const token = getToken();
  if (!user || !token) return <Login onLogin={setUser} />;
  return <Workspace user={user} token={token} onSignOut={signOut} />;
}
