"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";
import { WolfeChart, type WolfePattern } from "@/components/scans/WolfeChart";

type Factors = Record<string, number | null>;
type Pattern = {
  pivot: number;
  cup_weeks: number;
  cup_depth_pct: number;
  handle_days: number;
  handle_depth_pct: number;
};
type Candidate = {
  id: number;
  symbol: string;
  setup: string | null;
  direction: string | null;
  valid: number;
  reject_reason: string | null;
  score: number | null;
  factors: Factors | null;
  entry: number | null;
  stop: number | null;
  target: number | null;
  rr: number | null;
  regime: string | null;
  shortlisted: number;
  extra: {
    stop_atr?: number;
    vol_ratio?: number;
    rs_vs_nifty_pct?: number | null;
    turnover_cr?: number;
    tradingsymbol?: string;
    lots?: number;
    oi_change_pct?: number | null;
    pattern?: Pattern | WolfePattern | null;
    token?: number | null;
    or_high?: number | null;
    or_low?: number | null;
    stop_type?: string | null;
    penalties?: Record<string, number> | null;
  };
};
type ScanRun = {
  status: string;
  signal_date?: string;
  started_at?: string;
  detail: {
    reason?: string;
    regime?: { regime: string; reason?: string };
    entry_window?: string;
    signal_bar?: string;
    instruments?: number;
    time_stop?: string;
    stocks?: number;
    valid_setups?: number;
    above_60?: number;
  };
  candidates: Candidate[];
};

const SETUP_LABEL: Record<string, string> = {
  pullback: "Trend pullback",
  breakout: "Volume breakout",
  range_reversal: "Range reversal",
  mean_reversion: "Mean reversion (band)",
  cup_handle: "Cup and handle",
  orb: "Opening range breakout",
};
const FACTOR_LABEL: [string, string, number][] = [
  ["htf_trend", "Higher-timeframe trend", 25],
  ["setup_quality", "Setup quality", 25],
  ["volume_oi", "Volume", 15],
  ["rr_stop", "Reward:risk & stop", 15],
  ["regime_fit", "Regime fit", 10],
  ["relative_strength", "Strength vs NIFTY", 10],
  // cup and handle
  ["breakout_volume", "Breakout volume", 25],
  ["handle_tightness", "Handle tightness", 20],
  ["cup_depth", "Cup depth", 15],
  ["handle_dry_up", "Handle volume dry-up", 10],
  ["near_pivot", "Close near pivot", 10],
  // opening range breakout
  ["volume", "Breakout volume", 30],
  ["nifty_alignment", "NIFTY same direction", 25],
  ["range_quality", "Range quality", 15],
  ["close_strength", "Close strength", 15],
  // Wolfe Wave
  ["symmetry", "Wave symmetry", 25],
  ["reward_risk", "Reward:risk", 25],
  ["sweet_zone", "Point 5 in sweet zone", 15],
  ["convergence", "Lines converge", 15],
  ["rsi_divergence", "RSI divergence at 5", 20],
];

function isWolfe(p: Pattern | WolfePattern | null | undefined): p is WolfePattern {
  return !!p && "points" in p;
}

function pct(a: number, b: number) {
  return `${(((a - b) / b) * 100).toFixed(1)}%`;
}

