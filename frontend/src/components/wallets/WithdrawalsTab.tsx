"use client";

import { useEffect, useState } from "react";
import { CopyButton, FilterBar, PersonCell } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useToast } from "@/components/feedback";
import { MethodBadge, TxRef } from "@/components/wallets/shared";
import { Alert, Badge, Button, Card, Field, Input, KeyValue, Modal, Money, Mono, Select, StatusBadge } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import { dateTime, flag } from "@/lib/format";
import type { Page, WithdrawalRow } from "@/lib/types";

const PAGE_SIZE = 25;

type Action = "approve" | "reject" | "mark-paid" | "retry";

const COPY: Record<Action, { title: string; button: string; tone: "primary" | "danger" | "success" }> = {
  approve: { title: "Approve withdrawal", button: "Approve", tone: "primary" },
  reject: { title: "Reject withdrawal", button: "Reject and return funds", tone: "danger" },
  "mark-paid": { title: "Mark as paid", button: "I sent the money - mark as paid", tone: "success" },
  retry: { title: "Retry payout", button: "Retry payout", tone: "primary" },
};

function ActionModal({ target, onClose, onDone }: { target: { withdrawal: WithdrawalRow; action: Action } | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setText(""), [target]);
  if (!target) return null;
  const { withdrawal: w, action } = target;
  const copy = COPY[action];

  const submit = async () => {
    setBusy(true);
    try {
      const body = action === "mark-paid" ? { tx_hash: text.trim() || null } : action === "retry" ? {} : { note: text.trim() || null };
      await api.post(`/api/withdrawals/${w.id}/${action}`, body);
      toast.success(action === "reject" ? "Rejected - funds returned to the user" : action === "mark-paid" ? "Marked as paid - the user was notified" : action === "approve" ? (w.auto_payout ? "Approved - the bot will send it automatically" : "Approved") : "Queued for another attempt");
      onDone();
      refreshBadges();
      onClose();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      busy={busy}
      title={`${copy.title} #${w.id}`}
      footer={<><Button onClick={onClose} disabled={busy}>Cancel</Button><Button variant={copy.tone} loading={busy} onClick={() => void submit()}>{copy.button}</Button></>}
    >
      <div className="space-y-4">
        <KeyValue
          items={[
            { label: "User", value: `${flag(w.country)} ${w.alias ?? w.name}` },
            { label: "Method", value: <MethodBadge method={w.method} /> },
            { label: "Requested", value: <Money value={w.amount} /> },
            { label: "Fee kept by platform", value: <Money value={w.fee} /> },
            { label: "Send exactly", value: <strong><Money value={w.net} /></strong> },
            { label: "Requested at", value: dateTime(w.created_at) },
          ]}
        />
        <div>
          <div className="mb-1 text-xs font-medium text-muted">{w.method === "bep20" ? "BEP-20 address (BNB Smart Chain)" : "Binance Pay ID / UID"}</div>
          <div className="flex items-start gap-1 rounded-lg bg-surface2 px-3 py-2"><Mono className="min-w-0 flex-1 !bg-transparent !p-0">{w.address}</Mono><CopyButton text={w.address} /></div>
        </div>

        {action === "mark-paid" && (
          <>
            {w.status === "processing" && <Alert tone="red">A transfer was already broadcast for this withdrawal{w.tx_hash ? ` (${w.tx_hash.slice(0, 10)}…)` : ""}. Only mark it paid if you are sure - sending again would pay twice.</Alert>}
            <Field label={w.method === "bep20" ? "Transaction hash (optional but recommended)" : "Binance reference (optional)"} hint="Stored with the withdrawal so you can prove the payout later. The user is told it was paid.">
              {(id) => <Input id={id} value={text} onChange={(e) => setText(e.target.value)} placeholder={w.method === "bep20" ? "0x…" : "Order / transfer ID"} maxLength={100} />}
            </Field>
          </>
        )}
        {(action === "approve" || action === "reject") && (
          <Field label={action === "reject" ? "Reason shown to the user (optional)" : "Note (optional)"}>
            {(id) => <Input id={id} value={text} onChange={(e) => setText(e.target.value)} maxLength={200} />}
          </Field>
        )}
        {action === "approve" && <Alert tone="blue">{w.auto_payout ? "Automatic payouts are on: after approval the bot sends this from the hot wallet within a minute." : "Approving does not send any money. Send it yourself, then press \"Mark as paid\"."}</Alert>}
        {action === "reject" && <Alert tone="amber">The amount goes back to the user&apos;s available balance and they are notified.</Alert>}
        {action === "retry" && <Alert tone="amber">The previous automatic attempt did not succeed. First check on the chain explorer that no transfer to this address went through, otherwise the user is paid twice.</Alert>}
      </div>
    </Modal>
  );
}

export function WithdrawalsTab() {
  const [view, setView] = useState("handle");
  const [method, setMethod] = useState("");
  const [page, setPage] = useState(1);
  const [target, setTarget] = useState<{ withdrawal: WithdrawalRow; action: Action } | null>(null);
  const { data, error, loading, reload } = useApi<Page<WithdrawalRow>>("/api/withdrawals", {
    to_handle: view === "handle", status: view === "handle" || view === "all" ? "" : view, method, page, page_size: PAGE_SIZE,
  }, 30000);

  const open = (withdrawal: WithdrawalRow, action: Action) => setTarget({ withdrawal, action });

  const columns: Column<WithdrawalRow>[] = [
    { key: "when", header: "Requested", cell: (w) => dateTime(w.created_at) },
    { key: "user", header: "Scanner", cell: (w) => <PersonCell name={w.name} alias={w.alias} country={w.country} /> },
    { key: "method", header: "Method", hideBelow: "sm", cell: (w) => <MethodBadge method={w.method} /> },
    { key: "net", header: "Send", align: "right", cell: (w) => <span><Money value={w.net} unit="" /><div className="text-xs text-muted">fee {w.fee}</div></span> },
    { key: "addr", header: "Destination", hideBelow: "lg", cell: (w) => <span className="inline-flex items-center gap-1"><Mono>{w.address.length > 18 ? `${w.address.slice(0, 8)}…${w.address.slice(-6)}` : w.address}</Mono><CopyButton text={w.address} /></span> },
    {
      key: "status", header: "Status",
      cell: (w) => (
        <span className="inline-flex flex-col items-start gap-0.5">
          <span className="inline-flex flex-wrap items-center gap-1"><StatusBadge status={w.status} />{w.auto_payout && <Badge tone="violet">auto</Badge>}</span>
          {w.tx_hash && <TxRef value={w.tx_hash} method={w.method} />}
          {w.admin_note && <span className="max-w-44 truncate text-xs text-muted" title={w.admin_note}>{w.admin_note}</span>}
        </span>
      ),
    },
    {
      key: "act", header: "", align: "right",
      cell: (w) => (
        <span className="inline-flex flex-wrap justify-end gap-1.5">
          {w.status === "pending" && <Button size="sm" variant="primary" onClick={() => open(w, "approve")}>Approve</Button>}
          {w.status === "failed" && <Button size="sm" variant="primary" onClick={() => open(w, "retry")}>Retry</Button>}
          {["pending", "approved", "processing", "failed"].includes(w.status) && !(w.auto_payout && w.status === "approved") && <Button size="sm" variant="success" onClick={() => open(w, "mark-paid")}>Mark paid</Button>}
          {["pending", "approved", "failed"].includes(w.status) && <Button size="sm" variant="danger" onClick={() => open(w, "reject")}>Reject</Button>}
        </span>
      ),
    },
  ];

  return (
    <>
      <Card padded={false}>
        <FilterBar>
          <Select value={view} onChange={(e) => { setView(e.target.value); setPage(1); }} className="!w-auto" aria-label="Show withdrawals">
            <option value="handle">To handle</option>
            <option value="all">All withdrawals</option>
            <option value="completed">Paid</option>
            <option value="rejected">Rejected</option>
            <option value="failed">Failed</option>
          </Select>
          <Select value={method} onChange={(e) => { setMethod(e.target.value); setPage(1); }} className="!w-auto" aria-label="Method">
            <option value="">All methods</option>
            <option value="bep20">BEP-20</option>
            <option value="binance">Binance</option>
          </Select>
          <Button variant="ghost" icon="refresh" className="ml-auto" onClick={reload}>Refresh</Button>
        </FilterBar>
        {error && <div className="p-4 text-sm text-bad">{error}</div>}
        <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(w) => w.id} empty={view === "handle" ? "No withdrawals are waiting. 🎉" : "No withdrawals match."} />
        <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
      </Card>
      <p className="mt-2 text-xs text-muted">Manual flow: <strong>Approve</strong> → send the exact amount yourself → <strong>Mark paid</strong> (add the tx hash). With automatic payouts enabled, approved BEP-20 withdrawals up to the configured limit are sent by the bot.</p>
      <ActionModal target={target} onClose={() => setTarget(null)} onDone={reload} />
    </>
  );
}
