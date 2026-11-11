import { useEffect, useMemo, useState } from "react";
import {
  byScreen,
  type Column,
  DEFAULT_COLUMNS,
  DEFAULT_FILTERS,
  type Filters,
  hasEnded,
  type Instrument,
  isPeak,
  MAX_ROWS,
  marketStatus,
  matchesSearch,
  type Sort,
  tradingEnds,
} from "./board";
import { type Contract, parseSymbol, unknownContract } from "./symbols";
import { usePolling } from "./usePolling";

export type View = "list" | "strip";
type Saved = { filters: Filters; columns: Column[]; view: View; sort: Sort };
type Stored = Saved & { views: Record<string, Saved> };

const DEFAULTS: Stored = {
  filters: DEFAULT_FILTERS,
  columns: DEFAULT_COLUMNS,
  view: "list",
  sort: { key: "screen", desc: false },
  views: {},
};

function load(userId: string): Stored {
  try {
    const raw = window.localStorage.getItem(`board:${userId}`);
    if (raw) {
      const saved = JSON.parse(raw);
      return { ...DEFAULTS, ...saved, filters: { ...DEFAULT_FILTERS, ...saved.filters } };
    }
    const older = window.localStorage.getItem(`board-filters:${userId}`); // before saved views existed
    if (older) {
      const { market = "", hub = "", product = "", day = "" } = JSON.parse(older);
      return { ...DEFAULTS, filters: { ...DEFAULT_FILTERS, market, hub, product, day } };
    }
  } catch {
    // storage blocked or unreadable: start from the defaults
  }
  return DEFAULTS;
}

const unique = (values: string[]) => [...new Set(values.filter(Boolean))].sort();
const keep = (value: string, options: string[]) => (options.includes(value) ? value : "");

function useTick(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(t);
  }, [everyMs]);
  return now;
}

/** Every listed contract, the filters and the contracts they select (at most MAX_ROWS), how they are shown,
 *  and the named views a user has saved. All of it is remembered per user in this browser. `mine` is the set
 *  of contracts with a position or a working order. */
export function useBoard(userId: string, mine: Set<string>) {
  // Asked often, but cheap: while the list is unchanged the api answers "304 Not Modified" with no body.
  const instruments = usePolling<Instrument[]>("/api/instruments", 10000);
  const summary = usePolling<{ two_sided: string[] }>("/api/md/summary", 5000);
  const [stored, setStored] = useState<Stored>(() => load(userId));
  const now = useTick(30000); // contracts that stop trading drop out of "hide ended" without a reload

  const update = (change: Partial<Stored>) =>
    setStored((prev) => {
      const next = { ...prev, ...change };
      try {
        window.localStorage.setItem(`board:${userId}`, JSON.stringify(next));
      } catch {
        // private window or storage blocked: the settings just are not remembered
      }
      return next;
    });

  const computed = useMemo(() => {
    const f = stored.filters;
    const byId = new Map((instruments ?? []).map((i) => [i.symbol, i]));
    const twoSided = new Set(summary?.two_sided ?? []);
    const everything = (instruments ?? []).map((i) => parseSymbol(i.symbol) ?? unknownContract(i.symbol));
    const live = everything.filter((c) => !f.hideEnded || !hasEnded(byId.get(c.symbol), now));

    // Each choice narrows the next one's options; a remembered choice that no longer exists counts as "all".
    const markets = unique(live.map((c) => c.market));
    const market = keep(f.market, markets);
    const inMarket = live.filter((c) => !market || c.market === market);
    const hubs = unique(inMarket.map((c) => c.hub));
    const hub = keep(f.hub, hubs);
    const families = unique(inMarket.map((c) => c.family));
    const family = keep(f.family, families);
    const inFamily = inMarket.filter((c) => !family || c.family === family);
    const products = unique(inFamily.map((c) => c.product));
    const product = keep(f.product, products);
    const days = unique(inFamily.map((c) => c.day));
    const day = keep(f.day, days);
    const filters: Filters = { ...f, market, hub, family, product, day };

    const closingBy = f.closingWithin ? now + f.closingWithin * 3600_000 : null;
    const selected = inFamily
      .filter((c) => !hub || c.hub === hub)
      .filter((c) => !product || c.product === product)
      .filter((c) => !day || c.day === day)
      .filter((c) => !c.hourNum || (c.hourNum >= f.hourFrom && c.hourNum <= f.hourTo))
      .filter((c) => !f.load || (f.load === "peak") === isPeak(c))
      .filter((c) => {
        const ends = tradingEnds(byId.get(c.symbol));
        return closingBy === null || (ends !== null && ends > now && ends <= closingBy);
      })
      .filter((c) => !f.withPrices || twoSided.has(c.symbol))
      .filter((c) => !f.mine || mine.has(c.symbol))
      .filter((c) => matchesSearch(c, f.search))
      .sort(byScreen);

    // The strip: hours down, hubs across, for one market, delivery day and product.
    let strip: { hubs: string[]; hours: Contract[]; cells: Map<string, string> } | null = null;
    let stripNeeds = "";
    if (stored.view === "strip") {
      const missing = [!market && "a market", !day && "a delivery day", !product && "a product"].filter(Boolean);
      if (missing.length) {
        stripNeeds = `Pick ${missing.join(", ")} for the strip.`;
      } else {
        const cells = new Map<string, string>();
        const hours = new Map<string, Contract>();
        for (const c of selected.slice(0, MAX_ROWS)) {
          cells.set(`${c.hub}|${c.hour}`, c.symbol);
          hours.set(c.hour, c);
        }
        strip = {
          hubs: unique([...cells.keys()].map((k) => k.split("|")[0])),
          hours: [...hours.values()].sort((a, b) => a.order - b.order),
          cells,
        };
      }
    }

    const rows = selected.slice(0, MAX_ROWS);
    return {
      loaded: instruments !== null,
      instruments: byId,
      filters,
      options: { market: markets, hub: hubs, family: families, product: products, day: days },
      matching: selected.length,
      rows,
      strip,
      stripNeeds,
      watched: strip ? [...strip.cells.values()] : stored.view === "strip" ? [] : rows.map((c) => c.symbol),
      statuses: marketStatus(market ? inMarket : live, byId, now),
    };
  }, [instruments, summary, stored.filters, stored.view, mine, now]);

  return {
    ...computed,
    view: stored.view,
    columns: stored.columns,
    sort: stored.sort,
    views: stored.views,
    setFilters: (change: Partial<Filters>) => update({ filters: { ...stored.filters, ...change } }),
    resetFilters: () => update({ filters: DEFAULT_FILTERS }),
    setView: (view: View) => update({ view }),
    setColumns: (columns: Column[]) => update({ columns }),
    setSort: (sort: Sort) => update({ sort }),
    saveView: (name: string) => {
      const { filters, columns, view, sort } = stored;
      update({ views: { ...stored.views, [name]: { filters, columns, view, sort } } });
    },
    openView: (name: string) => stored.views[name] && update({ ...stored.views[name] }),
    deleteView: (name: string) => {
      const views = { ...stored.views };
      delete views[name];
      update({ views });
    },
  };
}

export type Board = ReturnType<typeof useBoard>;
