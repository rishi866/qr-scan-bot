"use client";

import { useState } from "react";
import { FilterBar, useDebounced } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { Badge, Button, Card, Input } from "@/components/ui";
import { useApi } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { AuditRow, Page } from "@/lib/types";

const PAGE_SIZE = 25;

function details(row: AuditRow): string {
  if (!row.details) return "";
  return Object.entries(row.details)
    .map(([k, v]) => `${k}: ${typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
}

export function AuditTab() {
  const [action, setAction] = useState("");
  const [admin, setAdmin] = useState("");
  const [page, setPage] = useState(1);
  const a = useDebounced(action);
  const u = useDebounced(admin);
  const { data, error, loading, reload } = useApi<Page<AuditRow>>("/api/audit", { action: a, admin: u, page, page_size: PAGE_SIZE });

  const columns: Column<AuditRow>[] = [
    { key: "when", header: "When", cell: (r) => dateTime(r.created_at) },
    { key: "admin", header: "Admin", cell: (r) => <span className="font-medium">{r.admin}</span> },
    { key: "action", header: "Action", cell: (r) => <Badge tone="blue">{r.action.replaceAll("_", " ")}</Badge> },
    { key: "target", header: "Target", hideBelow: "sm", cell: (r) => (r.target_type ? `${r.target_type} ${r.target_id ?? ""}` : "-") },
    { key: "details", header: "Details", hideBelow: "md", cell: (r) => <span className="line-clamp-2 max-w-md break-words text-xs text-muted" title={details(r)}>{details(r)}</span> },
    { key: "ip", header: "IP", hideBelow: "lg", cell: (r) => <span className="text-xs text-muted">{r.ip ?? "-"}</span> },
  ];

  return (
    <Card padded={false}>
      <FilterBar>
        <Input value={action} onChange={(e) => { setAction(e.target.value); setPage(1); }} placeholder="Action contains… (e.g. withdrawal)" className="!w-64" aria-label="Action" />
        <Input value={admin} onChange={(e) => { setAdmin(e.target.value); setPage(1); }} placeholder="Admin username" className="!w-44" aria-label="Admin" />
        <Button variant="ghost" icon="refresh" className="ml-auto" onClick={reload}>Refresh</Button>
      </FilterBar>
      {error && <div className="p-4 text-sm text-bad">{error}</div>}
      <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(r) => r.id} empty="Nothing recorded yet." />
      <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
    </Card>
  );
}
