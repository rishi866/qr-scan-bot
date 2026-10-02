"use client";

import { useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { DataTable, type Column } from "@/components/DataTable";
import { useConfirm, useToast } from "@/components/feedback";
import { Alert, Badge, Button, Card, Field, Input } from "@/components/ui";
import { api, errorMessage, useApi } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { Admin } from "@/lib/types";

const PASSWORD_HINT = "At least 12 characters with upper- and lower-case letters and a digit; it must not contain your username.";

function PasswordCard() {
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const mismatch = again !== "" && again !== next;

  const submit = async () => {
    setBusy(true);
    try {
      await api.post("/api/auth/password", { current_password: current, new_password: next });
      toast.success("Password changed. Other sessions were signed out.");
      setCurrent("");
      setNext("");
      setAgain("");
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Change password">
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <Field label="Current password">{(id) => <Input id={id} type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />}</Field>
        <Field label="New password" hint={PASSWORD_HINT}>{(id) => <Input id={id} type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />}</Field>
        <Field label="Repeat new password" hint={mismatch ? "The passwords do not match." : undefined}>{(id) => <Input id={id} type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} aria-invalid={mismatch} />}</Field>
        <Button type="submit" variant="primary" loading={busy} disabled={!current || !next || next !== again}>Change password</Button>
      </form>
    </Card>
  );
}

interface Enrolment { secret: string; otpauth_uri: string; qr_png_base64: string }

function TwoFactorCard() {
  const { admin, refresh } = useAuth();
  const toast = useToast();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [enrol, setEnrol] = useState<Enrolment | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (call: () => Promise<void>, ok: string) => {
    setBusy(true);
    try {
      await call();
      toast.success(ok);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const start = () => run(async () => setEnrol(await api.post<Enrolment>("/api/auth/2fa/setup", { password })), "Scan the QR code with your authenticator app");
  const enable = () => run(async () => {
    await api.post("/api/auth/2fa/enable", { code });
    setEnrol(null);
    setPassword("");
    setCode("");
    await refresh();
  }, "Two-factor authentication is on");
  const disable = () => run(async () => {
    await api.post("/api/auth/2fa/disable", { password, code });
    setPassword("");
    setCode("");
    await refresh();
  }, "Two-factor authentication is off");

  return (
    <Card title="Two-factor authentication" actions={<Badge tone={admin?.totp_enabled ? "green" : "amber"}>{admin?.totp_enabled ? "on" : "off"}</Badge>}>
      {admin?.totp_enabled ? (
        <div className="space-y-3">
          <p className="text-sm text-muted">Sign-in asks for a 6-digit code from your authenticator app. To turn it off, confirm with your password and a current code.</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Password">{(id) => <Input id={id} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />}</Field>
            <Field label="Current 6-digit code">{(id) => <Input id={id} inputMode="numeric" autoComplete="one-time-code" maxLength={8} value={code} onChange={(e) => setCode(e.target.value)} />}</Field>
          </div>
          <Button variant="danger" loading={busy} disabled={!password || code.length < 6} onClick={() => void disable()}>Turn off 2FA</Button>
        </div>
      ) : enrol ? (
        <div className="space-y-3">
          <p className="text-sm text-muted">1. Scan this QR code with Google Authenticator, Authy, 1Password… 2. Type the 6-digit code it shows to finish.</p>
          <div className="flex flex-wrap items-center gap-4">
            <img src={`data:image/png;base64,${enrol.qr_png_base64}`} alt="Authenticator QR code" className="h-40 w-40 rounded-lg border border-line bg-white p-2" />
            <div className="min-w-0 text-xs text-muted">Can&apos;t scan? Enter this key manually:<div className="mt-1 break-all rounded bg-surface2 px-2 py-1.5 font-mono text-sm text-fg">{enrol.secret}</div></div>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <Field label="6-digit code" className="w-40">{(id) => <Input id={id} inputMode="numeric" autoComplete="one-time-code" maxLength={8} value={code} onChange={(e) => setCode(e.target.value)} />}</Field>
            <Button variant="primary" loading={busy} disabled={code.length < 6} onClick={() => void enable()}>Turn on 2FA</Button>
            <Button onClick={() => { setEnrol(null); setCode(""); }} disabled={busy}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <Alert tone="amber">Strongly recommended: this panel can move money. A stolen password alone should not be enough.</Alert>
          <Field label="Confirm your password to begin">{(id) => <Input id={id} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />}</Field>
          <Button variant="primary" loading={busy} disabled={!password} onClick={() => void start()}>Set up 2FA</Button>
        </div>
      )}
    </Card>
  );
}

function AdminsCard() {
  const { admin } = useAuth();
  const toast = useToast();
  const confirm = useConfirm();
  const { data, error, loading, reload } = useApi<{ items: Admin[] }>("/api/admins");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    setBusy(true);
    try {
      await api.post("/api/admins", { username: username.trim(), password });
      toast.success(`Admin ${username.trim().toLowerCase()} created - they can enable 2FA after their first sign-in`);
      setUsername("");
      setPassword("");
      reload();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (a: Admin) => {
    const ok = await confirm({ title: `Delete admin ${a.username}?`, message: "They are signed out immediately and can no longer use the panel.", confirmLabel: "Delete admin", tone: "danger" });
    if (!ok) return;
    try {
      await api.del(`/api/admins/${a.id}`);
      toast.success("Admin deleted");
      reload();
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const columns: Column<Admin>[] = [
    { key: "u", header: "Username", cell: (a) => <span className="font-medium">{a.username}{a.id === admin?.id && <span className="ml-1.5 text-xs text-muted">(you)</span>}</span> },
    { key: "2fa", header: "2FA", cell: (a) => <Badge tone={a.totp_enabled ? "green" : "amber"}>{a.totp_enabled ? "on" : "off"}</Badge> },
    { key: "last", header: "Last sign-in", hideBelow: "sm", cell: (a) => dateTime(a.last_login_at) },
    { key: "created", header: "Created", hideBelow: "md", cell: (a) => dateTime(a.created_at) },
    { key: "x", header: "", align: "right", cell: (a) => (a.id === admin?.id ? null : <Button size="sm" variant="danger" onClick={() => void remove(a)}>Delete</Button>) },
  ];

  return (
    <Card title="Admin accounts" padded={false} className="lg:col-span-2">
      {error && <div className="p-4 text-sm text-bad">{error}</div>}
      <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(a) => a.id} />
      <form className="grid gap-3 border-t border-line p-4 sm:grid-cols-[1fr_1fr_auto] sm:items-start" onSubmit={(e) => { e.preventDefault(); void add(); }}>
        <Field label="New admin username" hint="3-32 characters: letters, digits, . _ -">{(id) => <Input id={id} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />}</Field>
        <Field label="Temporary password" hint={PASSWORD_HINT}>{(id) => <Input id={id} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />}</Field>
        <Button type="submit" variant="primary" icon="plus" className="sm:mt-[22px]" loading={busy} disabled={username.trim().length < 3 || !password}>Add admin</Button>
      </form>
    </Card>
  );
}

export function SecurityTab() {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <PasswordCard />
      <TwoFactorCard />
      <AdminsCard />
    </div>
  );
}
