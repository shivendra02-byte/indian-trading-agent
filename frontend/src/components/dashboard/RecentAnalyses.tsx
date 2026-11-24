"use client";

import { useEffect, useState } from "react";
import { getAnalysisHistory, getRecentAnalysisTasks } from "@/lib/api";
import type { AnalysisHistoryItem } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import Link from "next/link";

const signalColors: Record<string, string> = {
  BUY: "bg-green-500/20 text-green-400 border-green-500/30",
  "STRONG BUY": "bg-green-500/20 text-green-400 border-green-500/30",
  OVERWEIGHT: "bg-green-500/15 text-green-300 border-green-500/20",
  HOLD: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  SELL: "bg-red-500/20 text-red-400 border-red-500/30",
  SHORT: "bg-red-500/20 text-red-400 border-red-500/30",
  UNDERWEIGHT: "bg-red-500/15 text-red-300 border-red-500/20",
};

// GET /api/analysis/tasks/recent: runs since the backend started (running, error or completed).
type RecentTask = { task_id: string; ticker: string | null; trade_date: string | null; status: string | null; error: string | null };

export function RecentAnalyses() {
  const [analyses, setAnalyses] = useState<AnalysisHistoryItem[]>([]);
  const [unfinished, setUnfinished] = useState<RecentTask[]>([]);

  useEffect(() => {
    getAnalysisHistory(5).then((data) => setAnalyses(data as AnalysisHistoryItem[])).catch(() => {});
    // Runs still going or that failed never reach history; show them so a run isn't silently missing.
    getRecentAnalysisTasks()
      .then((data) => setUnfinished((data as RecentTask[]).filter((t) => t.status !== "completed").slice(0, 3)))
      .catch(() => {});
  }, []);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-lg">Recent Analyses</CardTitle>
      </CardHeader>
      <CardContent className="pt-0 space-y-3">
        {unfinished.map((t) => (
          <Link key={t.task_id} href={t.status === "running" ? "/analysis" : `/analysis/${t.task_id}`}>
            <div className="p-3 rounded-lg bg-muted/50 hover:bg-muted transition-colors">
              <div className="flex items-center justify-between">
                <div>
                  <span className="font-medium">{t.ticker ?? "—"}</span>
                  <span className="text-xs text-muted-foreground ml-2">{t.trade_date}</span>
                </div>
                <Badge variant="outline" className={t.status === "running" ? "" : "border-red-300 text-red-600"}>
                  {t.status === "running" ? "running…" : "failed"}
                </Badge>
              </div>
              {t.error && <p className="mt-1 text-xs text-red-600 line-clamp-2">{t.error}</p>}
            </div>
          </Link>
        ))}
        {analyses.length === 0 && unfinished.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-4">
            No analyses yet. <Link href="/analysis" className="text-primary hover:underline">Run your first analysis</Link>
          </p>
        ) : (
          analyses.map((a) => (
            <Link key={a.task_id} href={`/analysis/${a.task_id}`}>
              <div className="flex items-center justify-between p-3 rounded-lg bg-muted/50 hover:bg-muted transition-colors">
                <div>
                  <span className="font-medium">{a.ticker}</span>
                  <span className="text-xs text-muted-foreground ml-2">{a.trade_date}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant="outline" className={signalColors[a.signal] || ""}>
                    {a.signal}
                  </Badge>
                  {a.duration_seconds && (
                    <span className="text-xs text-muted-foreground">
                      {Math.round(a.duration_seconds)}s
                    </span>
                  )}
                </div>
              </div>
            </Link>
          ))
        )}
      </CardContent>
    </Card>
  );
}
