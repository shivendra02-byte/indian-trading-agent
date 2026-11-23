"use client";

import { useCallback, useEffect, useState } from "react";
import { FlaskConical, Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getDivergence, getDivergenceStudy, runDivergenceScan, runDivergenceStudy } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";
import { OutcomeSummaryTable as SummaryTable, num, tone, type OutcomeSummary as Summary } from "@/components/scans/OutcomeSummaryTable";

type Signal = {
  id: number;
  symbol: string;
  segment: string;
  contract: string | null;
  kind: "bullish" | "bearish";
  pivot1_date: string;
  pivot1_price: number;
  pivot1_rsi: number;
  pivot2_date: string;
  pivot2_price: number;
  pivot2_rsi: number;
  signal_date: string;
  close: number;
  stop: number;
  trend_ok: number;
  status: "open" | "closed";
  exit_reason: string | null;
  result_r: number | null;
  ret_5: number | null;
  ret_10: number | null;
  ret_20: number | null;
  alert_status: string | null;
  created_at: string;
};

// Telegram outcome per signal (backend/signals/divergence.py run_scan sets alert_status).
function telegramLabel(status: string | null): { text: string; title: string; tone: string } {
  if (!status) return { text: "—", title: "Not alerted", tone: "text-muted-foreground" };
  if (status === "sent") return { text: "sent", title: "Sent to Telegram", tone: "text-emerald-600" };
  if (status.startsWith("not sent: bearish_alert"))
    return {
      text: "not sent",
      title: "Bearish divergences go to Telegram only as an exit warning for a symbol you hold long (bearish_alert: open_longs)",
      tone: "text-muted-foreground",
    };
  return { text: status.startsWith("failed") ? "failed" : "not sent", title: status, tone: "text-red-600" };
}
type Recent = { signals: Signal[]; live_record: Record<string, Summary> };
type Study = {
  created_at?: string;
  instruments?: number;
  signals?: number;
  first_date?: string | null;
  groups?: Record<string, Summary>;
  baseline_10d?: { bullish: number | null; bearish: number | null };
  split_date?: string;
  uptrend_sma?: number;
};
type ScanResult = { status: string; reason?: string; new?: number; instruments?: number; alert?: string };

