"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Download, FlaskConical, Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { getSpreadResearch, getSpreadResearchRuns, spreadResearchCsvUrl, startSpreadResearch } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";

// Shapes from backend/options/spread_research.py
type M = {
  trades: number;
  win_rate?: number;
  expectancy_r?: number;
  expectancy_inr?: number;
  total_r?: number;
  total_inr?: number;
  avg_win_r?: number | null;
  avg_loss_r?: number | null;
  win_loss_ratio?: number | null;
  profit_factor?: number | null;
  max_losing_streak?: number;
  max_drawdown_r?: number;
  max_drawdown_inr?: number;
  max_drawdown_days?: number;
  consistency?: number | null;
  sharpe_daily_annual?: number | null;
  sortino_daily_annual?: number | null;
  return_pct_annual?: number;
  sharpe_per_trade?: number | null;
};
type Cell = {
  symbol: string;
  regime: string;
  strategy: string;
  eligible: boolean;
  why: string;
  score_train: number | null;
  train: M;
  test: M;
};
type Result = {
  range: { start: string; end: string; split: string };
  costs_included: boolean;
  settings: { live_exit: string };
  baseline: { all: M; train: M; test: M };
  experiment_a: { chosen_on_train: string; rules: { rule: string; is_live: boolean; score_train: number | null; train: M; test: M }[] };
  experiment_b: { policy: { symbol: string; regime: string; strategy: string }[]; cells: Cell[]; test: M };
  experiment_c: {
    exit: string;
    test: M;
    walk_forward: { folds: { quarter: string; cells_chosen: number; exit: string; result: M }[]; all: M };
  };
  limitations: string[];
};
type Run = { id: number; created_at: string; status: string; error?: string | null; params: Record<string, unknown> };
type Status = { running: boolean; progress: string | null; runs: Run[] };

const n = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : x.toFixed(d));
const inr = (x: number | null | undefined) =>
  x === null || x === undefined ? "—" : `₹${Math.round(x).toLocaleString("en-IN")}`;
const tone = (x: number | null | undefined) =>
  x === null || x === undefined ? "" : x > 0 ? "text-green-600" : x < 0 ? "text-red-600" : "";
const strat = (s: string) => s.replace(/_/g, " ");

const COLS: [string, (m: M) => ReactNode, string?][] = [
  ["Trades", (m) => m.trades],
  ["Win", (m) => (m.win_rate === undefined ? "—" : `${m.win_rate}%`)],
  ["Expectancy R", (m) => <span className={tone(m.expectancy_r)}>{n(m.expectancy_r, 3)}</span>, "Average R per trade after costs"],
  ["Exp ₹", (m) => <span className={tone(m.expectancy_inr)}>{inr(m.expectancy_inr)}</span>],
  ["Avg win / loss R", (m) => `${n(m.avg_win_r)} / ${n(m.avg_loss_r)}`],
  ["Win÷loss", (m) => n(m.win_loss_ratio)],
  ["PF", (m) => n(m.profit_factor)],
  ["Losing streak", (m) => m.max_losing_streak ?? "—", "Most losing trades in a row"],
  ["Max DD", (m) => `${n(m.max_drawdown_r, 1)}R · ${inr(m.max_drawdown_inr)}`],
  ["DD days", (m) => m.max_drawdown_days ?? "—", "Longest time below a previous equity high (calendar days)"],
  ["Sharpe (daily, yr)", (m) => n(m.sharpe_daily_annual), "Daily marked-to-market portfolio returns, annualised"],
  ["Sortino (yr)", (m) => n(m.sortino_daily_annual)],
  ["Return %/yr", (m) => n(m.return_pct_annual)],
  ["Consistency", (m) => (m.consistency === null || m.consistency === undefined ? "—" : `${Math.round(m.consistency * 100)}%`), "Share of quarters with a positive total"],
];

