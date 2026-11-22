"use client";

import { useCallback, useEffect, useState } from "react";
import { FlaskConical, Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getChartPatternStudy, getChartPatterns, runChartPatternScan, startChartPatternStudy } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";
import { OutcomeSummaryTable, num, tone, type OutcomeSummary } from "@/components/scans/OutcomeSummaryTable";

type Detail = {
  line?: number;
  touches?: number;
  first_anchor?: string;
  channel?: boolean;
  channel_rr?: number | null;
  level?: number;
  flat_touches?: number;
  volume_ratio?: number | null;
  near_sma?: boolean;
};
type Signal = {
  id: number;
  symbol: string;
  segment: string;
  contract: string | null;
  pattern: string;
  direction: "long" | "short";
  signal_date: string;
  close: number;
  stop: number;
  target_r: number;
  detail: Detail;
  status: "open" | "closed";
  exit_reason: string | null;
  result_r: number | null;
  ret_10: number | null;
};
type Recent = { signals: Signal[]; live_record: Record<string, OutcomeSummary> };
type Study = {
  created_at?: string;
  instruments?: number;
  signals?: number;
  first_date?: string | null;
  groups?: Record<string, OutcomeSummary>;
  baseline_10d?: { long: number | null; short: number | null };
};
type Progress = { status: string; done?: number; total?: number; current?: string | null; message?: string };
type StudyResponse = { latest: Study; progress: Progress };
type ScanResult = { status: string; reason?: string; new?: number; instruments?: number; alert?: string };

const LABEL: Record<string, string> = {
  trendline_bounce: "Trendline bounce",
  trendline_break: "Trendline break",
  triangle_breakout: "Triangle breakout",
};

function describe(s: Signal): string {
  const d = s.detail;
  const parts: string[] = [];
  if (d.touches) parts.push(`${d.touches} touches since ${String(d.first_anchor ?? "").slice(5)}`);
  if (d.channel) parts.push(`channel, target ${s.target_r.toFixed(1)}R`);
  if (d.level) parts.push(`level ${num(d.level)}`);
  if (d.volume_ratio) parts.push(`volume ${d.volume_ratio}x`);
  if (d.near_sma) parts.push("near 200-day SMA");
  return parts.join("; ");
}

