"use client";

import Link from "next/link";
import { ComboChart } from "@/components/Charts";
import { Alert, Badge, Card, ErrorBox, Money, PageHeader, Spinner, Stat } from "@/components/ui";
import { useApi } from "@/lib/api";
import { flag, money, timeAgo, usdt } from "@/lib/format";
import type { Dashboard } from "@/lib/types";

function StatusRow({ ok, label, detail }: { ok: boolean | null; label: string; detail?: string }) {
  return (
    <li className="flex items-center justify-between gap-3 py-2 text-sm">
      <span>{label}</span>
      <Badge tone={ok === null ? "gray" : ok ? "green" : "amber"}>{detail ?? (ok ? "on" : "off")}</Badge>
    </li>
  );
}

export default function DashboardPage() {
  const { data, error, loading, reload } = useApi<Dashboard>("/api/dashboard", undefined, 30000);

  if (!data) {
    return (
      <>
        <PageHeader title="Dashboard" />
        {error ? <ErrorBox onRetry={reload}>{error}</ErrorBox> : loading ? <div className="flex items-center gap-2 text-muted"><Spinner /> Loading…</div> : null}
      </>
    );
  }

  const d = data;
  const labels = d.series.map((s) => s.date.slice(5));
  return (
    <>
      <PageHeader title="Dashboard" subtitle={`Live overview · updated ${timeAgo(d.generated_at ?? undefined)}`} />

      <div className="mb-5 space-y-2">
        {!d.ledger.ok && <Alert tone="red" href="/wallets/">Ledger check failed - wallets and the ledger disagree. {d.ledger.problems[0]}</Alert>}
        {!d.system.worker_online && <Alert tone="red">The Telegram bot worker is not reporting. Users cannot be served until it is running (<code>docker compose logs bot</code>).</Alert>}
        {d.pending_approvals > 0 && <Alert tone="amber" href="/users/?status=pending">{d.pending_approvals} user{d.pending_approvals === 1 ? "" : "s"} waiting for approval.</Alert>}
        {d.scanners_needing_name > 0 && <Alert tone="amber" href="/scanners/?needs_name=1">{d.scanners_needing_name} approved scanner{d.scanners_needing_name === 1 ? "" : "s"} still need a name (user1, user2, …) before they can receive URLs.</Alert>}
        {d.disputes.pending_review > 0 && <Alert tone="amber" href="/disputes/">{d.disputes.pending_review} dispute{d.disputes.pending_review === 1 ? "" : "s"} waiting for your decision.</Alert>}
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <Stat label="Total users" value={d.users.total} hint={`${d.users.sellers} senders · ${d.users.scanners} scanners`} icon="users" href="/users/" />
        <Stat label="Pending approvals" value={d.pending_approvals} tone={d.pending_approvals ? "amber" : undefined} hint={`${d.users.suspended} suspended`} icon="bell" href="/users/?status=pending" />
        <Stat label="Today's transactions" value={d.today.transactions} hint={`${usdt(d.today.volume)} volume`} icon="swap" href="/transactions/" />
        <Stat label="Pending disputes" value={d.disputes.open} tone={d.disputes.open ? "red" : undefined} hint={`${d.disputes.pending_review} to review · ${d.disputes.awaiting_proof} awaiting proof`} icon="shield" href="/disputes/" />
        <Stat label="Total commission" value={<Money value={d.commission_total} />} hint={`${usdt(d.today.commission)} today`} icon="wallet" href="/reports/" />
        <Stat label="Withdrawals to handle" value={d.withdrawals.to_handle} tone={d.withdrawals.to_handle ? "amber" : undefined} hint={usdt(d.withdrawals.amount)} icon="wallet" href="/wallets/" />
        <Stat label="Deposits to review" value={d.deposits_to_review} tone={d.deposits_to_review ? "amber" : undefined} hint="Binance claims, below-min" icon="download" href="/wallets/" />
        <Stat label="Live tasks" value={d.live_sessions} hint="in flight right now" icon="clock" href="/transactions/" />
        <Stat label="Scanners active now" value={d.active_scanners_now.length} hint="inside their slot" icon="scanner" href="/slots/" />
        <Stat label="User funds" value={<Money value={d.ledger.user_total} />} hint={d.ledger.ok ? "ledger balanced ✓" : "ledger mismatch!"} tone={d.ledger.ok ? "green" : "red"} icon="wallet" />
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-3">
        <Card title="Last 14 days" className="xl:col-span-2">
          <ComboChart labels={labels} bars={{ label: "Transactions", data: d.series.map((s) => s.transactions) }} line={{ label: "Commission (USDT)", data: d.series.map((s) => Number(s.commission)) }} />
        </Card>

        <div className="space-y-5">
          <Card title="Active scanners right now" actions={<Link href="/slots/" className="text-xs text-brand hover:underline">All slots</Link>}>
            {d.active_scanners_now.length === 0 ? (
              <p className="text-sm text-muted">Nobody is inside a slot at the moment - senders see &ldquo;no scanner is active&rdquo;.</p>
            ) : (
              <ul className="divide-y divide-line">
                {d.active_scanners_now.map((s) => (
                  <li key={s.user_id} className="flex items-center justify-between py-2 text-sm">
                    <span className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-ok" /><strong>{s.alias}</strong> <span className="text-muted">{flag(s.country)}</span></span>
                    <span className="text-xs text-muted">{s.slot} · ⭐ {s.reputation}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="System">
            <ul className="divide-y divide-line">
              <StatusRow ok={d.system.worker_online} label="Bot worker" detail={d.system.worker_online ? "online" : `offline · seen ${timeAgo(d.system.worker_last_seen)}`} />
              <StatusRow ok={d.system.deposits_configured} label="BEP-20 deposits" />
              <StatusRow ok={d.system.auto_payout ? true : null} label="Automatic payouts" detail={d.system.auto_payout ? "on" : "manual"} />
              <StatusRow ok={d.system.ai_configured ? true : null} label="AI screenshot check" detail={d.system.ai_configured ? "on" : "off - admin reviews"} />
              <StatusRow ok={d.system.binance_configured ? true : null} label="Binance verification" detail={d.system.binance_configured ? "on" : "manual"} />
              <StatusRow ok={d.system.telegram_admins > 0} label="Telegram admins" detail={`${d.system.telegram_admins}`} />
            </ul>
            <p className="mt-2 text-xs text-muted">Volume today: {money(d.today.volume)} USDT</p>
          </Card>
        </div>
      </div>
    </>
  );
}
