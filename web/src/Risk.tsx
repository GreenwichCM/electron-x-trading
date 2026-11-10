import { useState } from "react";
import { api, type User } from "./auth";
import { usePolling } from "./usePolling";

type Pnl = { realized: number; unrealized: number; total: number };

/** Top-level realized/unrealized/total are since riskmonitor's first fill; `today` is this trading day. */
type PositionRisk = Pnl & {
  qty: number;
  avg_px: number;
  mark: number | null;
  mark_source: string;
  mark_stale: boolean;
  mark_age_s: number | null;
  expired: boolean; // no longer trades: kept at its last mark until settlement
  settled: boolean; // closed at the exchange's final settlement price
  sod_qty: number; // held when the trading day began
  sod_mark: number | null;
  sod_mark_late: boolean; // not taken at the roll (riskmonitor was down, or no mark yet)
  today: Pnl;
};

type Usage = { used: number; limit: number; pct: number; symbol?: string };

type TraderRisk = Pnl & {
  positions: Record<string, PositionRisk>;
  today?: Pnl;
  limits?: { loss: Usage | null; position: Usage | null }; // null: no limit set in SQL Server
  blocked: { reason: string; by: string; time: string } | null;
};

type RiskResponse = {
  time: string;
  trading_day?: { day: string; starts_at: string; tz: string } | null;
  traders: Record<string, TraderRisk>;
  desk: (Pnl & { today?: Pnl; positions: Record<string, number> }) | null;
};

const ZERO: Pnl = { realized: 0, unrealized: 0, total: 0 };
const money = (v: number) => (v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const age = (s: number | null) => (s == null ? "" : s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);
const signed = (q: number) => (q > 0 ? `+${q}` : `${q}`);
const startTime = (iso: string) => iso.slice(11, 16); // "2026-11-10T00:00:00-05:00" -> "00:00"

function PnlCell({ value, title }: { value: number; title?: string }) {
  return (
    <td className={`num ${value > 0 ? "buy" : value < 0 ? "sell" : ""}`} title={title}>
      {money(value)}
    </td>
  );
}

const split = (p: Pnl) => `realized ${money(p.realized)}, unrealized ${money(p.unrealized)}`;

/** A limit usage bar: green under 80%, amber to 100%, red at or over the limit. */
function UsageBar({ usage }: { usage?: Usage | null }) {
  if (usage === undefined) return <td />; // no positions yet
  if (usage === null) return <td className="qty">no limit</td>;
  const level = usage.pct >= 100 ? "over" : usage.pct >= 80 ? "near" : "ok";
  return (
    <td title={`${usage.used} of ${usage.limit}${usage.symbol ? ` in ${usage.symbol}` : ""}`}>
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

function MarkFrom({ p }: { p: PositionRisk }) {
  if (p.settled) {
    return (
      <td className="qty" title="Closed at the exchange's final settlement price; the P&L is realized">
        settled
      </td>
    );
  }
  if (p.expired && p.mark_source === "indicative settlement") {
    return (
      <td className="stale-mark" title="Trading has ended; the exchange has sent an indicative settlement price">
        expired, indicative settlement
      </td>
    );
  }
  if (p.expired) {
    return (
      <td className="stale-mark" title="Trading in this contract has ended; it keeps its last mark until settlement">
        expired, awaiting settlement
      </td>
    );
  }
  return (
    <td className={p.mark_stale ? "stale-mark" : "qty"}>
      {p.mark_stale ? "stale: " : ""}
      {p.mark_source}
      {p.mark_age_s != null && `, ${age(p.mark_age_s)} ago`}
    </td>
  );
}

function StartOfDay({ p }: { p: PositionRisk }) {
  if (!p.sod_qty) return <td className="qty">flat</td>;
  const price = p.sod_mark == null ? "no mark yet" : `@ ${p.sod_mark.toFixed(2)}`;
  return p.sod_mark_late ? (
    <td className="stale-mark" title="Not taken when the day began (riskmonitor was down, or there was no mark yet)">
      {signed(p.sod_qty)} {price}, late
    </td>
  ) : (
    <td className="num">
      {signed(p.sod_qty)} {price}
    </td>
  );
}

/** Today's P&L, limit usage and marks. Risk and management see every trader; a trader sees their own
 *  row and, if allowed, the desk's totals. */
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
  const day = data.trading_day;

  return (
    <section className="risk">
      <h2>{seesAll ? "Risk by trader" : "My risk"}</h2>
      {day && (
        <p className="day">
          Trading day {day.day}, from {startTime(day.starts_at)} {day.tz}. The loss limit is for the day.
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Trader</th>
            <th className="num">Realized today</th>
            <th className="num">Unrealized today</th>
            <th className="num">P&amp;L today</th>
            <th className="num">P&amp;L since start</th>
            <th>Daily loss limit</th>
            <th>Position limit</th>
            <th>Status</th>
            {canBlock && <th />}
          </tr>
        </thead>
        <tbody>
          {traders.map((t) => {
            const r = data.traders[t];
            const today = r.today ?? ZERO;
            return (
              <tr key={t} className={r.blocked ? "blocked" : ""}>
                <td>{t}</td>
                <PnlCell value={today.realized} />
                <PnlCell value={today.unrealized} />
                <PnlCell value={today.total} />
                <PnlCell value={r.total} title={split(r)} />
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
              <td colSpan={canBlock ? 9 : 8} className="empty">
                No positions yet.
              </td>
            </tr>
          )}
          {data.desk && (
            <tr className="desk">
              <td>Desk</td>
              <PnlCell value={(data.desk.today ?? ZERO).realized} />
              <PnlCell value={(data.desk.today ?? ZERO).unrealized} />
              <PnlCell value={(data.desk.today ?? ZERO).total} />
              <PnlCell value={data.desk.total} title={split(data.desk)} />
              <td colSpan={canBlock ? 4 : 3} className="qty">
                {Object.entries(data.desk.positions)
                  .filter(([, q]) => q !== 0)
                  .map(([s, q]) => `${s} ${signed(q)}`)
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
                <th>Start of day</th>
                <th className="num">P&amp;L today</th>
                <th className="num">P&amp;L since start</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ trader, symbol, p }) => (
                <tr key={`${trader}-${symbol}`}>
                  {seesAll && <td>{trader}</td>}
                  <td className="symbol">{symbol}</td>
                  <td className="num">{p.settled ? `${signed(p.qty)} → 0` : signed(p.qty)}</td>
                  <td className="num">{p.qty ? p.avg_px.toFixed(2) : ""}</td>
                  <td className="num">{p.mark == null ? "–" : p.mark.toFixed(2)}</td>
                  <MarkFrom p={p} />
                  <StartOfDay p={p} />
                  <PnlCell value={p.today.total} title={split(p.today)} />
                  <PnlCell value={p.total} title={split(p)} />
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
