"use client";

import type { ReactNode } from "react";
import { Button, Empty, Spinner } from "@/components/ui";
import { cn } from "@/lib/format";

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  className?: string;
  align?: "right" | "center";
  /** hide this column on narrow screens */
  hideBelow?: "sm" | "md" | "lg" | "xl";
}

const HIDE = { sm: "hidden sm:table-cell", md: "hidden md:table-cell", lg: "hidden lg:table-cell", xl: "hidden xl:table-cell" };

export function DataTable<T>({
  columns,
  rows,
  loading,
  empty,
  rowKey,
  onRowClick,
  rowClassName,
}: {
  columns: Column<T>[];
  rows: T[] | undefined;
  loading?: boolean;
  empty?: ReactNode;
  rowKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  rowClassName?: (row: T) => string | undefined;
}) {
  // On phones every row becomes a card: first column = title, header-less columns = actions, the rest = label/value pairs.
  const actionColumns = columns.filter((c) => c.header === "");
  const detailColumns = columns.slice(1).filter((c) => c.header !== "" && c.hideBelow !== "lg" && c.hideBelow !== "xl");
  const title = columns[0];

  return (
    <div className="relative">
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-line text-xs uppercase tracking-wide text-muted">
              {columns.map((c) => (
                <th key={c.key} scope="col" className={cn("whitespace-nowrap px-3 py-2.5 font-medium", c.align === "right" && "text-right", c.align === "center" && "text-center", c.hideBelow && HIDE[c.hideBelow], c.className)}>
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows?.map((row) => (
              <tr
                key={rowKey(row)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={cn("border-b border-line/70 last:border-0", onRowClick && "cursor-pointer hover:bg-surface2", rowClassName?.(row))}
              >
                {columns.map((c) => (
                  <td key={c.key} className={cn("px-3 py-2.5 align-middle", c.align === "right" && "text-right tabular", c.align === "center" && "text-center", c.hideBelow && HIDE[c.hideBelow], c.className)}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="divide-y divide-line md:hidden">
        {rows?.map((row) => (
          <li
            key={rowKey(row)}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            className={cn("px-4 py-3", onRowClick && "cursor-pointer active:bg-surface2", rowClassName?.(row))}
          >
            <div className="min-w-0 text-sm">{title.cell(row)}</div>
            {detailColumns.length > 0 && (
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                {detailColumns.map((c) => (
                  <div key={c.key} className="min-w-0">
                    <dt className="text-[11px] font-medium uppercase tracking-wide text-muted">{c.header}</dt>
                    <dd className="mt-0.5 break-words tabular">{c.cell(row)}</dd>
                  </div>
                ))}
              </dl>
            )}
            {actionColumns.length > 0 && <div className="mt-3 flex flex-wrap justify-end gap-2">{actionColumns.map((c) => <div key={c.key}>{c.cell(row)}</div>)}</div>}
          </li>
        ))}
      </ul>

      {loading && rows === undefined && (
        <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted"><Spinner /> Loading…</div>
      )}
      {!loading && rows?.length === 0 && <Empty>{empty ?? "No results."}</Empty>}
      {loading && rows !== undefined && <div className="pointer-events-none absolute inset-0 bg-surface/50" />}
    </div>
  );
}

export function Pagination({ page, pageSize, total, onChange }: { page: number; pageSize: number; total: number; onChange: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return total > 0 ? <div className="border-t border-line px-4 py-2.5 text-xs text-muted">{total} result{total === 1 ? "" : "s"}</div> : null;
  const from = (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-4 py-2.5 text-xs text-muted">
      <span>{from}-{to} of {total}</span>
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={page <= 1} onClick={() => onChange(page - 1)}>Previous</Button>
        <span>Page {page} / {pages}</span>
        <Button size="sm" disabled={page >= pages} onClick={() => onChange(page + 1)}>Next</Button>
      </div>
    </div>
  );
}
