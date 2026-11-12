import { useState } from "react";
import { api, type User } from "./auth";
import { usePolling } from "./usePolling";

type Key = { trader_id: string; market: string; product: string };
/** A person: their id is their ElectronX login (company email), the same id ElectronX knows them by. */
type UserRow = { user_id: string; name: string; active: boolean; note: string; roles: string[]; accounts: string[] };
type AccountRow = { account_id: string; name: string; capacity: string; active: boolean };
type LimitRow = Key & Record<LimitName, number | null>;
type RulesData = {
  users: UserRow[];
  accounts: AccountRow[];
  entitlements: Key[];
  limits: LimitRow[];
  can_edit: boolean;
  can_approve: boolean;
};
type Change = Key & {
  change_id: number;
  kind: Kind;
  action: "set" | "delete";
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  reason: string;
  status: string;
  requested_by: string;
  requested_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string;
};
type Kind = "user" | "entitlement" | "limit";
type Value = string | boolean | string[];
type Draft = Key & { kind: Kind; action: "set" | "delete"; values: Record<string, Value>; reason: string };

const LIMITS = {
  max_order_qty: "Max lots per order",
  max_order_notional: "Max order value",
  max_open_orders: "Max open orders",
  max_position: "Max position per contract",
  max_loss: "Max loss (USD)",
} as const;
type LimitName = keyof typeof LIMITS;
const LIMIT_NAMES = Object.keys(LIMITS) as LimitName[];
const PER_TRADER: LimitName[] = ["max_open_orders", "max_loss"];
const MARKETS = ["*", "ERCOT", "PJM", "MISO", "CAISO", "SPP", "ISONE", "NYISO"];
const ROLES = ["trader", "risk", "management"];

const show = (v: unknown) =>
  Array.isArray(v)
    ? v.length
      ? v.join(", ")
      : "none"
    : v == null || v === ""
      ? "not set"
      : v === true
        ? "yes"
        : v === false
          ? "no"
          : String(v);
const when = (iso: string | null) => (iso ? new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleString() : "");
const scope = (k: Key) => (k.market === "*" && k.product === "*" ? "everything" : `${k.market} ${k.product}`);

function blank(kind: Kind): Draft {
  return { kind, action: "set", trader_id: "", market: "*", product: "*", values: {}, reason: "" };
}

/** What a change does, in a line: "dave · PJM BFUT100: max_position 5 → 8". */
function describe(c: Change): string {
  const who = c.trader_id === "*" ? "everyone" : c.trader_id;
  if (c.kind === "entitlement") return `${c.action === "delete" ? "stop" : "let"} ${who} trade ${scope(c)}`;
  if (c.after === null) return `remove ${c.kind} ${who}${c.kind === "limit" ? ` · ${scope(c)}` : ""}`;
  const keys = Object.keys(c.after).filter((k) => show(c.before?.[k]) !== show(c.after?.[k]));
  const diff = keys.map((k) => `${k} ${show(c.before?.[k])} → ${show(c.after?.[k])}`).join(", ");
  return `${c.before === null ? "add" : "change"} ${c.kind} ${who}${c.kind === "limit" ? ` · ${scope(c)}` : ""}: ${diff}`;
}

