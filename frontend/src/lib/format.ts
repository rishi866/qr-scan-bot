export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** Exact-ish display of a decimal string: 2 to 8 decimals, thousands separators, never scientific. */
export function money(value: string | number | null | undefined, minDecimals = 2): string {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return new Intl.NumberFormat("en-US", { minimumFractionDigits: minDecimals, maximumFractionDigits: 8 }).format(n);
}

export function usdt(value: string | number | null | undefined): string {
  return `${money(value)} USDT`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function dateOnly(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "-" : d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
}

export function utcStamp(iso: string | null | undefined): string {
  return iso ? new Date(iso).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "";
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function flag(code: string | null | undefined): string {
  if (!code || code.length !== 2 || !/^[A-Za-z]{2}$/.test(code)) return "🌐";
  return String.fromCodePoint(...[...code.toUpperCase()].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

export function shortHash(h: string | null | undefined, keep = 6): string {
  if (!h) return "-";
  return h.length <= keep * 2 + 3 ? h : `${h.slice(0, keep + 2)}…${h.slice(-keep)}`;
}

export function today(offsetDays = 0): string {
  const d = new Date(Date.now() + offsetDays * 86400000);
  return d.toISOString().slice(0, 10);
}

export type Tone = "gray" | "green" | "amber" | "red" | "blue" | "violet";

export const STATUS_TONE: Record<string, Tone> = {
  // users
  approved: "green", pending: "amber", rejected: "red", suspended: "red", onboarding: "gray",
  // sessions
  awaiting_scanner: "amber", accepted: "blue", done: "violet", confirmed: "green", disputed: "red", refunded: "gray",
  expired: "gray", skipped: "gray", timed_out: "gray", cancelled: "gray",
  // transactions / disputes / deposits / withdrawals
  completed: "green", awaiting_proof: "amber", pending_review: "violet", resolved: "blue",
  credited: "green", below_min: "amber", processing: "blue", failed: "red",
};

export function copyText(text: string): void {
  void navigator.clipboard?.writeText(text);
}
