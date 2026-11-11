import { usePolling } from "./usePolling";

type Unknown = { exchange_user: string; orders: number; last: string };

/** For risk: people trading on the ElectronX screen whose ElectronX id no trader here has. Their orders
 *  are recorded without a trader and count towards nobody's limits until someone is given that id. */
export default function UnknownUsers() {
  const unknown = usePolling<Unknown[]>("/api/exchange-users/unknown", 10000);
  if (!unknown || unknown.length === 0) return null;
  return (
    <section className="unknown-users" role="alert">
      <h2>ElectronX users with no trader here</h2>
      <p>
        Their orders on the ElectronX screen are recorded without a trader and count towards nobody's limits. On the
        Trading rules page, give the right trader this ElectronX id; their past orders are then filled in too.
      </p>
      <ul>
        {unknown.map((u) => (
          <li key={u.exchange_user}>
            <code>{u.exchange_user || "(no ElectronX id sent)"}</code> · {u.orders} order{u.orders === 1 ? "" : "s"}
            {u.last ? ` · last ${new Date(u.last).toLocaleString()}` : ""}
          </li>
        ))}
      </ul>
    </section>
  );
}
