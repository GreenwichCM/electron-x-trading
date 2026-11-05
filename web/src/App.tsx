import { useCallback, useEffect, useRef, useState } from "react";
import { api, clearToken, getToken, Unauthorized, type User } from "./auth";
import Login from "./Login";
import { type Book, useMarketData } from "./useMarketData";

const fmtPrice = (v: number | null | undefined) => (v == null ? "" : v.toFixed(2));
const fmtQty = (v: number | null | undefined) => (v == null ? "" : String(v));
const fmtTime = (iso: string | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "");

/** A price cell that flashes green when the value rises and red when it falls. */
function PriceCell({ value }: { value: number | null }) {
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
  return <td className={`num ${flash}`}>{fmtPrice(value)}</td>;
}

function Row({ book }: { book: Book }) {
  const trade = book.last_trade;
  return (
    <tr className={book.stale ? "stale" : ""}>
      <td className="symbol">{book.symbol}</td>
      <td className="num qty">{fmtQty(book.bid_qty)}</td>
      <PriceCell value={book.bid} />
      <PriceCell value={book.ask} />
      <td className="num qty">{fmtQty(book.ask_qty)}</td>
      <td className={`num ${trade?.aggressor === "BUY" ? "buy" : trade?.aggressor === "SELL" ? "sell" : ""}`}>
        {fmtPrice(trade?.price)}
      </td>
      <td className="num qty">{fmtQty(trade?.qty)}</td>
      <td className="time">{fmtTime(book.time)}</td>
    </tr>
  );
}

function Prices({ user, token, onSignOut }: { user: User; token: string; onSignOut: () => void }) {
  const { books, status } = useMarketData(token, onSignOut);
  const rows = Object.values(books).sort((a, b) => a.symbol.localeCompare(b.symbol));
  return (
    <main>
      <header>
        <h1>ElectronX prices</h1>
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
      <table>
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
            <Row key={book.symbol} book={book} />
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
  return <Prices user={user} token={token} onSignOut={signOut} />;
}
