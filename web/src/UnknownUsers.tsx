import { usePolling } from "./usePolling";

type Unknown = { exchange_user: string; orders: number; last: string };

/** For risk: people trading on the ElectronX screen whose ElectronX login is not a user here. Their orders
 *  are recorded without a trader and count towards nobody's limits until they are added as a user. */
export default function UnknownUsers() {
  const unknown = usePolling<Unknown[]>("/api/exchange-users/unknown", 10000);
  if (!unknown || unknown.length === 0) return null;
  return (
    <section className="unknown-users" role="alert">
      <h2>ElectronX users with no trader here</h2>
      <p>
        Their orders on the ElectronX screen are recorded without a trader and count towards nobody's limits. Add
        them as a user on the Trading rules page (their ElectronX login is their id); their past orders are then
        filled in too.
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
