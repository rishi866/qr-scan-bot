"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import type { Admin } from "@/lib/types";

interface AuthState {
  admin: Admin | null;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

const PUBLIC_PATHS = ["/login"];

/** Loads the session once; sends visitors without one to /login/ and signed-out admins back there. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const [admin, setAdmin] = useState<Admin | null>(null);
  const [ready, setReady] = useState(false);
  const isPublic = PUBLIC_PATHS.some((p) => pathname.startsWith(p));

  const refresh = useCallback(async () => {
    try {
      setAdmin(await api.get<Admin>("/api/auth/me"));
    } catch {
      setAdmin(null);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const onExpired = () => setAdmin(null);
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, []);

  useEffect(() => {
    if (ready && !admin && !isPublic) router.replace("/login/");
  }, [ready, admin, isPublic, router]);

  const logout = useCallback(async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      setAdmin(null);
      router.replace("/login/");
    }
  }, [router]);

  if (!ready && !isPublic) {
    return <div className="flex min-h-screen items-center justify-center text-muted"><Spinner className="h-6 w-6" /></div>;
  }
  if (!admin && !isPublic) return null;
  return <AuthContext.Provider value={{ admin, logout, refresh }}>{children}</AuthContext.Provider>;
}
