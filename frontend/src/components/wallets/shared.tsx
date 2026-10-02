"use client";

import { CopyButton } from "@/components/common";
import { Badge, Mono } from "@/components/ui";
import { shortHash } from "@/lib/format";

export function MethodBadge({ method }: { method: string }) {
  return method === "bep20" ? <Badge tone="amber">BEP-20</Badge> : <Badge tone="blue">Binance</Badge>;
}

const isChainHash = (v: string) => /^0x[0-9a-fA-F]{64}$/.test(v);

/** A transaction hash (linked to BscScan) or a Binance order / Pay ID, with a copy button. */
export function TxRef({ value, method }: { value: string | null | undefined; method: string }) {
  if (!value) return <span className="text-muted">-</span>;
  return (
    <span className="inline-flex items-center gap-1">
      {method === "bep20" && isChainHash(value) ? (
        <a href={`https://bscscan.com/tx/${value}`} target="_blank" rel="noreferrer" className="text-brand hover:underline" title="Open on BscScan"><Mono>{shortHash(value, 6)}</Mono></a>
      ) : (
        <Mono>{shortHash(value, 8)}</Mono>
      )}
      <CopyButton text={value} />
    </span>
  );
}
