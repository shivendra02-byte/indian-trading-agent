"use client";

import { Badge } from "@/components/ui/badge";

export type RStats = {
  n: number;
  sufficient?: boolean;
  win_rate?: number;
  avg_r?: number;
  total_r?: number;
  profit_factor?: number | null;
  max_drawdown_r?: number;
  worst_quarter?: string | null;
  worst_quarter_r?: number | null;
  quarters_positive_pct?: number | null;
  avg_days?: number | null;
  pnl_rupees?: number;
  sharpe_annual?: number | null;
  sortino_annual?: number | null;
  calmar?: number | null;
};

export type ResearchTrade = {
  id?: number;
  strategy?: string;
  symbol: string;
  direction: string | null;
  entry_date: string | null;
  entry_price: number | null;
  stop: number | null;
  target?: number | null;
  entry_ts?: string | null;
  exit_ts?: string | null;
  timeframe?: string | null;
  exit_date: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  status: string;
  pnl_r: number | null;
  pnl_rupees: number | null;
  lots?: unknown;
  legs?: { symbol: string; side: string; lots?: number | null; entry?: number | null; exit?: number | null }[] | null;
  extra?: Record<string, unknown> | null;
  first_seen?: string;
};

export type BearResult = {
  bear_quarters: string[];
  avg_r_per_bear_quarter: number;
  avg_r_per_other_quarter: number | null;
  total_r_in_bear_quarters: number;
} | null;

export type ResearchSummary = {
  all: RStats;
  open: number;
  open_r: number;
  by_year?: Record<string, RStats>;
  by_symbol?: Record<string, RStats>;
  by_direction?: Record<string, RStats>;
  by_exit?: Record<string, RStats>;
  by_period?: { train: RStats; test: RStats };
  bear?: BearResult;
  limitations?: string[];
  extra?: Record<string, unknown>;
};

export const num = (x: number | null | undefined, d = 2) =>
  x === null || x === undefined || Number.isNaN(x) ? "—" : x.toFixed(d);
export const tone = (x: number | null | undefined) =>
  x === null || x === undefined ? "" : x > 0 ? "text-green-600" : x < 0 ? "text-red-600" : "";
export const rupees = (x: number | null | undefined) =>
  x === null || x === undefined ? "—" : `₹${Math.round(x).toLocaleString("en-IN")}`;

