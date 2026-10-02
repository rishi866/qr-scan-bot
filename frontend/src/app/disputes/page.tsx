"use client";

import { useEffect, useState } from "react";
import { CopyButton, FilterBar, PersonCell } from "@/components/common";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useConfirm, useToast } from "@/components/feedback";
import { Alert, Badge, Button, Card, Field, Icon, KeyValue, Modal, Money, PageHeader, Select, Spinner, StatusBadge, Textarea } from "@/components/ui";
import { api, errorMessage, refreshBadges, useApi } from "@/lib/api";
import { dateTime, flag, timeAgo } from "@/lib/format";
import type { DisputeRow, Page } from "@/lib/types";

const PAGE_SIZE = 20;

const RESOLUTION_TEXT: Record<string, string> = { pay_scanner: "Scanner paid (dispute rejected)", refund_seller: "Sender refunded (dispute upheld)" };

/** "rejected" in the database means the dispute was rejected, i.e. the scanner was paid - a normal outcome, not an error. */
function DisputeBadge({ status }: { status: string }) {
  if (status === "pending_review") return <Badge tone="violet">needs decision</Badge>;
  if (status === "awaiting_proof") return <Badge tone="amber">awaiting proof</Badge>;
  if (status === "rejected") return <Badge tone="green">scanner paid</Badge>;
  if (status === "resolved") return <Badge tone="blue">sender refunded</Badge>;
  return <StatusBadge status={status} />;
}

function decidedBy(by: string | null): string {
  if (!by) return "the system";
  if (by === "ai") return "the AI";
  return by.startsWith("admin:") ? by.slice("admin:".length) : by;
}

function AiBadge({ d }: { d: DisputeRow }) {
  if (d.ai_status === "pending") return <Badge tone="gray">AI checking…</Badge>;
  if (!d.ai_verdict) return d.ai_status === "failed" ? <Badge tone="amber">AI failed</Badge> : <span className="text-muted">-</span>;
  const pct = d.ai_confidence !== null ? ` ${Math.round(d.ai_confidence * 100)}%` : "";
  const tone = d.ai_verdict === "completed" ? "green" : d.ai_verdict === "not_completed" ? "red" : "amber";
  return <Badge tone={tone}>AI: {d.ai_verdict.replaceAll("_", " ")}{pct}</Badge>;
}

function ProofViewer({ d }: { d: DisputeRow }) {
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const src = api.url(`/api/disputes/${d.id}/proof`);
  useEffect(() => setState("loading"), [d.id]);
  if (!d.has_proof) {
    return (
      <div className="flex h-56 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line text-center text-sm text-muted">
        <Icon name="eye" className="h-6 w-6 opacity-60" />
        {d.status === "awaiting_proof" ? <>Waiting for the scanner&apos;s screenshot{d.proof_deadline_at && <> - due {dateTime(d.proof_deadline_at)}</>}.</> : "The scanner did not upload a screenshot."}
      </div>
    );
  }
  return (
    <div>
      <a href={src} target="_blank" rel="noreferrer" className="relative block overflow-hidden rounded-lg border border-line bg-surface2" title="Open the full-size image in a new tab">
        {state === "loading" && <div className="absolute inset-0 flex items-center justify-center text-muted"><Spinner /></div>}
        {state === "error" ? (
          <div className="flex h-56 items-center justify-center px-4 text-center text-sm text-bad">The proof image could not be loaded (file missing on the server?).</div>
        ) : (
          <img src={src} alt={`Screenshot uploaded by ${d.scanner.alias ?? "the scanner"}`} onLoad={() => setState("ok")} onError={() => setState("error")} className="mx-auto max-h-[60vh] w-auto object-contain" />
        )}
      </a>
      <p className="mt-1.5 text-center text-xs text-muted">Click the image to open it full size.</p>
    </div>
  );
}

