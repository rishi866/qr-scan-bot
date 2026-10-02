"use client";

import { useEffect, useState } from "react";
import { CountrySelect, useDebounced, useInitialParams, FilterBar, PersonCell } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useConfirm, useToast } from "@/components/feedback";
import { UserDetailModal } from "@/components/UserDetail";
import { Badge, Button, Card, Money, PageHeader, SearchBox, Select, StatusBadge } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import { dateOnly, flag } from "@/lib/format";
import type { Page, UserRow } from "@/lib/types";

const PAGE_SIZE = 25;

export default function UsersPage() {
  const initial = useInitialParams();
  const toast = useToast();
  const confirm = useConfirm();
  const [ready, setReady] = useState(false);
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const [country, setCountry] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const q = useDebounced(search);

  useEffect(() => {
    if (!initial) return;
    setStatus(initial.get("status") ?? "");
    setRole(initial.get("role") ?? "");
    setSearch(initial.get("q") ?? "");
    setReady(true);
  }, [initial]);

  const { data, error, loading, reload } = useApi<Page<UserRow>>(ready ? "/api/users" : null, { role, status, country, q, page, page_size: PAGE_SIZE });

  const decide = async (u: UserRow, action: "approve" | "reject") => {
    if (action === "reject") {
      const ok = await confirm({ title: `Reject ${u.name}?`, message: "They are told to contact the admin. You can approve them again later.", confirmLabel: "Reject", tone: "danger" });
      if (!ok) return;
    }
    setBusyId(u.user_id);
    try {
      await api.post(`/api/users/${u.user_id}/${action}`);
      toast.success(action === "approve" ? `${u.name} approved - they were notified in Telegram` : `${u.name} rejected`);
      reload();
      refreshBadges();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusyId(null);
    }
  };

  const columns: Column<UserRow>[] = [
    { key: "user", header: "User", cell: (u) => <PersonCell name={u.name} alias={u.alias} sub={`${u.username ? `@${u.username} · ` : ""}${u.telegram_id}`} /> },
    { key: "role", header: "Role", cell: (u) => <Badge tone={u.role === "scanner" ? "blue" : "violet"}>{u.role === "scanner" ? "Scanner" : "Sender"}</Badge> },
    { key: "country", header: "Country", hideBelow: "md", cell: (u) => <span title={u.country_name ?? ""}>{flag(u.country)} {u.country ?? "-"}<div className="text-xs text-muted">{u.timezone ?? "no time zone"}</div></span> },
    { key: "status", header: "Status", cell: (u) => <span className="inline-flex flex-wrap items-center gap-1"><StatusBadge status={u.status} />{u.bot_blocked && <Badge tone="amber">blocked bot</Badge>}</span> },
    { key: "balance", header: "Balance", align: "right", hideBelow: "lg", cell: (u) => (u.balance === null ? "-" : <Money value={u.balance} unit="" />) },
    { key: "joined", header: "Joined", hideBelow: "xl", cell: (u) => dateOnly(u.created_at) },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (u) =>
        u.status === "pending" || u.status === "rejected" ? (
          <span className="inline-flex gap-1.5" onClick={(e) => e.stopPropagation()}>
            <Button size="sm" variant="success" loading={busyId === u.user_id} onClick={() => void decide(u, "approve")}>Approve</Button>
            {u.status === "pending" && <Button size="sm" variant="danger" disabled={busyId === u.user_id} onClick={() => void decide(u, "reject")}>Reject</Button>}
          </span>
        ) : (
          <Button size="sm" variant="ghost" onClick={() => setOpen(u.user_id)}>Details</Button>
        ),
    },
  ];

  const change = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setPage(1);
  };

  return (
    <>
      <PageHeader title="Users" subtitle="Senders and scanners. Everyone must be approved here before they can use the bot." />
      <Card padded={false}>
        <FilterBar>
          <SearchBox value={search} onChange={change(setSearch)} placeholder="Name, @username, Telegram ID…" />
          <Select value={role} onChange={(e) => change(setRole)(e.target.value)} className="!w-auto" aria-label="Role">
            <option value="">All roles</option>
            <option value="seller">Senders</option>
            <option value="scanner">Scanners</option>
          </Select>
          <Select value={status} onChange={(e) => change(setStatus)(e.target.value)} className="!w-auto" aria-label="Status">
            <option value="">All statuses</option>
            <option value="pending">Pending approval</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="suspended">Suspended</option>
          </Select>
          <CountrySelect value={country} onChange={change(setCountry)} />
          <Button variant="ghost" icon="refresh" onClick={reload}>Refresh</Button>
        </FilterBar>
        {error && <div className="p-4 text-sm text-bad">{error}</div>}
        <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(u) => u.user_id} onRowClick={(u) => setOpen(u.user_id)} empty="No users match these filters." />
        <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
      </Card>
      <UserDetailModal userId={open} onClose={() => setOpen(null)} onChanged={reload} />
    </>
  );
}
