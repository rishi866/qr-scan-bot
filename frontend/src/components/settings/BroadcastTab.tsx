"use client";

import { useState } from "react";
import { DataTable, Pagination, type Column } from "@/components/DataTable";
import { useConfirm, useToast } from "@/components/feedback";
import { Badge, Button, Card, Field, Select, Textarea } from "@/components/ui";
import { api, errorMessage, useApi } from "@/lib/api";
import { dateTime } from "@/lib/format";
import type { BroadcastRow, Page } from "@/lib/types";

const PAGE_SIZE = 10;
const AUDIENCE_LABEL: Record<string, string> = { all: "Everyone", sellers: "Senders", scanners: "Scanners" };

export function BroadcastTab() {
  const toast = useToast();
  const confirm = useConfirm();
  const [message, setMessage] = useState("");
  const [audience, setAudience] = useState("all");
  const [sending, setSending] = useState(false);
  const [page, setPage] = useState(1);
  const preview = useApi<{ recipients: number }>("/api/broadcasts/preview", { audience });
  const history = useApi<Page<BroadcastRow>>("/api/broadcasts", { page, page_size: PAGE_SIZE }, 10000);
  const recipients = preview.data?.recipients ?? 0;

  const send = async () => {
    const ok = await confirm({
      title: `Send to ${recipients} user${recipients === 1 ? "" : "s"}?`,
      message: <>Audience: <strong>{AUDIENCE_LABEL[audience]}</strong> (approved users who have not blocked the bot). A broadcast cannot be recalled once sent.<blockquote className="mt-3 whitespace-pre-wrap rounded-lg bg-surface2 p-3 text-fg">{message.trim()}</blockquote></>,
      confirmLabel: "Send broadcast",
    });
    if (!ok) return;
    setSending(true);
    try {
      const res = await api.post<{ total: number }>("/api/broadcasts", { message: message.trim(), audience, confirm: true });
      toast.success(`Queued for ${res.total} user${res.total === 1 ? "" : "s"} - delivery takes about ${Math.max(1, Math.ceil(res.total / 25))} s`);
      setMessage("");
      history.reload();
      setPage(1);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setSending(false);
    }
  };

  const columns: Column<BroadcastRow>[] = [
    { key: "when", header: "Sent", cell: (b) => dateTime(b.created_at) },
    { key: "aud", header: "Audience", hideBelow: "sm", cell: (b) => <Badge>{AUDIENCE_LABEL[b.audience] ?? b.audience}</Badge> },
    { key: "msg", header: "Message", cell: (b) => <span className="line-clamp-2 max-w-md whitespace-pre-wrap text-sm" title={b.message}>{b.message}</span> },
    { key: "result", header: "Delivered", align: "right", cell: (b) => <span className="tabular">{b.sent}<span className="text-muted"> / {b.total}</span>{b.failed > 0 && <span className="ml-1 text-bad">· {b.failed} failed</span>}{b.queued > 0 && <span className="ml-1 text-warn">· {b.queued} queued</span>}</span> },
    { key: "by", header: "By", hideBelow: "md", cell: (b) => b.created_by ?? "-" },
  ];

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,26rem)_1fr]">
      <Card title="New announcement">
        <div className="space-y-3">
          <Field label="Audience">
            {(id) => (
              <Select id={id} value={audience} onChange={(e) => setAudience(e.target.value)}>
                <option value="all">Everyone</option>
                <option value="sellers">Senders only</option>
                <option value="scanners">Scanners only</option>
              </Select>
            )}
          </Field>
          <Field label="Message" hint={`${message.length} / 3500 · plain text, sent as an "📣 Announcement". Rate-limited delivery (about 25 users per second).`}>
            {(id) => <Textarea id={id} value={message} maxLength={3500} onChange={(e) => setMessage(e.target.value)} className="min-h-36" placeholder="e.g. Maintenance tonight at 22:00 UTC - no URLs will be delivered for 10 minutes." />}
          </Field>
          <div className="flex items-center justify-between gap-3">
            <span className="text-xs text-muted">{preview.data ? `${recipients} recipient${recipients === 1 ? "" : "s"}` : "Counting recipients…"}</span>
            <Button variant="primary" icon="bell" loading={sending} disabled={!message.trim() || recipients === 0} onClick={() => void send()}>Review &amp; send</Button>
          </div>
        </div>
      </Card>
      <Card title="Recent broadcasts" padded={false}>
        <DataTable columns={columns} rows={history.data?.items} loading={history.loading} rowKey={(b) => b.id} empty="No broadcasts yet." />
        <Pagination page={page} pageSize={PAGE_SIZE} total={history.data?.total ?? 0} onChange={setPage} />
      </Card>
    </div>
  );
}