function DisputeModal({ id, onClose, onChanged }: { id: number | null; onClose: () => void; onChanged: () => void }) {
  const { data: d, error, reload } = useApi<DisputeRow>(id === null ? null : `/api/disputes/${id}`);
  const toast = useToast();
  const confirm = useConfirm();
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setNotes(d?.admin_notes ?? ""), [d?.id]);

  const decide = async (resolution: "pay_scanner" | "refund_seller") => {
    if (!d) return;
    const pay = resolution === "pay_scanner";
    const ok = await confirm({
      title: pay ? `Pay ${d.scanner.alias ?? "the scanner"}?` : `Refund the sender?`,
      message: pay
        ? "The dispute is rejected: the scanner receives the reward and the platform keeps the commission. Both people are notified. This cannot be undone."
        : "The dispute is upheld: the sender gets reward + commission back and the scanner loses reputation. Both people are notified. This cannot be undone.",
      confirmLabel: pay ? "Pay scanner" : "Refund sender",
      tone: pay ? "success" : "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api.post(`/api/disputes/${d.id}/resolve`, { resolution, notes: notes.trim() || null });
      toast.success(pay ? "Scanner paid - dispute closed" : "Sender refunded - dispute closed");
      reload();
      onChanged();
      refreshBadges();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const open = d && (d.status === "awaiting_proof" || d.status === "pending_review");
  return (
    <Modal
      open={id !== null}
      onClose={onClose}
      busy={busy}
      size="xl"
      title={d ? <span className="flex flex-wrap items-center gap-2">Dispute #{d.id} · task #{d.session_id} <DisputeBadge status={d.status} /></span> : "Dispute"}
      footer={
        open && (
          <>
            <Button variant="danger" disabled={busy} onClick={() => void decide("refund_seller")}>Refund sender (uphold dispute)</Button>
            <Button variant="success" icon="check" loading={busy} onClick={() => void decide("pay_scanner")}>Pay scanner (reject dispute)</Button>
          </>
        )
      }
    >
      {!d && error && <div className="text-sm text-bad">{error}</div>}
      {!d && !error && <div className="flex items-center gap-2 py-10 text-muted"><Spinner /> Loading…</div>}
      {d && (
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-3">
            <h3 className="text-sm font-semibold">Scanner&apos;s proof</h3>
            <ProofViewer d={d} />
            {d.ai_verdict && (
              <div className="rounded-lg border border-line bg-surface2 px-3 py-2.5 text-sm">
                <div className="mb-1 flex items-center gap-2 text-xs font-medium text-muted">AI screenshot check <AiBadge d={d} /></div>
                <p>{d.ai_reason ?? "No explanation returned."}</p>
                <p className="mt-1 text-xs text-muted">The AI only reads the screenshot - it does not know who is right. Treat it as a hint.</p>
              </div>
            )}
          </div>

          <div className="space-y-4">
            {d.status === "awaiting_proof" && <Alert tone="amber">The scanner still has time to upload a screenshot{d.proof_deadline_at ? ` (until ${dateTime(d.proof_deadline_at)})` : ""}. You can decide now, but waiting is usually fairer.</Alert>}
            {!open && (
              <Alert tone="blue">
                Closed {timeAgo(d.resolved_at)} by <strong>{decidedBy(d.resolved_by)}</strong>: {RESOLUTION_TEXT[d.resolution ?? ""] ?? d.resolution ?? "timed out"}.
              </Alert>
            )}

            <KeyValue
              items={[
                { label: "Reward", value: <Money value={d.amount} /> },
                { label: "Commission", value: <Money value={d.commission} /> },
                { label: "Sender", value: <span>{flag(d.seller.country)} {d.seller.name}{d.seller.username ? ` (@${d.seller.username})` : ""}</span> },
                { label: "Scanner", value: <span>{flag(d.scanner.country)} <strong>{d.scanner.alias ?? "unnamed"}</strong> · {d.scanner.name}{d.scanner.username ? ` (@${d.scanner.username})` : ""}</span> },
                { label: "URL sent", value: dateTime(d.timeline.sent_at) },
                { label: "Accepted", value: dateTime(d.timeline.accepted_at) },
                { label: "Scanner said Done", value: dateTime(d.timeline.done_at) },
                { label: "Sender rejected", value: dateTime(d.created_at) },
              ]}
            />

            <div>
              <div className="mb-1 text-xs font-medium text-muted">Sender&apos;s reason</div>
              <p className="rounded-lg bg-surface2 px-3 py-2 text-sm">{d.reason ? d.reason : <span className="text-muted">No reason given.</span>}</p>
            </div>

            <div>
              <div className="mb-1 text-xs font-medium text-muted">URL</div>
              <div className="flex items-start gap-1 rounded-lg bg-surface2 px-3 py-2 text-xs"><code className="min-w-0 flex-1 break-all">{d.url}</code><CopyButton text={d.url} /></div>
            </div>

            <Field label="Decision notes" hint={open ? "Optional. Saved with the decision (people are not shown these notes)." : undefined}>
              {(fid) => <Textarea id={fid} value={notes} onChange={(e) => setNotes(e.target.value)} readOnly={!open} maxLength={4000} placeholder={open ? "Why you decided this way…" : "No notes."} className="min-h-24" />}
            </Field>
          </div>
        </div>
      )}
    </Modal>
  );
}

export default function DisputesPage() {
  const [view, setView] = useState("open");
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);
  const { data, error, loading, reload } = useApi<Page<DisputeRow>>("/api/disputes", { status: view === "all" ? "" : view, page, page_size: PAGE_SIZE }, 30000);

  const columns: Column<DisputeRow>[] = [
    { key: "id", header: "Dispute", cell: (d) => <span><span className="font-medium tabular">#{d.id}</span><div className="text-xs text-muted">task #{d.session_id}</div></span> },
    { key: "status", header: "Status", cell: (d) => <DisputeBadge status={d.status} /> },
    { key: "seller", header: "Sender", hideBelow: "sm", cell: (d) => <PersonCell name={d.seller.name} country={d.seller.country} /> },
    { key: "scanner", header: "Scanner", cell: (d) => <PersonCell name={d.scanner.name} alias={d.scanner.alias} country={d.scanner.country} /> },
    { key: "amount", header: "At stake", align: "right", hideBelow: "md", cell: (d) => <Money value={Number(d.amount) + Number(d.commission)} unit="" /> },
    { key: "proof", header: "Proof", hideBelow: "md", cell: (d) => (d.has_proof ? <Badge tone="blue">screenshot</Badge> : <span className="text-muted">none</span>) },
    { key: "ai", header: "AI check", hideBelow: "lg", cell: (d) => <AiBadge d={d} /> },
    { key: "when", header: "Opened", hideBelow: "sm", cell: (d) => <span title={dateTime(d.created_at)}>{timeAgo(d.created_at)}</span> },
    { key: "act", header: "", align: "right", cell: (d) => <Button size="sm" variant={d.status === "pending_review" ? "primary" : "secondary"} onClick={() => setOpenId(d.id)}>{d.status === "pending_review" ? "Review" : "View"}</Button> },
  ];

  return (
    <>
      <PageHeader title="Disputes" subtitle="A sender rejected a finished task. The scanner uploads a screenshot; the AI (if enabled) reads it; you make the final call when it is not clear-cut." />
      <Card padded={false}>
        <FilterBar>
          <Select value={view} onChange={(e) => { setView(e.target.value); setPage(1); }} className="!w-auto" aria-label="Show disputes">
            <option value="open">Open (needs attention)</option>
            <option value="pending_review">Waiting for my decision</option>
            <option value="awaiting_proof">Waiting for the scanner&apos;s proof</option>
            <option value="rejected">Closed - scanner paid</option>
            <option value="resolved">Closed - sender refunded</option>
            <option value="all">All</option>
          </Select>
          <Button variant="ghost" icon="refresh" onClick={reload} className="ml-auto">Refresh</Button>
        </FilterBar>
        {error && <div className="p-4 text-sm text-bad">{error}</div>}
        <DataTable columns={columns} rows={data?.items} loading={loading} rowKey={(d) => d.id} onRowClick={(d) => setOpenId(d.id)} empty={view === "open" ? "No open disputes. 🎉" : "No disputes in this view."} />
        <Pagination page={page} pageSize={PAGE_SIZE} total={data?.total ?? 0} onChange={setPage} />
      </Card>
      <DisputeModal id={openId} onClose={() => setOpenId(null)} onChanged={reload} />
    </>
  );
}
