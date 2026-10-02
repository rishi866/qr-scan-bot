"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CopyButton, timeZoneOptions } from "@/components/common";
import { DataTable } from "@/components/DataTable";
import { useConfirm, useToast } from "@/components/feedback";
import { Badge, Button, Card, ErrorBox, Field, Input, KeyValue, Modal, Money, Spinner, StatusBadge, Textarea } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import { dateTime, flag, timeAgo } from "@/lib/format";
import type { UserDetail } from "@/lib/types";

/** Everything the admin may want to know (and do) about one user, in a dialog. */
export function UserDetailModal({ userId, onClose, onChanged }: { userId: number | null; onClose: () => void; onChanged?: () => void }) {
  const { data, error, loading, reload } = useApi<UserDetail>(userId === null ? null : `/api/users/${userId}`);
  const toast = useToast();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [country, setCountry] = useState("");
  const [timezone, setTimezone] = useState("");
  const [zones] = useState(timeZoneOptions);

  useEffect(() => {
    if (data) {
      setNote(data.admin_note ?? "");
      setCountry(data.country ?? "");
      setTimezone(data.timezone ?? "");
    }
  }, [data]);

  const run = async (label: string, call: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await call();
      toast.success(label);
      reload();
      onChanged?.();
      refreshBadges();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const decide = async (action: "approve" | "reject" | "suspend" | "reinstate") => {
    if (!data) return;
    const who = data.alias ?? data.name;
    if (action === "reject" || action === "suspend") {
      const ok = await confirm({
        title: action === "reject" ? `Reject ${who}?` : `Suspend ${who}?`,
        message: action === "reject" ? "They are told to contact the admin. You can approve them again later." : "They cannot use the bot until you reinstate them. Tasks already in flight are not touched.",
        confirmLabel: action === "reject" ? "Reject" : "Suspend",
        tone: "danger",
      });
      if (!ok) return;
    }
    const labels = { approve: "User approved", reject: "User rejected", suspend: "User suspended", reinstate: "User reinstated" };
    await run(labels[action], () => api.post(`/api/users/${data.user_id}/${action}`, action === "suspend" ? { note: note || null } : {}));
  };

  const saveProfile = () =>
    run("Saved", () => api.patch(`/api/users/${userId}`, { country: country.trim().toUpperCase() || null, timezone: timezone.trim() || null, note: note.trim() || null }));

  const u = data;
  return (
    <Modal
      open={userId !== null}
      onClose={onClose}
      busy={busy}
      size="lg"
      title={u ? <span className="flex flex-wrap items-center gap-2">{u.alias ?? u.name} <StatusBadge status={u.status} /></span> : "User"}
      footer={
        u && (
          <>
            {(u.status === "pending" || u.status === "rejected") && <Button variant="success" icon="check" loading={busy} onClick={() => void decide("approve")}>Approve</Button>}
            {u.status === "pending" && <Button variant="danger" disabled={busy} onClick={() => void decide("reject")}>Reject</Button>}
            {u.status === "approved" && <Button variant="danger" disabled={busy} onClick={() => void decide("suspend")}>Suspend</Button>}
            {u.status === "suspended" && <Button variant="success" loading={busy} onClick={() => void decide("reinstate")}>Reinstate</Button>}
            <Button variant="primary" loading={busy} onClick={() => void saveProfile()}>Save changes</Button>
          </>
        )
      }
    >
      {!u && error && <ErrorBox onRetry={reload}>{error}</ErrorBox>}
      {!u && !error && loading && <div className="flex items-center gap-2 py-8 text-muted"><Spinner /> Loading…</div>}
      {u && (
        <div className="space-y-5">
          {u.bot_blocked && <div className="rounded-lg border border-warn/30 bg-warn-soft px-3 py-2 text-sm text-warn">This user blocked the bot - messages to them cannot be delivered until they press Start again.</div>}

          <KeyValue
            items={[
              { label: "Name", value: u.name },
              { label: "Telegram", value: <span className="inline-flex items-center gap-1">{u.username ? `@${u.username}` : "no username"} · <code>{u.telegram_id}</code><CopyButton text={String(u.telegram_id)} /></span> },
              { label: "Role", value: <Badge tone={u.role === "scanner" ? "blue" : "violet"}>{u.role === "scanner" ? "QR Scanner" : "QR Sender"}</Badge> },
              { label: "Country", value: `${flag(u.country)} ${u.country_name ?? "unknown"}` },
              { label: "Time zone", value: u.timezone ? `${u.timezone}${u.utc_offset ? ` (${u.utc_offset})` : ""}` : "not set" },
              { label: "Joined", value: `${dateTime(u.created_at)} · ${timeAgo(u.created_at)}` },
              ...(u.role === "scanner" ? [{ label: "Scanner name", value: u.alias ?? <span className="text-warn">not named yet - <Link href="/scanners/?needs_name=1" className="underline">name it</Link></span> }, { label: "Reputation", value: u.reputation ?? "-" }] : []),
              { label: "Approved", value: u.approved_at ? dateTime(u.approved_at) : "-" },
            ]}
          />

          {u.wallet && (
            <Card title="Wallet" actions={<Link href={`/wallets/?q=${u.user_id}`} className="text-xs text-brand hover:underline">Open in wallets</Link>}>
              <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
                <div><div className="text-xs text-muted">Available</div><Money value={u.wallet.balance} className="text-base font-semibold" /></div>
                <div><div className="text-xs text-muted">Held in tasks</div><Money value={u.wallet.pending} /></div>
                <div><div className="text-xs text-muted">Earned</div><Money value={u.wallet.total_earned} /></div>
                <div><div className="text-xs text-muted">Spent</div><Money value={u.wallet.total_spent} /></div>
                <div><div className="text-xs text-muted">Deposited</div><Money value={u.wallet.total_deposited} /></div>
                <div><div className="text-xs text-muted">Withdrawn</div><Money value={u.wallet.total_withdrawn} /></div>
              </div>
              {(u.wallet.bep20_address || u.wallet.binance_address) && (
                <div className="mt-3 space-y-1 border-t border-line pt-3 text-xs text-muted">
                  {u.wallet.bep20_address && <div className="flex flex-wrap items-center gap-1">BEP-20: <code className="break-all">{u.wallet.bep20_address}</code><CopyButton text={u.wallet.bep20_address} /></div>}
                  {u.wallet.binance_address && <div className="flex flex-wrap items-center gap-1">Binance: <code className="break-all">{u.wallet.binance_address}</code><CopyButton text={u.wallet.binance_address} /></div>}
                </div>
              )}
            </Card>
          )}

          <div className="grid grid-cols-3 gap-3 text-center text-sm">
            <div className="rounded-lg border border-line p-3"><div className="text-xl font-semibold tabular">{u.stats.completed_tasks}</div><div className="text-xs text-muted">completed tasks</div></div>
            <div className="rounded-lg border border-line p-3"><div className="text-xl font-semibold tabular"><Money value={u.stats.volume} unit="" /></div><div className="text-xs text-muted">volume (USDT)</div></div>
            <div className="rounded-lg border border-line p-3"><div className="text-xl font-semibold tabular"><Money value={u.stats.commission} unit="" /></div><div className="text-xs text-muted">commission (USDT)</div></div>
          </div>

          {u.role === "scanner" && (
            <div>
              <h3 className="mb-2 text-sm font-semibold">Slots {u.slots.length > 0 && <span className="font-normal text-muted">({u.slots.length})</span>}</h3>
              {u.slots.length === 0 ? (
                <p className="text-sm text-muted">No slots chosen yet - this scanner will not receive URLs.</p>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {u.slots.map((s) => (
                    <Badge key={s.id} tone={s.live_now ? "green" : s.is_active ? "gray" : "red"} className="!text-xs" >
                      {s.label} · {s.tz_label}{!s.is_active && " · paused"}{s.live_now && " · live"}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          )}

          <div>
            <h3 className="mb-2 text-sm font-semibold">Recent tasks</h3>
            <div className="rounded-lg border border-line">
              <DataTable
                rows={u.recent_sessions}
                rowKey={(s) => s.session_id}
                empty="No tasks yet."
                columns={[
                  { key: "id", header: "Task", cell: (s) => `#${s.session_id}` },
                  { key: "status", header: "Status", cell: (s) => <StatusBadge status={s.status} /> },
                  { key: "amount", header: "Reward", align: "right", cell: (s) => <Money value={s.amount} unit="" /> },
                  { key: "when", header: "Sent", hideBelow: "sm", cell: (s) => dateTime(s.sent_at) },
                ]}
              />
            </div>
            <p className="mt-1.5 text-xs text-muted">{u.deposits} deposit{u.deposits === 1 ? "" : "s"} · {u.withdrawals} withdrawal{u.withdrawals === 1 ? "" : "s"} on record.</p>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Country (2-letter code)" hint="Used for reports; the bot also asks users for it.">
              {(id) => <Input id={id} value={country} maxLength={2} onChange={(e) => setCountry(e.target.value.toUpperCase())} placeholder="IN" />}
            </Field>
            <Field label="Time zone (IANA)" hint="Scanner slots follow this zone.">
              {(id) => (
                <>
                  <Input id={id} value={timezone} list="tz-options" onChange={(e) => setTimezone(e.target.value)} placeholder="Asia/Kolkata" />
                  <datalist id="tz-options">{zones.map((z) => <option key={z} value={z} />)}</datalist>
                </>
              )}
            </Field>
          </div>
          <Field label="Internal note" hint="Only admins see this. Also stored when you suspend someone.">
            {(id) => <Textarea id={id} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} className="min-h-20" />}
          </Field>
        </div>
      )}
    </Modal>
  );
}
