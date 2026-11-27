"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { getSpreadTriggerBacktest, startSpreadTriggerBacktest } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";

// Shapes from backend/options/mtf_backtest.py (asked 27 Nov 2026)
type S = {
  trades: number;
  win_rate?: number;
  avg_r?: number;
  max_drawdown_r?: number;
  profit_factor?: number | null;
  total_inr?: number;
  avg_inr?: number;
};
type Variant = {
  name: string;
  signal_days: number;
  all: S;
  by_symbol: Record<string, S>;
  by_exit: Record<string, S>;
  by_period?: { train: S; test: S };
  skipped: Record<string, number>;
};
type Result = {
  split_date: string | null;
  coverage: Record<string, { h1_from: string | null; m15_from: string | null; m15_source: string | null }>;
  variants: Variant[];
  limitations: string[];
};
type Job = { status: string; progress: string | null; result: Result | null; error: string | null };

const n = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : x.toFixed(d));
const inr = (x: number | null | undefined) => (x === null || x === undefined ? "—" : `₹${Math.round(x).toLocaleString("en-IN")}`);

function Cells({ s }: { s?: S }) {
  if (!s || !s.trades) {
    return (
      <>
        <td className="px-2 py-1 text-right">0</td>
        <td className="px-2 py-1 text-right text-muted-foreground" colSpan={5}>—</td>
      </>
    );
  }
  const tone = (s.avg_r ?? 0) > 0 ? "text-green-600" : (s.avg_r ?? 0) < 0 ? "text-red-600" : "";
  return (
    <>
      <td className="px-2 py-1 text-right">{s.trades}</td>
      <td className="px-2 py-1 text-right">{n(s.win_rate, 1)}%</td>
      <td className={`px-2 py-1 text-right font-medium ${tone}`}>{n(s.avg_r, 3)}</td>
      <td className="px-2 py-1 text-right">{n(s.profit_factor)}</td>
      <td className="px-2 py-1 text-right">{n(s.max_drawdown_r)}</td>
      <td className="px-2 py-1 text-right">{inr(s.total_inr)}</td>
    </>
  );
}

const HEAD = ["Trades", "Win %", "Avg R", "PF", "Max DD R", "Total ₹"];

export function TriggerBacktest() {
  const [form, setForm] = useState({ start_date: "2025-12-01", end_date: "2026-11-26", split_date: "2026-06-01" });
  const [job, setJob] = useState<Job | null>(null);
  const [preset, setPreset] = useState<"pullback" | "research">("pullback");

  const fetchJob = useCallback(() => {
    getSpreadTriggerBacktest()
      .then((d) => setJob(d as Job))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    fetchJob();
  }, [fetchJob]);

  useEffect(() => {          // poll while a run is going
    if (job?.status !== "running") return;
    const t = setTimeout(fetchJob, 3000);
    return () => clearTimeout(t);
  }, [job, fetchJob]);

  const start = async () => {
    try {
      const r = (await startSpreadTriggerBacktest({ ...form, split_date: form.split_date || null, preset })) as { status: string };
      if (r.status === "busy") toast.error("A run is already going");
      fetchJob();
    } catch (e) {
      toast.error(errorMessage(e, "Could not start"));
    }
  };

  const res = job?.result;
  const running = job?.status === "running";
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Entry trigger test</CardTitle>
        <CardDescription>
          Replays the intraday trigger (daily regime → 4H → 1H pullback → 15m) on stored index bars. Compare the 1H pullback
          settings (touch within 0.25 or 0.75 ATR of the 1H EMA20, in the last 6 or 10 bars) or the research ideas: a
          regime change confirmed over 2 days with an ADX exit band, the 20/250-day trend as a direction filter, and no
          1H invalidation exit. Spreads are priced on the day&apos;s closing
          option chain, with costs.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-3 text-sm">
          {(["start_date", "end_date", "split_date"] as const).map((k) => (
            <label key={k} className="space-y-1">
              <span className="block text-xs text-muted-foreground">{k === "split_date" ? "Train/test split" : k.replace("_", " ")}</span>
              <Input type="date" value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })} className="w-40" />
            </label>
          ))}
          <label className="space-y-1">
            <span className="block text-xs text-muted-foreground">Compare</span>
            <select
              className="h-9 rounded border bg-background px-2 text-xs"
              value={preset}
              onChange={(e) => setPreset(e.target.value as "pullback" | "research")}
            >
              <option value="pullback">1H pullback settings (0.25/0.75 ATR × 6/10 bars)</option>
              <option value="research">Research: regime hysteresis, 20/250 trend filter, no invalidation exit</option>
            </select>
          </label>
          <Button size="sm" disabled={running} onClick={start}>
            {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />} Run test
          </Button>
        </div>
        {running && <p className="text-xs text-muted-foreground">Running… {job?.progress}</p>}
        {job?.status === "error" && <p className="text-xs text-red-600">{job.error}</p>}

        {res && (
          <div className="space-y-4 text-xs">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="text-muted-foreground">
                  <tr className="border-b">
                    <th className="px-2 py-1 text-left">Setting</th>
                    <th className="px-2 py-1 text-right font-normal">Signal days</th>
                    {HEAD.map((h) => (
                      <th key={h} className="px-2 py-1 text-right font-normal">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {res.variants.map((v) => (
                    <tr key={v.name} className="border-b">
                      <td className="px-2 py-1 font-medium">{v.name}</td>
                      <td className="px-2 py-1 text-right">{v.signal_days}</td>
                      <Cells s={v.all} />
                    </tr>
                  ))}
                  {res.split_date &&
                    res.variants.flatMap((v) =>
                      (["train", "test"] as const).map((p) => (
                        <tr key={`${v.name}-${p}`} className="border-b text-muted-foreground">
                          <td className="px-2 py-1">
                            {v.name} · {p === "train" ? `before ${res.split_date}` : `from ${res.split_date}`}
                          </td>
                          <td />
                          <Cells s={v.by_period?.[p]} />
                        </tr>
                      )),
                    )}
                </tbody>
              </table>
            </div>
            {res.variants.map((v) => (
              <details key={`d-${v.name}`}>
                <summary className="cursor-pointer text-muted-foreground">{v.name}: by index, by exit, skipped signals</summary>
                <table className="mt-1 w-full">
                  <tbody>
                    {Object.entries(v.by_symbol).map(([k, s]) => (
                      <tr key={`s-${k}`} className="border-b">
                        <td className="px-2 py-0.5">{k}</td>
                        <td />
                        <Cells s={s} />
                      </tr>
                    ))}
                    {Object.entries(v.by_exit).map(([k, s]) => (
                      <tr key={`e-${k}`} className="border-b">
                        <td className="px-2 py-0.5">exit: {k.replace("_", " ")}</td>
                        <td />
                        <Cells s={s} />
                      </tr>
                    ))}
                  </tbody>
                </table>
                {Object.keys(v.skipped).length > 0 && (
                  <p className="mt-1 text-muted-foreground">
                    Signals not traded:{" "}
                    {Object.entries(v.skipped)
                      .map(([k, c]) => `${k} (${c})`)
                      .join(" · ")}
                  </p>
                )}
              </details>
            ))}
            <p className="text-muted-foreground">
              Data:{" "}
              {Object.entries(res.coverage)
                .map(([k, c]) => `${k} 1H from ${c.h1_from ?? "—"}, 15m from ${c.m15_from ?? "—"}${c.m15_source ? ` (${c.m15_source})` : ""}`)
                .join(" · ")}
            </p>
            <ul className="list-disc space-y-0.5 pl-4 text-muted-foreground">
              {res.limitations.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