function SetupCard({ c }: { c: Candidate }) {
  return (
    <Card className={c.shortlisted ? "border-green-600" : undefined}>
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          {c.symbol}
          <Badge variant="outline">{SETUP_LABEL[c.setup ?? ""] ?? c.setup}</Badge>
          {c.shortlisted ? <Badge className="bg-green-600 text-white">Shortlisted for AI</Badge> : null}
          <span className="ml-auto text-lg font-bold">{c.score?.toFixed(0)}</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {c.entry !== null && c.stop !== null && c.target !== null && (
          <div className="grid grid-cols-4 gap-2">
            <div><p className="text-xs text-muted-foreground">Entry (limit)</p><p className="font-medium">{c.entry}</p></div>
            <div><p className="text-xs text-muted-foreground">Stop</p><p className="font-medium text-red-600">{c.stop} <span className="text-xs">({pct(c.stop, c.entry)})</span></p></div>
            <div><p className="text-xs text-muted-foreground">Target</p><p className="font-medium text-green-600">{c.target} <span className="text-xs">({pct(c.target, c.entry)})</span></p></div>
            <div><p className="text-xs text-muted-foreground">Reward:risk</p><p className="font-medium">{c.rr}R</p></div>
          </div>
        )}
        {c.factors && (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
            {FACTOR_LABEL.map(([key, label, max]) =>
              c.factors && c.factors[key] !== null && c.factors[key] !== undefined ? (
                <div key={key} className="flex justify-between">
                  <span className="text-muted-foreground">{label}</span>
                  <span>{c.factors[key]}/{c.setup === "orb" && key === "relative_strength" ? 15 : max}</span>
                </div>
              ) : null,
            )}
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          {c.extra.tradingsymbol ? `${c.extra.tradingsymbol} · ${c.extra.lots} lot(s) · ` : ""}
          {c.direction === "short" ? "Short · " : ""}
          {c.extra.or_high !== undefined && c.extra.or_high !== null
            ? `Opening range ${c.extra.or_low}–${c.extra.or_high} · stop at ${c.extra.stop_type === "midpoint" ? "range midpoint" : "other side"} · `
            : ""}
          {isWolfe(c.extra.pattern)
            ? `Points ${c.extra.pattern.points.map((q) => q.price).join(" → ")} · span ${c.extra.pattern.span_bars} bars · symmetry ${c.extra.pattern.time_ratio} · point 5 ${c.extra.pattern.overshoot_atr} ATR below 1–3 line${c.extra.pattern.rsi_divergence ? " · RSI divergence" : ""}`
            : c.extra.pattern
            ? `Pivot ${c.extra.pattern.pivot} · cup ${c.extra.pattern.cup_weeks} wk, ${c.extra.pattern.cup_depth_pct}% deep · handle ${c.extra.pattern.handle_days} d, ${c.extra.pattern.handle_depth_pct}% deep · `
            : c.extra.stop_atr !== undefined && c.extra.stop_atr !== null ? `Stop ${c.extra.stop_atr} ATR · ` : ""}
          {c.extra.vol_ratio !== undefined && c.extra.vol_ratio !== null ? `volume ${c.extra.vol_ratio}×` : ""}
          {c.extra.penalties ? ` · penalties −${Object.values(c.extra.penalties).reduce((a, b) => a + b, 0)}` : ""}
          {c.extra.oi_change_pct !== undefined && c.extra.oi_change_pct !== null ? ` · OI ${c.extra.oi_change_pct}%` : ""}
          {c.extra.rs_vs_nifty_pct !== undefined && c.extra.rs_vs_nifty_pct !== null ? ` · vs NIFTY ${c.extra.rs_vs_nifty_pct}%` : ""}
          {c.extra.turnover_cr ? ` · turnover ₹${c.extra.turnover_cr} cr` : ""}
        </p>
        {isWolfe(c.extra.pattern) && c.extra.token ? (
          <WolfeChart token={c.extra.token} pattern={c.extra.pattern} entry={c.entry} stop={c.stop} target={c.target} />
        ) : null}
      </CardContent>
    </Card>
  );
}

type Props = {
  title: string;
  subtitle: string;
  load: () => Promise<unknown>;
  run: () => Promise<unknown>;
  emptyText: string;
};

export function ScanView({ title, subtitle, load: loadScan, run: runScan, emptyText }: Props) {
  const [run, setRun] = useState<ScanRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);

  const fetchData = useCallback(() => {
    loadScan()
      .then((data) => setRun(data as ScanRun))
      .catch((e) => toast.error(errorMessage(e, "Could not load the scan")))
      .finally(() => setLoading(false));
  }, [loadScan]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const load = () => {
    setLoading(true);
    fetchData();
  };

  const runNow = async () => {
    setRunning(true);
    try {
      const result = (await runScan()) as { status: string; reason?: string };
      if (result.status === "ok") toast.success("Scan finished");
      else toast.warning(`Scan ${result.status}${result.reason ? `: ${result.reason}` : ""}`);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Scan failed"));
    } finally {
      setRunning(false);
    }
  };

  const valid = run?.candidates.filter((c) => c.valid) ?? [];
  const rejected = run?.candidates.filter((c) => !c.valid) ?? [];
  const reasons = rejected.reduce<Record<string, number>>((acc, c) => {
    for (const r of (c.reject_reason ?? "unknown").split("; ")) {
      const key = r.replace(/\d[\d.,]*/g, "#");
      acc[key] = (acc[key] ?? 0) + 1;
    }
    return acc;
  }, {});

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-2xl font-bold">{title}</h1>
          <p className="text-sm text-muted-foreground">{subtitle}</p>
        </div>
        <div className="ml-auto flex gap-2">
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className="mr-1 h-4 w-4" /> Refresh
          </Button>
          <Button size="sm" onClick={runNow} disabled={running}>
            {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
            Run scan now
          </Button>
        </div>
      </div>

      {loading ? (
        <Loader2 className="h-6 w-6 animate-spin" />
      ) : !run || run.status === "none" ? (
        <p className="text-sm text-muted-foreground">{emptyText}</p>
      ) : (
        <>
          <Card>
            <CardContent className="grid gap-2 pt-6 text-sm sm:grid-cols-4">
              <div><p className="text-xs text-muted-foreground">Signal</p><p className="font-medium">{run.detail.signal_bar ? `${run.detail.signal_bar.replace("T", " ")} bar` : run.signal_date}</p></div>
              <div><p className="text-xs text-muted-foreground">Status</p><p className="font-medium">{run.status}</p></div>
              <div><p className="text-xs text-muted-foreground">Market regime (NIFTY)</p><p className="font-medium">{run.detail.regime?.regime ?? "—"}</p></div>
              <div><p className="text-xs text-muted-foreground">Entry day · time stop</p><p className="font-medium">{run.detail.entry_window ?? "—"} · {run.detail.time_stop ?? "—"}</p></div>
              {run.detail.reason && <p className="text-amber-600 sm:col-span-4">{run.detail.reason}</p>}
              {(run.detail.stocks ?? run.detail.instruments) !== undefined && (
                <p className="text-muted-foreground sm:col-span-4">
                  {run.detail.stocks ?? run.detail.instruments} checked · {run.detail.valid_setups} valid setups · {run.detail.above_60} scored 60+
                </p>
              )}
            </CardContent>
          </Card>

          <div className="space-y-3">
            <h2 className="font-semibold">Valid setups ({valid.length})</h2>
            {valid.length === 0 ? (
              <p className="text-sm text-muted-foreground">None today. No trade is a valid outcome.</p>
            ) : (
              valid.map((c) => <SetupCard key={c.id} c={c} />)
            )}
          </div>

          {rejected.length > 0 && (
            <Card>
              <CardHeader className="pb-2"><CardTitle className="text-base">Why the rest were rejected</CardTitle></CardHeader>
              <CardContent className="space-y-1 text-xs">
                {Object.entries(reasons)
                  .sort((a, b) => b[1] - a[1])
                  .slice(0, 12)
                  .map(([reason, n]) => (
                    <div key={reason} className="flex justify-between gap-4">
                      <span className="text-muted-foreground">{reason}</span>
                      <span>{n}</span>
                    </div>
                  ))}
              </CardContent>
            </Card>
          )}
          <p className="text-xs text-muted-foreground">
            Shortlisted setups go to the AI panel once it is connected (Phase 4). Nothing here is an order.
          </p>
        </>
      )}
    </div>
  );
}
