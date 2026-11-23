"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, Loader2, Play, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import {
  deleteFinishedRuleBacktests,
  deleteRuleBacktest,
  downloadBacktestHistory,
  getHistoryProgress,
  getRuleBacktest,
  getRuleSets,
  listRuleBacktests,
  startRuleBacktest,
} from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";

type Stats = {
  n: number;
  sufficient: boolean;
  win_rate?: number;
  avg_r?: number;
  profit_factor?: number | null;
  total_r?: number;
  max_drawdown_r?: number;
  trailing_avg_r?: number | null;
  fixed_avg_r?: number | null;
  sharpe_trade?: number | null;
  sharpe_annual?: number | null;
  sortino_annual?: number | null;
  calmar?: number | null;
};
type Funnel = {
  counts: Record<string, number>;
  regime_days: Record<string, number>;
  rejections: Record<string, Record<string, number>>;
};
type RunRow = {
  id: number;
  created_at: string;
  segment: string;
  rule_set: string;
  rule_version: string | null;
  start_date: string;
  end_date: string;
  status: string;
  progress: string | null;
  headline: Stats | null;
  trades: number | null;
  options?: { daily_only?: boolean; split_date?: string | null };
};
type PeriodBlock = {
  groups: Record<string, Stats>;
  by_setup: Record<string, Stats>;
  by_score_band: Record<string, Stats>;
};
type Summary = {
  candidates: number;
  trades: number;
  no_fill: number;
  symbols: number;
  groups: Record<string, Stats>;
  by_setup: Record<string, Stats>;
  by_regime: Record<string, Stats>;
  by_exit: Record<string, Stats>;
  by_setup_regime?: Record<string, Stats>;
  by_data?: Record<string, Stats>;
  by_year?: Record<string, Stats>;
  by_stop_type?: Record<string, Stats>;
  untested?: string[];
  soft_checks: Record<string, { passed: Stats; failed: Stats }>;
  limitations: string[];
  funnel?: Funnel;
  exit_mode?: string;
  by_score_band?: Record<string, Stats>;
  by_period?: { train: PeriodBlock; test: PeriodBlock; split_date: string };
  stacked_signals?: number;
  one_open_trade_per_instrument?: boolean;
};
type RunDetail = RunRow & { summary: Summary | null };
type HistoryProgress = {
  status: "idle" | "starting" | "running" | "done" | "error";
  done?: number;
  total?: number;
  current?: string | null;
  candles_saved?: number;
  errors?: string[];
  message?: string | null;
  eta_s?: number | null;
  finished_at?: string | null;
};

const SEGMENT_LABEL: Record<string, string> = {
  cash_swing: "Cash swing (daily)",
  cup_handle: "Cup & handle (daily, ~8 weeks)",
  stock_futures_proxy: "Stock futures (1H, spot proxy)",
  orb_proxy: "Stock futures ORB (10-min, spot proxy)",
  index_futures_proxy: "Index futures (1H, NIFTY/BANKNIFTY spot proxy)",
};

function formatEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `about ${seconds}s left`;
  return `about ${Math.round(seconds / 60)} min left`;
}

