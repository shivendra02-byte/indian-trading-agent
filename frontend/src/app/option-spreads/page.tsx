"use client";

import { useCallback, useEffect, useState } from "react";
import { FlaskConical, Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { backtestOptionSpreads, getOptionSpreads, runOptionSpreads } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";

type Leg = { type: "CE" | "PE"; strike: number; side: "short" | "long"; price: number; delta?: number };
type Spread = {
  id: number;
  symbol: string;
  signal_date: string;
  expiry: string;
  strategy: string;
  legs: Leg[];
  credit: number;
  width: number;
  lots: number;
  lot_size: number;
  regime: string | null;
  vix: number | null;
  exit_by: string;
  decision: string;
  panel: { confidence?: number; reasons?: string[]; risks?: string[]; skipped?: string; error?: string } | null;
  status: string;
  last_value: number | null;
  exit_date: string | null;
  exit_reason: string | null;
  pnl_r: number | null;
  pnl_inr: number | null;
};
type Stats = {
  trades: number;
  win_rate?: number;
  avg_r?: number;
  total_r?: number;
  max_drawdown_r?: number;
  profit_factor?: number | null;
  sharpe_trade?: number | null;
  total_inr_per_lot?: number;
};
type Status = { enabled: boolean; open: Spread[]; closed: Spread[]; record: { paper: Stats; all: Stats } };
type Backtest = {
  all: Stats;
  by_symbol: Record<string, Stats>;
  by_strategy: Record<string, Stats>;
  by_exit: Record<string, Stats>;
  by_year: Record<string, Stats>;
  by_period?: Record<string, Stats>;
  skipped: Record<string, number>;
  limitations: string[];
};

const NAME: Record<string, string> = { bull_put: "Bull put", bear_call: "Bear call", iron_condor: "Iron condor" };
const v = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : x.toFixed(d));
const tone = (x: number | null | undefined) =>
  x === null || x === undefined ? "" : x > 0 ? "text-green-600" : x < 0 ? "text-red-600" : "";
const legsText = (legs: Leg[]) =>
  legs.map((l) => `${l.side === "short" ? "S" : "B"} ${l.strike} ${l.type} ${l.price.toFixed(1)}`).join(" · ");

