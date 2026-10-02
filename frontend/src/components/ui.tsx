"use client";

import Link from "next/link";
import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useEffect,
  useId,
  useRef,
} from "react";
import { cn, money, STATUS_TONE, type Tone } from "@/lib/format";

/* ── icons (simple stroke icons, no dependency) ─────────────────────────── */

const ICONS: Record<string, ReactNode> = {
  dashboard: (<><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>),
  users: (<><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6" /><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.4c2 .8 3.5 2.6 3.5 5.6" /></>),
  scanner: (<><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3" /><path d="M7 12h10" /></>),
  clock: (<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>),
  swap: (<><path d="M4 8h13l-3-3M20 16H7l3 3" /></>),
  shield: (<><path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6l7-3z" /><path d="M12 8v4M12 15.5v.01" /></>),
  wallet: (<><path d="M3 7a2 2 0 0 1 2-2h12v3" /><path d="M3 7v11a2 2 0 0 0 2 2h14a1 1 0 0 0 1-1v-9a1 1 0 0 0-1-1H5a2 2 0 0 1-2-2z" /><circle cx="16.5" cy="14" r="1.2" /></>),
  chart: (<><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>),
  settings: (<><path d="M4 7h10M18 7h2M4 17h2M10 17h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></>),
  logout: (<><path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M16 8l4 4-4 4M20 12H9" /></>),
  menu: (<><path d="M4 7h16M4 12h16M4 17h16" /></>),
  x: (<><path d="M6 6l12 12M18 6L6 18" /></>),
  check: (<><path d="M5 12.5l4.5 4.5L19 7" /></>),
  search: (<><circle cx="11" cy="11" r="6.5" /><path d="M20 20l-4.2-4.2" /></>),
  download: (<><path d="M12 4v11M7.5 11l4.5 4.5 4.5-4.5M5 20h14" /></>),
  refresh: (<><path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6" /></>),
  copy: (<><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></>),
  alert: (<><path d="M12 4l9.5 16.5h-19L12 4z" /><path d="M12 10v4.5M12 17.5v.01" /></>),
  info: (<><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8v.01" /></>),
  bell: (<><path d="M6 17V11a6 6 0 1 1 12 0v6l1.5 2h-15L6 17zM10 21h4" /></>),
  trash: (<><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></>),
  edit: (<><path d="M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4" /></>),
  plus: (<><path d="M12 5v14M5 12h14" /></>),
  eye: (<><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></>),
};

export function Icon({ name, className = "h-5 w-5" }: { name: keyof typeof ICONS | string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      {ICONS[name] ?? null}
    </svg>
  );
}

/* ── buttons ────────────────────────────────────────────────────────────── */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "success" | "ghost";
  size?: "sm" | "md";
  loading?: boolean;
  icon?: string;
};

const BUTTON_VARIANTS = {
  primary: "bg-brand text-brand-fg hover:bg-brand-hover border border-transparent",
  secondary: "bg-surface text-fg border border-line hover:bg-surface2",
  danger: "bg-bad-soft text-bad border border-bad/30 hover:bg-bad/15",
  success: "bg-ok-soft text-ok border border-ok/30 hover:bg-ok/15",
  ghost: "bg-transparent text-muted hover:bg-surface2 hover:text-fg border border-transparent",
};

export function Button({ variant = "secondary", size = "md", loading, icon, className, children, disabled, type = "button", ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "px-2.5 py-1.5 text-xs" : "px-3.5 py-2 text-sm",
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner className="h-3.5 w-3.5" /> : icon ? <Icon name={icon} className="h-4 w-4" /> : null}
      {children}
    </button>
  );
}

export function LinkButton({ href, children, className, icon, download }: { href: string; children: ReactNode; className?: string; icon?: string; download?: boolean }) {
  const cls = cn("inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3.5 py-2 text-sm font-medium text-fg transition-colors hover:bg-surface2", className);
  const body = (<>{icon && <Icon name={icon} className="h-4 w-4" />}{children}</>);
  return download || href.startsWith("/api") || href.startsWith("http") ? <a href={href} className={cls}>{body}</a> : <Link href={href} className={cls}>{body}</Link>;
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={cn("animate-spin", className)} viewBox="0 0 24 24" fill="none" aria-label="Loading">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity=".25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

/* ── surfaces ───────────────────────────────────────────────────────────── */

export function Card({ children, className, title, actions, padded = true }: { children: ReactNode; className?: string; title?: ReactNode; actions?: ReactNode; padded?: boolean }) {
  return (
    <section className={cn("rounded-xl border border-line bg-surface shadow-sm", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className={padded ? "p-4" : ""}>{children}</div>
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">{actions}</div>
    </div>
  );
}

const TONE_CLASSES: Record<Tone, string> = {
  gray: "bg-surface2 text-muted",
  green: "bg-ok-soft text-ok",
  amber: "bg-warn-soft text-warn",
  red: "bg-bad-soft text-bad",
  blue: "bg-info-soft text-info",
  violet: "bg-violet-soft text-violet",
};

export function Badge({ children, tone = "gray", className }: { children: ReactNode; tone?: Tone; className?: string }) {
  return <span className={cn("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium", TONE_CLASSES[tone], className)}>{children}</span>;
}

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="text-muted">-</span>;
  return <Badge tone={STATUS_TONE[status] ?? "gray"}>{status.replaceAll("_", " ")}</Badge>;
}

export function Stat({ label, value, hint, tone, href, icon }: { label: string; value: ReactNode; hint?: ReactNode; tone?: Tone; href?: string; icon?: string }) {
  const body = (
    <div className={cn("flex h-full flex-col justify-between rounded-xl border border-line bg-surface p-4 shadow-sm", href && "transition-colors hover:bg-surface2")}>
      <div className="flex items-center justify-between text-xs font-medium uppercase tracking-wide text-muted">
        <span>{label}</span>
        {icon && <Icon name={icon} className="h-4 w-4" />}
      </div>
      <div className={cn("tabular mt-2 text-2xl font-semibold", tone === "red" && "text-bad", tone === "amber" && "text-warn", tone === "green" && "text-ok")}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
  return href ? <Link href={href} className="block">{body}</Link> : body;
}

export function Empty({ children = "Nothing here yet.", icon = "info" }: { children?: ReactNode; icon?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center text-sm text-muted">
      <Icon name={icon} className="h-7 w-7 opacity-60" />
      <div>{children}</div>
    </div>
  );
}

export function ErrorBox({ children, onRetry }: { children: ReactNode; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-center justify-between gap-3 rounded-lg border border-bad/30 bg-bad-soft px-3 py-2.5 text-sm text-bad">
      <span className="flex items-center gap-2"><Icon name="alert" className="h-4 w-4 shrink-0" />{children}</span>
      {onRetry && <Button size="sm" onClick={onRetry}>Retry</Button>}
    </div>
  );
}

export function Alert({ tone = "amber", children, href }: { tone?: "amber" | "red" | "blue" | "green"; children: ReactNode; href?: string }) {
  const cls = { amber: "border-warn/30 bg-warn-soft text-warn", red: "border-bad/30 bg-bad-soft text-bad", blue: "border-info/30 bg-info-soft text-info", green: "border-ok/30 bg-ok-soft text-ok" }[tone];
  const body = <div className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-sm", cls)}><Icon name="alert" className="mt-0.5 h-4 w-4 shrink-0" /><div>{children}</div></div>;
  return href ? <Link href={href} className="block hover:opacity-90">{body}</Link> : body;
}

/* ── form controls ──────────────────────────────────────────────────────── */

const CONTROL = "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-fg placeholder:text-muted focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/25 disabled:opacity-60";

export function Field({ label, hint, children, className }: { label: string; hint?: ReactNode; children: (id: string) => ReactNode; className?: string }) {
  const id = useId();
  return (
    <div className={className}>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-muted">{label}</label>
      {children(id)}
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  );
}

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(CONTROL, className)} {...rest} />;
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(CONTROL, "pr-8", className)} {...rest}>{children}</select>;
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(CONTROL, "min-h-24 resize-y", className)} {...rest} />;
}

export function SearchBox({ value, onChange, placeholder = "Search…" }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <div className="relative min-w-52 flex-1 sm:flex-none">
      <Icon name="search" className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
      <Input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="pl-8" aria-label={placeholder} />
    </div>
  );
}

export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn("relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-line transition-colors disabled:opacity-50", checked ? "bg-brand" : "bg-surface2")}
    >
      <span className={cn("inline-block h-4.5 w-4.5 transform rounded-full bg-white shadow transition-transform", checked ? "translate-x-5.5" : "translate-x-0.5")} />
    </button>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: ReactNode }[]; value: T; onChange: (id: T) => void }) {
  return (
    <div role="tablist" className="mb-4 flex flex-wrap gap-1 rounded-xl border border-line bg-surface p-1">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
          className={cn("rounded-lg px-3 py-1.5 text-sm font-medium transition-colors", value === t.id ? "bg-brand text-brand-fg" : "text-muted hover:bg-surface2 hover:text-fg")}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

/* ── modal ──────────────────────────────────────────────────────────────── */

export function Modal({ open, onClose, title, children, footer, size = "md", busy }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; size?: "sm" | "md" | "lg" | "xl"; busy?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    ref.current?.focus();
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, busy, onClose]);
  if (!open) return null;
  const width = { sm: "max-w-md", md: "max-w-xl", lg: "max-w-3xl", xl: "max-w-5xl" }[size];
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-0 sm:items-center sm:p-4" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined} className={cn("flex max-h-[92vh] w-full flex-col rounded-t-2xl border border-line bg-surface shadow-xl outline-none sm:rounded-2xl", width)}>
        <header className="flex items-center justify-between gap-3 border-b border-line px-5 py-3.5">
          <h2 className="text-base font-semibold">{title}</h2>
          <button type="button" onClick={onClose} disabled={busy} className="rounded-md p-1 text-muted hover:bg-surface2 hover:text-fg" aria-label="Close"><Icon name="x" className="h-5 w-5" /></button>
        </header>
        <div className="overflow-y-auto px-5 py-4">{children}</div>
        {footer && <footer className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</footer>}
      </div>
    </div>
  );
}

export function KeyValue({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2.5 text-sm sm:grid-cols-2">
      {items.map((it) => (
        <div key={it.label} className="min-w-0">
          <dt className="text-xs font-medium text-muted">{it.label}</dt>
          <dd className="mt-0.5 break-words">{it.value ?? "-"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cn("break-all rounded bg-surface2 px-1.5 py-0.5 font-mono text-xs", className)}>{children}</code>;
}

/** An amount with a small unit, e.g. ``12.50 USDT``. */
export function Money({ value, unit = "USDT", className }: { value: string | number | null | undefined; unit?: string; className?: string }) {
  return (
    <span className={cn("tabular whitespace-nowrap", className)}>
      {money(value)}
      {unit && <span className="ml-1 text-[0.7em] font-medium text-muted">{unit}</span>}
    </span>
  );
}
