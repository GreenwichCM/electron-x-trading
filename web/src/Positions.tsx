import { usePolling } from "./usePolling";

type PositionsResponse = {
  exchange: Record<string, number>;
  dropcopy_connected: boolean;
  exchange_time: string;
  by_trader: Record<string, Record<string, number>> | null;
};

const signed = (n: number) => (n > 0 ? `+${n}` : String(n));

/** Exchange positions (drop copy) next to orderentry's, for risk and management. */
export default function Positions() {
  const data = usePolling<PositionsResponse>("/api/positions", 2000);
  if (!data) return null;

  const traders = Object.keys(data.by_trader ?? {}).sort();
  const ours: Record<string, number> = {};
  for (const t of traders) {
    for (const [symbol, qty] of Object.entries(data.by_trader![t])) ours[symbol] = (ours[symbol] ?? 0) + qty;
  }
  const symbols = [...new Set([...Object.keys(data.exchange), ...Object.keys(ours)])].sort();

  return (
    <section className="positions">
      <h2>Positions</h2>
      {!data.dropcopy_connected && <p className="error">Drop copy is not connected: exchange positions may be behind.</p>}
      {data.by_trader === null && <p className="error">orderentry is not answering.</p>}
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="num">Exchange (drop copy)</th>
            <th className="num">orderentry</th>
            {traders.map((t) => (
              <th key={t} className="num">
                {t}
              </th>
            ))}
            <th>Check</th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((s) => {
            const theirs = data.exchange[s] ?? 0;
            const mine = ours[s] ?? 0;
            const match = data.by_trader === null || theirs === mine;
            return (
              <tr key={s} className={match ? "" : "break"}>
                <td className="symbol">{s}</td>
                <td className="num">{signed(theirs)}</td>
                <td className="num">{data.by_trader === null ? "" : signed(mine)}</td>
                {traders.map((t) => (
                  <td key={t} className="num qty">
                    {signed(data.by_trader![t][s] ?? 0)}
                  </td>
                ))}
                <td>{match ? <span className="pill filled">match</span> : <span className="pill rejected">break</span>}</td>
              </tr>
            );
          })}
          {symbols.length === 0 && (
            <tr>
              <td colSpan={4 + traders.length} className="empty">
                No positions yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
