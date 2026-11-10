import { usePolling } from "./usePolling";

type PositionsResponse = {
  exchange: Record<string, number>;
  screen?: Record<string, number>; // traded on the ElectronX screen: orders orderentry never sent
  screen_by_user?: Record<string, Record<string, number>>; // ElectronX user -> symbol -> lots
  pending?: Record<string, number>; // fills dropcopy cannot place yet: ours or the screen's in a moment
  settled?: Record<string, number>; // closed at their settlement price: left out of the check
  dropcopy_connected: boolean;
  exchange_time: string;
  by_trader: Record<string, Record<string, number>> | null;
};

const signed = (n: number) => (n > 0 ? `+${n}` : String(n));

/** Exchange positions (drop copy) next to orderentry's, for risk and management. Positions traded on
 *  the ElectronX screen are shown apart: the check compares the rest with orderentry. */
export default function Positions() {
  const data = usePolling<PositionsResponse>("/api/positions", 2000);
  if (!data) return null;

  const traders = Object.keys(data.by_trader ?? {}).sort();
  const ours: Record<string, number> = {};
  for (const t of traders) {
    for (const [symbol, qty] of Object.entries(data.by_trader![t])) ours[symbol] = (ours[symbol] ?? 0) + qty;
  }
  const screen = data.screen ?? {};
  const symbols = [...new Set([...Object.keys(data.exchange), ...Object.keys(ours), ...Object.keys(screen)])].sort();
  const settled = Object.keys(data.settled ?? {}).length;
  const screenUsers = (symbol: string) =>
    Object.entries(data.screen_by_user ?? {})
      .filter(([, held]) => held[symbol])
      .map(([user, held]) => `${user}: ${signed(held[symbol])}`)
      .join("\n");

  return (
    <section className="positions">
      <h2>Positions</h2>
      {!data.dropcopy_connected && <p className="error">Drop copy is not connected: exchange positions may be behind.</p>}
      {data.by_trader === null && <p className="error">orderentry is not answering.</p>}
      {settled > 0 && (
        <p className="note">
          {settled} settled contract{settled === 1 ? "" : "s"} not shown: both sides agree, and they are closed.
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="num">Exchange (drop copy)</th>
            <th className="num" title="Orders entered on the ElectronX screen, not sent from here">
              ElectronX screen
            </th>
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
            const fromScreen = screen[s] ?? 0;
            const mine = ours[s] ?? 0;
            const pending = data.pending?.[s] ?? 0;
            const match = data.by_trader === null || theirs - fromScreen - pending === mine;
            const check = pending ? (
              <span className="pill new" title="A new fill: dropcopy is finding out whose order it was">
                checking
              </span>
            ) : match ? (
              <span className="pill filled">match</span>
            ) : (
              <span className="pill rejected">break</span>
            );
            return (
              <tr key={s} className={match || pending ? "" : "break"}>
                <td className="symbol">{s}</td>
                <td className="num">{signed(theirs)}</td>
                <td className="num qty" title={screenUsers(s) || undefined}>
                  {fromScreen ? signed(fromScreen) : ""}
                </td>
                <td className="num">{data.by_trader === null ? "" : signed(mine)}</td>
                {traders.map((t) => (
                  <td key={t} className="num qty">
                    {signed(data.by_trader![t][s] ?? 0)}
                  </td>
                ))}
                <td>{check}</td>
              </tr>
            );
          })}
          {symbols.length === 0 && (
            <tr>
              <td colSpan={5 + traders.length} className="empty">
                No positions yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
