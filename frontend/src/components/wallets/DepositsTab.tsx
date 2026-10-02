"use client";

import { useEffect, useState } from "react";
import { FilterBar, PersonCell, useDebounced } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useToast } from "@/components/feedback";
import { MethodBadge, TxRef } from "@/components/wallets/shared";
import { Alert, Button, Card, Field, Input, KeyValue, Modal, Money, SearchBox, Select, StatusBadge } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import { dateTime, flag } from "@/lib/format";
import type { DepositRow, Page } from "@/lib/types";

const PAGE_SIZE = 25;

function ReviewModal({ deposit, onClose, onDone }: { deposit: DepositRow | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"confirm" | "reject" | null>(null);

  useEffect(() => {
    setAmount(deposit?.amount ?? "");
    setNote("");
  }, [deposit]);

  const submit = async (action: "confirm" | "reject") => {
    if (!deposit) return;
    setBusy(action);
    try {
      await api.post(`/api/deposits/${deposit.id}/${action}`, action === "confirm" ? { amount: amount.trim() || null, note: note.trim() || null } : { note: note.trim() || null });
      toast.success(action === "confirm" ? "Deposit credited" : "Deposit rejected");
      onDone();
      refreshBadges();
      onClose();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const claim = deposit?.method === "binance";
  return (
    <Modal
      open={deposit !== null}
      onClose={onClose}
      busy={busy !== null}
      title={deposit ? `Deposit #${deposit.id} from ${deposit.alias ?? deposit.name}` : "Deposit"}
      footer={
        <>
          <Button variant="danger" loading={busy === "reject"} disabled={busy === "confirm"} onClick={() => void submit("reject")}>Reject</Button>
          <Button variant="success" icon="check" loading={busy === "confirm"} disabled={busy === "reject"} onClick={() => void submit("confirm")}>Credit to wallet</Button>
        </>
      }
    >
      {deposit && (
        <div className="space-y-4">
          {claim ? (
            <Alert tone="blue">The user says they sent this through Binance. Open Binance → Pay / Wallet history, find an incoming transfer with this reference and the same amount, then credit it. If you cannot find it, reject it - the user can claim again.</Alert>
          ) : (
            <Alert tone="amber">This on-chain transfer was seen but is below the minimum deposit. Credit it if you want to honour it, or reject it (the funds stay on the platform&apos;s deposit address).</Alert>
          )}
          <KeyValue
            items={[
              { label: "User", value: `${flag(deposit.country)} ${deposit.name}${deposit.alias ? ` (${deposit.alias})` : ""}` },
              { label: "Telegram ID", value: deposit.telegram_id },
              { label: "Method", value: <MethodBadge method={deposit.method} /> },
              { label: "Claimed / seen", value: dateTime(deposit.created_at) },
              { label: claim ? "Binance reference" : "Transaction", value: <TxRef value={deposit.tx_hash} method={deposit.method} /> },
              { label: "Status", value: <StatusBadge status={deposit.status} /> },
            ]}
          />
          {deposit.note && <p className="rounded-lg bg-surface2 px-3 py-2 text-sm">{deposit.note}</p>}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Amount to credit (USDT)" hint="Change it if you actually received a different amount.">{(id) => <Input id={id} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />}</Field>
            <Field label="Note (optional)">{(id) => <Input id={id} value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} placeholder="e.g. matched in Binance history" />}</Field>
          </div>
        </div>
      )}
    </Modal>
  );
}

export function DepositsTab({ initialView }: { initialView?: string }) {
  const [view, setView] = useState(initialView ?? "review");
  const [method, setMethod] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [review, setReview] = useState<DepositRow | null>(null);
  const q = useDebounced(search);
  const { data, error, loading, reload } = useApi<Page<DepositRow>>("/api/deposits", {
    needs_review: view === "review", status: view === "review" || view === "all" ? "" : view, method, q, page, page_size: PAGE_SIZE,
  }, 30000);

  const columns: Column<DepositRow>[] = [
    { key: "when", header: "Date", cell: (d) => dateTime(d.created_at) },
    { key: "user", header: "User", cell: (d) => <PersonCell name={d.name} alias={d.alias} country={d.country} /> },
    { key: "method", header: "Method", hideBelow: "sm", cell: (d) => <MethodBadge method={d.method} /> },
    { key: "amount", header: "Amount", align: "right", cell: (d) => <Money value={d.amount} unit="" /> },
    { key: "ref", header: "Reference", hideBelow: "md", cell: (d) => <TxRef value={d.tx_hash} method={d.method} /> },
    { key: "status", header: "Status", cell: (d) => <span className="inline-flex flex-col items-start gap-0.5"><StatusBadge status={d.status} />{d.note && <span className="max-w-48 truncate text-xs text-muted" title={d.note}>{d.note}</span>}</span> },
    { key: "act", header: "", align: "right", cell: (d) => (d.status === "pending" || d.status === "below_min" ? <Button size="sm" variant="primary" onClick={() => setReview(d)}>Review</Button> : null) },
  ];

  return (
    <>
      <Card padded={false}>
        <FilterBar>
          <SearchBox value={search} onChange={(v) => { setSearch(v); setPage(1); }} placeholder="User, tx hash, address…" />
          <Select value={view} onChange={(e) => { setView(e.target.value); setPage(1); }} className="!w-auto" aria-label="Show deposits">
            <option value="review">Needs review</option>
            <option value="all">All deposits</option>
            <option value="credited">Credited</option>
            <option value="rejected">Rejected</option>
          </Select>
          <Select value={method} onChange={(e) => { setMethod(e.target.value); setPage(1); }} className="!w-auto" aria-label="Method">
            <option value="">All methods</option>
            <option value="bep20">BEP-20</option>
            <option value="binance">Binance</option>
          </Select>
          <Button variant="ghost" icon="refresh" className="ml-auto" onClick={reload}>Refresh</Button>
        </FilterBar>
        {error && <div className="p-4 text-sm text-bad">{error}</div>}
        <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(d) => d.id} onRowClick={(d) => (d.status === "pending" || d.status === "below_min" ? setReview(d) : undefined)} empty={view === "review" ? "Nothing to review - on-chain deposits are credited automatically." : "No deposits match."} />
        <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
      </Card>
      <p className="mt-2 text-xs text-muted">On-chain (BEP-20) deposits are credited automatically after the required confirmations. Only Binance claims and deposits below the minimum wait here.</p>
      <ReviewModal deposit={review} onClose={() => setReview(null)} onDone={reload} />
    </>
  );
}
