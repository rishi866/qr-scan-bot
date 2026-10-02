"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { Icon } from "@/components/ui";
import { useApi } from "@/lib/api";
import { cn } from "@/lib/format";
import type { Badges } from "@/lib/types";

interface NavItem {
  href: string;
  label: string;
  icon: string;
  badge?: keyof Badges;
  badgeTone?: "amber" | "red";
}

const NAV: NavItem[] = [
  { href: "/", label: "Dashboard", icon: "dashboard" },
  { href: "/users/", label: "Users", icon: "users", badge: "pending_users", badgeTone: "amber" },
  { href: "/scanners/", label: "Scanners", icon: "scanner", badge: "scanners_needing_name", badgeTone: "amber" },
  { href: "/slots/", label: "Slots", icon: "clock" },
  { href: "/transactions/", label: "Transactions", icon: "swap" },
  { href: "/disputes/", label: "Disputes", icon: "shield", badge: "open_disputes", badgeTone: "red" },
  { href: "/wallets/", label: "Wallets", icon: "wallet", badge: "withdrawals_to_handle", badgeTone: "amber" },
  { href: "/reports/", label: "Reports", icon: "chart" },
  { href: "/settings/", label: "Settings", icon: "settings" },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const { admin, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const { data: badges } = useApi<Badges>("/api/badges", undefined, 30000);

  useEffect(() => setOpen(false), [pathname]);

  const nav = (
    <nav className="flex flex-1 flex-col gap-0.5 px-3 py-3" aria-label="Main">
      {NAV.map((item) => {
        const count = item.badge && badges ? badges[item.badge] : 0;
        const active = isActive(pathname, item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn("flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors", active ? "bg-sidebar-active text-white" : "text-sidebar-fg hover:bg-sidebar-active/70 hover:text-white")}
          >
            <Icon name={item.icon} className="h-[18px] w-[18px]" />
            <span className="flex-1">{item.label}</span>
            {count > 0 && (
              <span className={cn("min-w-5 rounded-full px-1.5 py-0.5 text-center text-[11px] font-semibold leading-none", item.badgeTone === "red" ? "bg-bad text-white" : "bg-warn text-black")}>{count}</span>
            )}
          </Link>
        );
      })}
    </nav>
  );

  const brand = (
    <div className="flex items-center gap-2.5 px-5 py-4 text-white">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand text-brand-fg"><Icon name="scanner" className="h-[18px] w-[18px]" /></span>
      <div className="leading-tight">
        <div className="text-sm font-semibold">QR Exchange</div>
        <div className="text-[11px] text-sidebar-fg">Admin panel</div>
      </div>
    </div>
  );

  const account = (
    <div className="border-t border-white/10 p-3">
      <div className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-sidebar-fg">
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-sidebar-active text-xs font-semibold uppercase text-white">{admin?.username.slice(0, 2)}</span>
        <div className="min-w-0 flex-1 text-sm">
          <div className="truncate font-medium text-white">{admin?.username}</div>
          <div className="text-[11px]">{admin?.totp_enabled ? "2FA on" : "2FA off"}</div>
        </div>
        <button type="button" onClick={() => void logout()} className="rounded-md p-1.5 hover:bg-sidebar-active hover:text-white" aria-label="Sign out" title="Sign out">
          <Icon name="logout" className="h-[18px] w-[18px]" />
        </button>
      </div>
    </div>
  );

  return (
    <div className="min-h-screen lg:flex">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col bg-sidebar lg:flex">
        {brand}
        {nav}
        {account}
      </aside>

      {/* mobile top bar + drawer */}
      <div className="sticky top-0 z-30 flex items-center justify-between border-b border-line bg-surface px-4 py-2.5 lg:hidden">
        <button type="button" onClick={() => setOpen(true)} className="rounded-md p-1.5 text-fg hover:bg-surface2" aria-label="Open menu"><Icon name="menu" /></button>
        <span className="text-sm font-semibold">QR Exchange</span>
        <button type="button" onClick={() => void logout()} className="rounded-md p-1.5 text-muted hover:bg-surface2" aria-label="Sign out"><Icon name="logout" /></button>
      </div>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Menu">
          <div className="absolute inset-0 bg-black/50" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 flex w-64 flex-col bg-sidebar">
            {brand}
            {nav}
            {account}
          </aside>
        </div>
      )}

      <main className="min-w-0 flex-1 px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
        <div className="mx-auto max-w-[1400px]">{children}</div>
      </main>
    </div>
  );
}
