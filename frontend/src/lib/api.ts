"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Same origin in production (Caddy / FastAPI serve both). For `next dev`, set NEXT_PUBLIC_API_BASE=http://localhost:8000 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export type Params = Record<string, string | number | boolean | undefined | null>;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function qs(params?: Params): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

function detailToMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc: [...], msg: "..."}]
    return detail
      .map((d) => {
        const where = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
        return where ? `${where}: ${d?.msg ?? "invalid"}` : (d?.msg ?? "invalid");
      })
      .join("; ");
  }
  return "Something went wrong";
}

async function readError(res: Response): Promise<string> {
  try {
    const data = await res.json();
    return detailToMessage(data?.detail);
  } catch {
    return `Request failed (${res.status})`;
  }
}

async function request<T>(
  method: string,
  path: string,
  opts: { params?: Params; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const hasBody = opts.body !== undefined;
  const res = await fetch(API_BASE + path + qs(opts.params), {
    method,
    credentials: "include",
    headers: {
      // custom header = CSRF defence (a cross-site form cannot set it)
      "X-Requested-With": "qr-admin",
      ...(hasBody ? { "Content-Type": "application/json" } : {}),
    },
    body: hasBody ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
  if (res.status === 401 && !path.startsWith("/api/auth/login") && !path.startsWith("/api/auth/2fa")) {
    window.dispatchEvent(new Event("auth:expired"));
  }
  if (!res.ok) throw new ApiError(res.status, await readError(res));
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("json") ? await res.json() : await res.text()) as T;
}

export const api = {
  get: <T,>(path: string, params?: Params, signal?: AbortSignal) => request<T>("GET", path, { params, signal }),
  post: <T,>(path: string, body: unknown = {}) => request<T>("POST", path, { body }),
  put: <T,>(path: string, body: unknown = {}) => request<T>("PUT", path, { body }),
  patch: <T,>(path: string, body: unknown = {}) => request<T>("PATCH", path, { body }),
  del: <T,>(path: string) => request<T>("DELETE", path),
  /** URL for downloads / <img src> (the session cookie travels along). */
  url: (path: string, params?: Params) => API_BASE + path + qs(params),
};

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong";
}

/** GET with loading / error state; keeps the previous data while a new page loads. */
export function useApi<T>(path: string | null, params?: Params, refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const [tick, setTick] = useState(0);
  const key = path === null ? "" : path + qs(params);
  const paramsRef = useRef(params);
  paramsRef.current = params;

  useEffect(() => {
    if (path === null) return;
    const ctl = new AbortController();
    setLoading(true);
    api
      .get<T>(path, paramsRef.current, ctl.signal)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (e?.name !== "AbortError") setError(errorMessage(e));
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false);
      });
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);

  useEffect(() => {
    if (!refreshMs || path === null) return;
    const id = setInterval(() => setTick((t) => t + 1), refreshMs);
    return () => clearInterval(id);
  }, [refreshMs, path]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
