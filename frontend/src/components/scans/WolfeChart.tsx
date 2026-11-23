"use client";

import { useEffect, useState } from "react";
import { getStoredCandles } from "@/lib/api";

export type WolfePoint = { n: number; date: string; index: number; price: number };
export type WolfePattern = {
  points: WolfePoint[];
  line13_at5: number;
  overshoot_atr: number;
  target_line14: number;
  time_ratio: number;
  span_bars: number;
  converging: boolean;
  rsi_divergence: boolean;
};
type Bar = { ts: string; open: number; high: number; low: number; close: number };

const W = 640;
const H = 240;
const PAD = { l: 8, r: 52, t: 10, b: 18 };

/** Daily candles with the five Wolfe points, the 1–3 and 2–4 lines, the 1–4 target line
 *  and the trade levels, drawn from stored candles. */
export function WolfeChart({ token, pattern, entry, stop, target }: {
  token: number;
  pattern: WolfePattern;
  entry: number | null;
  stop: number | null;
  target: number | null;
}) {
  const [bars, setBars] = useState<Bar[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    getStoredCandles(token, "day", 220)
      .then((res) => {
        const body = res as { candles?: Bar[] } | Bar[];
        setBars(Array.isArray(body) ? body : body.candles ?? []);
      })
      .catch(() => setError(true));
  }, [token]);

  if (error) return <p className="text-xs text-muted-foreground">Chart unavailable.</p>;
  if (!bars) return <p className="text-xs text-muted-foreground">Loading chart…</p>;

  const first = pattern.points[0].date;
  const startIdx = Math.max(0, bars.findIndex((b) => b.ts.slice(0, 10) >= first) - 10);
  const shown = bars.slice(startIdx);
  if (shown.length < 5) return <p className="text-xs text-muted-foreground">Not enough stored bars to draw.</p>;
  const pos = (d: string) => shown.findIndex((b) => b.ts.slice(0, 10) === d);
  const pts = pattern.points.map((p) => ({ ...p, x: pos(p.date) })).filter((p) => p.x >= 0);
  const extra = 8;                                     // bars drawn to the right for the target line
  const n = shown.length + extra;
  const levels = [entry, stop, target, pattern.target_line14].filter((v): v is number => v !== null);
  const lo = Math.min(...shown.map((b) => b.low), ...levels);
  const hi = Math.max(...shown.map((b) => b.high), ...levels);
  const x = (i: number) => PAD.l + (i / (n - 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + ((hi - v) / (hi - lo || 1)) * (H - PAD.t - PAD.b);
  const cw = Math.max(1, ((W - PAD.l - PAD.r) / n) * 0.6);
  const p = (k: number) => pts.find((q) => q.n === k);
  const line = (a?: { x: number; price: number }, b?: { x: number; price: number }, toX?: number) => {
    if (!a || !b || a.x === b.x) return null;
    const slope = (b.price - a.price) / (b.x - a.x);
    const end = toX ?? b.x;
    return { x1: x(a.x), y1: y(a.price), x2: x(end), y2: y(a.price + slope * (end - a.x)) };
  };
  const last = shown.length - 1;
  const l13 = line(p(1), p(3), p(5)?.x);
  const l24 = line(p(2), p(4), p(5)?.x);
  const l14 = line(p(1), p(4), last + extra);
  const hline = (v: number | null, color: string, label: string) =>
    v === null ? null : (
      <g key={label}>
        <line x1={x(last - 2)} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke={color} strokeDasharray="4 3" strokeWidth={1} />
        <text x={W - PAD.r + 3} y={y(v) + 3} fontSize={9} fill={color}>{label} {v.toFixed(1)}</text>
      </g>
    );

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label="Wolfe Wave chart">
      {shown.map((b, i) => {
        const up = b.close >= b.open;
        const color = up ? "var(--chart-up, #16a34a)" : "var(--chart-down, #dc2626)";
        return (
          <g key={b.ts}>
            <line x1={x(i)} x2={x(i)} y1={y(b.high)} y2={y(b.low)} stroke={color} strokeWidth={0.8} />
            <rect x={x(i) - cw / 2} width={cw} y={y(Math.max(b.open, b.close))}
                  height={Math.max(0.8, Math.abs(y(b.open) - y(b.close)))} fill={color} />
          </g>
        );
      })}
      {l13 && <line {...l13} stroke="#2563eb" strokeWidth={1.4} />}
      {l24 && <line {...l24} stroke="#2563eb" strokeWidth={1.4} />}
      {l14 && <line {...l14} stroke="#d97706" strokeWidth={1.4} strokeDasharray="6 3" />}
      {pts.map((q) => (
        <g key={q.n}>
          <circle cx={x(q.x)} cy={y(q.price)} r={3.5} fill="#2563eb" />
          <text x={x(q.x)} y={q.n % 2 ? y(q.price) + 13 : y(q.price) - 6} fontSize={10} textAnchor="middle"
                fill="currentColor" fontWeight={600}>{q.n}</text>
        </g>
      ))}
      {hline(entry, "#64748b", "entry")}
      {hline(stop, "#dc2626", "stop")}
      {hline(target, "#16a34a", "target")}
      <text x={PAD.l} y={H - 4} fontSize={9} fill="currentColor" opacity={0.6}>
        {shown[0].ts.slice(0, 10)} – {shown[last].ts.slice(0, 10)} · blue: 1–3 and 2–4 lines · orange: 1–4 target line
      </text>
    </svg>
  );
}
