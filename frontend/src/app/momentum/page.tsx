"use client";

import { useCallback, useEffect, useState } from "react";
import { FlaskConical, Loader2, Play, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import {
  deleteMomentumBacktest,
  getMomentum,
  getMomentumBacktests,
  runMomentumRank,
  runMomentumScan,
  startMomentumBacktest,
} from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";

type RankRow = { symbol: string; rank: number; rank_from_bottom: number; score: number; [k: string]: string | number | null };
type Position = {
  id: number;
  symbol: string;
  side: "long" | "short";
  tradingsymbol: string | null;
  lots: number | null;
  lot_size: number | null;
  entry_ts: string;
  entry: number;
  stop: number;
  initial_stop: number;
  status: string;
  exit_ts: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  pnl_r: number | null;
  pnl_inr: number | null;
};
type Status = {
  enabled: boolean;
  long_enabled: boolean;
  short_enabled: boolean;
  settings: Record<string, number | number[]>;
  week: string | null;
  long_group: RankRow[];
  short_group: RankRow[];
  ranked: number;
  open: Position[];
  closed: Position[];
  record: { closed: number; wins: number; total_r: number; avg_r: number | null };
};
type Stats = {
  trades?: number;
  win_rate?: number;
  avg_r?: number;
  total_r?: number;
  profit_factor?: number | null;
  avg_days?: number;
  sharpe_annual?: number | null;
  sortino_annual?: number | null;
  calmar?: number | null;
  max_drawdown_r?: number;
  return_pct?: number;
};
type Run = {
  id: number;
  created_at: string;
  status: string;
  progress: string | null;
  params: { start_date: string; end_date: string; sides: string[]; daily_only: boolean; split_date: string | null };
  summary: {
    reason?: string;
    book?: Stats;
    by_side?: Record<string, Stats>;
    by_exit?: Record<string, Stats>;
    by_period?: Record<string, Stats>;
    by_year?: Record<string, Stats>;
    data?: Record<string, number>;
    open_at_end?: number;
    limitations?: string[];
  };
};

const v = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : x.toFixed(d));
const tone = (x: number | null | undefined) =>
  x === null || x === undefined ? "" : x > 0 ? "text-green-600" : x < 0 ? "text-red-600" : "";

function StatsRow({ label, s }: { label: string; s?: Stats }) {
  if (!s) return null;
  return (
    <tr className="border-t">
      <td className="py-1 pr-3 font-medium">{label}</td>
      <td className="pr-3 text-right">{s.trades ?? 0}</td>
      <td className="pr-3 text-right">{v(s.win_rate, 1)}%</td>
      <td className={`pr-3 text-right ${tone(s.avg_r)}`}>{v(s.avg_r, 3)}</td>
      <td className={`pr-3 text-right ${tone(s.total_r)}`}>{v(s.total_r, 1)}</td>
      <td className="pr-3 text-right">{v(s.profit_factor)}</td>
      <td className="pr-3 text-right">{v(s.sharpe_annual)}</td>
      <td className="pr-3 text-right">{v(s.sortino_annual)}</td>
      <td className="pr-3 text-right">{v(s.calmar)}</td>
      <td className="pr-3 text-right">{v(s.max_drawdown_r, 1)}</td>
    </tr>
  );
}

