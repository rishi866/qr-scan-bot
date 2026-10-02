"use client";

import { useCallback, useEffect, useState } from "react";
import { CountrySelect, FilterBar, PersonCell, useDebounced, useInitialParams } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useToast } from "@/components/feedback";
import { ScannerModal } from "@/components/ScannerModal";
import { Alert, Badge, Button, Card, Money, PageHeader, SearchBox, Select, StatusBadge } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import type { Page, ScannerRow } from "@/lib/types";

const PAGE_SIZE = 25;

export default function ScannersPage() {
  const initial = useInitialParams();
  const toast = useToast();
  const [ready, setReady] = useState(false);
  const [filter, setFilter] = useState("");
  const [status, setStatus] = useState("");
  const [country, setCountry] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const q = useDebounced(search);

  useEffect(() => {
    if (!initial) return;
    setFilter(initial.get("needs_name") ? "needs_name" : initial.get("active_now") ? "active_now" : "");
    setStatus(initial.get("status") ?? "");
    setReady(true);
  }, [initial]);

  const { data, error, loading, reload } = useApi<Page<ScannerRow>>(ready ? "/api/scanners" : null, {
    q, status, country, page, page_size: PAGE_SIZE, needs_name: filter === "needs_name", active_now: filter === "active_now",
  });

  const autoName = useCallback(async (s: ScannerRow) => {
    setBusyId(s.user_id);
    try {
      const updated = await api.put<{ alias: string }>(`/api/scanners/${s.user_id}/alias`, { alias: null });
      toast.success(`${s.name} is now ${updated.alias}`);
      reload();
      refreshBadges();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusyId(null);
    }
  }, [reload, toast]);

  const columns: Column<ScannerRow>[] = [
    { key: "name", header: "Scanner", cell: (s) => (s.alias ? <PersonCell name={s.name} alias={s.alias} sub={s.username ? `@${s.username}` : String(s.telegram_id)} country={s.country} /> : <div><div className="font-medium text-warn">Unnamed</div><div className="text-xs text-muted">{s.name}{s.username ? ` · @${s.username}` : ""}</div></div>) },
    { key: "status", header: "Status", cell: (s) => <span className="inline-flex flex-wrap items-center gap-1"><StatusBadge status={s.status} />{s.active_now && <Badge tone="green">live</Badge>}</span> },
    { key: "tz", header: "Time zone", hideBelow: "lg", cell: (s) => <span>{s.timezone ?? "-"}<div className="text-xs text-muted">{s.utc_offset}</div></span> },
    {
      key: "slots", header: "Slots", hideBelow: "md",
      cell: (s) => s.slot_labels.length === 0 ? <span className="text-xs text-muted">none</span> : (
        <span className="flex max-w-56 flex-wrap gap-1">
          {s.slots.slice(0, 4).map((x) => <Badge key={x.id} tone={x.live_now ? "green" : x.is_active ? "gray" : "red"} className="tabular">{x.label}</Badge>)}
          {s.slots.length > 4 && <Badge>+{s.slots.length - 4}</Badge>}
        </span>
      ),
    },
    { key: "rep", header: "Rep.", align: "right", hideBelow: "sm", cell: (s) => s.reputation ?? "-" },
    { key: "tasks", header: "Tasks", align: "right", hideBelow: "xl", cell: (s) => s.completed_tasks },
    { key: "earned", header: "Earned", align: "right", hideBelow: "xl", cell: (s) => <Money value={s.total_earned} unit="" /> },
    {
      key: "actions", header: "", align: "right",
      cell: (s) => (
        <span className="inline-flex gap-1.5" onClick={(e) => e.stopPropagation()}>
          {!s.alias && s.status === "approved" && <Button size="sm" variant="success" loading={busyId === s.user_id} onClick={() => void autoName(s)}>Auto-name</Button>}
          <Button size="sm" onClick={() => setOpenId(s.user_id)}>Manage</Button>
        </span>
      ),
    },
  ];

  const resetPage = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setPage(1);
  };

  return (
    <>
      <PageHeader title="Scanners" subtitle="Give every approved scanner an anonymous name (user1, user2, …), tune reputation and edit their slots." />
      {filter !== "needs_name" && data && data.items.some((s) => !s.alias && s.status === "approved") && (
        <div className="mb-4"><Alert tone="amber">Some approved scanners still have no name on this page. They cannot receive URLs until named - use <strong>Auto-name</strong> or <strong>Manage</strong>.</Alert></div>
      )}
      <Card padded={false}>
        <FilterBar>
          <SearchBox value={search} onChange={resetPage(setSearch)} placeholder="Name, @username, user3…" />
          <Select value={filter} onChange={(e) => resetPage(setFilter)(e.target.value)} className="!w-auto" aria-label="Filter">
            <option value="">All scanners</option>
            <option value="needs_name">Needs a name</option>
            <option value="active_now">Live right now</option>
          </Select>
          <Select value={status} onChange={(e) => resetPage(setStatus)(e.target.value)} className="!w-auto" aria-label="Status">
            <option value="">All statuses</option>
            <option value="approved">Approved</option>
            <option value="pending">Pending</option>
            <option value="suspended">Suspended</option>
            <option value="rejected">Rejected</option>
          </Select>
          <CountrySelect value={country} onChange={resetPage(setCountry)} />
          <Button variant="ghost" icon="refresh" onClick={reload}>Refresh</Button>
        </FilterBar>
        {error && <div className="p-4 text-sm text-bad">{error}</div>}
        <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(s) => s.user_id} onRowClick={(s) => setOpenId(s.user_id)} empty="No scanners match these filters." />
        <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
      </Card>
      <ScannerModal scannerId={openId} onClose={() => setOpenId(null)} onChanged={reload} />
    </>
  );
}
