"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { AppShell } from "@/components/AppShell";
import { AuthProvider } from "@/components/AuthProvider";
import { ConfirmProvider, ToastProvider } from "@/components/feedback";

export function Providers({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";
  const bare = pathname.startsWith("/login");
  return (
    <ToastProvider>
      <ConfirmProvider>
        <AuthProvider>{bare ? children : <AppShell>{children}</AppShell>}</AuthProvider>
      </ConfirmProvider>
    </ToastProvider>
  );
}
