"use client";

import { type ReactNode, useState } from "react";
import { BarChart, ComboChart } from "@/components/Charts";
import { DataTable, type Column } from "@/components/DataTable";
import { Button, Card, ErrorBox, Input, LinkButton, Money, PageHeader, Select, Spinner, Stat, Tabs } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { flag, money, today } from "@/lib/format";

type Tab = "overview" | "countries" | "scanners" | "senders" | "commission";
type Group = "day" | "month";

interface Range { from: string; to: string }

const PRESETS: { label: string; range: () => Range }[] = [
  { label: "7 days", range: () => ({ from: today(-6), to: today() }) },
  { label: "30 days", range: () => ({ from: today(-29), to: today() }) },
  { label: "90 days", range: () => ({ from: today(-89), to: today() }) },
  { label: "This month", range: () => ({ from: `${today().slice(0, 8)}01`, to: today() }) },
  { label: "All time", range: () => ({ from: "2000-01-01", to: today() }) },
];

interface SummaryRow { period: string; completed: number; volume: string; commission: string; refunded: number; disputed: number }
interface CountryRow { country: string; country_name: string; tasks: number; volume: string; commission: string }
interface ScannerReportRow { scanner_id: number; alias: string; country: string; tasks: number; earned: string; refunded: number; disputed: number }
interface SellerReportRow { seller_id: number; name: string; country: string; tasks: number; spent: string; refunded: number; disputed: number }
interface CommissionRow { period: string; task_commission: string; withdrawal_fees: string; total: string; cumulative: string }

function Section<T>({ title, path, params, actions, children }: { title: string; path: string; params: Record<string, string>; actions?: ReactNode; children: (rows: T[], extra: Record<string, unknown>) => ReactNode }) {
  const { data, error, loading, reload } = useApi<{ items: T[] } & Record<string, unknown>>(path, params);
  return (
    <Card title={title} padded={false} actions={<>{actions}<LinkButton href={api.url(path, { ...params, format: "csv" })} icon="download" download className="!px-2.5 !py-1.5 !text-xs">CSV</LinkButton></>}>
      {error && <div className="p-4"><ErrorBox onRetry={reload}>{error}</ErrorBox></div>}
      {!data && !error && loading && <div className="flex items-center gap-2 p-6 text-muted"><Spinner /> Loading…</div>}
      {data && children(data.items, data)}
    </Card>
  );
}

function Overview({ range, group }: { range: Range; group: Group }) {
  const params = { group, date_from: range.from, date_to: range.to };
  return (
    <Section<SummaryRow> title={`Completed tasks per ${group}`} path="/api/reports/summary" params={params}>
      {(rows, extra) => {
        const totals = extra.totals as { completed: number; volume: string; commission: string };
        const refunded = rows.reduce((n, r) => n + r.refunded, 0);
        const disputed = rows.reduce((n, r) => n + r.disputed, 0);
        const columns: Column<SummaryRow>[] = [
          { key: "p", header: group === "day" ? "Day" : "Month", cell: (r) => r.period },
          { key: "c", header: "Completed", align: "right", cell: (r) => r.completed },
          { key: "v", header: "Scanner rewards", align: "right", cell: (r) => <Money value={r.volume} unit="" /> },
          { key: "m", header: "Commission", align: "right", cell: (r) => <Money value={r.commission} unit="" /> },
          { key: "r", header: "Refunded", align: "right", hideBelow: "sm", cell: (r) => r.refunded },
          { key: "d", header: "Still disputed", align: "right", hideBelow: "sm", cell: (r) => r.disputed },
        ];
        return (
          <div>
            <div className="grid grid-cols-2 gap-3 p-4 md:grid-cols-4">
              <Stat label="Completed" value={totals.completed} icon="check" />
              <Stat label="Scanner rewards" value={<Money value={totals.volume} />} icon="swap" />
              <Stat label="Commission" value={<Money value={totals.commission} />} icon="wallet" />
              <Stat label="Refunded / disputed" value={`${refunded} / ${disputed}`} icon="shield" />
            </div>
            {rows.length > 0 && (
              <div className="px-4 pb-4">
                <ComboChart labels={rows.map((r) => (group === "day" ? r.period.slice(5) : r.period))} bars={{ label: "Completed tasks", data: rows.map((r) => r.completed) }} line={{ label: "Commission (USDT)", data: rows.map((r) => Number(r.commission)) }} />
              </div>
            )}
            <DataTable columns={columns} rows={[...rows].reverse()} rowKey={(r) => r.period} empty="No tasks in this period." />
          </div>
        );
      }}
    </Section>
  );
}