function StatsTable({ rows }: { rows: [string, Stats | undefined][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            {["Group", "Trades", "Win", "Avg R", "Total R", "PF", "Sharpe/trade", "Max DD R", "₹ per lot"].map((h, i) => (
              <th key={h} className={`py-1 pr-3 font-normal ${i ? "text-right" : "text-left"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, s]) =>
            s ? (
              <tr key={label} className="border-t">
                <td className="py-1 pr-3 font-medium">{label}</td>
                <td className="pr-3 text-right">{s.trades}</td>
                <td className="pr-3 text-right">{v(s.win_rate, 1)}%</td>
                <td className={`pr-3 text-right ${tone(s.avg_r)}`}>{v(s.avg_r, 3)}</td>
                <td className={`pr-3 text-right ${tone(s.total_r)}`}>{v(s.total_r, 1)}</td>
                <td className="pr-3 text-right">{v(s.profit_factor)}</td>
                <td className="pr-3 text-right">{v(s.sharpe_trade, 3)}</td>
                <td className="pr-3 text-right">{v(s.max_drawdown_r, 1)}</td>
                <td className={`pr-3 text-right ${tone(s.total_inr_per_lot)}`}>
                  {s.total_inr_per_lot === undefined ? "—" : s.total_inr_per_lot.toLocaleString("en-IN")}
                </td>
              </tr>
            ) : null,
          )}
        </tbody>
      </table>
    </div>
  );
}

function SpreadRow({ s }: { s: Spread }) {
  const tip = s.panel?.reasons?.length
    ? `AI ${s.panel.confidence}: ${s.panel.reasons.join("; ")}${s.panel.risks?.length ? ` · risks: ${s.panel.risks.join("; ")}` : ""}`
    : s.panel?.skipped ?? s.panel?.error ?? "";
  return (
    <tr className="border-t" title={tip}>
      <td className="py-1 pr-3 font-medium">{s.symbol}</td>
      <td className="pr-3">{NAME[s.strategy] ?? s.strategy}</td>
      <td className="pr-3">{s.signal_date}</td>
      <td className="pr-3">{s.expiry}</td>
      <td className="pr-3">{legsText(s.legs)}</td>
      <td className="pr-3 text-right">{v(s.credit, 1)}</td>
      <td className="pr-3 text-right">{s.lots}</td>
      <td className="pr-3">
        <Badge variant={s.decision === "approved" ? "default" : "outline"}>{s.decision.replace("_", " ")}</Badge>
      </td>
      <td className="pr-3">
        {s.status === "open" ? `now ${v(s.last_value, 1)} · exit by ${s.exit_by}` : `${(s.exit_reason ?? "").replace("_", " ")} ${s.exit_date}`}
      </td>
      <td className={`pr-3 text-right ${tone(s.pnl_r)}`}>{v(s.pnl_r)}</td>
      <td className={`text-right ${tone(s.pnl_inr)}`}>{s.pnl_inr === null ? "—" : s.pnl_inr.toLocaleString("en-IN")}</td>
    </tr>
  );
}

export default function OptionSpreadsPage() {
  const [data, setData] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [bt, setBt] = useState<Backtest | null>(null);
  const [form, setForm] = useState({ start_date: "2024-06-01", end_date: "2026-11-20", split_date: "2025-12-01" });

  const fetchData = useCallback(() => {
    getOptionSpreads()
      .then((res) => setData(res as Status))
      .catch((e) => toast.error(errorMessage(e, "Could not load option spreads")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const runNow = async () => {
    setBusy("run");
    try {
      const res = (await runOptionSpreads()) as {
        status: string;
        reason?: string;
        note?: string;
        results?: Record<string, string>;
      };
      if (res.note) toast.message(res.note);
      if (res.status === "ok") toast.success(Object.entries(res.results ?? {}).map(([k, x]) => `${k}: ${x}`).join(" · ") || "Done");
      else toast.message(res.reason ?? res.status);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Run failed"));
    } finally {
      setBusy(null);
    }
  };

  const runBacktest = async () => {
    setBusy("bt");
    try {
      setBt((await backtestOptionSpreads({ ...form, split_date: form.split_date || null })) as Backtest);
    } catch (e) {
      toast.error(errorMessage(e, "Backtest failed"));
    } finally {
      setBusy(null);
    }
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
          <h1 className="text-2xl font-semibold">Option Spreads</h1>
          <p className="text-sm text-muted-foreground">
            NIFTY / BANKNIFTY / SENSEX credit spreads from the evening option files · short leg at 0.15–0.20 delta · bull put in
            uptrends, bear call in downtrends, iron condor sideways · take profit 50%, stop at 2× credit, out the day before
            expiry · AI-reviewed, paper
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={fetchData}>
            <RefreshCw className="mr-1 h-4 w-4" /> Refresh
          </Button>
          <Button variant="outline" size="sm" disabled={busy !== null} onClick={runNow}>
            {busy === "run" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Run now
          </Button>
        </div>
      </div>

      {data && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              Spreads {!data.enabled && <Badge variant="destructive">Disabled</Badge>}
            </CardTitle>
            <CardDescription>
              Paper (AI-approved): {data.record.paper.trades} closed · {v(data.record.paper.total_r, 1)}R · all tracked:{" "}
              {data.record.all.trades} closed · {v(data.record.all.total_r, 1)}R. Hover a row for the AI&apos;s reasons.
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            {data.open.length + data.closed.length === 0 ? (
              <p className="text-sm text-muted-foreground">No spreads yet. Candidates are built after the evening option download.</p>
            ) : (
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    {["Index", "Spread", "Signal", "Expiry", "Legs", "Credit", "Lots", "Decision", "Status", "R", "₹"].map((h) => (
                      <th key={h} className="py-1 pr-3 text-left font-normal">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[...data.open, ...data.closed].map((s) => (
                    <SpreadRow key={s.id} s={s} />
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
            <FlaskConical className="h-4 w-4" /> Backtest on stored option files
          </CardTitle>
          <CardDescription>Same rules, one spread per index at a time. 1R = one credit (the loss at the stop).</CardDescription>
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
            <Button size="sm" disabled={busy !== null} onClick={runBacktest}>
              {busy === "bt" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Run
            </Button>
          </div>
          {bt && (
            <>
              <StatsTable
                rows={[
                  ["All", bt.all],
                  ...Object.entries(bt.by_period ?? {}).map(([k, s]) => [k, s] as [string, Stats]),
                  ...Object.entries(bt.by_symbol).map(([k, s]) => [k, s] as [string, Stats]),
                  ...Object.entries(bt.by_strategy).map(([k, s]) => [NAME[k] ?? k, s] as [string, Stats]),
                  ...Object.entries(bt.by_exit).map(([k, s]) => [`exit: ${k.replace("_", " ")}`, s] as [string, Stats]),
                  ...Object.entries(bt.by_year).map(([k, s]) => [k, s] as [string, Stats]),
                ]}
              />
              {Object.keys(bt.skipped).length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Days skipped: {Object.entries(bt.skipped).slice(0, 6).map(([k, n]) => `${k} (${n})`).join(" · ")}
                </p>
              )}
              <ul className="list-disc pl-5 text-xs text-muted-foreground">
                {bt.limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