export function StatsTable({ rows, minSample = 30 }: { rows: [string, RStats | undefined][]; minSample?: number }) {
  const shown = rows.filter(([, s]) => s && s.n);
  if (!shown.length) return <p className="text-xs text-muted-foreground">No closed trades.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 text-left font-normal">Group</th>
            <th className="pr-3 text-right font-normal">Trades</th>
            <th className="pr-3 text-right font-normal">Win</th>
            <th className="pr-3 text-right font-normal">Avg R</th>
            <th className="pr-3 text-right font-normal">Total R</th>
            <th className="pr-3 text-right font-normal">PF</th>
            <th className="pr-3 text-right font-normal">Max DD R</th>
            <th className="pr-3 text-right font-normal">Worst qtr R</th>
            <th className="pr-3 text-right font-normal">Qtrs +</th>
            <th className="pr-3 text-right font-normal">Sharpe</th>
            <th className="pr-3 text-right font-normal">Calmar</th>
            <th className="pr-3 text-right font-normal">Days</th>
            <th className="pr-3 text-right font-normal">₹</th>
          </tr>
        </thead>
        <tbody>
          {shown.map(([label, s]) => s && (
            <tr key={label} className="border-t">
              <td className="py-1 pr-3 font-medium">
                {label}
                {s.n < minSample && <span className="ml-1 text-[10px] text-muted-foreground">(few)</span>}
              </td>
              <td className="pr-3 text-right">{s.n}</td>
              <td className="pr-3 text-right">{num(s.win_rate, 1)}%</td>
              <td className={`pr-3 text-right ${tone(s.avg_r)}`}>{num(s.avg_r, 3)}</td>
              <td className={`pr-3 text-right ${tone(s.total_r)}`}>{num(s.total_r, 1)}</td>
              <td className="pr-3 text-right">{num(s.profit_factor)}</td>
              <td className="pr-3 text-right">{num(s.max_drawdown_r, 1)}</td>
              <td className={`pr-3 text-right ${tone(s.worst_quarter_r)}`} title={s.worst_quarter ?? ""}>
                {num(s.worst_quarter_r, 1)}
              </td>
              <td className="pr-3 text-right">{s.quarters_positive_pct == null ? "—" : `${s.quarters_positive_pct}%`}</td>
              <td className="pr-3 text-right">{num(s.sharpe_annual)}</td>
              <td className="pr-3 text-right">{num(s.calmar)}</td>
              <td className="pr-3 text-right">{num(s.avg_days, 0)}</td>
              <td className={`pr-3 text-right ${tone(s.pnl_rupees)}`}>{rupees(s.pnl_rupees)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function BearLine({ bear }: { bear?: BearResult }) {
  if (!bear) return null;
  return (
    <p className="text-xs text-muted-foreground">
      NIFTY&apos;s worst quarters ({bear.bear_quarters.join(", ")}): avg{" "}
      <span className={tone(bear.avg_r_per_bear_quarter)}>{num(bear.avg_r_per_bear_quarter)}R</span> per quarter vs{" "}
      <span className={tone(bear.avg_r_per_other_quarter)}>{num(bear.avg_r_per_other_quarter)}R</span> in other quarters
      — positive in bad quarters means it diversifies the long books.
    </p>
  );
}

function when(ts: string | null | undefined, day: string | null): string {
  if (ts && ts.length > 10 && ts.slice(11, 16) !== "00:00") return `${ts.slice(0, 10)} ${ts.slice(11, 16)}`;
  return day ?? "";
}

export function TradesTable({ trades, showStrategy = false }: { trades: ResearchTrade[]; showStrategy?: boolean }) {
  if (!trades.length) return <p className="text-xs text-muted-foreground">No trades yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            {showStrategy && <th className="py-1 pr-3 text-left font-normal">Strategy</th>}
            <th className="py-1 pr-3 text-left font-normal">Instrument</th>
            <th className="pr-3 text-left font-normal">Side</th>
            <th className="pr-3 text-left font-normal">Entry</th>
            <th className="pr-3 text-right font-normal">Price</th>
            <th className="pr-3 text-right font-normal">Stop</th>
            <th className="pr-3 text-right font-normal">Target</th>
            <th className="pr-3 text-left font-normal">Exit</th>
            <th className="pr-3 text-right font-normal">Price</th>
            <th className="pr-3 text-left font-normal">Reason</th>
            <th className="pr-3 text-right font-normal">R</th>
            <th className="pr-3 text-right font-normal">₹</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => (
            <tr key={t.id ?? `${t.symbol}-${t.entry_date}-${i}`} className="border-t">
              {showStrategy && <td className="py-1 pr-3">{t.strategy}</td>}
              <td className="py-1 pr-3 font-medium" title={t.legs ? JSON.stringify(t.legs) : ""}>
                {t.symbol}
                {typeof t.extra?.size_note === "string" && (
                  <span className="ml-1 text-[10px] text-amber-600" title={t.extra.size_note}>lot</span>
                )}
              </td>
              <td className={`pr-3 ${t.direction === "short" ? "text-red-600" : "text-green-600"}`}>{t.direction}</td>
              <td className="pr-3">
                {when(t.entry_ts, t.entry_date)}
                {t.timeframe && <span className="ml-1 text-[10px] text-muted-foreground">{t.timeframe}</span>}
              </td>
              <td className="pr-3 text-right">{num(t.entry_price)}</td>
              <td className="pr-3 text-right">
                {num(t.stop)}
                {t.status === "open" && typeof t.extra?.stop_now === "number" && t.extra.stop_now !== t.stop && (
                  <span className="block text-[10px] text-muted-foreground">now {num(t.extra.stop_now)}</span>
                )}
              </td>
              <td className="pr-3 text-right">{num(t.target)}</td>
              <td className="pr-3">
                {t.status === "open" ? <Badge variant="outline">open</Badge> : when(t.exit_ts, t.exit_date)}
              </td>
              <td className="pr-3 text-right">{num(t.exit_price)}</td>
              <td className="pr-3">{t.exit_reason ?? (t.status === "open" ? "marked at last close" : "")}</td>
              <td className={`pr-3 text-right ${tone(t.pnl_r)}`}>{num(t.pnl_r)}</td>
              <td className={`pr-3 text-right ${tone(t.pnl_rupees)}`}>{rupees(t.pnl_rupees)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ShadowNote() {
  return (
    <p className="text-xs text-muted-foreground">
      Research strategies are <b>shadow only</b>: no AI review per trade, no Telegram, nothing counted in the live
      books, limits or Today&apos;s P&amp;L. They run in the live market after every completed hourly bar (stops and
      targets are hit during the session) and again after the close. 1R = 1% of ₹10 lakh; costs are a model.
    </p>
  );
}
