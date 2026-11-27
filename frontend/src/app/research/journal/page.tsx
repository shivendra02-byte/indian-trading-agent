"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { getResearchJournal, getResearchOverview } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/utils";
import {
  ShadowNote, StatsTable, TradesTable, num, tone, type RStats, type ResearchSummary, type ResearchTrade,
} from "@/components/research/shared";

type Overview = {
  live_start: string;
  strategies: {
    key: string;
    label: string;
    enabled: boolean;
    page: string;
    kind: string;
    record: RStats;
    open: number;
    last_run: { run_at: string; status: string } | null;
  }[];
};

export default function ResearchJournalPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [trades, setTrades] = useState<ResearchTrade[]>([]);
  const [summary, setSummary] = useState<ResearchSummary | null>(null);
  const [strategy, setStrategy] = useState<string>("");
  const [status, setStatus] = useState<"" | "open" | "closed">("");

  useEffect(() => {
    let alive = true;
    getResearchOverview()
      .then((res) => {
        if (alive) setOverview(res as Overview);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load research strategies")));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    getResearchJournal(strategy || undefined, status || undefined)
      .then((res) => {
        if (!alive) return;
        const r = res as { trades: ResearchTrade[]; summary: ResearchSummary };
        setTrades(r.trades);
        setSummary(r.summary);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load the research journal")));
    return () => {
      alive = false;
    };
  }, [strategy, status]);

  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Research Journal</h1>
        <p className="text-sm text-muted-foreground">
          Every trade the research strategies would have taken since {overview?.live_start ?? "the research start"}.
          Kept apart from the live Trade Journal.
        </p>
        <ShadowNote />
      </div>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-sm">Strategies</CardTitle></CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 text-left font-normal">Strategy</th>
                <th className="pr-3 text-right font-normal">Closed</th>
                <th className="pr-3 text-right font-normal">Open</th>
                <th className="pr-3 text-right font-normal">Win</th>
                <th className="pr-3 text-right font-normal">Total R</th>
                <th className="pr-3 text-left font-normal">Last evening run</th>
              </tr>
            </thead>
            <tbody>
              {overview?.strategies.map((s) => (
                <tr key={s.key} className="border-t">
                  <td className="py-1 pr-3">
                    <Link href={`/research/${s.page}`} className="font-medium hover:underline">{s.label}</Link>
                    {!s.enabled && <Badge variant="secondary" className="ml-1">off</Badge>}
                  </td>
                  <td className="pr-3 text-right">{s.kind === "trades" ? s.record.n : "—"}</td>
                  <td className="pr-3 text-right">{s.kind === "trades" ? s.open : "—"}</td>
                  <td className="pr-3 text-right">{s.record.n ? `${num(s.record.win_rate, 1)}%` : "—"}</td>
                  <td className={`pr-3 text-right ${tone(s.record.total_r)}`}>{s.record.n ? num(s.record.total_r, 1) : "—"}</td>
                  <td className="pr-3">{s.last_run ? `${s.last_run.run_at} · ${s.last_run.status}` : "not run yet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
            Trades
            <select
              className="h-7 rounded border bg-background px-2 text-xs"
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
            >
              <option value="">All strategies</option>
              {overview?.strategies.filter((s) => s.kind === "trades").map((s) => (
                <option key={s.key} value={s.key}>{s.label}</option>
              ))}
            </select>
            {(["", "open", "closed"] as const).map((v) => (
              <Button key={v || "all"} size="sm" variant={status === v ? "default" : "outline"} className="h-7 px-2 text-xs"
                      onClick={() => setStatus(v)}>
                {v || "all"}
              </Button>
            ))}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {summary && <StatsTable rows={[["Closed trades", summary.all]]} />}
          <TradesTable trades={trades} showStrategy={!strategy} />
        </CardContent>
      </Card>
    </div>
  );
}