function MetricTable({ rows }: { rows: [ReactNode, M][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 text-left font-normal">Group</th>
            {COLS.map(([h, , tip]) => (
              <th key={h} className="py-1 pr-3 text-right font-normal" title={tip}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, m], i) => (
            <tr key={i} className="border-t">
              <td className="py-1 pr-3">{label}</td>
              {m.trades
                ? COLS.map(([h, f]) => (
                    <td key={h} className="py-1 pr-3 text-right tabular-nums">
                      {f(m)}
                    </td>
                  ))
                : (
                  <td colSpan={COLS.length} className="py-1 text-right text-muted-foreground">
                    no trades
                  </td>
                )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SpreadResearch({ defaults }: { defaults: { start_date: string; end_date: string; split_date: string; minSingle: string } }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [form, setForm] = useState({ ...defaults, costs: true });
  const [busy, setBusy] = useState(false);

  const fetchRun = useCallback((id: number) => {
    getSpreadResearch(id)
      .then((r) => setResult(((r as { result: Result | null }).result) ?? null))
      .catch((e) => toast.error(errorMessage(e, "Could not load the research run")));
  }, []);

  const fetchStatus = useCallback(() => {
    getSpreadResearchRuns()
      .then((r) => {
        const st = r as Status;
        setStatus(st);
        const latestOk = st.runs.find((x) => x.status === "ok");
        setSelected((cur) => {
          if (cur === null && latestOk) {
            fetchRun(latestOk.id);
            return latestOk.id;
          }
          return cur;
        });
      })
      .catch(() => undefined);
  }, [fetchRun]);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  useEffect(() => {
    if (!status?.running) return;
    const t = setInterval(() => {
      getSpreadResearchRuns()
        .then((r) => {
          const st = r as Status;
          setStatus(st);
          if (!st.running && st.runs[0]?.status === "ok") {
            setSelected(st.runs[0].id);
            fetchRun(st.runs[0].id);
            toast.success("Research run finished");
          } else if (!st.running && st.runs[0]?.status === "error") {
            toast.error(st.runs[0].error ?? "Research run failed");
          }
        })
        .catch(() => undefined);
    }, 4000);
    return () => clearInterval(t);
  }, [status?.running, fetchRun]);

  const start = async () => {
    setBusy(true);
    try {
      const res = (await startSpreadResearch({
        start_date: form.start_date,
        end_date: form.end_date,
        split_date: form.split_date,
        min_credit_ratio_single: form.minSingle ? Number(form.minSingle) / 100 : null,
        costs: form.costs,
      })) as { status: string };
      if (res.status === "busy") toast.message("A research run is already going");
      else toast.success("Research started — this takes a minute or two");
      fetchStatus();
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the research run"));
    } finally {
      setBusy(false);
    }
  };

  const pick = (id: number) => {
    setSelected(id);
    fetchRun(id);
  };

  const r = result;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <FlaskConical className="h-4 w-4" /> Research: which strategy, which exit, does it hold up unseen?
        </CardTitle>
        <CardDescription>
          A: exits re-played on the same entries. B: every strategy per index × market regime, eligible only if its
          train trades pass the minimums. C: B&apos;s choice with A&apos;s exit on unseen data, plus a quarter-by-quarter
          walk-forward. R and ₹ are after costs; Sharpe is on daily marked-to-market portfolio returns. Never changes the
          live rules.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="flex flex-wrap items-end gap-3">
          {(["start_date", "end_date", "split_date"] as const).map((k) => (
            <label key={k} className="space-y-1">
              <span className="text-xs text-muted-foreground">{k === "start_date" ? "From" : k === "end_date" ? "To" : "Train/test split"}</span>
              <Input type="date" value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
            </label>
          ))}
          <label className="space-y-1">
            <span className="text-xs text-muted-foreground">Min credit, bull put / bear call (%)</span>
            <Input type="number" min={0} max={100} placeholder="15" value={form.minSingle} onChange={(e) => setForm({ ...form, minSingle: e.target.value })} />
          </label>
          <label className="flex items-center gap-1 text-xs">
            <input type="checkbox" checked={form.costs} onChange={(e) => setForm({ ...form, costs: e.target.checked })} />
            Include costs
          </label>
          <Button size="sm" disabled={busy || !!status?.running} onClick={start}>
            {busy || status?.running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
            Run research
          </Button>
        </div>
        {status?.running && <p className="text-xs text-muted-foreground">Running: {status.progress ?? "…"}</p>}
        {status && status.runs.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-muted-foreground">Runs:</span>
            {status.runs.slice(0, 8).map((x) => (
              <Button
                key={x.id}
                size="sm"
                variant={selected === x.id ? "default" : "outline"}
                className="h-7 px-2 text-xs"
                disabled={x.status !== "ok"}
                title={x.error ?? undefined}
                onClick={() => pick(x.id)}
              >
                #{x.id} {x.created_at.slice(5, 16)} {x.status !== "ok" ? `(${x.status})` : ""}
              </Button>
            ))}
          </div>
        )}

        {r && selected !== null && (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <span className="text-muted-foreground">
                {r.range.start} → {r.range.end}, split {r.range.split} · {r.costs_included ? "after costs" : "before costs"}
              </span>
              <a className="inline-flex items-center gap-1 text-primary hover:underline" href={spreadResearchCsvUrl(selected, "baseline")}>
                <Download className="h-3 w-3" /> Live-rules trades (CSV)
              </a>
              <a className="inline-flex items-center gap-1 text-primary hover:underline" href={spreadResearchCsvUrl(selected, "all_strategies")}>
                <Download className="h-3 w-3" /> All-strategies trades (CSV)
              </a>
            </div>

            <section className="space-y-2">
              <h3 className="font-medium">Live rules ({r.settings.live_exit})</h3>
              <MetricTable rows={[["All", r.baseline.all], ["Train", r.baseline.train], ["Test (unseen)", r.baseline.test]]} />
            </section>

            <section className="space-y-2">
              <h3 className="font-medium">A · Exit rules on the same entries</h3>
              <p className="text-xs text-muted-foreground">
                Ranked on the train period; the test columns show how each held up. Chosen on train: <b>{r.experiment_a.chosen_on_train}</b>.
                R is measured against the live rules&apos; 1R for every row.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-muted-foreground">
                    <tr>
                      {["Exit rule", "Train trades", "Train exp R", "Train PF", "Test trades", "Test exp R", "Test PF", "Test max DD R", "Test Sharpe (yr)"].map((h, i) => (
                        <th key={h} className={`py-1 pr-3 font-normal ${i ? "text-right" : "text-left"}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {r.experiment_a.rules.slice(0, 12).map((x) => (
                      <tr key={x.rule} className="border-t">
                        <td className="py-1 pr-3">
                          {x.rule} {x.is_live && <Badge variant="outline" className="ml-1 text-[10px]">live</Badge>}
                        </td>
                        <td className="py-1 pr-3 text-right">{x.train.trades}</td>
                        <td className={`py-1 pr-3 text-right ${tone(x.train.expectancy_r)}`}>{n(x.train.expectancy_r, 3)}</td>
                        <td className="py-1 pr-3 text-right">{n(x.train.profit_factor)}</td>
                        <td className="py-1 pr-3 text-right">{x.test.trades}</td>
                        <td className={`py-1 pr-3 text-right ${tone(x.test.expectancy_r)}`}>{n(x.test.expectancy_r, 3)}</td>
                        <td className="py-1 pr-3 text-right">{n(x.test.profit_factor)}</td>
                        <td className="py-1 pr-3 text-right">{n(x.test.max_drawdown_r, 1)}</td>
                        <td className="py-1 pr-3 text-right">{n(x.test.sharpe_daily_annual)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="space-y-2">
              <h3 className="font-medium">B · Strategy per index × regime (live exits)</h3>
              <p className="text-xs text-muted-foreground">
                Chosen on train:{" "}
                {r.experiment_b.policy.length
                  ? r.experiment_b.policy.map((p) => `${p.symbol} ${p.regime} → ${strat(p.strategy)}`).join(" · ")
                  : "no cell passed the minimums"}
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-muted-foreground">
                    <tr>
                      {["Index", "Regime", "Strategy", "Eligible", "Score", "Train n", "Train exp R", "Train PF", "Train cons.", "Test n", "Test exp R", "Test PF"].map((h, i) => (
                        <th key={h} className={`py-1 pr-3 font-normal ${i > 3 ? "text-right" : "text-left"}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {r.experiment_b.cells.map((c) => (
                      <tr key={`${c.symbol}-${c.regime}-${c.strategy}`} className="border-t">
                        <td className="py-1 pr-3">{c.symbol}</td>
                        <td className="py-1 pr-3">{c.regime}</td>
                        <td className="py-1 pr-3">{strat(c.strategy)}</td>
                        <td className="py-1 pr-3">
                          {c.eligible ? <Badge className="text-[10px]">eligible</Badge> : <span className="text-muted-foreground">{c.why}</span>}
                        </td>
                        <td className="py-1 pr-3 text-right">{n(c.score_train, 3)}</td>
                        <td className="py-1 pr-3 text-right">{c.train.trades}</td>
                        <td className={`py-1 pr-3 text-right ${tone(c.train.expectancy_r)}`}>{n(c.train.expectancy_r, 3)}</td>
                        <td className="py-1 pr-3 text-right">{n(c.train.profit_factor)}</td>
                        <td className="py-1 pr-3 text-right">
                          {c.train.consistency === null || c.train.consistency === undefined ? "—" : `${Math.round(c.train.consistency * 100)}%`}
                        </td>
                        <td className="py-1 pr-3 text-right">{c.test.trades}</td>
                        <td className={`py-1 pr-3 text-right ${tone(c.test.expectancy_r)}`}>{n(c.test.expectancy_r, 3)}</td>
                        <td className="py-1 pr-3 text-right">{n(c.test.profit_factor)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <MetricTable rows={[["B's choice on the test period", r.experiment_b.test]]} />
            </section>

            <section className="space-y-2">
              <h3 className="font-medium">C · Combined, on data the choice never saw</h3>
              <p className="text-xs text-muted-foreground">
                B&apos;s strategies with A&apos;s exit ({r.experiment_c.exit}). Walk-forward: each quarter is traded with the choice made
                on all data before it; the joined quarters are the out-of-sample result.
              </p>
              <MetricTable
                rows={[
                  ["Test period", r.experiment_c.test],
                  ["Walk-forward (out of sample)", r.experiment_c.walk_forward.all],
                  ...r.experiment_c.walk_forward.folds.map(
                    (f) => [`${f.quarter} (${f.cells_chosen} cells · ${f.exit})`, f.result] as [ReactNode, M],
                  ),
                ]}
              />
            </section>

            <ul className="list-disc pl-5 text-xs text-muted-foreground">
              {r.limitations.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