export default function ChartPatternsPage() {
  const [recent, setRecent] = useState<Recent | null>(null);
  const [study, setStudy] = useState<StudyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const fetchData = useCallback(() => {
    Promise.all([getChartPatterns(), getChartPatternStudy()])
      .then((res) => {
        const [r, s] = res as [Recent, StudyResponse];
        setRecent(r);
        setStudy(s);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load chart patterns")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const running = study?.progress.status === "running" || study?.progress.status === "starting";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(fetchData, 3000);
    return () => clearInterval(timer);
  }, [running, fetchData]);

  const load = () => {
    setLoading(true);
    fetchData();
  };

  const scan = async (group: "nse" | "mcx") => {
    setBusy(group);
    try {
      const r = (await runChartPatternScan(group)) as ScanResult;
      if (r.status !== "ok") toast.warning(r.reason ?? r.status);
      else toast.success(`${r.instruments} checked, ${r.new} new${r.new ? ` (Telegram: ${r.alert})` : ""}`);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Scan failed"));
    } finally {
      setBusy(null);
    }
  };

  const runStudy = async () => {
    setBusy("study");
    try {
      await startChartPatternStudy();
      toast.success("History study started");
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the study"));
    } finally {
      setBusy(null);
    }
  };

  const latest = study?.latest ?? {};
  const groups = latest.groups ?? {};
  const keys = Object.keys(groups);
  const main = keys.filter((k) => !k.includes("|")).map((k) => [k, groups[k]] as [string, OutcomeSummary]);
  const split = (re: RegExp) => keys.filter((k) => re.test(k)).map((k) => [k, groups[k]] as [string, OutcomeSummary]);
  const progress = study?.progress;

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">Chart Patterns</h1>
          <p className="text-sm text-muted-foreground">
            Trendline bounces (with channels), trendline breaks and triangle breakouts on daily bars: the patterns from
            the chart ideas you shared. Sent to Telegram after the close as information only, tracked, and tested on
            your stored history before any of them becomes a trade setup.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={load} disabled={loading}>
            <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} /> Refresh
          </Button>
          <Button size="sm" variant="outline" onClick={() => scan("nse")} disabled={busy !== null}>
            {busy === "nse" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Scan NSE now
          </Button>
          <Button size="sm" variant="outline" onClick={() => scan("mcx")} disabled={busy !== null}>
            {busy === "mcx" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Scan MCX now
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">How they are defined</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm text-muted-foreground">
          <p>
            <b>Trendline bounce:</b> a rising line through at least 3 confirmed swing lows (within 0.3 ATR), never closed
            through by more than 0.25 ATR; the day touches it and closes strongly back above. Invalid 0.25 ATR under the
            line. If the swing highs form a parallel line it is a <b>channel</b> and the target is the upper line.
            Shorts mirror this off a falling line.
          </p>
          <p>
            <b>Trendline break:</b> the close breaks a falling resistance line (3+ touches) on 1.5× average volume;
            invalid under the last swing low. <b>Triangle breakout:</b> flat swing highs (within 1.5%) with rising swing
            lows, closing above the flat side on volume. Both mirrored for shorts. Signals further than 3 ATR from the
            invalid level are skipped. Each alert is scored: −1R at the invalid level, the target (2R or the channel
            line), else the close after 30 days.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle className="text-base">History: does it work on your stored data?</CardTitle>
              <CardDescription>
                {latest.created_at
                  ? `Run ${latest.created_at.replace("T", " ").slice(0, 16)} over ${latest.instruments} instruments, ${latest.signals} signals since ${String(latest.first_date ?? "").slice(0, 10)}.`
                  : "Not run yet."}
              </CardDescription>
            </div>
            <Button size="sm" onClick={runStudy} disabled={busy !== null || running}>
              {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <FlaskConical className="mr-1 h-4 w-4" />}
              {running ? `Studying ${progress?.done ?? 0}/${progress?.total ?? 0}` : "Run history study"}
            </Button>
          </div>
        </CardHeader>
        {progress?.status === "error" && (
          <CardContent className="text-sm text-red-600">Study failed: {progress.message}</CardContent>
        )}
        {latest.groups && (
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="outline">Baseline 10-day move up: {num(latest.baseline_10d?.long, 2, "%")}</Badge>
              <Badge variant="outline">Baseline 10-day move down: {num(latest.baseline_10d?.short, 2, "%")}</Badge>
              <span className="text-muted-foreground">* fewer than 30 signals</span>
            </div>
            <OutcomeSummaryTable rows={main} />
            <details>
              <summary className="cursor-pointer text-sm text-muted-foreground">
                Channel vs line only, near the 200-day SMA, instrument type, year
              </summary>
              <div className="mt-2 space-y-3">
                <OutcomeSummaryTable rows={split(/\| (channel|line only)$/)} />
                <OutcomeSummaryTable rows={split(/SMA$/)} />
                <OutcomeSummaryTable rows={split(/\| (stock|index|mcx)$/)} />
                <OutcomeSummaryTable rows={split(/\| \d{4}$/)} />
              </div>
            </details>
          </CardContent>
        )}
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Live record (alerts sent so far)</CardTitle>
        </CardHeader>
        <CardContent>
          {recent && Object.keys(recent.live_record).length > 0 ? (
            <OutcomeSummaryTable rows={Object.entries(recent.live_record)} />
          ) : (
            <p className="text-sm text-muted-foreground">No closed alerts yet.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Alerts</CardTitle>
        </CardHeader>
        <CardContent>
          {!recent || recent.signals.length === 0 ? (
            <p className="text-sm text-muted-foreground">No pattern alerts yet. They are checked after each close.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    {["Date", "Symbol", "Pattern", "Close", "Invalid", "Target", "Details", "Status", "Result", "10-day"].map((h) => (
                      <th key={h} className="px-2 py-1 text-left font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {recent.signals.map((s) => (
                    <tr key={s.id} className="border-t">
                      <td className="px-2 py-1">{s.signal_date}</td>
                      <td className="px-2 py-1 font-medium">
                        {s.contract ?? s.symbol} <span className="text-muted-foreground">{s.segment}</span>
                      </td>
                      <td className={`px-2 py-1 ${s.direction === "long" ? "text-emerald-600" : "text-red-600"}`}>
                        {LABEL[s.pattern] ?? s.pattern} {s.direction}
                      </td>
                      <td className="px-2 py-1">{num(s.close)}</td>
                      <td className="px-2 py-1">{num(s.stop)}</td>
                      <td className="px-2 py-1">{s.target_r.toFixed(1)}R</td>
                      <td className="px-2 py-1 text-muted-foreground">{describe(s)}</td>
                      <td className="px-2 py-1">{s.status === "open" ? "open" : s.exit_reason}</td>
                      <td className={`px-2 py-1 ${tone(s.result_r)}`}>{num(s.result_r, 2, "R")}</td>
                      <td className={`px-2 py-1 ${tone(s.ret_10)}`}>{num(s.ret_10, 2, "%")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
