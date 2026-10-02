"use client";

import { useState } from "react";
import { CopyButton, FilterBar, PersonCell, useDebounced } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { Button, Card, Input, KeyValue, LinkButton, Modal, Money, PageHeader, SearchBox, Select, Stat, StatusBadge, Tabs } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { dateTime, flag, timeAgo } from "@/lib/format";
import type { Page, SessionRow, Tx } from "@/lib/types";

const PAGE_SIZE = 25;

type TxPage = Page<Tx> & { totals: { completed_count: number; volume: string; commission: string } };

function TxDetail({ tx, onClose }: { tx: Tx | null; onClose: () => void }) {
  return (
    <Modal open={tx !== null} onClose={onClose} size="lg" title={tx ? <span className="flex items-center gap-2">Transaction #{tx.id} <StatusBadge status={tx.status} /></span> : "Transaction"}>
      {tx && (
        <div className="space-y-4">
          <KeyValue
            items={[
              { label: "Task", value: `#${tx.session_id}` },
              { label: "Slot", value: tx.slot ?? "-" },
              { label: "Scanner reward", value: <Money value={tx.amount} /> },
              { label: "Commission", value: <Money value={tx.commission} /> },
              { label: "Sender", value: `${flag(tx.seller_country)} ${tx.seller_name ?? "-"} · ${tx.seller_timezone ?? "no time zone"}` },
              { label: "Scanner", value: `${flag(tx.scanner_country)} ${tx.scanner_name ?? "-"} · ${tx.scanner_timezone ?? "no time zone"}` },
              { label: "Decided", value: dateTime(tx.confirmed_at) },
              { label: "Created", value: dateTime(tx.created_at) },
            ]}
          />
          <div>
            <div className="mb-1 text-xs font-medium text-muted">URL</div>
            <div className="flex items-start gap-1 rounded-lg bg-surface2 px-3 py-2 text-xs"><code className="min-w-0 flex-1 break-all">{tx.url}</code><CopyButton text={tx.url} /></div>
          </div>
        </div>
      )}
    </Modal>
  );
}

function TransactionsTab() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<Tx | null>(null);
  const q = useDebounced(search);
  const filters = { q, status, date_from: from, date_to: to };
  const { data, error, loading, reload } = useApi<TxPage>("/api/transactions", { ...filters, page, page_size: PAGE_SIZE });

  const reset = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setPage(1);
  };

  const columns: Column<Tx>[] = [
    { key: "id", header: "#", cell: (t) => <span className="tabular text-muted">{t.id}</span> },
    { key: "when", header: "Date", cell: (t) => dateTime(t.confirmed_at ?? t.created_at) },
    { key: "seller", header: "Sender", hideBelow: "sm", cell: (t) => <PersonCell name={t.seller_name} country={t.seller_country} /> },
    { key: "scanner", header: "Scanner", cell: (t) => <PersonCell name={t.scanner_name} country={t.scanner_country} sub={t.slot ? `slot ${t.slot}` : undefined} /> },
    { key: "amount", header: "Reward", align: "right", cell: (t) => <Money value={t.amount} unit="" /> },
    { key: "commission", header: "Commission", align: "right", hideBelow: "md", cell: (t) => <Money value={t.commission} unit="" /> },
    { key: "status", header: "Status", cell: (t) => <StatusBadge status={t.status} /> },
  ];

  return (
    <>
      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Stat label="Completed (filtered)" value={data?.totals.completed_count ?? "-"} icon="check" />
        <Stat label="Volume" value={data ? <Money value={data.totals.volume} /> : "-"} icon="swap" />
        <Stat label="Commission" value={data ? <Money value={data.totals.commission} /> : "-"} icon="wallet" />
      </div>
      <Card padded={false}>
        <FilterBar>
          <SearchBox value={search} onChange={reset(setSearch)} placeholder="Scanner, sender, URL, task #…" />
          <Select value={status} onChange={(e) => reset(setStatus)(e.target.value)} className="!w-auto" aria-label="Status">
            <option value="">All statuses</option>
            <option value="completed">Completed</option>
            <option value="disputed">Disputed</option>
            <option value="refunded">Refunded</option>
          </Select>
          <label className="flex items-center gap-1.5 text-xs text-muted">From <Input type="date" value={from} onChange={(e) => reset(setFrom)(e.target.value)} className="!w-auto" /></label>
          <label className="flex items-center gap-1.5 text-xs text-muted">To <Input type="date" value={to} onChange={(e) => reset(setTo)(e.target.value)} className="!w-auto" /></label>
          <span className="ml-auto flex gap-2">
            <Button variant="ghost" icon="refresh" onClick={reload}>Refresh</Button>
            <LinkButton href={api.url("/api/transactions/export.csv", filters)} icon="download" download>Export CSV</LinkButton>
          </span>
        </FilterBar>
        {error && <div className="p-4 text-sm text-bad">{error}</div>}
        <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(t) => t.id} onRowClick={setOpen} empty="No transactions match these filters." />
        <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
      </Card>
      <p className="mt-2 text-xs text-muted">Dates are shown in your browser&apos;s time zone; the CSV export uses UTC. Totals only count completed transactions.</p>
      <TxDetail tx={open} onClose={() => setOpen(null)} />
    </>
  );
}

