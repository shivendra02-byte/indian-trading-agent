"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { getWinModelSources, runWinModel } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/utils";
import { num, tone } from "@/components/research/shared";

type Sources = {
  journal_closed: number;
  backtests: { id: number; segment: string; rule_set: string; start_date: string; end_date: string }[];
};
type Third = { n: number; avg_r: number; win_rate: number };
type Result = {
  status: string;
  reason?: string;
  rows?: number;
  out_of_sample?: number;
  months_tested?: number;
  first_test_month?: string;
  base_win_rate?: number;
  auc?: number | null;
  brier?: number;
  brier_base_rate?: number;
  verdict?: string;
  by_model_third?: Record<string, Third>;
  by_score_third?: Record<string, Third>;
  calibration?: { band: string; n: number; predicted: number; actual: number }[];
  ai_panel?: Record<string, { n: number; avg_r: number; model_prob: number }>;
};

function ThirdsTable({ title, rows }: { title: string; rows?: Record<string, Third> }) {
  if (!rows) return null;
  return (
    <table className="w-full text-xs">
      <thead className="text-muted-foreground">
        <tr>
          <th className="py-1 pr-3 text-left font-normal">{title}</th>
          <th className="pr-3 text-right font-normal">Trades</th>
          <th className="pr-3 text-right font-normal">Win</th>
          <th className="pr-3 text-right font-normal">Avg R</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(rows).map(([k, v]) => (
          <tr key={k} className="border-t">
            <td className="py-1 pr-3">{k} third</td>
            <td className="pr-3 text-right">{v.n}</td>
            <td className="pr-3 text-right">{num(v.win_rate, 1)}%</td>
            <td className={`pr-3 text-right ${tone(v.avg_r)}`}>{num(v.avg_r, 3)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function LabExtras() {
  const [sources, setSources] = useState<Sources | null>(null);
  const [choice, setChoice] = useState("journal");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    getWinModelSources()
      .then((res) => {
        if (alive) setSources(res as Sources);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const run = async () => {
    setBusy(true);
    try {
      const isJournal = choice === "journal";
      setResult((await runWinModel({ source: isJournal ? "journal" : "backtest", run_id: isJournal ? null : Number(choice) })) as Result);
    } catch (e) {
      toast.error(errorMessage(e, "Model run failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
            Win-probability model
            <select className="h-8 rounded border bg-background px-2 text-xs" value={choice}
                    onChange={(e) => setChoice(e.target.value)}>
              <option value="journal">Trade journal ({sources?.journal_closed ?? 0} closed signals)</option>
              {sources?.backtests.map((b) => (
                <option key={b.id} value={String(b.id)}>
                  Backtest #{b.id} · {b.segment} · {b.rule_set} · {b.start_date} → {b.end_date}
                </option>
              ))}
            </select>
            <Button size="sm" onClick={run} disabled={busy}>
              {busy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
              Train and test
            </Button>
          </CardTitle>
          <CardDescription className="text-xs">
            &quot;Identifying Trades&quot; (VJTI): a gradient-boosted tree learns which rule signals tend to win, from facts known
            when they fired (setup, regime, score, R:R, volume, stop distance, time). Walk-forward by month, so every
            prediction is out of sample. AUC 0.5 = no skill. Nothing here changes a rule.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-xs">
          {result?.status === "too few" && <p className="text-muted-foreground">{result.reason}</p>}
          {result?.status === "ok" && (
            <>
              <p>
                <Badge variant={result.verdict?.startsWith("useful") ? "default" : "outline"}>{result.verdict}</Badge>{" "}
                AUC {num(result.auc, 3)} · Brier {num(result.brier, 4)} vs {num(result.brier_base_rate, 4)} for the base rate ·{" "}
                {result.out_of_sample} out-of-sample signals from {result.first_test_month} ({result.months_tested} months) ·
                base win rate {num(result.base_win_rate, 1)}%
              </p>
              <div className="grid gap-4 md:grid-cols-2">
                <ThirdsTable title="Model probability" rows={result.by_model_third} />
                <ThirdsTable title="Rule score" rows={result.by_score_third} />
              </div>
              {!!result.calibration?.length && (
                <p className="text-muted-foreground">
                  Calibration: {result.calibration.map((c) => `${c.band}: predicted ${c.predicted}% vs actual ${c.actual}% (${c.n})`).join(" · ")}
                </p>
              )}
              {result.ai_panel && Object.keys(result.ai_panel).length > 0 && (
                <p className="text-muted-foreground">
                  AI panel: {Object.entries(result.ai_panel).map(([k, v]) =>
                    `${k} ${v.n} trades, avg ${num(v.avg_r, 3)}R, model ${v.model_prob}%`).join(" · ")}
                </p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Regime hysteresis</CardTitle>
          <CardDescription className="text-xs">
            Berzal &amp; García switch with a margin so small wobbles don&apos;t flip the decision. The experiment rule set{" "}
            <b>regime_hysteresis</b> = live rules + a new regime label must hold 2 days, and a STRONG trend stays STRONG until
            ADX falls below 22 (enters at 25). Live rules are unchanged.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-1 text-xs">
          <p>
            Futures / cash: <Link href="/rule-backtests" className="underline">Rule Backtest</Link> → rule set
            &quot;regime_hysteresis&quot; vs &quot;current&quot; on the same dates.
          </p>
          <p>
            Option spreads: <Link href="/option-spreads" className="underline">Option Spreads</Link> → Entry trigger test →
            Compare &quot;Research&quot; (hysteresis, 20/250 trend filter, no invalidation exit).
          </p>
        </CardContent>
      </Card>
    </>
  );
}
