/** ElectronX symbols: <PRODUCT>-<MARKET>-<LOCATION>-<YYMMDD>-<HH>, e.g. BFUT500-ERCOT-HB_NORTH-261110-15.
 *  The date and hour ending are in the ISO's time zone; the repeated hour when clocks fall back is X2. */

export type Contract = {
  symbol: string;
  product: string; // with its size: BFUT500, BFUT2000Y, BOPT
  family: string; // BFUT or BOPT
  market: string; // ERCOT, PJM, ...
  hub: string;
  day: string; // 2026-11-10
  hour: string; // 15, or X2
  hourNum: number; // 1..24 (X2 counts as 2)
  order: number; // sorts the hours of a day: 1..24, X2 straight after 02
  weekday: number; // of the delivery day: 0 Sunday .. 6 Saturday
};

const RE = /^(B[A-Z]+\d*[A-Z]*)-(ERCOT|PJM|MISO|CAISO|SPP|ISONE|ISO-NE|NYISO)-(.+)-(\d{2})(\d{2})(\d{2})-(\d{2}|X\d)$/;

export function parseSymbol(symbol: string): Contract | null {
  const m = RE.exec(symbol);
  if (!m) return null;
  const [, product, market, hub, yy, mm, dd, hour] = m;
  const repeated = hour.startsWith("X");
  const hourNum = Number(repeated ? hour.slice(1) : hour);
  const day = `20${yy}-${mm}-${dd}`;
  return {
    symbol,
    product,
    family: product.startsWith("BOPT") ? "BOPT" : "BFUT",
    market: market === "ISO-NE" ? "ISONE" : market,
    hub,
    day,
    hour,
    hourNum,
    order: repeated ? hourNum + 0.5 : hourNum,
    weekday: new Date(`${day}T12:00:00Z`).getUTCDay(),
  };
}

/** A contract the parser does not recognise still gets a row, under "All markets". */
export function unknownContract(symbol: string): Contract {
  return { symbol, product: "", family: "", market: "", hub: "", day: "", hour: "", hourNum: 0, order: 0, weekday: -1 };
}
