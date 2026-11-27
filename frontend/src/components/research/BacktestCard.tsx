"use client";

import { useEffect, useState } from "react";
import { FlaskConical, Loader2, Play, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { deleteResearchBacktest, getResearchBacktests, startResearchBacktest } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";
import { BearLine, StatsTable, num, tone, type RStats, type ResearchSummary } from "@/components/research/shared";

type GridRow = RStats & { params: Record<string, number | string> };
type Run = {
  id: number;
  strategy: string;
  created_at: string;
  finished_at: string | null;
  status: string;
  progress: string | null;
  params: { start_date: string; end_date: string; split_date: string | null; grid?: Record<string, unknown[]> };
  summary: ResearchSummary | null;
  grid: GridRow[] | null;
};

export function BacktestCard({
  strategy,
  gridKeys = [],
  defaultStart = "2018-01-01",
}: {
  strategy: string;
  gridKeys?: string[];
  defaultStart?: string;
}) {
  const [start, setStart] = useState(defaultStart);
  const [end, setEnd] = useState(() => new Date().toISOString().slice(0, 10));
  const [split, setSplit] = useState("2024-01-01");
  const [grid, setGrid] = useState<Record<string, string>>({});
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [poll, setPoll] = useState(0);

  useEffect(() => {
    let alive = true;
    getResearchBacktests(strategy)
      .then((res) => {
        if (!alive) return;
        const list = (res as { runs: Run[] }).runs;
        setRuns(list);
        setSelected((cur) => cur ?? list.find((r) => r.status === "done")?.id ?? null);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load backtests")));
    return () => {
      alive = false;
    };
  }, [strategy, poll]);

  const running = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = setTimeout(() => setPoll((p) => p + 1), 4000);
    return () => clearTimeout(t);
  }, [running, poll]);

  const run = async () => {
    const parsed: Record<string, number[]> = {};
    for (const [k, v] of Object.entries(grid)) {
      const vals = v.split(",").map((x) => Number(x.trim())).filter((x) => !Number.isNaN(x) && x !== 0);
      if (vals.length) parsed[k] = vals;
    }
    setBusy(true);
    try {
      const res = (await startResearchBacktest({
        strategy,
        start_date: start,
        end_date: end,
        split_date: split || null,
        grid: parsed,
      })) as { status: string; id?: number };
      if (res.status === "busy") toast.message("Another research backtest is running — try again when it finishes.");
      else {
        toast.success("Backtest started");
        setSelected(res.id ?? null);
      }
      setPoll((p) => p + 1);
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the backtest"));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    try {
      await deleteResearchBacktest(id);
      if (selected === id) setSelected(null);
      setPoll((p) => p + 1);
    } catch (e) {
      toast.error(errorMessage(e, "Could not delete"));
    }
  };

  const current = runs.find((r) => r.id === selected);
  const s = current?.summary;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          <FlaskConical className="h-4 w-4" /> Backtest on stored history
        </CardTitle>
        <CardDescription className="text-xs">
          Same rules as the evening run. Settings grid = a sensitivity check (do neighbouring settings also work?),
          not a way to pick the best one.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-end gap-2 text-xs">
          <label className="space-y-1">
            <span className="block text-muted-foreground">From</span>
            <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="h-8 w-36" />
          </label>
          <label className="space-y-1">
            <span className="block text-muted-foreground">To</span>
            <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="h-8 w-36" />
          </label>
          <label className="space-y-1">
            <span className="block text-muted-foreground">Train / test split</span>
            <Input type="date" value={split} onChange={(e) => setSplit(e.target.value)} className="h-8 w-36" />
          </label>
          {gridKeys.map((k) => (
            <label key={k} className="space-y-1">
              <span className="block text-muted-foreground">Grid {k}</span>
              <Input
                placeholder="e.g. 10, 20, 40"
                value={grid[k] ?? ""}
                onChange={(e) => setGrid({ ...grid, [k]: e.target.value })}
                className="h-8 w-28"
              />
            </label>
          ))}
          <Button size="sm" onClick={run} disabled={busy || running}>
            {busy || running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
            Run
          </Button>
        </div>

        {runs.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {runs.map((r) => (
              <span key={r.id} className="inline-flex items-center gap-1">
                <Button
                  size="sm"
                  variant={r.id === selected ? "default" : "outline"}
                  className="h-7 px-2 text-xs"
                  onClick={() => setSelected(r.id)}
                >
                  #{r.id} {r.params.start_date} → {r.params.end_date}
                  {r.status !== "done" && <Badge variant="outline" className="ml-1">{r.status}</Badge>}
                </Button>
                {r.status !== "running" && (
                  <button type="button" aria-label="Delete run" onClick={() => remove(r.id)}
                          className="text-muted-foreground hover:text-red-600">
                    <Trash2 className="h-3 w-3" />
                  </button>
                )}
              </span>
            ))}
          </div>
        )}

        {current?.status === "running" && (
          <p className="text-xs text-muted-foreground">Running… {current.progress ?? ""}</p>
        )}
        {current?.status === "error" && <p className="text-xs text-red-600">Failed: {current.progress}</p>}

        {s && (
          <div className="space-y-3">
            <StatsTable
              rows={[
                ["All", s.all],
                ["Train", s.by_period?.train],
                ["Test (unseen)", s.by_period?.test],
              ]}
            />
            <BearLine bear={s.bear} />
            {s.open > 0 && (
              <p className="text-xs text-muted-foreground">
                {s.open} trade(s) still open at the end, marked at the last close:{" "}
                <span className={tone(s.open_r)}>{num(s.open_r)}R</span> (not in the table).
              </p>
            )}
            <details>
              <summary className="cursor-pointer text-xs text-muted-foreground">By instrument, side, exit and year</summary>
              <div className="mt-2 space-y-3">
                <StatsTable rows={Object.entries(s.by_symbol ?? {})} />
                <StatsTable rows={Object.entries(s.by_direction ?? {})} />
                <StatsTable rows={Object.entries(s.by_exit ?? {})} />
                <StatsTable rows={Object.entries(s.by_year ?? {})} />
              </div>
            </details>
            {current?.grid && current.grid.length > 0 && (
              <details open>
                <summary className="cursor-pointer text-xs text-muted-foreground">Settings grid (sensitivity)</summary>
                <StatsTable
                  rows={current.grid.map((g) => [
                    Object.entries(g.params).map(([k, v]) => `${k}=${v}`).join(" · "),
                    g,
                  ] as [string, RStats])}
                />
              </details>
            )}
            {s.limitations && s.limitations.length > 0 && (
              <ul className="list-disc pl-4 text-[11px] text-muted-foreground">
                {s.limitations.map((l) => <li key={l}>{l}</li>)}
              </ul>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
