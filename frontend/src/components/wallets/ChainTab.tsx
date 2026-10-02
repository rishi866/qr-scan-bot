"use client";

import type { ReactNode } from "react";
import { CopyButton } from "@/components/common";
import { Alert, Badge, Card, ErrorBox, Mono, Spinner } from "@/components/ui";
import { useApi } from "@/lib/api";
import { money, timeAgo } from "@/lib/format";
import type { ChainStatus } from "@/lib/types";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 py-2.5 text-sm">
      <span className="text-muted">{label}</span>
      <span className="flex items-center gap-1.5 text-right">{children}</span>
    </li>
  );
}

export function ChainTab() {
  const { data: c, error, reload } = useApi<ChainStatus>("/api/chain/status", undefined, 60000);
  if (!c) return error ? <ErrorBox onRetry={reload}>{error}</ErrorBox> : <div className="flex items-center gap-2 text-muted"><Spinner /> Checking the chain…</div>;

  const hotLow = c.hot_wallet.configured && c.hot_wallet.bnb !== undefined && Number(c.hot_wallet.bnb) < 0.002;
  return (
    <div className="space-y-5">
      {!c.rpc.ok && <Alert tone="red">The BSC node is not reachable{c.rpc.error ? `: ${c.rpc.error}` : ""}. Deposits are not being detected until this is fixed (check <code>BSC_RPC_URL</code>).</Alert>}
      {!c.deposits.configured && <Alert tone="amber">BEP-20 deposits are not set up{c.deposits.error ? ` (${c.deposits.error})` : ""}. Generate a wallet with <code>python -m app.cli gen-wallet</code> and put the <em>xpub</em> into <code>HD_XPUB</code>.</Alert>}
      {hotLow && <Alert tone="amber">The hot wallet is almost out of BNB for gas ({c.hot_wallet.bnb} BNB). Automatic payouts will fail until it is topped up.</Alert>}

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="BNB Smart Chain">
          <ul className="divide-y divide-line">
            <Row label="Node"><Badge tone={c.rpc.ok ? "green" : "red"}>{c.rpc.ok ? "reachable" : "offline"}</Badge></Row>
            <Row label="Chain ID">{c.rpc.chain_id ?? "-"}</Row>
            <Row label="Latest block"><span className="tabular">{c.rpc.latest_block ?? "-"}</span></Row>
            <Row label="Last scanned block"><span className="tabular">{c.last_scanned_block ?? "-"}</span>{c.rpc.latest_block && c.last_scanned_block ? <Badge tone={c.rpc.latest_block - c.last_scanned_block > 200 ? "amber" : "gray"}>{c.rpc.latest_block - c.last_scanned_block} behind</Badge> : null}</Row>
            <Row label="Confirmations required">{c.confirmations}</Row>
            <Row label="Token">{c.token.symbol} · {c.token.decimals} decimals</Row>
            <Row label="Contract"><Mono>{c.token.contract.slice(0, 10)}…{c.token.contract.slice(-6)}</Mono><CopyButton text={c.token.contract} /></Row>
            <Row label="Bot worker heartbeat">{c.worker_heartbeat ? timeAgo(c.worker_heartbeat) : <Badge tone="red">never</Badge>}</Row>
          </ul>
        </Card>

        <Card title="Payments">
          <ul className="divide-y divide-line">
            <Row label="Deposit addresses"><Badge tone={c.deposits.configured ? "green" : "amber"}>{c.deposits.configured ? (c.deposits.mode === "watch-only" ? "watch-only (xpub)" : "mnemonic (can sweep)") : "not configured"}</Badge></Row>
            <Row label="Automatic payouts"><Badge tone={c.auto_payout.enabled ? "violet" : "gray"}>{c.auto_payout.enabled ? `on · up to ${money(c.auto_payout.max_amount)} USDT` : "off - manual"}</Badge></Row>
            <Row label="Hot wallet">{c.hot_wallet.configured && c.hot_wallet.address ? <><Mono>{c.hot_wallet.address.slice(0, 8)}…{c.hot_wallet.address.slice(-6)}</Mono><CopyButton text={c.hot_wallet.address} /></> : <span className="text-muted">not configured</span>}</Row>
            {c.hot_wallet.configured && <Row label="Hot wallet balance"><span className="tabular">{money(c.hot_wallet.token)} {c.token.symbol}</span> · <span className="tabular">{money(c.hot_wallet.bnb, 4)} BNB</span></Row>}
            <Row label="Binance verification"><Badge tone={c.binance_configured ? "green" : "gray"}>{c.binance_configured ? "on (read-only key)" : "off - manual"}</Badge></Row>
            <Row label="AI screenshot check"><Badge tone={c.ai_configured ? "green" : "gray"}>{c.ai_configured ? "on" : "off - you review every dispute"}</Badge></Row>
          </ul>
        </Card>
      </div>
      <p className="text-xs text-muted">Private keys never appear here. The panel only shows addresses and balances. Prefer a hot wallet that holds just enough for a day or two of payouts.</p>
    </div>
  );
}
