export const NA = "n/a";

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return NA;
  const abs = Math.abs(value);
  if (abs >= 1e12) return `$${(value / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return `$${value.toFixed(2)}`;
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined) return NA;
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 6 : 8;
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: digits })}`;
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return NA;
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function bps(value: number | null | undefined): string {
  if (value === null || value === undefined) return NA;
  return `${value.toFixed(1)} bps`;
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return NA;
  return `${(value * 100).toFixed(digits)}%`;
}

export function signedPct(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined) return NA;
  const text = (value * 100).toFixed(digits);
  return `${value > 0 ? "+" : ""}${text}%`;
}

export function age(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return NA;
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function utc(iso: string | null | undefined): string {
  if (!iso) return NA;
  return iso.replace("T", " ").replace(/:\d\dZ$/, " UTC").replace("Z", " UTC");
}
