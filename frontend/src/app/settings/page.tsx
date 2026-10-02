"use client";

import { useState } from "react";
import { AuditTab } from "@/components/settings/AuditTab";
import { BroadcastTab } from "@/components/settings/BroadcastTab";
import { GeneralTab } from "@/components/settings/GeneralTab";
import { SecurityTab } from "@/components/settings/SecurityTab";
import { PageHeader, Tabs } from "@/components/ui";

type Tab = "general" | "broadcast" | "security" | "audit";

export default function SettingsPage() {
  const [tab, setTab] = useState<Tab>("general");
  return (
    <>
      <PageHeader title="Settings" subtitle="Business rules are stored in the database and take effect immediately - no restart needed." />
      <Tabs
        tabs={[{ id: "general", label: "General" }, { id: "broadcast", label: "Broadcast" }, { id: "security", label: "Security & admins" }, { id: "audit", label: "Audit log" }]}
        value={tab}
        onChange={setTab}
      />
      {tab === "general" && <GeneralTab />}
      {tab === "broadcast" && <BroadcastTab />}
      {tab === "security" && <SecurityTab />}
      {tab === "audit" && <AuditTab />}
    </>
  );
}
