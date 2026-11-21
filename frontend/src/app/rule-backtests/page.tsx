"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import {
  downloadBacktestHistory,
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
  soft_checks: Record<string, { passed: Stats; failed: Stats }>;
  limitations: string[];
};
type RunDetail = RunRow & { summary: Summary | null };

const SEGMENT_LABEL: Record<string, string> = {
  cash_swing: "Cash swing (daily)",
  stock_futures_proxy: "Stock futures (1H, spot proxy)",
};
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
    return <td className="px-2 py-1 text-muted-foreground" colSpan={6}>no closed trades</td>;
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

function RunDetailCard({ run }: { run: RunDetail }) {
  const s = run.summary;
  if (!s) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">
          #{run.id} · {SEGMENT_LABEL[run.segment] ?? run.segment} · {run.rule_set}{" "}
          <span className="font-normal text-muted-foreground">
            ({run.rule_version}) · {run.start_date} → {run.end_date} · {s.symbols} stocks · {s.candidates} candidates,{" "}
            {s.no_fill} not filled
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <StatsTable rows={Object.entries(GROUP_LABEL).map(([k, label]) => [label, s.groups[k]])} />
        <div>
          <p className="mb-1 text-xs font-medium">By setup (all trackable)</p>
          <StatsTable rows={Object.entries(s.by_setup)} />
        </div>
        <div>
          <p className="mb-1 text-xs font-medium">By regime</p>
          <StatsTable rows={Object.entries(s.by_regime)} />
        </div>
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
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

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
      await startRuleBacktest({ segment, rule_set: rs, start_date: start, end_date: end });
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
      toast.message(r.status === "busy" ? "Another data job is running; try again shortly" : "History download started (5–10 min the first time)");
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the download"));
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
        <Button className="ml-auto" variant="outline" size="sm" onClick={download}>
          <Download className="mr-1 h-4 w-4" /> Download history
        </Button>
        <Button variant="outline" size="sm" onClick={() => { setLoading(true); fetchData(); }} disabled={loading}>
          <RefreshCw className="mr-1 h-4 w-4" /> Refresh
        </Button>
      </div>

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
          <Button size="sm" onClick={() => run(ruleSet)} disabled={busy || running}>
            <Play className="mr-1 h-4 w-4" /> Run
          </Button>
          <p className="w-full text-xs text-muted-foreground">
            One run at a time. To compare, run each rule set on the same segment and dates, then tick both below. Stock futures runs take longer (hourly bars).
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Runs</CardTitle>
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
