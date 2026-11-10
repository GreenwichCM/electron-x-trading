import { useCallback, useEffect, useState } from "react";
import { api, clearToken, getToken, Unauthorized, type User } from "./auth";
import Blotter from "./Blotter";
import KillSwitch from "./KillSwitch";
import Login from "./Login";
import OrderTicket, { type TicketPreset } from "./OrderTicket";
import Positions from "./Positions";
import PriceBoard, { useBoard } from "./PriceBoard";
import Risk from "./Risk";
import Rules from "./Rules";
import { useMarketData } from "./useMarketData";
import { useOrders } from "./useOrders";

function Workspace({ user, token, onSignOut }: { user: User; token: string; onSignOut: () => void }) {
  const board = useBoard(user.id);
  const { books, status } = useMarketData(token, onSignOut, board.rows);
  const { orders } = useOrders(token, onSignOut);
  const [preset, setPreset] = useState<TicketPreset | null>(null);
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
          onPick={(symbol, side, price) => setPreset({ symbol, side, price, nonce: Date.now() })}
        />
        <OrderTicket symbols={board.rows} canTrade={canTrade} preset={preset} />
      </div>
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