function StatsTable({ rows }: { rows: [string, Stats | undefined][] }) {
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
            <th className="pr-3 text-right font-normal">Sharpe</th>
            <th className="pr-3 text-right font-normal">Sortino</th>
            <th className="pr-3 text-right font-normal">Calmar</th>
            <th className="pr-3 text-right font-normal">Max DD R</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, s]) => (
            <StatsRow key={label} label={label} s={s} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Group({ title, rows, side }: { title: string; rows: RankRow[]; side: "long" | "short" }) {
  return (
    <div>
      <p className="mb-1 text-sm font-medium">{title}</p>
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">No ranking yet.</p>
      ) : (
        <table className="w-full text-xs">
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol} className="border-t">
                <td className="py-1 pr-2 text-muted-foreground">#{side === "long" ? r.rank : r.rank_from_bottom}</td>
                <td className="pr-2 font-medium">{r.symbol}</td>
                <td className={`pr-2 text-right ${tone(r.score)}`}>{r.score.toFixed(1)}</td>
                <td className="text-right text-muted-foreground">
                  {Object.entries(r)
                    .filter(([k]) => k.startsWith("rs_"))
                    .map(([k, x]) => `${k.replace("rs_", "").replace("_pct", "")} ${x}%`)
                    .join(" · ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function MomentumPage() {
  const [data, setData] = useState<Status | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [form, setForm] = useState({
    start_date: "2024-01-01",
    end_date: "2026-11-20",
    split_date: "2025-06-01",
    long: true,
    short: true,
    daily_only: false,
  });

  const fetchData = useCallback(() => {
    Promise.all([getMomentum(), getMomentumBacktests()])
      .then((res) => {
        const [status, bt] = res as [Status, { runs: Run[] }];
        setData(status);
        setRuns(bt.runs);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load the momentum book")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const running = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = setInterval(fetchData, 5000);
    return () => clearInterval(t);
  }, [running, fetchData]);

  const act = async (name: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(name);
    try {
      const res = (await fn()) as { status?: string; reason?: string };
      if (res.status === "ok" || res.status === "started" || res.status === "deleted") toast.success(ok);
      else toast.message(res.reason ?? res.status ?? "Done");
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Request failed"));
    } finally {
      setBusy(null);
    }
  };

  const startBacktest = () => {
    const sides = [form.long ? "long" : null, form.short ? "short" : null].filter(Boolean) as ("long" | "short")[];
    if (!sides.length) {
      toast.error("Pick at least one side");
      return;
    }
    act(
      "backtest",
      () =>
        startMomentumBacktest({
          start_date: form.start_date,
          end_date: form.end_date,
          sides,
          daily_only: form.daily_only,
          split_date: form.split_date || null,
        }),
      "Backtest started",
    );
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Momentum Book</h1>
          <p className="text-sm text-muted-foreground">
            NSE stock futures ranked weekly on 3- and 6-month strength vs NIFTY (skipping the last month) · enter on an
            hourly pullback and break of structure · 2 ATR stop, 3 ATR trail · exit on EMA reversal or leaving the
            buffer group · paper, rules only
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={fetchData}>
            <RefreshCw className="mr-1 h-4 w-4" /> Refresh
          </Button>
          <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => act("rank", runMomentumRank, "Ranking updated")}>
            {busy === "rank" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Re-rank
          </Button>
          <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => act("scan", runMomentumScan, "Scan done")}>
            {busy === "scan" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Scan entries
          </Button>
        </div>
      </div>

      {data && (
        <Card>
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-2 text-base">
              Week of {data.week ?? "—"}
              <Badge variant={data.long_enabled ? "default" : "outline"}>Long {data.long_enabled ? "on" : "off"}</Badge>
              <Badge variant={data.short_enabled ? "default" : "outline"}>Short {data.short_enabled ? "on" : "off"}</Badge>
              {!data.enabled && <Badge variant="destructive">Disabled</Badge>}
            </CardTitle>
            <CardDescription>
              {data.ranked} stocks ranked · up to {String(data.settings.max_positions_per_side)} positions per side · held
              while within the top/bottom {String(data.settings.buffer_n)}
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-6 md:grid-cols-2">
            <Group title="Strongest (long group)" rows={data.long_group} side="long" />
            <Group title="Weakest (short group)" rows={data.short_group} side="short" />
          </CardContent>
        </Card>
      )}

      {data && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Positions</CardTitle>
            <CardDescription>
              Closed {data.record.closed} · wins {data.record.wins} · total {v(data.record.total_r, 1)}R · avg{" "}
              {v(data.record.avg_r, 3)}R
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            {data.open.length + data.closed.length === 0 ? (
              <p className="text-sm text-muted-foreground">No positions yet.</p>
            ) : (
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-3 text-left font-normal">Contract</th>
                    <th className="pr-3 text-left font-normal">Side</th>
                    <th className="pr-3 text-left font-normal">Entered</th>
                    <th className="pr-3 text-right font-normal">Entry</th>
                    <th className="pr-3 text-right font-normal">Stop now</th>
                    <th className="pr-3 text-right font-normal">Lots</th>
                    <th className="pr-3 text-left font-normal">Exit</th>
                    <th className="pr-3 text-right font-normal">R</th>
                    <th className="text-right font-normal">₹</th>
                  </tr>
                </thead>
                <tbody>
                  {[...data.open, ...data.closed].map((p) => (
                    <tr key={p.id} className="border-t">
                      <td className="py-1 pr-3 font-medium">{p.tradingsymbol ?? p.symbol}</td>
                      <td className="pr-3">{p.side}</td>
                      <td className="pr-3">{p.entry_ts.slice(0, 16).replace("T", " ")}</td>
                      <td className="pr-3 text-right">{v(p.entry)}</td>
                      <td className="pr-3 text-right">{p.status === "open" ? v(p.stop) : "—"}</td>
                      <td className="pr-3 text-right">{p.lots ?? "—"}</td>
                      <td className="pr-3">
                        {p.status === "open"
                          ? "open"
                          : `${(p.exit_reason ?? "").replace("_", " ")} ${v(p.exit_price)} · ${(p.exit_ts ?? "").slice(0, 10)}`}
                      </td>
                      <td className={`pr-3 text-right ${tone(p.pnl_r)}`}>{v(p.pnl_r)}</td>
                      <td className={`text-right ${tone(p.pnl_inr)}`}>{p.pnl_inr === null ? "—" : p.pnl_inr.toLocaleString("en-IN")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <FlaskConical className="h-4 w-4" /> Portfolio backtest
          </CardTitle>
          <CardDescription>
            Weekly re-rank and daily entries/exits over stored history, point in time. Sharpe, Sortino and Calmar come from
            the book&apos;s daily equity curve (1R = 1% of capital).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-3 text-sm">
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">From</span>
              <Input type="date" value={form.start_date} onChange={(e) => setForm({ ...form, start_date: e.target.value })} />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">To</span>
              <Input type="date" value={form.end_date} onChange={(e) => setForm({ ...form, end_date: e.target.value })} />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">Train/test split</span>
              <Input type="date" value={form.split_date} onChange={(e) => setForm({ ...form, split_date: e.target.value })} />
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={form.long} onChange={(e) => setForm({ ...form, long: e.target.checked })} /> Long
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={form.short} onChange={(e) => setForm({ ...form, short: e.target.checked })} /> Short
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={form.daily_only} onChange={(e) => setForm({ ...form, daily_only: e.target.checked })} />{" "}
              Daily bars only
            </label>
            <Button size="sm" disabled={busy !== null || running} onClick={startBacktest}>
              {busy === "backtest" || running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Run
            </Button>
          </div>
          {runs.length === 0 && <p className="text-sm text-muted-foreground">No backtests yet.</p>}
          {runs.map((r) => (
            <div key={r.id} className="space-y-2 rounded-md border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="font-medium">
                  #{r.id} · {r.params.start_date} → {r.params.end_date} · {r.params.sides.join(" + ")}
                  {r.params.daily_only ? " · daily only" : ""}
                </span>
                <span className="flex items-center gap-2">
                  <Badge variant={r.status === "ok" ? "default" : r.status === "running" ? "secondary" : "destructive"}>{r.status}</Badge>
                  {r.status !== "running" && (
                    <Button variant="ghost" size="sm" onClick={() => act(`del${r.id}`, () => deleteMomentumBacktest(r.id), "Deleted")}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  )}
                </span>
              </div>
              {r.status === "running" && <p className="text-xs text-muted-foreground">{r.progress ?? "starting…"}</p>}
              {r.status === "error" && <p className="text-xs text-red-600">{r.progress}</p>}
              {r.summary.book && (
                <>
                  <p className="text-xs text-muted-foreground">
                    Return {v(r.summary.book.return_pct, 1)}% of capital · avg hold {v(r.summary.book.avg_days, 1)} days ·{" "}
                    {r.summary.open_at_end ?? 0} open at the end · entries hourly {r.summary.data?.hourly ?? 0} / daily-only{" "}
                    {r.summary.data?.daily_only ?? 0}
                  </p>
                  <StatsTable
                    rows={[
                      ["Book", r.summary.book],
                      ...Object.entries(r.summary.by_side ?? {}).map(([k, s]) => [k, s] as [string, Stats]),
                      ...Object.entries(r.summary.by_period ?? {}).map(([k, s]) => [k, s] as [string, Stats]),
                      ...Object.entries(r.summary.by_exit ?? {}).map(([k, s]) => [`exit: ${k.replace("_", " ")}`, s] as [string, Stats]),
                      ...Object.entries(r.summary.by_year ?? {}).map(([k, s]) => [k, s] as [string, Stats]),
                    ]}
                  />
                  <ul className="list-disc pl-5 text-xs text-muted-foreground">
                    {(r.summary.limitations ?? []).map((l) => (
                      <li key={l}>{l}</li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
