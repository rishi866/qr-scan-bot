"use client";

import { useEffect, useMemo, useState } from "react";
import { useToast } from "@/components/feedback";
import { SlotEditor, type Block } from "@/components/SlotEditor";
import { UserDetailModal } from "@/components/UserDetail";
import { Badge, Button, ErrorBox, Field, Input, Modal, Spinner, StatusBadge } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import { flag } from "@/lib/format";
import type { SettingsResponse, UserDetail } from "@/lib/types";

/** Name, reputation and slots of one scanner. Used from the Scanners and Slots pages. */
export function ScannerModal({ scannerId, onClose, onChanged }: { scannerId: number | null; onClose: () => void; onChanged?: () => void }) {
  const toast = useToast();
  const open = scannerId !== null;
  const { data: scanner, error, reload } = useApi<UserDetail>(open ? `/api/users/${scannerId}` : null);
  const { data: cfg } = useApi<SettingsResponse>(open ? "/api/settings" : null);
  const { data: next, reload: reloadNext } = useApi<{ alias: string }>(open ? "/api/scanners/next-alias" : null);
  const [alias, setAlias] = useState("");
  const [reputation, setReputation] = useState("50");
  const [busy, setBusy] = useState<string | null>(null);
  const [profile, setProfile] = useState(false);

  useEffect(() => {
    if (scanner) {
      setAlias(scanner.alias ?? "");
      setReputation(String(scanner.reputation ?? 50));
    }
  }, [scanner?.user_id, scanner?.alias, scanner?.reputation]);

  const blocks: Block[] = useMemo(() => (scanner?.slots ?? []).map((s) => ({ start: s.slot_start, end: s.slot_end })), [scanner]);
  const hours = Number(cfg?.values.slot_duration_hours ?? 2);

  const run = async (what: string, message: string, call: () => Promise<unknown>) => {
    setBusy(what);
    try {
      await call();
      toast.success(message);
      reload();
      reloadNext();
      onChanged?.();
      refreshBadges();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const live = scanner?.slots.some((s) => s.live_now) ?? false;
  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        size="lg"
        title={scanner ? <span className="flex flex-wrap items-center gap-2">{scanner.alias ?? "Unnamed scanner"} <StatusBadge status={scanner.status} />{live && <Badge tone="green">live now</Badge>}</span> : "Scanner"}
        footer={scanner && <Button onClick={() => setProfile(true)}>Full profile &amp; wallet</Button>}
      >
        {!scanner && error && <ErrorBox onRetry={reload}>{error}</ErrorBox>}
        {!scanner && !error && <div className="flex items-center gap-2 py-8 text-muted"><Spinner /> Loading…</div>}
        {scanner && (
          <div className="space-y-6">
            <div className="rounded-lg border border-line bg-surface2 px-3 py-2.5 text-sm">
              <div className="text-xs font-medium text-muted">Real identity (senders never see this)</div>
              <div className="mt-0.5">{flag(scanner.country)} {scanner.name}{scanner.username ? ` · @${scanner.username}` : ""} · Telegram ID <code>{scanner.telegram_id}</code></div>
              <div className="text-xs text-muted">{scanner.timezone ?? "no time zone"}{scanner.utc_offset ? ` (${scanner.utc_offset})` : ""} · {scanner.stats.completed_tasks} tasks completed</div>
            </div>

            <section>
              <h3 className="mb-2 text-sm font-semibold">Anonymous name</h3>
              <div className="flex flex-wrap items-end gap-2">
                <Field label="Shown to senders instead of the real name" className="min-w-52 flex-1">
                  {(id) => <Input id={id} value={alias} onChange={(e) => setAlias(e.target.value)} placeholder={next ? next.alias : "user1"} maxLength={24} />}
                </Field>
                <Button variant="primary" loading={busy === "alias"} disabled={!alias.trim() || alias.trim() === scanner.alias} onClick={() => void run("alias", "Name saved", () => api.put(`/api/scanners/${scanner.user_id}/alias`, { alias: alias.trim() }))}>Save name</Button>
                {!scanner.alias && (
                  <Button loading={busy === "auto"} onClick={() => void run("auto", "Named automatically", () => api.put(`/api/scanners/${scanner.user_id}/alias`, { alias: null }))}>
                    Use {next?.alias ?? "next free name"}
                  </Button>
                )}
              </div>
              {!scanner.alias && scanner.status === "approved" && <p className="mt-2 text-xs text-warn">Until this scanner has a name they are not matched with any sender.</p>}
            </section>

            <section>
              <h3 className="mb-2 text-sm font-semibold">Reputation</h3>
              <div className="flex flex-wrap items-end gap-2">
                <Field label="0 - 100 (higher = offered URLs first)" className="w-72 max-w-full">
                  {(id) => <Input id={id} type="number" min={0} max={100} value={reputation} onChange={(e) => setReputation(e.target.value)} />}
                </Field>
                <Button variant="primary" loading={busy === "rep"} disabled={Number(reputation) === scanner.reputation} onClick={() => void run("rep", "Reputation saved", () => api.put(`/api/scanners/${scanner.user_id}/reputation`, { value: Number(reputation) }))}>Save</Button>
              </div>
            </section>

            <section>
              <h3 className="mb-2 text-sm font-semibold">Slots</h3>
              <SlotEditor
                initial={blocks}
                timezone={scanner.timezone}
                hours={hours}
                saving={busy === "slots"}
                onSave={(b) => void run("slots", "Slots saved", () => api.put(`/api/scanners/${scanner.user_id}/slots`, { slots: b }))}
              />
            </section>
          </div>
        )}
      </Modal>
      <UserDetailModal userId={profile && scanner ? scanner.user_id : null} onClose={() => setProfile(false)} onChanged={() => { reload(); onChanged?.(); }} />
    </>
  );
}
