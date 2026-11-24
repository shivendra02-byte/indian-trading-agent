"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getNiftyOptions, runNiftyOptions } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";

// GET /api/scans/nifty-options (backend/routers/marketdata.py nifty_options_latest)
type Rec = {
  id: number;
  signal_ts: string;
  setup: string;
  direction: "long" | "short";
  entry: number;
  stop: number;
  target: number;
  track_status: string;
  exit_reason: string | null;
  pnl_r: number | null;
  extra: { tradingsymbol?: string; stop_now?: { level: number; state: string } } | null;
  premium: { entry_premium: number; exit_premium: number; move_pct: number } | null;
};
type Run = { status: string; detail?: { reason?: string; signal_bar?: string; rejected?: Record<string, string> } } | null;
type Response = {
  latest_run: Run;
  records: Rec[];
  today: string;
  shadow: boolean;
  settings: { enabled: boolean; first_signal: string; last_signal: string; exit_time: string; target_r: number };
};

const SETUP: Record<string, string> = {
  opt_vwap_ema: "VWAP + EMA",
  opt_orb: "Opening range",
  opt_supertrend: "Supertrend",
};

function stats(rows: Rec[]) {
  const closed = rows.filter((r) => r.pnl_r !== null);
  const total = closed.reduce((a, r) => a + (r.pnl_r ?? 0), 0);
  const wins = closed.filter((r) => (r.pnl_r ?? 0) > 0).length;
  return { n: closed.length, total, win: closed.length ? (100 * wins) / closed.length : null };
}

export default function NiftyOptionsPage() {
  const [data, setData] = useState<Response | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchData = useCallback(() => {
    getNiftyOptions()
      .then((res) => setData(res as Response))
      .catch((e) => toast.error(errorMessage(e, "Could not load NIFTY option signals")));
  }, []);

  useEffect(() => {
    fetchData();
    const t = setInterval(fetchData, 60_000);
    return () => clearInterval(t);
  }, [fetchData]);

  const runNow = async () => {
    setBusy(true);
    try {
      const res = (await runNiftyOptions()) as { status: string; reason?: string; signals?: string[] };
      toast.message(res.status === "ok" ? `Signals: ${res.signals?.join(", ") || "none"}` : res.reason ?? res.status);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Scan failed"));
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }
  const run = data.latest_run;
  const bySetup = Object.keys(SETUP).map((k) => [k, stats(data.records.filter((r) => r.setup === k))] as const);

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">NIFTY Option Buying</h1>
          <p className="text-sm text-muted-foreground">
            5-minute NIFTY signals → buy the ATM call (up) or put (down), nearest weekly expiry · three setups tracked side
            by side · stop beyond the last 3 bars, {data.settings.target_r}R target, breakeven then trail · signals{" "}
            {data.settings.first_signal}–{data.settings.last_signal}, out by {data.settings.exit_time}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={fetchData}>
            <RefreshCw className="mr-1 h-4 w-4" /> Refresh
          </Button>
          <Button variant="outline" size="sm" disabled={busy} onClick={runNow}>
            {busy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Run now
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            Status
            {data.shadow && <Badge variant="outline">Shadow: tracked, no AI or alerts</Badge>}
            {!data.settings.enabled && <Badge variant="destructive">Disabled</Badge>}
          </CardTitle>
          <CardDescription>
            Last scan: {run ? `${run.status}${run.detail?.signal_bar ? ` · bar ${run.detail.signal_bar}` : ""}${run.detail?.reason ? ` · ${run.detail.reason}` : ""}` : "none yet"}
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 text-left font-normal">Setup</th>
                <th className="pr-3 text-right font-normal">Closed</th>
                <th className="pr-3 text-right font-normal">Win</th>
                <th className="pr-3 text-right font-normal">Total R</th>
              </tr>
            </thead>
            <tbody>
              {bySetup.map(([k, s]) => (
                <tr key={k} className="border-t">
                  <td className="py-1 pr-3">{SETUP[k]}</td>
                  <td className="pr-3 text-right">{s.n}</td>
                  <td className="pr-3 text-right">{s.win === null ? "—" : `${s.win.toFixed(0)}%`}</td>
                  <td className={`pr-3 text-right ${s.total > 0 ? "text-green-600" : s.total < 0 ? "text-red-600" : ""}`}>
                    {s.total.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-muted-foreground">
            R is measured on the NIFTY index. The option&apos;s real premium move is shown per trade once its 5-minute bars are stored.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Signals</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {data.records.length === 0 ? (
            <p className="text-sm text-muted-foreground">No signals yet.</p>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-muted-foreground">
                <tr>
                  {["Time", "Setup", "Buy", "Option", "Entry / stop / target", "Status", "R", "Premium"].map((h) => (
                    <th key={h} className="py-1 pr-3 text-left font-normal">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.records.map((r) => (
                  <tr key={r.id} className="border-t">
                    <td className="py-1 pr-3">{r.signal_ts.slice(5, 16).replace("T", " ")}</td>
                    <td className="pr-3">{SETUP[r.setup] ?? r.setup}</td>
                    <td className="pr-3">
                      <Badge variant="outline" className={r.direction === "long" ? "border-green-300 text-green-700" : "border-red-300 text-red-600"}>
                        {r.direction === "long" ? "CALL" : "PUT"}
                      </Badge>
                    </td>
                    <td className="pr-3">{r.extra?.tradingsymbol ?? "—"}</td>
                    <td className="pr-3">
                      {r.entry} / {r.stop} / {r.target}
                    </td>
                    <td className="pr-3">
                      {r.track_status === "closed"
                        ? (r.exit_reason ?? "").replace(/_/g, " ")
                        : r.extra?.stop_now
                          ? `${r.track_status} · stop ${r.extra.stop_now.level}`
                          : r.track_status}
                    </td>
                    <td className={`pr-3 ${(r.pnl_r ?? 0) > 0 ? "text-green-600" : (r.pnl_r ?? 0) < 0 ? "text-red-600" : ""}`}>
                      {r.pnl_r === null ? "—" : r.pnl_r.toFixed(2)}
                    </td>
                    <td className="pr-3">
                      {r.premium ? `${r.premium.entry_premium} → ${r.premium.exit_premium} (${r.premium.move_pct > 0 ? "+" : ""}${r.premium.move_pct}%)` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