export default function DivergencePage() {
  const [recent, setRecent] = useState<Recent | null>(null);
  const [study, setStudy] = useState<Study | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const fetchData = useCallback(() => {
    Promise.all([getDivergence(), getDivergenceStudy()])
      .then((res) => {
        const [r, s] = res as [Recent, Study];
        setRecent(r);
        setStudy(s);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load divergence alerts")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const load = () => {
    setLoading(true);
    fetchData();
  };

  const scan = async (group: "nse" | "mcx") => {
    setBusy(group);
    try {
      const r = (await runDivergenceScan(group)) as ScanResult;
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
      setStudy((await runDivergenceStudy()) as Study);
      toast.success("History study done");
    } catch (e) {
      toast.error(errorMessage(e, "Study failed"));
    } finally {
      setBusy(null);
    }
  };

  const groups = study?.groups ?? {};
  const sma = study?.uptrend_sma ?? 200;
  const pick = (keys: string[]) => keys.filter((k) => groups[k]).map((k) => [k, groups[k]] as [string, Summary]);
  const yearKeys = Object.keys(groups).filter((k) => /\| \d{4}$/.test(k));
  const segmentKeys = Object.keys(groups).filter((k) => /\| (stock|index|mcx)$/.test(k));

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">RSI Divergence</h1>
          <p className="text-sm text-muted-foreground">
            Daily reversal warnings on F&amp;O stocks, NIFTY/BANKNIFTY and MCX minis, sent to Telegram after the close
            whether or not anything is shortlisted. Information only: no AI review, no paper trade. Every alert is
            tracked so you can see whether it is worth acting on.
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
          <CardTitle className="text-base">How it is defined</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm text-muted-foreground">
          <p>
            <b>Bullish:</b> price makes a lower swing low while RSI(14) makes a higher low; the first RSI low is 40 or
            below; the two lows are 5–60 days apart; price is below its 50-day EMA (a downtrend to reverse).
            <b> Bearish</b> is the mirror image (RSI first high 60 or above, price above the 50 EMA). Bearish
            divergence lost money as a short in the history study, so it is sent only as an <b>exit warning</b> for
            symbols you hold long (shortlisted or approved trades, open ideas, positions); all are still tracked.
          </p>
          <p>
            A swing is confirmed 2 days after it forms, so the alert comes on that day. It is &quot;invalid&quot; beyond the
            second swing by 0.25 ATR. For the record, each alert is scored as a trade: −1R at the invalid level,
            +2R at twice that distance, otherwise the close after 20 days.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle className="text-base">History: does it work on your stored data?</CardTitle>
              <CardDescription>
                {study?.created_at
                  ? `Run ${study.created_at.replace("T", " ").slice(0, 16)} over ${study.instruments} instruments, ${study.signals} divergences since ${String(study.first_date ?? "").slice(0, 10)}.`
                  : "Not run yet."}{" "}
                Compare the 10-day move with the baseline: the average 10-day move from any day.
              </CardDescription>
            </div>
            <Button size="sm" onClick={runStudy} disabled={busy !== null}>
              {busy === "study" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <FlaskConical className="mr-1 h-4 w-4" />}
              Run history study
            </Button>
          </div>
        </CardHeader>
        {study?.groups && (
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="outline">Baseline 10-day move up: {num(study.baseline_10d?.bullish, 2, "%")}</Badge>
              <Badge variant="outline">Baseline 10-day move down: {num(study.baseline_10d?.bearish, 2, "%")}</Badge>
              <span className="text-muted-foreground">* fewer than 30 signals</span>
            </div>
            <SummaryTable
              rows={pick(["bullish", `bullish | above ${sma} SMA`, `bullish | below ${sma} SMA`, "bearish",
                `bearish | above ${sma} SMA`, `bearish | below ${sma} SMA`])}
            />
            {study.split_date && (
              <div className="space-y-1">
                <p className="text-sm font-medium">
                  Train (before {study.split_date}) vs test (from {study.split_date})
                </p>
                <p className="text-xs text-muted-foreground">
                  A variant is only worth switching to if it is better in both halves. &quot;Above {sma} SMA&quot; is the
                  pullback-in-an-uptrend version (config <code>bullish_context: pullback_in_uptrend</code>).
                </p>
                <SummaryTable
                  rows={pick(["train", "test"].flatMap((p) => [`bullish | ${p}`, `bullish | above ${sma} SMA | ${p}`,
                    `bullish | below ${sma} SMA | ${p}`, `bearish | ${p}`]))}
                />
              </div>
            )}
            <details>
              <summary className="cursor-pointer text-sm text-muted-foreground">Trend filter, instrument type and year</summary>
              <div className="mt-2 space-y-3">
                <SummaryTable rows={pick(["bullish | with trend filter", "bullish | against the filter", "bearish | with trend filter", "bearish | against the filter"])} />
                <SummaryTable rows={pick(segmentKeys)} />
                <SummaryTable rows={pick(yearKeys)} />
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
          {recent && <SummaryTable rows={Object.entries(recent.live_record)} />}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Signals found</CardTitle>
          <p className="text-xs text-muted-foreground">
            Every divergence is tracked here. Only bullish ones, and bearish ones on a symbol you hold long (as an exit
            warning), go to Telegram: see the Telegram column.
          </p>
        </CardHeader>
        <CardContent>
          {!recent || recent.signals.length === 0 ? (
            <p className="text-sm text-muted-foreground">No divergence alerts yet. They are checked after each close.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    {["Date", "Found", "Symbol", "Type", "Telegram", "Price swings", "RSI", "Close", "Invalid", "Trend", "Status", "Result", "10-day"].map((h) => (
                      <th key={h} className="px-2 py-1 text-left font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {recent.signals.map((s) => (
                    <tr key={s.id} className="border-t">
                      <td className="px-2 py-1">{s.signal_date}</td>
                      <td className="px-2 py-1 text-muted-foreground">{s.created_at.slice(5, 16)}</td>
                      <td className="px-2 py-1 font-medium">
                        {s.contract ?? s.symbol} <span className="text-muted-foreground">{s.segment}</span>
                      </td>
                      <td className={`px-2 py-1 ${s.kind === "bullish" ? "text-emerald-600" : "text-red-600"}`}>{s.kind}</td>
                      <td className={`px-2 py-1 ${telegramLabel(s.alert_status).tone}`} title={telegramLabel(s.alert_status).title}>
                        {telegramLabel(s.alert_status).text}
                      </td>
                      <td className="px-2 py-1">
                        {num(s.pivot1_price)} ({s.pivot1_date.slice(5)}) → {num(s.pivot2_price)} ({s.pivot2_date.slice(5)})
                      </td>
                      <td className="px-2 py-1">
                        {s.pivot1_rsi.toFixed(0)} → {s.pivot2_rsi.toFixed(0)}
                      </td>
                      <td className="px-2 py-1">{num(s.close)}</td>
                      <td className="px-2 py-1">{num(s.stop)}</td>
                      <td className="px-2 py-1">{s.trend_ok ? "yes" : "no"}</td>
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
