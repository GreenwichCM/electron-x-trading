/** ElectronX symbols: <PRODUCT>-<MARKET>-<LOCATION>-<YYMMDD>-<HH>, e.g. BFUT500-ERCOT-HB_NORTH-261110-15.
 *  The date and hour ending are in the ISO's time zone; the repeated hour when clocks fall back is X2. */

export type Contract = {
  symbol: string;
  product: string; // with its size: BFUT500, BOPT
  market: string; // ERCOT, PJM, ...
  hub: string;
  day: string; // 2026-11-10
  hour: string; // 15, or X2
  order: number; // sorts the hours of a day: 1..24, X2 straight after 02
};

const RE = /^(B[A-Z]+\d*)-(ERCOT|PJM|MISO|CAISO|SPP|ISO-NE|NYISO)-(.+)-(\d{2})(\d{2})(\d{2})-(\d{2}|X\d)$/;

export function parseSymbol(symbol: string): Contract | null {
  const m = RE.exec(symbol);
  if (!m) return null;
  const [, product, market, hub, yy, mm, dd, hour] = m;
  const order = hour.startsWith("X") ? Number(hour.slice(1)) + 0.5 : Number(hour);
  return { symbol, product, market, hub, day: `20${yy}-${mm}-${dd}`, hour, order };
}
