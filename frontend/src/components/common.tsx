"use client";

import { type ReactNode, useEffect, useState } from "react";
import { Icon, Mono } from "@/components/ui";
import { useToast } from "@/components/feedback";
import { copyText, flag, shortHash } from "@/lib/format";

/** The page's query string, available after mount (null during the first render). */
export function useInitialParams(): URLSearchParams | null {
  const [params, setParams] = useState<URLSearchParams | null>(null);
  useEffect(() => setParams(new URLSearchParams(window.location.search)), []);
  return params;
}

/** Debounced copy of a value (for search boxes). */
export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const toast = useToast();
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation();
        copyText(text);
        toast.info("Copied");
      }}
      className="inline-flex rounded p-1 text-muted hover:bg-surface2 hover:text-fg"
    >
      <Icon name="copy" className="h-3.5 w-3.5" />
    </button>
  );
}

export function HashCell({ value, keep = 6 }: { value: string | null | undefined; keep?: number }) {
  if (!value) return <span className="text-muted">-</span>;
  return (
    <span className="inline-flex items-center gap-1">
      <Mono>{shortHash(value, keep)}</Mono>
      <CopyButton text={value} />
    </span>
  );
}

export function PersonCell({ name, sub, country, alias }: { name: string | null | undefined; sub?: ReactNode; country?: string | null; alias?: string | null }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5 font-medium">
        {country !== undefined && <span title={country ?? ""}>{flag(country)}</span>}
        <span className="truncate">{alias ?? name ?? "-"}</span>
      </div>
      {(sub || (alias && name)) && <div className="truncate text-xs text-muted">{[alias ? name : null, sub].filter(Boolean).join(" · ")}</div>}
    </div>
  );
}

export function FilterBar({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">{children}</div>;
}

/** All IANA zones the browser knows, for <datalist>. */
export function timeZoneOptions(): string[] {
  try {
    return (Intl as unknown as { supportedValuesOf: (k: string) => string[] }).supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}
