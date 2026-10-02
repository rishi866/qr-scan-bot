"use client";

import { useEffect, useState } from "react";
import { CopyButton, FilterBar, PersonCell, useDebounced } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useConfirm, useToast } from "@/components/feedback";
import { Alert, Badge, Button, Card, Field, Input, Modal, Money, SearchBox, Select, Spinner, Stat } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import { dateTime, money } from "@/lib/format";
import type { Page, WalletDetail, WalletRow, WalletsSummary } from "@/lib/types";

const PAGE_SIZE = 25;
const LEDGER_PAGE = 15;

const LEDGER_LABEL: Record<string, string> = {
  deposit: "Deposit", hold: "Reserved for task", release: "Released", payment: "Task paid", earning: "Task reward", commission: "Commission",
  withdrawal_lock: "Withdrawal requested", withdrawal_unlock: "Withdrawal returned", withdrawal_paid: "Withdrawal paid", withdrawal_fee: "Withdrawal fee", adjustment: "Manual adjustment",
};

function Signed({ value }: { value: string }) {
  const n = Number(value);
  if (!n) return <span className="text-muted">-</span>;
  return <span className={n > 0 ? "text-ok" : "text-bad"}>{n > 0 ? "+" : ""}{money(value)}</span>;
}

function WalletModal({ userId, onClose, onChanged }: { userId: number | null; onClose: () => void; onChanged: () => void }) {
  const [page, setPage] = useState(1);
  const { data, error, reload } = useApi<WalletDetail>(userId === null ? null : `/api/wallets/${userId}`, { page, page_size: LEDGER_PAGE });
  const toast = useToast();
  const confirm = useConfirm();
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setPage(1);
    setAmount("");
    setReason("");
  }, [userId]);

  const w = data?.wallet;
  const delta = Number(amount);
  const validAmount = amount.trim() !== "" && Number.isFinite(delta) && delta !== 0;

  const adjust = async () => {
    if (!w) return;
    const ok = await confirm({
      title: `${delta > 0 ? "Add" : "Remove"} ${money(Math.abs(delta))} USDT ${delta > 0 ? "to" : "from"} ${w.alias ?? w.name}?`,
      message: <>This changes the user&apos;s available balance immediately and is written to the ledger and the audit log. Reason: <em>{reason}</em></>,
      confirmLabel: "Apply adjustment",
      tone: delta > 0 ? "primary" : "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api.post(`/api/wallets/${w.user_id}/adjust`, { amount: amount.trim(), reason: reason.trim() });
      toast.success("Balance adjusted");
      setAmount("");
      setReason("");
      reload();
      onChanged();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={userId !== null} onClose={onClose} busy={busy} size="xl" title={w ? <span className="flex flex-wrap items-center gap-2">{w.alias ?? w.name} <Badge tone={w.role === "scanner" ? "blue" : "violet"}>{w.role === "scanner" ? "Scanner" : "Sender"}</Badge></span> : "Wallet"}>
      {!w && error && <div className="text-sm text-bad">{error}</div>}
      {!w && !error && <div className="flex items-center gap-2 py-10 text-muted"><Spinner /> Loading…</div>}
      {w && data && (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <Stat label="Available" value={<Money value={w.balance} unit="" />} />
            <Stat label="Held in tasks" value={<Money value={w.pending} unit="" />} />
            <Stat label="Earned" value={<Money value={w.total_earned} unit="" />} />
            <Stat label="Spent" value={<Money value={w.total_spent} unit="" />} />
            <Stat label="Deposited" value={<Money value={w.total_deposited} unit="" />} />
            <Stat label="Withdrawn" value={<Money value={w.total_withdrawn} unit="" />} />
          </div>
          {(w.bep20_address || w.binance_address) && (
            <div className="space-y-1 text-xs text-muted">
              {w.bep20_address && <div className="flex flex-wrap items-center gap-1">Saved BEP-20 address: <code className="break-all text-fg">{w.bep20_address}</code><CopyButton text={w.bep20_address} /></div>}
              {w.binance_address && <div className="flex flex-wrap items-center gap-1">Saved Binance ID: <code className="break-all text-fg">{w.binance_address}</code><CopyButton text={w.binance_address} /></div>}
            </div>
          )}

          <Card title="Manual adjustment" className="bg-surface2/40">
            <div className="grid gap-3 sm:grid-cols-[10rem_1fr_auto] sm:items-end">
              <Field label="Amount (+ or −)">{(id) => <Input id={id} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="-2.5" />}</Field>
              <Field label="Reason (required, 3+ characters)">{(id) => <Input id={id} value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Binance deposit 123 verified by hand" />}</Field>
              <Button variant="primary" loading={busy} disabled={!validAmount || reason.trim().length < 3} onClick={() => void adjust()}>Apply</Button>
            </div>
            <p className="mt-2 text-xs text-muted">For corrections only (a deposit that was not detected, a mistaken payout…). A removal can never push the balance below zero.</p>
          </Card>

          <div className="rounded-lg border border-line">
            <DataTable
              rows={data.ledger.items}
              rowKey={(e) => e.id}
              empty="No ledger entries yet."
              columns={[
                { key: "when", header: "When", cell: (e) => dateTime(e.created_at) },
                { key: "type", header: "Entry", cell: (e) => <span>{LEDGER_LABEL[e.type] ?? e.type}{e.note && <div className="max-w-64 truncate text-xs text-muted" title={e.note}>{e.note}</div>}</span> },
                { key: "bal", header: "Available", align: "right", cell: (e) => <Signed value={e.balance_delta} /> },
                { key: "pend", header: "Held", align: "right", hideBelow: "sm", cell: (e) => <Signed value={e.pending_delta} /> },
                { key: "after", header: "Balance after", align: "right", hideBelow: "md", cell: (e) => <Money value={e.balance_after} unit="" /> },
              ]}
            />
            <Pagination page={page} pageSize={LEDGER_PAGE} total={data.ledger.total} onChange={setPage} />
          </div>
        </div>
      )}
    </Modal>
  );
}

export function WalletsTab({ initialQuery }: { initialQuery: string }) {
  const toast = useToast();
  const [search, setSearch] = useState(initialQuery);
  const [role, setRole] = useState("");
  const [nonZero, setNonZero] = useState(false);
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);
  const q = useDebounced(search);

  const summary = useApi<WalletsSummary>("/api/wallets/summary");
  const { data, error, loading, reload } = useApi<Page<WalletRow>>("/api/wallets", { q, role, non_zero: nonZero, page, page_size: PAGE_SIZE });

  const changed = () => {
    reload();
    summary.reload();
    refreshBadges();
  };

  const s = summary.data;
  const sellerFunds = s ? Number(s.by_role.seller?.balance ?? 0) + Number(s.by_role.seller?.pending ?? 0) : null;
  const scannerFunds = s ? Number(s.by_role.scanner?.balance ?? 0) + Number(s.by_role.scanner?.pending ?? 0) : null;

  const columns: Column<WalletRow>[] = [
    { key: "user", header: "User", cell: (w) => <PersonCell name={w.name} alias={w.alias} country={w.country} sub={w.username ? `@${w.username}` : String(w.telegram_id)} /> },
    { key: "role", header: "Role", hideBelow: "sm", cell: (w) => <Badge tone={w.role === "scanner" ? "blue" : "violet"}>{w.role === "scanner" ? "Scanner" : "Sender"}</Badge> },
    { key: "bal", header: "Available", align: "right", cell: (w) => <Money value={w.balance} unit="" /> },
    { key: "pend", header: "Held", align: "right", hideBelow: "md", cell: (w) => <Money value={w.pending} unit="" /> },
    { key: "dep", header: "Deposited", align: "right", hideBelow: "lg", cell: (w) => <Money value={w.total_deposited} unit="" /> },
    { key: "earned", header: "Earned", align: "right", hideBelow: "lg", cell: (w) => <Money value={w.total_earned} unit="" /> },
    { key: "wd", header: "Withdrawn", align: "right", hideBelow: "xl", cell: (w) => <Money value={w.total_withdrawn} unit="" /> },
    { key: "act", header: "", align: "right", cell: (w) => <Button size="sm" onClick={() => setOpenId(w.user_id)}>Ledger</Button> },
  ];

  return (
    <>
      {s && !s.ledger.ok && (
        <div className="mb-4">
          <Alert tone="red">
            <strong>The ledger does not add up.</strong> Stop manual payouts and investigate: <code>python -m app.cli reconcile</code>
            <ul className="mt-1 list-inside list-disc text-xs">{s.ledger.problems.slice(0, 5).map((p) => <li key={p}>{p}</li>)}</ul>
          </Alert>
        </div>
      )}
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <Stat label="Sender funds" value={sellerFunds === null ? "-" : <Money value={sellerFunds} />} hint="available + held in tasks" icon="wallet" />
        <Stat label="Scanner funds" value={scannerFunds === null ? "-" : <Money value={scannerFunds} />} hint="owed to scanners" icon="scanner" />
        <Stat label="Platform commission" value={s ? <Money value={s.platform_commission} /> : "-"} icon="chart" />
        <Stat label="Deposited (all time)" value={s ? <Money value={s.deposits_credited} /> : "-"} icon="download" />
        <Stat label="Paid out (all time)" value={s ? <Money value={s.withdrawals_paid} /> : "-"} hint={s ? (s.ledger.ok ? "ledger balanced ✓" : "ledger mismatch!") : undefined} tone={s && !s.ledger.ok ? "red" : undefined} icon="swap" />
      </div>
      <Card padded={false}>
        <FilterBar>
          <SearchBox value={search} onChange={(v) => { setSearch(v); setPage(1); }} placeholder="Name, @username, user3, ID…" />
          <Select value={role} onChange={(e) => { setRole(e.target.value); setPage(1); }} className="!w-auto" aria-label="Role">
            <option value="">All roles</option>
            <option value="seller">Senders</option>
            <option value="scanner">Scanners</option>
          </Select>
          <label className="flex items-center gap-1.5 text-sm text-muted"><input type="checkbox" checked={nonZero} onChange={(e) => { setNonZero(e.target.checked); setPage(1); }} className="h-4 w-4 accent-[var(--brand)]" /> Only with funds</label>
          <Button variant="ghost" icon="refresh" className="ml-auto" onClick={() => { reload(); summary.reload(); toast.info("Refreshed"); }}>Refresh</Button>
        </FilterBar>
        {error && <div className="p-4 text-sm text-bad">{error}</div>}
        <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(w) => w.user_id} onRowClick={(w) => setOpenId(w.user_id)} empty="No wallets match these filters." />
        <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
      </Card>
      <WalletModal userId={openId} onClose={() => setOpenId(null)} onChanged={changed} />
    </>
  );
}
