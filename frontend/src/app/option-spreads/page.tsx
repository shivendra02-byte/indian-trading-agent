"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, FlaskConical, Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { backtestOptionSpreads, getOptionSpreads, getSpreadAlignment, getSpreadScans, runOptionSpreads } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";
import { SpreadResearch } from "@/components/spreads/SpreadResearch";

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
  mode?: string | null;
  signal_ts?: string | null;
  setup?: string | null;
  direction?: string | null;
  invalid?: number | { above: number; below: number } | null;
  iv_pct?: number | null;
};
// GET /api/options/spreads/alignment (spreads.alignment → spread_mtf.evaluate per index)
type TfState = { ok: boolean | null; note: string } | null;
type Alignment = {
  regime: string;
  bias: string | null;
  h4: TfState;
  h1: TfState;
  m15: TfState;
  signal: { direction: string; kind: string } | null;
  reason?: string;
  source: string;
  last_15m: string | null;
};
type AlignmentResponse = { mode: string; at: string; symbols: Record<string, Alignment> };
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
  by_regime?: Record<string, Stats>;
  skipped: Record<string, number>;
  funnel?: Record<string, { trading_days: number; no_option_file: number; entry_days: number; opened: number; book_busy: number }>;
  limitations: string[];
  trades?: Record<string, unknown>[];
};

// Trade-level CSV of the backtest on screen (asked 24 Nov 2026), built in the browser.
const CSV_COLS = ["symbol", "strategy", "regime", "vix", "signal_date", "expiry", "exit_date", "exit_reason", "credit",
  "width", "lot_size", "lots", "exit_value", "pnl_points", "pnl_r", "pnl_inr"];