function ChangeForm({
  draft,
  setDraft,
  onSent,
  accounts,
}: {
  draft: Draft;
  setDraft: (d: Draft | null) => void;
  onSent: () => void;
  accounts: AccountRow[];
}) {
  const [error, setError] = useState("");
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });
  const value = (name: string, v: Value) => set({ values: { ...draft.values, [name]: v } });
  const list = (name: string) => (Array.isArray(draft.values[name]) ? (draft.values[name] as string[]) : []);
  const toggle = (name: string, item: string, on: boolean) =>
    value(name, on ? [...list(name), item] : list(name).filter((x) => x !== item));
  const allScope = draft.market === "*" && draft.product === "*";
  const submit = async () => {
    try {
      await api("/api/rules/changes", { method: "POST", body: JSON.stringify(draft) });
      setError("");
      onSent();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    }
  };
  return (
    <div className="change-form">
      <h3>
        Propose: {draft.action === "delete" ? "remove" : "set"} {draft.kind}
      </h3>
      <label>
        {draft.kind === "user" ? "ElectronX login (company email)" : "User"}
        <input
          value={draft.trader_id}
          placeholder={draft.kind === "user" ? "jane@company.com" : "their email, or * for everyone"}
          onChange={(e) => set({ trader_id: e.target.value })}
        />
      </label>
      {draft.kind !== "user" && (
        <>
          <label>
            Market
            <select value={draft.market} onChange={(e) => set({ market: e.target.value })}>
              {MARKETS.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
          </label>
          <label>
            Product
            <input value={draft.product} placeholder="* or BFUT500" onChange={(e) => set({ product: e.target.value })} />
          </label>
        </>
      )}
      {draft.action === "set" && draft.kind === "user" && (
        <>
          <label>
            Name
            <input value={String(draft.values.name ?? "")} onChange={(e) => value("name", e.target.value)} />
          </label>
          <fieldset className="choices">
            <legend>Roles</legend>
            {ROLES.map((r) => (
              <label key={r} className="check">
                <input type="checkbox" checked={list("roles").includes(r)} onChange={(e) => toggle("roles", r, e.target.checked)} />
                {r}
              </label>
            ))}
          </fieldset>
          <fieldset className="choices">
            <legend>ElectronX accounts they may trade on</legend>
            {accounts.map((a) => (
              <label key={a.account_id} className="check">
                <input
                  type="checkbox"
                  checked={list("accounts").includes(a.account_id)}
                  onChange={(e) => toggle("accounts", a.account_id, e.target.checked)}
                />
                {a.account_id}
                {a.name ? ` (${a.name})` : ""}
              </label>
            ))}
            {accounts.length === 0 && <span className="qty">none in electronx.accounts yet</span>}
          </fieldset>
          <label className="check">
            <input type="checkbox" checked={draft.values.active !== false} onChange={(e) => value("active", e.target.checked)} />
            Active (may sign in and trade)
          </label>
          <label>
            Note
            <input value={String(draft.values.note ?? "")} onChange={(e) => value("note", e.target.value)} />
          </label>
        </>
      )}
      {draft.action === "set" && draft.kind === "limit" && (
        <div className="limit-fields">
          {LIMIT_NAMES.filter((n) => allScope || !PER_TRADER.includes(n)).map((n) => (
            <label key={n}>
              {LIMITS[n]}
              <input inputMode="decimal" value={String(draft.values[n] ?? "")} placeholder="not set" onChange={(e) => value(n, e.target.value)} />
            </label>
          ))}
          {!allScope && <p className="hint">Max open orders and max loss are per trader: set them on a row for every market and product.</p>}
        </div>
      )}
      <label className="wide">
        Reason (kept with the change)
        <input value={draft.reason} onChange={(e) => set({ reason: e.target.value })} />
      </label>
      <div className="buttons">
        <button onClick={submit}>Propose</button>
        <button className="link" onClick={() => setDraft(null)}>
          Cancel
        </button>
        {error && <span className="error">{error}</span>}
      </div>
    </div>
  );
}