function HistoryBar({ p }: { p: HistoryProgress }) {
  if (p.status === "idle") return null;
  const total = p.total ?? 0;
  const done = p.done ?? 0;
  const pctDone = total ? Math.round((100 * done) / total) : 0;
  return (
    <Card>
      <CardContent className="space-y-2 pt-6 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">History download</span>
          {p.status === "running" || p.status === "starting" ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : null}
          <span className={p.status === "error" ? "text-red-600" : p.status === "done" ? "text-emerald-600" : "text-muted-foreground"}>
            {p.status === "starting" && "starting…"}
            {p.status === "running" && `${done} / ${total} instruments${p.current ? ` · ${p.current}` : ""}`}
            {p.status === "done" && `finished · ${total} instruments`}
            {p.status === "error" && `failed: ${p.message ?? "unknown error"}`}
          </span>
          <span className="ml-auto text-xs text-muted-foreground">
            {(p.candles_saved ?? 0).toLocaleString("en-IN")} candles saved
            {p.status === "running" && p.eta_s ? ` · ${formatEta(p.eta_s)}` : ""}
          </span>
        </div>
        <div className="h-2 w-full overflow-hidden rounded bg-muted">
          <div
            className={`h-full ${p.status === "error" ? "bg-red-600" : "bg-emerald-600"}`}
            style={{ width: `${p.status === "done" ? 100 : pctDone}%` }}
          />
        </div>
        {p.errors && p.errors.length > 0 && (
          <p className="text-xs text-amber-600">Failed: {p.errors.join(", ")}</p>
        )}
      </CardContent>
    </Card>
  );
}
const GROUP_LABEL: Record<string, string> = {
  shortlisted: "Shortlisted (top picks per scan)",
  passed_all_rules: "Passed all rules",
  all_trackable: "All trackable (incl. soft rejects)",
};

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function StatCells({ s }: { s: Stats | undefined | null }) {
  if (!s || s.n === 0) {
    return <td className="px-2 py-1 text-muted-foreground" colSpan={9}>no closed trades</td>;
  }
  const tone = (s.avg_r ?? 0) > 0 ? "text-emerald-600" : (s.avg_r ?? 0) < 0 ? "text-red-600" : "";
  return (
    <>
      <td className="px-2 py-1 text-right">
        {s.n}
        {!s.sufficient && <span className="ml-1 text-amber-600">*</span>}
      </td>
      <td className="px-2 py-1 text-right">{s.win_rate}%</td>
      <td className={`px-2 py-1 text-right ${tone}`}>{s.avg_r}R</td>
      <td className="px-2 py-1 text-right">{s.profit_factor ?? "—"}</td>
      <td className="px-2 py-1 text-right">{s.total_r}R</td>
      <td className="px-2 py-1 text-right">{s.max_drawdown_r}R</td>
      <td className="px-2 py-1 text-right" title={`per trade ${s.sharpe_trade ?? "—"}`}>{s.sharpe_annual ?? "—"}</td>
      <td className="px-2 py-1 text-right">{s.sortino_annual ?? "—"}</td>
      <td className="px-2 py-1 text-right">{s.calmar ?? "—"}</td>
    </>
  );
}