function Countries({ range }: { range: Range }) {
  const [side, setSide] = useState<"scanner" | "seller">("scanner");
  const params = { side, date_from: range.from, date_to: range.to };
  const toggle = (
    <Select value={side} onChange={(e) => setSide(e.target.value as "scanner" | "seller")} className="!w-auto !py-1 !text-xs" aria-label="Group by">
      <option value="scanner">By scanner country</option>
      <option value="seller">By sender country</option>
    </Select>
  );
  return (
    <Section<CountryRow> title="Country-wise" path="/api/reports/by-country" params={params} actions={toggle}>
      {(rows) => {
        const columns: Column<CountryRow>[] = [
          { key: "c", header: "Country", cell: (r) => <span>{flag(r.country)} {r.country_name}</span> },
          { key: "t", header: "Completed tasks", align: "right", cell: (r) => r.tasks },
          { key: "v", header: "Scanner rewards", align: "right", cell: (r) => <Money value={r.volume} unit="" /> },
          { key: "m", header: "Commission", align: "right", hideBelow: "sm", cell: (r) => <Money value={r.commission} unit="" /> },
        ];
        const top = rows.slice(0, 10);
        return (
          <div>
            {top.length > 0 && <div className="p-4"><BarChart labels={top.map((r) => r.country_name)} series={[{ label: "Completed tasks", data: top.map((r) => r.tasks) }]} /></div>}
            <DataTable columns={columns} rows={rows} rowKey={(r) => r.country} empty="No tasks in this period." />
          </div>
        );
      }}
    </Section>
  );
}

function Scanners({ range }: { range: Range }) {
  return (
    <Section<ScannerReportRow> title="Scanner-wise" path="/api/reports/by-scanner" params={{ date_from: range.from, date_to: range.to }}>
      {(rows) => (
        <DataTable
          rows={rows}
          rowKey={(r) => r.scanner_id}
          empty="No tasks in this period."
          columns={[
            { key: "a", header: "Scanner", cell: (r) => <span className="font-medium">{flag(r.country)} {r.alias}</span> },
            { key: "t", header: "Completed", align: "right", cell: (r) => r.tasks },
            { key: "e", header: "Earned", align: "right", cell: (r) => <Money value={r.earned} unit="" /> },
            { key: "r", header: "Refunded", align: "right", hideBelow: "sm", cell: (r) => r.refunded },
            { key: "d", header: "Disputed", align: "right", hideBelow: "sm", cell: (r) => r.disputed },
            { key: "q", header: "Dispute rate", align: "right", hideBelow: "md", cell: (r) => (r.tasks + r.refunded + r.disputed > 0 ? `${Math.round(((r.refunded + r.disputed) / (r.tasks + r.refunded + r.disputed)) * 100)}%` : "-") },
          ]}
        />
      )}
    </Section>
  );
}

function Senders({ range }: { range: Range }) {
  return (
    <Section<SellerReportRow> title="Sender-wise" path="/api/reports/by-seller" params={{ date_from: range.from, date_to: range.to }}>
      {(rows) => (
        <DataTable
          rows={rows}
          rowKey={(r) => r.seller_id}
          empty="No tasks in this period."
          columns={[
            { key: "a", header: "Sender", cell: (r) => <span className="font-medium">{flag(r.country)} {r.name}</span> },
            { key: "t", header: "Completed", align: "right", cell: (r) => r.tasks },
            { key: "s", header: "Spent", align: "right", cell: (r) => <Money value={r.spent} unit="" /> },
            { key: "r", header: "Refunded", align: "right", hideBelow: "sm", cell: (r) => r.refunded },
            { key: "d", header: "Disputed", align: "right", hideBelow: "sm", cell: (r) => r.disputed },
          ]}
        />
      )}
    </Section>
  );
}

