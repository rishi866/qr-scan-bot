"use client";

import { useState } from "react";
import { FilterBar, PersonCell } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useConfirm, useToast } from "@/components/feedback";
import { ScannerModal } from "@/components/ScannerModal";
import { Badge, Button, Card, ErrorBox, Input, PageHeader, Select } from "@/components/ui";
import { api, errorMessage, useApi } from "@/lib/api";
import { cn } from "@/lib/format";
import type { CoverageResponse, Page, ScannerRow, SlotRow } from "@/lib/types";

const PAGE_SIZE = 25;
const pad = (n: number) => String(n).padStart(2, "0");

function CoverageStrip({ data }: { data: CoverageResponse }) {
  const [picked, setPicked] = useState<number>(data.current_hour);
  const hour = data.hours[picked];
  const gaps = data.hours.filter((h) => h.scanners.length === 0).length;
  return (
    <Card title="Who is on duty, hour by hour (UTC)" actions={<span className="text-xs text-muted">{data.date} · now {pad(data.current_hour)}:00 UTC</span>}>
      <div className="grid grid-cols-6 gap-1.5 sm:grid-cols-12" role="group" aria-label="Coverage per UTC hour">
        {data.hours.map((h) => {
          const n = h.scanners.length;
          return (
            <button
              key={h.hour}
              type="button"
              onClick={() => setPicked(h.hour)}
              aria-pressed={picked === h.hour}
              title={n === 0 ? "Nobody on duty" : `${n} scanner${n === 1 ? "" : "s"}`}
              className={cn(
                "rounded-lg border px-1 py-2 text-center transition-colors",
                n === 0 ? "border-warn/40 bg-warn-soft text-warn" : n === 1 ? "border-ok/30 bg-ok-soft text-ok" : "border-ok/50 bg-ok/25 text-ok",
                picked === h.hour && "ring-2 ring-brand",
                h.hour === data.current_hour && "font-bold",
              )}
            >
              <div className="text-[11px] tabular opacity-80">{pad(h.hour)}</div>
              <div className="text-base font-semibold tabular">{n}</div>
            </button>
          );
        })}
      </div>
      <p className="mt-3 text-xs text-muted">
        {gaps === 0 ? "Every hour of the day has at least one scanner." : <><strong className="text-warn">{gaps} hour{gaps === 1 ? "" : "s"}</strong> without any scanner - senders see &ldquo;no scanner is active&rdquo; then.</>}
      </p>
      <div className="mt-3 rounded-lg bg-surface2 px-3 py-2.5 text-sm">
        <div className="mb-1.5 text-xs font-medium text-muted">{pad(picked)}:00 - {pad((picked + 1) % 24)}:00 UTC</div>
        {hour.scanners.length === 0 ? (
          <span className="text-muted">Nobody is on duty in this hour.</span>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {hour.scanners.map((s) => <Badge key={`${s.user_id}-${s.slot}`} tone="blue">{s.alias ?? "unnamed"} · {s.slot} <span className="opacity-70">({s.timezone})</span></Badge>)}
          </div>
        )}
      </div>
    </Card>
  );
}