function StatsTable({ rows }: { rows: [string, Stats | undefined][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            <th className="px-2 py-1 text-left font-medium">Group</th>
            <th className="px-2 py-1 text-right font-medium">Trades</th>
            <th className="px-2 py-1 text-right font-medium">Win</th>
            <th className="px-2 py-1 text-right font-medium">Avg</th>
            <th className="px-2 py-1 text-right font-medium">PF</th>
            <th className="px-2 py-1 text-right font-medium">Total</th>
            <th className="px-2 py-1 text-right font-medium">Max DD</th>
            <th className="px-2 py-1 text-right font-medium" title="Annualised from the daily R curve; hover a value for the per-trade figure">Sharpe</th>
            <th className="px-2 py-1 text-right font-medium" title="Annualised; only losing days count as risk">Sortino</th>
            <th className="px-2 py-1 text-right font-medium" title="R per year divided by the max drawdown in R">Calmar</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, s]) => (
            <tr key={label} className="border-t">
              <td className="px-2 py-1">{label}</td>
              <StatCells s={s} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const FUNNEL_LABEL: Record<string, string> = {
  trading_days: "Trading days in range",
  days_skipped_rbi_budget: "Days skipped: RBI/Budget inside the hold",
  days_skipped_no_nifty_history: "Days skipped: not enough NIFTY history",
  bars_evaluated: "Hourly bars checked",
  bars_skipped_last_minutes: "Bars skipped: entry would be in the last minutes",
  stock_days_evaluated: "Stock-days checked",
  stock_days_skipped_history_or_turnover: "Stock-days skipped: history or turnover",
  candidates_rejected: "Setup checks rejected",
  candidates_trackable: "Candidates tracked",
  candidates_valid: "Candidates passing every rule",
  candidates_without_forward_bars: "Candidates without later bars",
};

function FunnelView({ f }: { f: Funnel }) {
  const counts = Object.entries(f.counts);
  const regimes = Object.entries(f.regime_days);
  return (
    <div className="space-y-2">
      <p className="text-xs font-medium">Funnel: where candidates drop out</p>
      <div className="grid gap-1 text-xs sm:grid-cols-2">
        {counts.map(([k, v]) => (
          <div key={k} className="flex justify-between border-b py-0.5">
            <span className="text-muted-foreground">{FUNNEL_LABEL[k] ?? k}</span>
            <span className="font-medium">{v.toLocaleString("en-IN")}</span>
          </div>
        ))}
      </div>
      {regimes.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Market regime on the days checked: {regimes.map(([r, n]) => `${r} ${n}`).join(" · ")}
        </p>
      )}
      {Object.entries(f.rejections).map(([setup, reasons]) => (
        <div key={setup} className="text-xs">
          <p className="font-medium">{setup}</p>
          <ul className="pl-4 text-muted-foreground">
            {Object.entries(reasons).map(([reason, n]) => (
              <li key={reason} className="flex justify-between gap-2">
                <span>{reason}</span>
                <span>{n.toLocaleString("en-IN")}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function PeriodView({ p }: { p: { train: PeriodBlock; test: PeriodBlock; split_date: string } }) {
  const half = (name: string, b: PeriodBlock) => [
    ...Object.entries(GROUP_LABEL).map(([k, label]) => [`${name}: ${label}`, b.groups[k]] as [string, Stats | undefined]),
  ];
  const setups = Array.from(new Set([...Object.keys(p.train.by_setup), ...Object.keys(p.test.by_setup)])).sort();
  const bands = Array.from(new Set([...Object.keys(p.train.by_score_band), ...Object.keys(p.test.by_score_band)])).sort();
  return (
    <div className="space-y-2 rounded border p-2">
      <p className="text-xs font-medium">Train (before {p.split_date}) vs test (from {p.split_date})</p>
      <StatsTable rows={[...half("Train", p.train), ...half("Test", p.test)]} />
      <p className="text-xs font-medium">By setup</p>
      <StatsTable rows={setups.flatMap((k) => [[`${k} · train`, p.train.by_setup[k]], [`${k} · test`, p.test.by_setup[k]]] as [string, Stats | undefined][])} />
      <p className="text-xs font-medium">By score band</p>
      <StatsTable rows={bands.flatMap((k) => [[`${k} · train`, p.train.by_score_band[k]], [`${k} · test`, p.test.by_score_band[k]]] as [string, Stats | undefined][])} />
    </div>
  );
}

function RunDetailCard({ run }: { run: RunDetail }) {
  const s = run.summary;
  if (!s) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">
          #{run.id} · {SEGMENT_LABEL[run.segment] ?? run.segment} · {run.rule_set}
          {run.options?.daily_only ? " · daily bars only" : ""}
          {run.options?.split_date ? ` · split ${run.options.split_date}` : ""}{" "}
          <span className="font-normal text-muted-foreground">
            ({run.rule_version}) · {run.start_date} → {run.end_date} · {s.symbols} stocks · {s.candidates} candidates,{" "}
            {s.no_fill} not filled
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <StatsTable rows={Object.entries(GROUP_LABEL).map(([k, label]) => [label, s.groups[k]])} />
        {s.one_open_trade_per_instrument && (
          <p className="text-xs text-muted-foreground">
            One open trade per instrument: {s.stacked_signals ?? 0} signals on a stock already in a trade were left out.
          </p>
        )}
        {s.by_period && <PeriodView p={s.by_period} />}
        {s.by_score_band && Object.keys(s.by_score_band).length > 0 && (
          <div>
            <p className="mb-1 text-xs font-medium">By score band (does a higher score mean better trades?)</p>
            <StatsTable rows={Object.entries(s.by_score_band)} />
          </div>
        )}
        <div>
          <p className="mb-1 text-xs font-medium">By setup (all trackable)</p>
          <StatsTable rows={Object.entries(s.by_setup)} />
        </div>
        <div>
          <p className="mb-1 text-xs font-medium">By regime</p>
          <StatsTable rows={Object.entries(s.by_regime)} />
        </div>
        {s.by_setup_regime && (
          <div>
            <p className="mb-1 text-xs font-medium">By setup and regime</p>
            <StatsTable rows={Object.entries(s.by_setup_regime)} />
          </div>
        )}
        {s.by_year && Object.keys(s.by_year).length > 1 && (
          <div>
            <p className="mb-1 text-xs font-medium">By year (signal date)</p>
            <StatsTable rows={Object.entries(s.by_year)} />
          </div>
        )}
        {s.by_stop_type && Object.keys(s.by_stop_type).length > 1 && (
          <div>
            <p className="mb-1 text-xs font-medium">By stop type</p>
            <StatsTable rows={Object.entries(s.by_stop_type)} />
          </div>
        )}
        {s.by_data && Object.keys(s.by_data).length > 1 && (
          <div>
            <p className="mb-1 text-xs font-medium">By data available</p>
            <StatsTable rows={Object.entries(s.by_data)} />
          </div>
        )}
        {s.untested && s.untested.length > 0 && (
          <p className="text-xs text-amber-600">Not tested for some or all trades: {s.untested.join(", ")}</p>
        )}
        <div>
          <p className="mb-1 text-xs font-medium">Soft checks: passed vs failed</p>
          <StatsTable
            rows={Object.entries(s.soft_checks).flatMap(([name, v]) => [
              [`${name} passed`, v.passed] as [string, Stats],
              [`${name} failed`, v.failed] as [string, Stats],
            ])}
          />
        </div>
        <div>
          <p className="mb-1 text-xs font-medium">By exit</p>
          <StatsTable rows={Object.entries(s.by_exit)} />
        </div>
        {s.exit_mode && s.exit_mode !== "fixed" && (
          <p className="text-xs text-muted-foreground">
            Exit: {s.exit_mode.replace("_", " + ")}. Same trades with the fixed exit (avg R, all trackable):{" "}
            {s.groups.all_trackable?.fixed_avg_r ?? "—"} vs {s.groups.all_trackable?.avg_r ?? "—"} managed.
          </p>
        )}
        {s.funnel && <FunnelView f={s.funnel} />}
        <ul className="list-disc pl-5 text-xs text-muted-foreground">
          {s.limitations.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

export default function RuleBacktestsPage() {
  const [ruleSets, setRuleSets] = useState<string[]>([]);
  const [segments, setSegments] = useState<string[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [selected, setSelected] = useState<RunDetail[]>([]);
  const [segment, setSegment] = useState("cash_swing");
  const [ruleSet, setRuleSet] = useState("current");
  const [start, setStart] = useState(isoDaysAgo(365));
  const [end, setEnd] = useState(isoDaysAgo(10));
  const [dailyOnly, setDailyOnly] = useState(false);
  const [splitDate, setSplitDate] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<HistoryProgress>({ status: "idle" });

  const fetchHistory = useCallback(() => {
    getHistoryProgress()
      .then((p) => setHistory(p as HistoryProgress))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  const downloading = history.status === "running" || history.status === "starting";
  useEffect(() => {
    if (!downloading) return;
    const t = setInterval(fetchHistory, 3000);
    return () => clearInterval(t);
  }, [downloading, fetchHistory]);

  const fetchData = useCallback(() => {
    Promise.all([getRuleSets(), listRuleBacktests()])
      .then((res) => {
        const [sets, list] = res as [{ rule_sets: string[]; segments: string[] }, { runs: RunRow[] }];
        setRuleSets(sets.rule_sets);
        setSegments(sets.segments);
        setRuns(list.runs);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load backtests")))
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

  const run = async (rs: string) => {
    setBusy(true);
    try {
      await startRuleBacktest({
        segment, rule_set: rs, start_date: start, end_date: end, daily_only: dailyOnly,
        split_date: splitDate || undefined,
      });
      toast.success(`Backtest started (${rs})`);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the backtest"));
    } finally {
      setBusy(false);
    }
  };

  const download = async () => {
    try {
      const r = (await downloadBacktestHistory()) as { status: string };
      if (r.status === "busy") {
        toast.message("Another data job is running; try again shortly");
      } else {
        setHistory({ status: "starting" });
        fetchHistory();
      }
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the download"));
    }
  };

  // Two clicks to delete: the first arms the button, the second deletes.
  const [armed, setArmed] = useState<number | "all" | null>(null);
  const remove = async (target: number | "all") => {
    if (armed !== target) {
      setArmed(target);
      return;
    }
    setArmed(null);
    try {
      if (target === "all") {
        const r = (await deleteFinishedRuleBacktests()) as { deleted: number };
        toast.success(`Deleted ${r.deleted} runs`);
        setSelected([]);
      } else {
        await deleteRuleBacktest(target);
        toast.success(`Deleted run #${target}`);
        setSelected(selected.filter((x) => x.id !== target));
      }
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not delete"));
    }
  };

  const toggle = async (id: number) => {
    if (selected.some((r) => r.id === id)) {
      setSelected(selected.filter((r) => r.id !== id));
      return;
    }
    try {
      const detail = (await getRuleBacktest(id)) as RunDetail;
      setSelected([...selected, detail].slice(-3));
    } catch (e) {
      toast.error(errorMessage(e, "Could not load the run"));
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-2xl font-bold">Rule Backtest</h1>
          <p className="text-sm text-muted-foreground">
            Replays a rule set over stored history and compares versions. Results only inform decisions; rules change only through a reviewed config PR.
          </p>
        </div>
        <Button className="ml-auto" variant="outline" size="sm" onClick={download} disabled={downloading}>
          <Download className="mr-1 h-4 w-4" /> Download history
        </Button>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchData(); }} disabled={loading}>
          <RefreshCw className="mr-1 h-4 w-4" /> Refresh
        </Button>
      </div>

      <HistoryBar p={history} />

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-6 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">Segment</span>
            <select className="rounded border bg-background px-2 py-1" value={segment} onChange={(e) => setSegment(e.target.value)}>
              {segments.map((s) => (
                <option key={s} value={s}>{SEGMENT_LABEL[s] ?? s}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">Rule set</span>
            <select className="rounded border bg-background px-2 py-1" value={ruleSet} onChange={(e) => setRuleSet(e.target.value)}>
              {ruleSets.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">From</span>
            <input type="date" className="rounded border bg-background px-2 py-1" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">To</span>
            <input type="date" className="rounded border bg-background px-2 py-1" value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">Train/test split (optional)</span>
            <input type="date" className="rounded border bg-background px-2 py-1" value={splitDate} onChange={(e) => setSplitDate(e.target.value)} />
          </label>
          <label className="flex items-center gap-2 pb-1 text-xs">
            <input type="checkbox" checked={dailyOnly} onChange={(e) => setDailyOnly(e.target.checked)} />
            Daily bars only
          </label>
          <Button size="sm" onClick={() => run(ruleSet)} disabled={busy || running}>
            <Play className="mr-1 h-4 w-4" /> Run
          </Button>
          <p className="w-full text-xs text-muted-foreground">
            One run at a time. To compare, run each rule set on the same segment and dates, then tick both below. Stock futures runs take longer (hourly bars).
            A split date reports the rules&apos; results before it (train) and from it (test) side by side: set rules on the train half, trust only what holds in the test half.
            &quot;Daily bars only&quot; models every entry on the next day&apos;s daily bar and skips the 4H check, the same way the years before the hourly history are tested, so periods can be compared on equal terms.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center text-sm">
            Runs
            {runs.some((r) => r.status !== "running") && (
              <Button size="sm" variant="ghost" className="ml-auto h-7 text-xs text-red-600" onClick={() => remove("all")}>
                <Trash2 className="mr-1 h-3 w-3" />
                {armed === "all" ? "Click again to delete all finished runs" : "Delete all finished"}
              </Button>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <Loader2 className="h-5 w-5 animate-spin" />
          ) : runs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No runs yet. Download history first, then run.</p>
          ) : (
            <div className="space-y-2">
              {runs.map((r) => (
                <label key={r.id} className="flex flex-wrap items-center gap-2 border-b pb-2 text-xs">
                  <input
                    type="checkbox"
                    disabled={r.status !== "done"}
                    checked={selected.some((x) => x.id === r.id)}
                    onChange={() => toggle(r.id)}
                  />
                  <span className="font-medium">#{r.id}</span>
                  <span>{SEGMENT_LABEL[r.segment] ?? r.segment}</span>
                  <Badge variant="outline">{r.rule_set}</Badge>
                  {r.options?.daily_only ? <Badge variant="outline">daily bars only</Badge> : null}
                  <span className="text-muted-foreground">{r.start_date} → {r.end_date}</span>
                  {r.status === "done" ? (
                    <span className="ml-auto">
                      {r.headline && r.headline.n > 0
                        ? `shortlisted: ${r.headline.n} trades · win ${r.headline.win_rate}% · avg ${r.headline.avg_r}R`
                        : `${r.trades ?? 0} trades`}
                    </span>
                  ) : (
                    <span className={`ml-auto ${r.status === "error" ? "text-red-600" : "text-muted-foreground"}`}>
                      {r.status === "running" && <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />}
                      {r.progress}
                    </span>
                  )}
                  {r.status !== "running" && (
                    <button
                      type="button"
                      className="text-red-600 hover:underline"
                      title="Delete this run"
                      onClick={(e) => {
                        e.preventDefault();
                        void remove(r.id);
                      }}
                    >
                      {armed === r.id ? "Delete?" : <Trash2 className="h-3 w-3" />}
                    </button>
                  )}
                </label>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {selected.map((r) => (
        <RunDetailCard key={r.id} run={r} />
      ))}
      <p className="text-xs text-muted-foreground">* fewer than 30 trades: too few to draw conclusions.</p>
    </div>
  );
}
