"use client";

import { useEffect, useMemo, useState } from "react";
import { useToast } from "@/components/feedback";
import { Alert, Button, Card, ErrorBox, Field, Input, Select, Spinner, Textarea, Toggle } from "@/components/ui";
import { api, errorMessage, useApi } from "@/lib/api";
import { cn, money } from "@/lib/format";
import type { SettingDef, SettingsResponse } from "@/lib/types";

type Draft = Record<string, string | boolean>;

/** Stored value -> what the form control edits. */
function toDraft(def: SettingDef, value: unknown): string | boolean {
  if (def.type === "bool") return Boolean(value);
  if (def.type === "list") return Array.isArray(value) ? value.join("\n") : String(value ?? "");
  return value === null || value === undefined ? "" : String(value);
}

function SettingControl({ def, value, onChange }: { def: SettingDef; value: string | boolean; onChange: (v: string | boolean) => void }) {
  if (def.type === "bool") {
    return (
      <div className="flex items-start justify-between gap-4 rounded-lg border border-line px-3.5 py-3 sm:col-span-2">
        <div>
          <div className="text-sm font-medium">{def.label}</div>
          {def.help && <p className="mt-0.5 text-xs text-muted">{def.help}</p>}
        </div>
        <Toggle checked={Boolean(value)} onChange={onChange} label={def.label} />
      </div>
    );
  }
  const hint = def.help || undefined;
  const range = def.min !== null || def.max !== null ? ` (${def.min ?? "…"} – ${def.max ?? "…"})` : "";
  return (
    <Field label={`${def.label}${def.type === "decimal" || def.type === "int" ? range : ""}`} hint={hint} className={def.type === "list" ? "sm:col-span-2" : undefined}>
      {(id) =>
        def.choices ? (
          <Select id={id} value={String(value)} onChange={(e) => onChange(e.target.value)}>{def.choices.map((c) => <option key={c} value={c}>{c}</option>)}</Select>
        ) : def.type === "list" ? (
          <Textarea id={id} value={String(value)} onChange={(e) => onChange(e.target.value)} className="min-h-24 font-mono text-xs" placeholder="one domain per line" />
        ) : def.type === "str" ? (
          <Input id={id} value={String(value)} maxLength={200} onChange={(e) => onChange(e.target.value)} />
        ) : (
          <Input id={id} type="number" inputMode="decimal" step={def.type === "int" ? 1 : "any"} min={def.min ?? undefined} max={def.max ?? undefined} value={String(value)} onChange={(e) => onChange(e.target.value)} />
        )
      }
    </Field>
  );
}

export function GeneralTab() {
  const { data, error, reload } = useApi<SettingsResponse>("/api/settings");
  const toast = useToast();
  const [draft, setDraft] = useState<Draft>({});
  const [saving, setSaving] = useState(false);

  const initial = useMemo<Draft>(() => {
    const out: Draft = {};
    for (const def of data?.definitions ?? []) out[def.key] = toDraft(def, data?.values[def.key]);
    return out;
  }, [data]);
  useEffect(() => setDraft(initial), [initial]);

  const changed = useMemo(() => Object.keys(initial).filter((k) => draft[k] !== undefined && draft[k] !== initial[k]), [draft, initial]);
  const groups = useMemo(() => {
    const out = new Map<string, SettingDef[]>();
    for (const def of data?.definitions ?? []) out.set(def.group, [...(out.get(def.group) ?? []), def]);
    return [...out.entries()];
  }, [data]);

  if (!data) return error ? <ErrorBox onRetry={reload}>{error}</ErrorBox> : <div className="flex items-center gap-2 text-muted"><Spinner /> Loading…</div>;

  const reward = Number(draft.task_amount);
  const percent = Number(draft.commission_percent);
  const commission = Number.isFinite(reward) && Number.isFinite(percent) ? Math.round(reward * percent * 1e6) / 1e8 : null;

  const save = async () => {
    setSaving(true);
    try {
      const values: Record<string, unknown> = {};
      for (const key of changed) values[key] = draft[key];
      await api.put("/api/settings", { values });
      toast.success(`Saved ${changed.length} setting${changed.length === 1 ? "" : "s"} - the bot uses them immediately`);
      reload();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-5 pb-20">
      {commission !== null && (
        <Alert tone="blue">
          Per task the sender pays <strong>{money(reward + commission, 4)} USDT</strong> = {money(reward, 4)} scanner reward + {money(commission, 4)} commission ({draft.commission_percent}% of the reward).
          Changes apply to new tasks only; tasks already running keep the price they were created with.
        </Alert>
      )}
      {groups.map(([group, defs]) => (
        <Card key={group} title={group}>
          <div className="grid gap-4 sm:grid-cols-2">
            {defs.map((def) => <SettingControl key={def.key} def={def} value={draft[def.key] ?? ""} onChange={(v) => setDraft((cur) => ({ ...cur, [def.key]: v }))} />)}
          </div>
        </Card>
      ))}

      <div className={cn("fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface/95 px-4 py-3 backdrop-blur transition-transform lg:left-60", changed.length === 0 && "translate-y-full")} aria-hidden={changed.length === 0}>
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-3 lg:px-4">
          <span className="text-sm">{changed.length} unsaved change{changed.length === 1 ? "" : "s"}</span>
          <span className="flex gap-2">
            <Button onClick={() => setDraft(initial)} disabled={saving || changed.length === 0} tabIndex={changed.length === 0 ? -1 : 0}>Discard</Button>
            <Button variant="primary" loading={saving} disabled={changed.length === 0} tabIndex={changed.length === 0 ? -1 : 0} onClick={() => void save()}>Save changes</Button>
          </span>
        </div>
      </div>
    </div>
  );
}
