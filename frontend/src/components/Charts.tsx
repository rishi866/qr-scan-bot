"use client";

import {
  BarController,
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
  type ChartOptions,
} from "chart.js";
import { Bar, Line } from "react-chartjs-2";

ChartJS.register(BarController, BarElement, CategoryScale, Filler, Legend, LinearScale, LineController, LineElement, PointElement, Tooltip);

const GRID = "rgba(128,138,160,0.18)";
const TICK = "#8a94a8";

export const PALETTE = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#0ea5e9", "#a855f7", "#14b8a6", "#f97316"];

function baseOptions(): ChartOptions<"bar" | "line"> {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: TICK, boxWidth: 10, usePointStyle: true } }, tooltip: { padding: 10 } },
    scales: {
      x: { grid: { display: false }, ticks: { color: TICK, maxRotation: 0, autoSkip: true } },
      y: { beginAtZero: true, grid: { color: GRID }, ticks: { color: TICK, precision: 0 } },
    },
  };
}

export interface Series {
  label: string;
  data: number[];
  color?: string;
  yAxisID?: string;
  type?: "bar" | "line";
}

export function BarChart({ labels, series, height = 240, stacked }: { labels: string[]; series: Series[]; height?: number; stacked?: boolean }) {
  const options = baseOptions() as ChartOptions<"bar">;
  if (stacked && options.scales) {
    (options.scales.x as { stacked?: boolean }).stacked = true;
    (options.scales.y as { stacked?: boolean }).stacked = true;
  }
  return (
    <div style={{ height }}>
      <Bar
        options={options}
        data={{
          labels,
          datasets: series.map((s, i) => ({ label: s.label, data: s.data, backgroundColor: s.color ?? PALETTE[i % PALETTE.length], borderRadius: 4, maxBarThickness: 36 })),
        }}
      />
    </div>
  );
}

/** Bars (first series, left axis) + a line (second series, right axis) - used for "transactions vs commission". */
export function ComboChart({ labels, bars, line, height = 260 }: { labels: string[]; bars: Series; line: Series; height?: number }) {
  const options = baseOptions() as ChartOptions<"line">;
  if (options.scales) {
    options.scales.y1 = { position: "right", beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { color: TICK } };
  }
  return (
    <div style={{ height }}>
      <Line
        options={options}
        data={{
          labels,
          datasets: [
            { type: "bar" as never, label: bars.label, data: bars.data, backgroundColor: bars.color ?? PALETTE[0], borderRadius: 4, maxBarThickness: 30, yAxisID: "y" } as never,
            { label: line.label, data: line.data, borderColor: line.color ?? PALETTE[1], backgroundColor: "transparent", tension: 0.3, pointRadius: 3, yAxisID: "y1" },
          ],
        }}
      />
    </div>
  );
}

export function LineChart({ labels, series, height = 240 }: { labels: string[]; series: Series[]; height?: number }) {
  return (
    <div style={{ height }}>
      <Line
        options={baseOptions() as ChartOptions<"line">}
        data={{
          labels,
          datasets: series.map((s, i) => ({ label: s.label, data: s.data, borderColor: s.color ?? PALETTE[i % PALETTE.length], backgroundColor: "transparent", tension: 0.3, pointRadius: 3 })),
        }}
      />
    </div>
  );
}
