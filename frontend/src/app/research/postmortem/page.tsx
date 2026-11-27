"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { getResearchPostmortem, getResearchPostmortems, runResearchPostmortem } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/utils";
import { ShadowNote, StatsTable, type RStats } from "@/components/research/shared";

type ReportRow = { week_start: string; week_end: string; created_at: string; ai_status: string | null };
type Report = ReportRow & {
  ai_model: string | null;
  ai_review: string | null;
  summary: {
    closed_this_week: number;
    strategies: Record<string, {
      label: string;
      this_week: RStats;
      since_live_start: RStats;
      latest_backtest: { id: number; all: RStats; by_period?: { train: RStats; test: RStats } } | null;
    }>;
  } | null;
};

export default function ResearchPostmortemPage() {
  const [reports, setReports] = useState<ReportRow[]>([]);
  const [week, setWeek] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [running, setRunning] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let alive = true;
    getResearchPostmortems()
      .then((res) => {
        if (!alive) return;
        const list = (res as { reports: ReportRow[] }).reports;
        setReports(list);
        setWeek((w) => w ?? list[0]?.week_start ?? null);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load reports")));
    return () => {
      alive = false;
    };
  }, [reload]);

  useEffect(() => {
    if (!week) return;
    let alive = true;
    getResearchPostmortem(week)
      .then((res) => {
        if (alive) setReport(res as Report);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load the report")));
    return () => {
      alive = false;
    };
  }, [week, reload]);

  const runNow = async (withAi: boolean) => {
    setRunning(true);
    try {
      const res = (await runResearchPostmortem({ with_ai: withAi })) as { week_start: string; ai_status: string };
      toast.success(`Report for the week of ${res.week_start}: AI ${res.ai_status}`);
      setWeek(res.week_start);
      setReload((r) => r + 1);
    } catch (e) {
      toast.error(errorMessage(e, "Could not build the report"));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Research Post-mortem</h1>
        <p className="text-sm text-muted-foreground">
          One AI review a week of all research strategies (Saturday from 08:30, after the main post-mortem). It never
          changes a rule.
        </p>
        <ShadowNote />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => runNow(false)} disabled={running}>
          {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
          This week so far (no AI)
        </Button>
        <Button size="sm" onClick={() => runNow(true)} disabled={running}>This week with AI review</Button>
        {reports.map((r) => (
          <Button key={r.week_start} size="sm" variant={r.week_start === week ? "default" : "outline"}
                  className="h-7 px-2 text-xs" onClick={() => setWeek(r.week_start)}>
            {r.week_start}
          </Button>
        ))}
      </div>
      {!reports.length && <p className="text-sm text-muted-foreground">No research reports yet.</p>}
      {report?.summary && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">
              Week {report.week_start} → {report.week_end} · {report.summary.closed_this_week} closed
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <StatsTable rows={Object.values(report.summary.strategies).map((s) => [`${s.label} — this week`, s.this_week])} />
            <StatsTable rows={Object.values(report.summary.strategies).map((s) => [`${s.label} — since start`, s.since_live_start])} />
            <StatsTable rows={Object.values(report.summary.strategies).map((s) => [`${s.label} — backtest (test half)`,
              s.latest_backtest?.by_period?.test ?? s.latest_backtest?.all])} />
          </CardContent>
        </Card>
      )}
      {report && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">
              AI review <Badge variant="outline" className="ml-1">{report.ai_status ?? "—"}</Badge>
              {report.ai_model && <span className="ml-2 font-normal text-muted-foreground">{report.ai_model}</span>}
            </CardTitle>
          </CardHeader>
          <CardContent className="prose prose-sm max-w-none dark:prose-invert">
            {report.ai_review ? <ReactMarkdown>{report.ai_review}</ReactMarkdown> : (
              <p className="text-sm text-muted-foreground">No AI review for this week.</p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