function downloadTradesCsv(trades: Record<string, unknown>[]) {
  const cell = (v: unknown) => {
    const t = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const lines = [CSV_COLS.join(","), ...trades.map((t) => CSV_COLS.map((c) => cell(t[c])).join(","))];
  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "spread-backtest-trades.csv";
  a.click();
  URL.revokeObjectURL(url);
}

const NAME: Record<string, string> = {
  bull_put: "Bull put (credit)",
  bear_call: "Bear call (credit)",
  iron_condor: "Iron condor",
  bull_call: "Bull call (debit)",
  bear_put: "Bear put (debit)",
};
const ALL_STRATEGIES = ["iron_condor", "bull_put", "bear_call", "bull_call", "bear_put"];
const LIVE_STRATEGIES = ["iron_condor", "bull_put", "bear_call"];
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

function invalidText(s: Spread): string {
  const inv = s.invalid;
  if (inv === null || inv === undefined) return "";
  if (typeof inv === "object") return ` · out if 1H closes outside ${Math.round(inv.below)}–${Math.round(inv.above)}`;
  return ` · out if 1H closes ${s.direction === "up" ? "below" : "above"} ${Math.round(inv)}`;
}

function TfCell({ label, st }: { label: string; st: TfState }) {
  const mark = !st || st.ok === null ? "·" : st.ok ? "✓" : "✗";
  const cls = !st || st.ok === null ? "text-muted-foreground" : st.ok ? "text-green-600" : "text-muted-foreground";
  return (
    <td className="pr-3" title={st?.note ?? ""}>
      <span className={cls}>{label} {mark}</span>
      <span className="ml-1 text-muted-foreground">{st?.note ?? ""}</span>
    </td>
  );
}

type ScanRow = { at: string; bar: string | null; symbol: string; result: string; signal: number; regime: string | null; manual: number };

// Every intraday spread scan today and why each index did or didn't get a spread (asked 25 Nov 2026).
function ScanLogCard({ rows }: { rows: ScanRow[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Scan log today</CardTitle>
      </CardHeader>
      <CardContent className="max-h-80 overflow-auto text-xs">
        <table className="w-full">
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.at}-${r.symbol}-${i}`} className="border-b last:border-0">
                <td className="py-1 pr-3 text-muted-foreground">
                  {r.at.slice(11, 16)}
                  {r.manual ? " (Run now)" : ""}
                </td>
                <td className="pr-3 font-medium">{r.symbol}</td>
                <td className="pr-3 text-muted-foreground">{r.regime ?? ""}</td>
                <td className={r.signal ? "font-medium" : "text-muted-foreground"}>
                  {r.signal ? "signal → " : ""}
                  {r.result}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function AlignmentCard({ a }: { a: AlignmentResponse }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Entry alignment · {a.at.slice(11, 16)}</CardTitle>
        <CardDescription>
          Daily regime → 4H confirms → 1H setup → 15-minute trigger, on completed bars of the index (SENSEX follows
          NIFTY). The scan runs after every 15-minute bar from 09:30 to 14:45; open spreads are checked on each 1H close.
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full text-xs">
          <tbody>
            {Object.entries(a.symbols).map(([sym, x]) => (
              <tr key={sym} className="border-t">
                <td className="py-1 pr-3 font-medium">{sym}</td>
                <td className="pr-3">
                  <Badge variant="outline">{x.regime.replace("_", " ")}</Badge>
                </td>
                <TfCell label="4H" st={x.h4} />
                <TfCell label="1H" st={x.h1} />
                <TfCell label="15m" st={x.m15} />
                <td className="pr-3">
                  {x.signal ? (
                    <Badge>signal: {x.signal.kind.replace("_", " ")}</Badge>
                  ) : (
                    <span className="text-muted-foreground">{x.reason ?? ""}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
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
      <td className="pr-3" title={s.setup ? `setup: ${s.setup.replace("_", " ")}${s.iv_pct != null ? ` · IV pct ${s.iv_pct}` : ""}` : ""}>
        {s.signal_ts ? `${s.signal_date} ${s.signal_ts.slice(11, 16)}` : s.signal_date}
      </td>
      <td className="pr-3">{s.expiry}</td>
      <td className="pr-3">{legsText(s.legs)}</td>
      <td className="pr-3 text-right">{v(s.credit, 1)}</td>
      <td className="pr-3 text-right">{s.lots}</td>
      <td className="pr-3">
        <Badge variant={s.decision === "approved" ? "default" : "outline"}>{s.decision.replace("_", " ")}</Badge>
      </td>
      <td className="pr-3">
        {s.status === "open" ? `now ${v(s.last_value, 1)}${invalidText(s)} · exit by ${s.exit_by}` : `${(s.exit_reason ?? "").replace("_", " ")} ${s.exit_date}`}
      </td>
      <td className={`pr-3 text-right ${tone(s.pnl_r)}`}>{v(s.pnl_r)}</td>
      <td className={`text-right ${tone(s.pnl_inr)}`}>{s.pnl_inr === null ? "—" : s.pnl_inr.toLocaleString("en-IN")}</td>
    </tr>
  );
}

export default function OptionSpreadsPage() {
  const [data, setData] = useState<Status | null>(null);
  const [align, setAlign] = useState<AlignmentResponse | null>(null);
  const [scans, setScans] = useState<ScanRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [bt, setBt] = useState<Backtest | null>(null);
  const [form, setForm] = useState({ start_date: "2024-06-01", end_date: "2026-11-20", split_date: "2025-12-01" });
  // compare strategies (asked 24 Nov 2026); the live rules are the three credit strategies
  const [strats, setStrats] = useState<string[]>(LIVE_STRATEGIES);
  const [condorUnstable, setCondorUnstable] = useState(false);
  const [minSingle, setMinSingle] = useState("");
  const [vixSwitch, setVixSwitch] = useState("11");

  const fetchData = useCallback(() => {
    getOptionSpreads()
      .then((res) => setData(res as Status))
      .catch((e) => toast.error(errorMessage(e, "Could not load option spreads")))
      .finally(() => setLoading(false));
    getSpreadAlignment()
      .then((res) => setAlign(res as AlignmentResponse))
      .catch(() => setAlign(null));
    getSpreadScans()
      .then((res) => setScans((res as { scans: ScanRow[] }).scans))
      .catch(() => setScans([]));
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
      const compare = strats.length !== LIVE_STRATEGIES.length || strats.some((x) => !LIVE_STRATEGIES.includes(x));
      setBt(
        (await backtestOptionSpreads({
          ...form,
          split_date: form.split_date || null,
          strategies: compare ? strats : null,
          condor_in_unstable: condorUnstable,
          min_credit_ratio_single: minSingle ? Number(minSingle) / 100 : null,
          debit_below_vix: vixSwitch === "" ? null : Number(vixSwitch),
        })) as Backtest,
      );
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
            NIFTY / BANKNIFTY / SENSEX · entry when the daily trend, 4H, 1H and a 15-minute trigger agree, priced from the
            live option chain · uptrend (strong or moderate): bull put (sell), or bull call (buy) when the ATM IV
            percentile is below 30 · downtrend: bear call, or bear put below it · iron condor on a 1H range rejection when
            sideways · exits on 1H closes · AI-reviewed, paper
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

      {align && align.mode === "intraday" && <AlignmentCard a={align} />}
      {scans.length > 0 && <ScanLogCard rows={scans} />}

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
                    {["Index", "Spread", "Signal", "Expiry", "Legs", "Credit / debit", "Lots", "Decision", "Status", "R", "₹"].map((h) => (
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
          <CardDescription>
            Uses the end-of-day rules (entry on the evening option files, regime only, exits on daily closes): stored
            option prices are daily, so the intraday 4H/1H/15m entry can&apos;t be replayed here yet. One spread per index at a time. Strong uptrend: bull put, or bull call when India VIX is below the
            switch level; strong downtrend: bear call, or bear put below it; iron condor sideways. Changing the ticked
            strategies switches to comparing them instead (each keeps its own book; the VIX switch is then not used).
            1R = the loss at the stop (credit spreads: one credit; debit spreads: half the debit).
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
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">Min credit, bull put / bear call (% of width)</span>
              <Input
                type="number"
                min={0}
                max={100}
                placeholder="same as condor (15)"
                value={minSingle}
                onChange={(e) => setMinSingle(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-muted-foreground">Debit spreads when VIX below (0 = never)</span>
              <Input type="number" min={0} max={100} step={0.5} value={vixSwitch} onChange={(e) => setVixSwitch(e.target.value)} />
            </label>
            <Button size="sm" disabled={busy !== null || strats.length === 0} onClick={runBacktest}>
              {busy === "bt" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Run
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
            {ALL_STRATEGIES.map((k) => (
              <label key={k} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={strats.includes(k)}
                  onChange={(e) => setStrats(e.target.checked ? [...strats, k] : strats.filter((x) => x !== k))}
                />
                {NAME[k]}
              </label>
            ))}
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={condorUnstable} onChange={(e) => setCondorUnstable(e.target.checked)} />
              Iron condor also in choppy (UNSTABLE) markets
            </label>
          </div>
          {bt && (
            <>
              {bt.trades && bt.trades.length > 0 && (
                <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => downloadTradesCsv(bt.trades ?? [])}>
                  <Download className="mr-1 h-3 w-3" /> Download trades (CSV)
                </Button>
              )}
              {bt.funnel && (
                <p className="text-xs text-muted-foreground">
                  Why so few trades:{" "}
                  {Object.entries(bt.funnel)
                    .map(
                      ([sym, f]) =>
                        `${sym}: ${f.trading_days} trading days, ${f.no_option_file} without an option file, ` +
                        `${f.entry_days} in the entry window, ${f.opened} opened` +
                        (f.book_busy ? `, ${f.book_busy} skipped (spread already open)` : ""),
                    )
                    .join(" · ")}
                </p>
              )}
              <StatsTable
                rows={[
                  ["All", bt.all],
                  ...Object.entries(bt.by_period ?? {}).map(([k, s]) => [k, s] as [string, Stats]),
                  ...Object.entries(bt.by_symbol).map(([k, s]) => [k, s] as [string, Stats]),
                  ...Object.entries(bt.by_strategy).map(([k, s]) => [NAME[k] ?? k, s] as [string, Stats]),
                  ...Object.entries(bt.by_regime ?? {}).map(([k, s]) => [`regime: ${k}`, s] as [string, Stats]),
                  ...Object.entries(bt.by_exit).map(([k, s]) => [`exit: ${k.replace("_", " ")}`, s] as [string, Stats]),
                  ...Object.entries(bt.by_year).map(([k, s]) => [k, s] as [string, Stats]),
                ]}
              />
              {Object.keys(bt.skipped).length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Skipped: {Object.entries(bt.skipped).slice(0, 10).map(([k, n]) => `${k} (${n})`).join(" · ")}
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
      <SpreadResearch
        defaults={{ start_date: form.start_date, end_date: form.end_date, split_date: form.split_date || "2025-12-01", minSingle: minSingle || "10" }}
      />
    </div>
  );
}