function Commission({ range, group }: { range: Range; group: Group }) {
  return (
    <Section<CommissionRow> title={`Platform income per ${group}`} path="/api/reports/commission" params={{ group, date_from: range.from, date_to: range.to }}>
      {(rows, extra) => {
        const sum = (key: "task_commission" | "withdrawal_fees" | "total") => rows.reduce((n, r) => n + Number(r[key]), 0);
        return (
          <div>
            <div className="grid grid-cols-2 gap-3 p-4 md:grid-cols-4">
              <Stat label="Task commission" value={<Money value={sum("task_commission")} />} hint="in this period" icon="swap" />
              <Stat label="Withdrawal fees" value={<Money value={sum("withdrawal_fees")} />} hint="in this period" icon="download" />
              <Stat label="Total (period)" value={<Money value={sum("total")} />} icon="chart" />
              <Stat label="All-time income" value={<Money value={String(extra.all_time_total ?? "0")} />} icon="wallet" tone="green" />
            </div>
            {rows.length > 0 && (
              <div className="px-4 pb-4">
                <BarChart stacked labels={rows.map((r) => (group === "day" ? r.period.slice(5) : r.period))} series={[{ label: "Task commission", data: rows.map((r) => Number(r.task_commission)) }, { label: "Withdrawal fees", data: rows.map((r) => Number(r.withdrawal_fees)) }]} />
              </div>
            )}
            <DataTable
              rows={[...rows].reverse()}
              rowKey={(r) => r.period}
              empty="No income in this period."
              columns={[
                { key: "p", header: group === "day" ? "Day" : "Month", cell: (r) => r.period },
                { key: "t", header: "Task commission", align: "right", cell: (r) => money(r.task_commission, 4) },
                { key: "f", header: "Withdrawal fees", align: "right", hideBelow: "sm", cell: (r) => money(r.withdrawal_fees, 4) },
                { key: "x", header: "Total", align: "right", cell: (r) => money(r.total, 4) },
                { key: "c", header: "Cumulative", align: "right", hideBelow: "md", cell: (r) => money(r.cumulative, 4) },
              ]}
            />
          </div>
        );
      }}
    </Section>
  );
}

export default function ReportsPage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [range, setRange] = useState<Range>(PRESETS[1].range());
  const [group, setGroup] = useState<Group>("day");
  const invalid = range.from !== "" && range.to !== "" && range.from > range.to;

  return (
    <>
      <PageHeader title="Reports" subtitle="Dates are UTC. Only decided tasks count; each CSV button exports exactly what you see." />
      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-3">
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Quick ranges">
          {PRESETS.map((p) => <Button key={p.label} size="sm" onClick={() => setRange(p.range())}>{p.label}</Button>)}
        </div>
        <label className="text-xs text-muted">From <Input type="date" value={range.from} onChange={(e) => setRange({ ...range, from: e.target.value })} className="mt-1 !w-auto" /></label>
        <label className="text-xs text-muted">To <Input type="date" value={range.to} onChange={(e) => setRange({ ...range, to: e.target.value })} className="mt-1 !w-auto" /></label>
        {(tab === "overview" || tab === "commission") && (
          <label className="text-xs text-muted">Group by
            <Select value={group} onChange={(e) => setGroup(e.target.value as Group)} className="mt-1 !w-auto"><option value="day">Day</option><option value="month">Month</option></Select>
          </label>
        )}
        {invalid && <span className="text-xs text-bad">&ldquo;From&rdquo; is after &ldquo;To&rdquo;.</span>}
      </div>
      <Tabs
        tabs={[{ id: "overview", label: "Daily / monthly" }, { id: "countries", label: "Countries" }, { id: "scanners", label: "Scanners" }, { id: "senders", label: "Senders" }, { id: "commission", label: "Commission" }]}
        value={tab}
        onChange={setTab}
      />
      {!invalid && (
        <>
          {tab === "overview" && <Overview range={range} group={group} />}
          {tab === "countries" && <Countries range={range} />}
          {tab === "scanners" && <Scanners range={range} />}
          {tab === "senders" && <Senders range={range} />}
          {tab === "commission" && <Commission range={range} group={group} />}
        </>
      )}
    </>
  );
}
