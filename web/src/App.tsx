import { useCallback, useEffect, useMemo, useState } from "react";
import { api, clearToken, getToken, Unauthorized, type User } from "./auth";
import Blotter from "./Blotter";
import DepthLadder from "./DepthLadder";
import KillSwitch from "./KillSwitch";
import Login from "./Login";
import OrderTicket, { type TicketPreset } from "./OrderTicket";
import Positions from "./Positions";
import PriceBoard, { type Working } from "./PriceBoard";
import Risk from "./Risk";
import Rules from "./Rules";
import UnknownUsers from "./UnknownUsers";
import { useBoard } from "./useBoard";
import { useMarketData } from "./useMarketData";
import { LIVE, useOrders } from "./useOrders";
import { usePolling } from "./usePolling";

type RiskPositions = {
  traders: Record<string, { positions: Record<string, { qty: number }> }>;
  desk: { positions: Record<string, number> } | null;
};

function Workspace({ user, token, onSignOut }: { user: User; token: string; onSignOut: () => void }) {
  const { orders } = useOrders(token, onSignOut);
  const risk = usePolling<RiskPositions>("/api/risk", 5000);
  const seesAll = user.permissions.includes("risk.view.all");

  // Positions and working orders per contract: the desk's for risk and management, otherwise the user's own.
  const positions = useMemo(() => {
    if (!risk) return {};
    if (seesAll && risk.desk) return risk.desk.positions;
    const out: Record<string, number> = {};
    for (const t of Object.values(risk.traders)) {
      for (const [symbol, p] of Object.entries(t.positions)) out[symbol] = (out[symbol] ?? 0) + p.qty;
    }
    return out;
  }, [risk, seesAll]);
  const working = useMemo(() => {
    const out: Record<string, Working> = {};
    for (const o of Object.values(orders)) {
      if (!LIVE.includes(o.status) || !o.leaves) continue;
      const w = (out[o.symbol] ??= { buy: 0, sell: 0 });
      if (o.side === "BUY") w.buy += o.leaves;
      else w.sell += o.leaves;
    }
    return out;
  }, [orders]);
  const mine = useMemo(
    () => new Set([...Object.keys(positions).filter((s) => positions[s]), ...Object.keys(working)]),
    [positions, working],
  );

  const board = useBoard(user.id, mine);
  const [selected, setSelected] = useState("");
  const watched = useMemo(
    () => (selected && !board.watched.includes(selected) ? [...board.watched, selected] : board.watched),
    [board.watched, selected],
  );
  const { books, status } = useMarketData(token, onSignOut, watched);
  const [preset, setPreset] = useState<TicketPreset | null>(null);
  const pick = (symbol: string, side: "BUY" | "SELL", price: number) => {
    setSelected(symbol);
    setPreset({ symbol, side, price, nonce: Date.now() });
  };
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
        <PriceBoard
          board={board}
          books={books}
          positions={positions}
          working={working}
          selected={selected}
          onPick={pick}
          onSelect={setSelected}
        />
        <div className="side-panel">
          <OrderTicket
            symbols={board.watched}
            canTrade={canTrade}
            preset={preset}
            instruments={board.instruments}
            books={books}
          />
          <DepthLadder
            symbol={selected}
            book={books[selected]}
            inst={board.instruments.get(selected)}
            onPick={(side, price) => pick(selected, side, price)}
          />
        </div>
      </div>
      {user.permissions.includes("risk.view.all") && <UnknownUsers />}
      <Blotter orders={orderList} user={user} />
      {(user.permissions.includes("risk.view.own") || user.permissions.includes("risk.view.all")) && (
        <Risk user={user} />
      )}
      {user.permissions.includes("risk.view.all") && <Positions />}
      {(user.permissions.includes("risk.view.all") || user.permissions.includes("orders.trade")) && <Rules user={user} />}
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