export default function SlotsPage() {
  const toast = useToast();
  const confirm = useConfirm();
  const [view, setView] = useState("");
  const [scannerId, setScannerId] = useState("");
  const [timezone, setTimezone] = useState("");
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const coverage = useApi<CoverageResponse>("/api/slots/coverage", undefined, 60000);
  const scanners = useApi<Page<ScannerRow>>("/api/scanners", { page_size: 200 });
  const slots = useApi<Page<SlotRow>>("/api/slots", { page, page_size: PAGE_SIZE, live: view === "live", inactive: view === "inactive", scanner_id: scannerId, timezone: timezone.trim() });

  const reloadAll = () => {
    slots.reload();
    coverage.reload();
  };

  const toggle = async (s: SlotRow) => {
    setBusyId(s.id);
    try {
      await api.patch(`/api/slots/${s.id}`, { is_active: !s.is_active });
      toast.success(s.is_active ? `${s.label} paused` : `${s.label} resumed`);
      reloadAll();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (s: SlotRow) => {
    const ok = await confirm({ title: `Delete ${s.label} of ${s.alias ?? "this scanner"}?`, message: "The scanner can add it again from the bot. Tasks already running are not affected.", confirmLabel: "Delete", tone: "danger" });
    if (!ok) return;
    setBusyId(s.id);
    try {
      await api.del(`/api/slots/${s.id}`);
      toast.success("Slot deleted");
      reloadAll();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusyId(null);
    }
  };

  const columns: Column<SlotRow>[] = [
    { key: "scanner", header: "Scanner", cell: (s) => <PersonCell name={null} alias={s.alias ?? "unnamed"} sub={s.status && s.status !== "approved" ? `scanner ${s.status}` : undefined} /> },
    { key: "label", header: "Local window", cell: (s) => <span className="font-medium tabular">{s.label}</span> },
    { key: "tz", header: "Time zone", hideBelow: "md", cell: (s) => <span>{s.timezone}<div className="text-xs text-muted">{s.tz_label}</div></span> },
    { key: "utc", header: "UTC today", hideBelow: "sm", cell: (s) => <span className="tabular">{s.utc_window}</span> },
    {
      key: "state", header: "State",
      cell: (s) => (!s.is_active ? <Badge tone="red">paused</Badge> : s.status && s.status !== "approved" ? <Badge tone="amber">scanner {s.status}</Badge> : s.live_now ? <Badge tone="green">live now</Badge> : <Badge>scheduled</Badge>),
    },
    { key: "rep", header: "Rep.", align: "right", hideBelow: "lg", cell: (s) => s.reputation },
    {
      key: "actions", header: "", align: "right",
      cell: (s) => (
        <span className="inline-flex gap-1.5">
          <Button size="sm" loading={busyId === s.id} onClick={() => void toggle(s)}>{s.is_active ? "Pause" : "Resume"}</Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(s.user_id)}>Edit scanner</Button>
          <Button size="sm" variant="danger" disabled={busyId === s.id} onClick={() => void remove(s)} aria-label={`Delete ${s.label}`}>Delete</Button>
        </span>
      ),
    },
  ];

  return (
    <>
      <PageHeader title="Slots" subtitle="Daily availability windows of every scanner, converted to UTC. A scanner only gets URLs inside an active slot." />
      <div className="space-y-5">
        {coverage.data ? <CoverageStrip data={coverage.data} /> : coverage.error ? <ErrorBox onRetry={coverage.reload}>{coverage.error}</ErrorBox> : null}

        <Card padded={false} title="All slots">
          <FilterBar>
            <Select value={view} onChange={(e) => { setView(e.target.value); setPage(1); }} className="!w-auto" aria-label="View">
              <option value="">All slots</option>
              <option value="live">Live right now</option>
              <option value="inactive">Paused</option>
            </Select>
            <Select value={scannerId} onChange={(e) => { setScannerId(e.target.value); setPage(1); }} className="!w-auto" aria-label="Scanner">
              <option value="">All scanners</option>
              {scanners.data?.items.map((s) => <option key={s.user_id} value={s.user_id}>{s.alias ?? `${s.name} (unnamed)`}</option>)}
            </Select>
            <Input value={timezone} onChange={(e) => { setTimezone(e.target.value); setPage(1); }} placeholder="Time zone, e.g. Asia/Kolkata" className="!w-56" aria-label="Time zone" />
            <Button variant="ghost" icon="refresh" onClick={reloadAll}>Refresh</Button>
          </FilterBar>
          {slots.error && <div className="p-4 text-sm text-bad">{slots.error}</div>}
          <DataTable columns={columns} rows={slots.data?.items} loading={slots.loading} rowKey={(s) => s.id} empty="No slots match these filters." />
          <Pagination page={page} pageSize={PAGE_SIZE} total={slots.data?.total ?? 0} onChange={setPage} />
        </Card>
      </div>
      <ScannerModal scannerId={editing} onClose={() => setEditing(null)} onChanged={reloadAll} />
    </>
  );
}
