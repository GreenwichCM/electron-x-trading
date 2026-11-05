import { useState } from "react";
import { api, type User } from "./auth";
import { usePolling } from "./usePolling";

type PositionRisk = {
  qty: number;
  avg_px: number;
  mark: number | null;
  mark_source: string;
  mark_stale: boolean;
  mark_age_s: number | null;
  realized: number;
  unrealized: number;
};

type Usage = { used: number; limit: number; pct: number };

type TraderRisk = {
  positions: Record<string, PositionRisk>;
  realized: number;
  unrealized: number;
  total: number;
  limits?: { loss: Usage; position: Usage };
  blocked: { reason: string; by: string; time: string } | null;
};

type RiskResponse = {
  time: string;
  traders: Record<string, TraderRisk>;
  desk: { realized: number; unrealized: number; total: number; positions: Record<string, number> } | null;
};

const money = (v: number) => (v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const age = (s: number | null) => (s == null ? "" : s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

function Pnl({ value }: { value: number }) {
  return <td className={`num ${value > 0 ? "buy" : value < 0 ? "sell" : ""}`}>{money(value)}</td>;
}

/** A limit usage bar: green under 80%, amber to 100%, red at or over the limit. */
function UsageBar({ usage }: { usage?: Usage }) {
  if (!usage) return <td />;
  const level = usage.pct >= 100 ? "over" : usage.pct >= 80 ? "near" : "ok";
  return (
    <td title={`${usage.used} of ${usage.limit}`}>
      <div className="usage">
        <div className="bar">
          <div className={`fill ${level}`} style={{ width: `${Math.min(usage.pct, 100)}%` }} />
        </div>
        <span>{usage.pct.toFixed(0)}%</span>
      </div>
    </td>
  );
}

function BlockButton({ trader, blocked, onChange }: { trader: string; blocked: boolean; onChange: () => void }) {
  const [error, setError] = useState("");
  const toggle = async () => {
    const reason = blocked ? "" : `blocked by risk from the dashboard`;
    try {
      await api(`/api/risk/blocks/${encodeURIComponent(trader)}`, {
        method: "POST",
        body: JSON.stringify({ blocked: !blocked, reason }),
      });
      setError("");
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    }
  };
  return (
    <>
      <button className={`link ${blocked ? "unblock" : "block"}`} onClick={toggle}>
        {blocked ? "Unblock" : "Block"}
      </button>
      {error && <span className="error">{error}</span>}
    </>
  );
}

/** P&L, limit usage and marks. Risk and management see every trader; a trader sees their own row
 *  and, if allowed, the desk's totals. */
export default function Risk({ user }: { user: User }) {
  const [refresh, setRefresh] = useState(0);
  const data = usePolling<RiskResponse>("/api/risk", 1000, refresh);
  if (!data) return null;
  const canBlock = user.permissions.includes("killswitch.trip");
  const seesAll = user.permissions.includes("risk.view.all");
  const traders = Object.keys(data.traders).sort();
  const rows = traders.flatMap((t) =>
    Object.entries(data.traders[t].positions).map(([symbol, p]) => ({ trader: t, symbol, p })),
  );

  return (
    <section className="risk">
      <h2>{seesAll ? "Risk by trader" : "My risk"}</h2>
      <table>
        <thead>
          <tr>
            <th>Trader</th>
            <th className="num">Realized</th>
            <th className="num">Unrealized</th>
            <th className="num">Total P&amp;L</th>
            <th>Loss limit</th>
            <th>Largest position</th>
            <th>Status</th>
            {canBlock && <th />}
          </tr>
        </thead>
        <tbody>
          {traders.map((t) => {
            const r = data.traders[t];
            return (
              <tr key={t} className={r.blocked ? "blocked" : ""}>
                <td>{t}</td>
                <Pnl value={r.realized} />
                <Pnl value={r.unrealized} />
                <Pnl value={r.total} />
                <UsageBar usage={r.limits?.loss} />
                <UsageBar usage={r.limits?.position} />
                <td>
                  {r.blocked ? (
                    <span className="pill rejected" title={`${r.blocked.reason} (by ${r.blocked.by})`}>
                      blocked
                    </span>
                  ) : (
                    <span className="pill filled">trading</span>
                  )}
                  {r.blocked && <div className="reason">{r.blocked.reason}</div>}
                </td>
                {canBlock && (
                  <td>
                    <BlockButton trader={t} blocked={!!r.blocked} onChange={() => setRefresh((n) => n + 1)} />
                  </td>
                )}
              </tr>
            );
          })}
          {traders.length === 0 && (
            <tr>
              <td colSpan={canBlock ? 8 : 7} className="empty">
                No positions yet.
              </td>
            </tr>
          )}
          {data.desk && (
            <tr className="desk">
              <td>Desk</td>
              <Pnl value={data.desk.realized} />
              <Pnl value={data.desk.unrealized} />
              <Pnl value={data.desk.total} />
              <td colSpan={canBlock ? 4 : 3} className="qty">
                {Object.entries(data.desk.positions)
                  .filter(([, q]) => q !== 0)
                  .map(([s, q]) => `${s} ${q > 0 ? "+" : ""}${q}`)
                  .join(" · ") || "flat"}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {rows.length > 0 && (
        <>
          <h2 className="sub">Positions and marks</h2>
          <table>
            <thead>
              <tr>
                {seesAll && <th>Trader</th>}
                <th>Symbol</th>
                <th className="num">Position</th>
                <th className="num">Avg price</th>
                <th className="num">Mark</th>
                <th>Mark from</th>
                <th className="num">Unrealized</th>
                <th className="num">Realized</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ trader, symbol, p }) => (
                <tr key={`${trader}-${symbol}`}>
                  {seesAll && <td>{trader}</td>}
                  <td className="symbol">{symbol}</td>
                  <td className="num">{p.qty > 0 ? `+${p.qty}` : p.qty}</td>
                  <td className="num">{p.qty ? p.avg_px.toFixed(2) : ""}</td>
                  <td className="num">{p.mark == null ? "–" : p.mark.toFixed(2)}</td>
                  <td className={p.mark_stale ? "stale-mark" : "qty"}>
                    {p.mark_stale ? "stale: " : ""}
                    {p.mark_source}
                    {p.mark_age_s != null && `, ${age(p.mark_age_s)} ago`}
                  </td>
                  <Pnl value={p.unrealized} />
                  <Pnl value={p.realized} />
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