function Changes({ user, canApprove, refresh, onChange }: { user: User; canApprove: boolean; refresh: number; onChange: () => void }) {
  const changes = usePolling<Change[]>("/api/rules/changes", 3000, refresh);
  const [error, setError] = useState("");
  if (!changes) return null;
  const act = async (c: Change, verb: "approve" | "reject" | "withdraw") => {
    const note = verb === "reject" ? window.prompt(`Why reject change ${c.change_id}?`) ?? null : "";
    if (note === null) return;
    try {
      await api(`/api/rules/changes/${c.change_id}/${verb}`, { method: "POST", body: JSON.stringify({ note }) });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    }
    onChange();
  };
  const pending = changes.filter((c) => c.status === "pending");
  const decided = changes.filter((c) => c.status !== "pending").slice(0, 15);
  return (
    <>
      <h3>Waiting for approval</h3>
      {error && <p className="error">{error}</p>}
      <table>
        <tbody>
          {pending.map((c) => (
            <tr key={c.change_id}>
              <td className="qty">#{c.change_id}</td>
              <td>{describe(c)}</td>
              <td className="reason">{c.reason}</td>
              <td className="qty">
                {c.requested_by}, {when(c.requested_at)}
              </td>
              <td>
                {canApprove && c.requested_by !== user.id && (
                  <>
                    <button className="link unblock" onClick={() => act(c, "approve")}>
                      Approve
                    </button>
                    <button className="link block" onClick={() => act(c, "reject")}>
                      Reject
                    </button>
                  </>
                )}
                {c.requested_by === user.id && (
                  <>
                    <span className="qty">needs someone else </span>
                    <button className="link" onClick={() => act(c, "withdraw")}>
                      Withdraw
                    </button>
                  </>
                )}
              </td>
            </tr>
          ))}
          {pending.length === 0 && (
            <tr>
              <td className="empty">Nothing waiting.</td>
            </tr>
          )}
        </tbody>
      </table>
      <h3>Recent decisions</h3>
      <table>
        <tbody>
          {decided.map((c) => (
            <tr key={c.change_id}>
              <td className="qty">#{c.change_id}</td>
              <td>
                <span className={`pill ${c.status === "approved" ? "filled" : "rejected"}`}>{c.status}</span>
              </td>
              <td>{describe(c)}</td>
              <td className="qty">
                {c.requested_by} → {c.decided_by}, {when(c.decided_at)}
              </td>
              <td className="reason">{c.decision_note || c.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

/** The trading rules from SQL Server. Risk proposes changes and a second risk user approves them;
 *  management sees everything and the history; a trader sees the rules that apply to them. */
export default function Rules({ user }: { user: User }) {
  const [refresh, setRefresh] = useState(0);
  const [draft, setDraft] = useState<Draft | null>(null);
  const rules = usePolling<RulesData>("/api/rules", 5000, refresh);
  if (!rules) return null;
  const seesAll = user.permissions.includes("risk.view.all");
  const edit = rules.can_edit;
  const bump = () => setRefresh((n) => n + 1);
  const start = (d: Draft) => setDraft(d);

  return (
    <section className="rules">
      <h2>{seesAll ? "Trading rules" : "My trading rules"}</h2>
      <p className="hint">
        * means any. For each limit the most specific row wins; a limit no row sets blocks trading.
        {edit && " Changes take effect once a second person approves them."}
      </p>
      <table className="users">
        <thead>
          <tr>
            <th>User (ElectronX login)</th>
            <th>Name</th>
            <th>Roles</th>
            <th>Accounts</th>
            <th>Active</th>
            {edit && <th />}
          </tr>
        </thead>
        <tbody>
          {rules.users.map((u) => (
            <tr key={u.user_id} className={u.active ? "" : "stale"}>
              <td title={u.note}>{u.user_id}</td>
              <td>{u.name}</td>
              <td>{show(u.roles)}</td>
              <td className="qty">{show(u.accounts)}</td>
              <td>{u.active ? "yes" : "no"}</td>
              {edit && (
                <td>
                  <button
                    className="link"
                    onClick={() =>
                      start({
                        ...blank("user"),
                        trader_id: u.user_id,
                        values: { name: u.name, roles: u.roles, accounts: u.accounts, active: u.active, note: u.note },
                      })
                    }
                  >
                    Change
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="rules-grid">
        <table>
          <thead>
            <tr>
              <th>May trade</th>
              <th>Market</th>
              <th>Product</th>
              {edit && <th />}
            </tr>
          </thead>
          <tbody>
            {rules.entitlements.map((e) => (
              <tr key={`${e.trader_id}/${e.market}/${e.product}`}>
                <td>{e.trader_id}</td>
                <td>{e.market}</td>
                <td>{e.product}</td>
                {edit && (
                  <td>
                    <button className="link block" onClick={() => start({ ...blank("entitlement"), ...e, action: "delete" })}>
                      Remove
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <table className="limits">
        <thead>
          <tr>
            <th>Trader</th>
            <th>Market</th>
            <th>Product</th>
            {LIMIT_NAMES.map((n) => (
              <th key={n} className="num">
                {LIMITS[n]}
              </th>
            ))}
            {edit && <th />}
          </tr>
        </thead>
        <tbody>
          {rules.limits.map((r) => (
            <tr key={`${r.trader_id}/${r.market}/${r.product}`}>
              <td>{r.trader_id === "*" ? "* (desk default)" : r.trader_id}</td>
              <td>{r.market}</td>
              <td>{r.product}</td>
              {LIMIT_NAMES.map((n) => (
                <td key={n} className={`num ${r[n] == null ? "qty" : ""}`}>
                  {r[n] == null ? "–" : r[n]}
                </td>
              ))}
              {edit && (
                <td>
                  <button
                    className="link"
                    onClick={() =>
                      start({
                        ...blank("limit"),
                        trader_id: r.trader_id,
                        market: r.market,
                        product: r.product,
                        values: Object.fromEntries(LIMIT_NAMES.map((n) => [n, r[n] == null ? "" : String(r[n])])),
                      })
                    }
                  >
                    Change
                  </button>
                  <button className="link block" onClick={() => start({ ...blank("limit"), trader_id: r.trader_id, market: r.market, product: r.product, action: "delete" })}>
                    Remove
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {edit &&
        (draft ? (
          <ChangeForm
            draft={draft}
            accounts={rules.accounts}
            setDraft={setDraft}
            onSent={() => {
              setDraft(null);
              bump();
            }}
          />
        ) : (
          <div className="buttons">
            <span className="qty">Propose a new </span>
            <button className="link" onClick={() => start({ ...blank("user"), values: { roles: ["trader"], accounts: [] } })}>
              user
            </button>
            <button className="link" onClick={() => start(blank("entitlement"))}>
              entitlement
            </button>
            <button className="link" onClick={() => start(blank("limit"))}>
              limit
            </button>
          </div>
        ))}
      {seesAll && <Changes user={user} canApprove={rules.can_approve} refresh={refresh} onChange={bump} />}
    </section>
  );
}
