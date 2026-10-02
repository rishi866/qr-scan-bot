"use client";

import { useEffect, useMemo, useState } from "react";
import { Button, Icon, Select } from "@/components/ui";
import { cn } from "@/lib/format";

export interface Block {
  start: number;
  end: number;
}

const key = (b: Block) => `${b.start}-${b.end}`;
const pad = (n: number) => String(n).padStart(2, "0");
const inOrder = (blocks: Block[]) => [...blocks].sort((a, b) => a.start - b.start || a.end - b.end);
export const blockLabel = (b: Block) => `${pad(b.start)}-${pad(b.end)}`;

/** The standard grid: 00-02, 02-04, ... 22-00 for a 2 h slot length. */
function standardBlocks(hours: number): Block[] {
  const out: Block[] = [];
  for (let s = 0; s < 24; s += hours) out.push({ start: s, end: (s + hours) % 24 });
  return out;
}

/**
 * Edit a scanner's daily windows (in the scanner's local time).
 * Standard blocks toggle with one click; any other window can be added by hand (e.g. 07-09 or 22-02).
 */
export function SlotEditor({ initial, timezone, hours = 2, saving, onSave }: { initial: Block[]; timezone: string | null; hours?: number; saving?: boolean; onSave: (blocks: Block[]) => void }) {
  const [blocks, setBlocks] = useState<Block[]>(initial);
  const [from, setFrom] = useState(8);
  const [to, setTo] = useState(10);
  // re-sync only when the saved slots really change (the parent passes a fresh array on every render)
  const savedKey = inOrder(initial).map(key).join(",");
  useEffect(() => setBlocks(initial), [savedKey]);

  const selected = useMemo(() => new Set(blocks.map(key)), [blocks]);
  const standard = useMemo(() => standardBlocks(24 % hours === 0 ? hours : 2), [hours]);
  const sorted = useMemo(() => inOrder(blocks), [blocks]);
  const dirty = sorted.map(key).join(",") !== savedKey;

  const toggle = (b: Block) => setBlocks((cur) => (cur.some((x) => key(x) === key(b)) ? cur.filter((x) => key(x) !== key(b)) : [...cur, b]));
  const addCustom = () => {
    if (from === to || selected.has(key({ start: from, end: to }))) return;
    setBlocks((cur) => [...cur, { start: from, end: to }]);
  };

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">Times are in the scanner&apos;s own time zone{timezone ? <> (<strong className="text-fg">{timezone}</strong>)</> : null}. They are converted to UTC automatically, including daylight-saving changes.</p>

      <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4 md:grid-cols-6">
        {standard.map((b) => {
          const on = selected.has(key(b));
          return (
            <button
              key={key(b)}
              type="button"
              aria-pressed={on}
              onClick={() => toggle(b)}
              className={cn("rounded-lg border px-2 py-1.5 text-sm font-medium tabular transition-colors", on ? "border-brand bg-brand text-brand-fg" : "border-line bg-surface text-muted hover:bg-surface2 hover:text-fg")}
            >
              {blockLabel(b)}
            </button>
          );
        })}
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-muted">Custom from
          <Select value={from} onChange={(e) => setFrom(Number(e.target.value))} className="mt-1 !w-28" aria-label="Custom slot start hour">
            {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{pad(h)}:00</option>)}
          </Select>
        </label>
        <label className="text-xs text-muted">to
          <Select value={to} onChange={(e) => setTo(Number(e.target.value))} className="mt-1 !w-28" aria-label="Custom slot end hour">
            {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{pad(h)}:00</option>)}
          </Select>
        </label>
        <Button icon="plus" onClick={addCustom} disabled={from === to}>Add window</Button>
      </div>

      <div>
        <div className="mb-1.5 text-xs font-medium text-muted">Selected ({sorted.length})</div>
        {sorted.length === 0 ? (
          <p className="text-sm text-muted">No slots - this scanner will not receive any URL.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {sorted.map((b) => (
              <span key={key(b)} className="inline-flex items-center gap-1 rounded-full bg-info-soft px-2.5 py-1 text-xs font-medium text-info tabular">
                {blockLabel(b)}
                <button type="button" onClick={() => toggle(b)} aria-label={`Remove ${blockLabel(b)}`} className="rounded-full p-0.5 hover:bg-info/20"><Icon name="x" className="h-3 w-3" /></button>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="flex justify-end">
        <Button variant="primary" loading={saving} disabled={!dirty} onClick={() => onSave(sorted)}>Save slots</Button>
      </div>
    </div>
  );
}