function SessionsTab() {
  const [view, setView] = useState("active");
  const [page, setPage] = useState(1);
  const { data, error, loading, reload } = useApi<Page<SessionRow>>("/api/sessions", { page, page_size: PAGE_SIZE, active: view === "active", status: view === "active" || view === "all" ? "" : view }, view === "active" ? 15000 : undefined);

  const columns: Column<SessionRow>[] = [
    { key: "id", header: "Task", cell: (s) => <span className="tabular font-medium">#{s.session_id}</span> },
    { key: "status", header: "Status", cell: (s) => <span className="inline-flex flex-col items-start gap-0.5"><StatusBadge status={s.status} />{s.closed_reason && <span className="text-xs text-muted">{s.closed_reason.replaceAll("_", " ")}</span>}</span> },
    { key: "seller", header: "Sender", hideBelow: "sm", cell: (s) => <PersonCell name={s.seller.name} sub={String(s.seller.telegram_id)} /> },
    { key: "scanner", header: "Scanner", cell: (s) => <PersonCell name={s.scanner.name} alias={s.scanner.alias} sub={s.slot ? `slot ${s.slot}` : undefined} /> },
    { key: "amount", header: "Reward", align: "right", hideBelow: "md", cell: (s) => <Money value={s.amount} unit="" /> },
    { key: "sent", header: "Sent", hideBelow: "md", cell: (s) => <span title={dateTime(s.sent_at)}>{timeAgo(s.sent_at)}</span> },
    { key: "deadline", header: "Next deadline", hideBelow: "lg", cell: (s) => (s.status === "awaiting_scanner" ? dateTime(s.expires_at) : s.status === "accepted" ? dateTime(s.deadline_at) : "-") },
  ];

  return (
    <Card padded={false}>
      <FilterBar>
        <Select value={view} onChange={(e) => { setView(e.target.value); setPage(1); }} className="!w-auto" aria-label="Show">
          <option value="active">In flight right now</option>
          <option value="all">Everything</option>
          <option value="confirmed">Confirmed</option>
          <option value="disputed">Disputed</option>
          <option value="refunded">Refunded</option>
          <option value="expired">Expired (no response)</option>
          <option value="skipped">Skipped</option>
          <option value="timed_out">Timed out</option>
        </Select>
        <span className="text-xs text-muted">{view === "active" ? "Refreshes every 15 s. URLs are only shown on the transaction record." : ""}</span>
        <Button variant="ghost" icon="refresh" onClick={reload} className="ml-auto">Refresh</Button>
      </FilterBar>
      {error && <div className="p-4 text-sm text-bad">{error}</div>}
      <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(s) => s.session_id} empty={view === "active" ? "No task is running at the moment." : "No tasks in this state."} />
      <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
    </Card>
  );
}

export default function TransactionsPage() {
  const [tab, setTab] = useState<"transactions" | "sessions">("transactions");
  return (
    <>
      <PageHeader title="Transactions" subtitle="Every finished task, plus a live view of tasks in flight. The sender pays reward + commission; the scanner receives the reward." />
      <Tabs tabs={[{ id: "transactions", label: "Transactions" }, { id: "sessions", label: "Task monitor" }]} value={tab} onChange={setTab} />
      {tab === "transactions" ? <TransactionsTab /> : <SessionsTab />}
    </>
  );
}
