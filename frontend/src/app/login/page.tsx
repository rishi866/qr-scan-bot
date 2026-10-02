"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { Button, Field, Icon, Input } from "@/components/ui";
import { api, errorMessage } from "@/lib/api";

interface LoginResponse {
  status: "ok" | "2fa_required";
  challenge?: string;
}

export default function LoginPage() {
  const router = useRouter();
  const { refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [challenge, setChallenge] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function finish() {
    await refresh();
    router.replace("/");
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (challenge) {
        await api.post("/api/auth/2fa", { challenge, code });
        await finish();
      } else {
        const res = await api.post<LoginResponse>("/api/auth/login", { username, password });
        if (res.status === "2fa_required" && res.challenge) setChallenge(res.challenge);
        else await finish();
      }
    } catch (err) {
      setError(errorMessage(err));
      if (challenge) setCode("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand text-brand-fg shadow-lg"><Icon name="scanner" className="h-6 w-6" /></span>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">QR Exchange admin</h1>
            <p className="text-sm text-muted">{challenge ? "Enter the 6-digit code from your authenticator app" : "Sign in to manage users, payments and disputes"}</p>
          </div>
        </div>
        <form onSubmit={onSubmit} className="space-y-4 rounded-2xl border border-line bg-surface p-6 shadow-sm">
          {challenge ? (
            <Field label="Authentication code">
              {(id) => (
                <Input id={id} value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={8} placeholder="123456" autoFocus required className="text-center text-lg tracking-[0.4em]" />
              )}
            </Field>
          ) : (
            <>
              <Field label="Username">
                {(id) => <Input id={id} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus required />}
              </Field>
              <Field label="Password">
                {(id) => <Input id={id} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />}
              </Field>
            </>
          )}
          {error && <p role="alert" className="rounded-lg bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p>}
          <Button type="submit" variant="primary" loading={busy} className="w-full">{challenge ? "Verify" : "Sign in"}</Button>
          {challenge && (
            <button type="button" className="w-full text-center text-xs text-muted hover:text-fg" onClick={() => { setChallenge(null); setCode(""); setError(null); }}>
              Back to sign in
            </button>
          )}
        </form>
      </div>
    </main>
  );
}
