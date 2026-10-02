"use client";

import { useEffect, useState } from "react";
import { useInitialParams } from "@/components/common";
import { ChainTab } from "@/components/wallets/ChainTab";
import { DepositsTab } from "@/components/wallets/DepositsTab";
import { WalletsTab } from "@/components/wallets/WalletsTab";
import { WithdrawalsTab } from "@/components/wallets/WithdrawalsTab";
import { PageHeader, Tabs } from "@/components/ui";
import { useApi } from "@/lib/api";
import type { Badges } from "@/lib/types";

type Tab = "wallets" | "deposits" | "withdrawals" | "chain";
const TAB_IDS: Tab[] = ["wallets", "deposits", "withdrawals", "chain"];

function CountLabel({ label, count }: { label: string; count: number | undefined }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {label}
      {count ? <span className="rounded-full bg-warn px-1.5 py-0.5 text-[11px] font-semibold leading-none text-black">{count}</span> : null}
    </span>
  );
}

export default function WalletsPage() {
  const initial = useInitialParams();
  const [tab, setTab] = useState<Tab | null>(null);
  const [query, setQuery] = useState("");
  const { data: badges } = useApi<Badges>("/api/badges", undefined, 30000);

  useEffect(() => {
    if (!initial) return;
    const wanted = initial.get("tab") as Tab | null;
    setTab(wanted && TAB_IDS.includes(wanted) ? wanted : "wallets");
    setQuery(initial.get("q") ?? "");
  }, [initial]);

  return (
    <>
      <PageHeader title="Wallets" subtitle="Balances, deposits, withdrawals and payout status. Every change is written to an append-only ledger." />
      <Tabs
        tabs={[
          { id: "wallets", label: "Wallets" },
          { id: "deposits", label: <CountLabel label="Deposits" count={badges?.deposits_to_review} /> },
          { id: "withdrawals", label: <CountLabel label="Withdrawals" count={badges?.withdrawals_to_handle} /> },
          { id: "chain", label: "Payout status" },
        ]}
        value={tab ?? "wallets"}
        onChange={setTab}
      />
      {tab === "wallets" && <WalletsTab initialQuery={query} />}
      {tab === "deposits" && <DepositsTab />}
      {tab === "withdrawals" && <WithdrawalsTab />}
      {tab === "chain" && <ChainTab />}
    </>
  );
}
