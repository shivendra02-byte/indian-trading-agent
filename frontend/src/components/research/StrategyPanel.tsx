"use client";

import { useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getResearchStrategy, runResearchStrategy } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";
import { BacktestCard } from "@/components/research/BacktestCard";
import { StatsTable, TradesTable, type ResearchSummary, type ResearchTrade } from "@/components/research/shared";

type StrategyResponse = {
  key: string;
  label: string;
  enabled: boolean;
  kind: string;
  limitations: string[];
  status: { settings?: Record<string, unknown>; states?: Record<string, Record<string, unknown>> } & Record<string, unknown>;
  trades: ResearchTrade[];
  summary: ResearchSummary | null;
};

const cell = (v: unknown) =>
  v === null || v === undefined ? "—" : typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v);

export function StateTable({ states }: { states: Record<string, Record<string, unknown>> }) {
  const rows = Object.entries(states);
  if (!rows.length) return null;
  const cols = Array.from(new Set(rows.flatMap(([, s]) => Object.keys(s))));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 text-left font-normal">Instrument</th>
            {cols.map((c) => <th key={c} className="pr-3 text-left font-normal">{c.replaceAll("_", " ")}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, s]) => (
            <tr key={name} className="border-t">
              <td className="py-1 pr-3 font-medium">{name}</td>
              {cols.map((c) => (
                <td key={c} className={`pr-3 ${s[c] === "long" ? "text-green-600" : s[c] === "short" ? "text-red-600" : ""}`}>
                  {cell(s[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StrategyPanel({
  strategy,
  description,
  gridKeys,
  defaultStart,
  extra,
}: {
  strategy: string;
  description: string;
  gridKeys?: string[];
  defaultStart?: string;
  extra?: (data: StrategyResponse) => React.ReactNode;
}) {
  const [data, setData] = useState<StrategyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let alive = true;
    getResearchStrategy(strategy)
      .then((res) => {
        if (alive) setData(res as StrategyResponse);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load the strategy")))
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [strategy, reload]);

  const runNow = async () => {
    setRunning(true);
    try {
      const res = (await runResearchStrategy(strategy)) as { status: string; reason?: string; trades?: number; new?: number };
      if (res.status === "skipped") toast.message(`Skipped: ${res.reason}`);
      else toast.success(`Updated: ${res.trades ?? 0} trade(s), ${res.new ?? 0} new`);
      setReload((r) => r + 1);
    } catch (e) {
      toast.error(errorMessage(e, "Run failed"));
    } finally {
      setRunning(false);
    }
  };

  const open = data?.trades.filter((t) => t.status === "open") ?? [];
  const closed = data?.trades.filter((t) => t.status === "closed") ?? [];

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center justify-between gap-2 text-base">
            <span>
              {data?.label ?? strategy}{" "}
              <Badge variant="outline" className="ml-1">shadow</Badge>
              {data && !data.enabled && <Badge variant="secondary" className="ml-1">off</Badge>}
            </span>
            <Button size="sm" variant="outline" onClick={runNow} disabled={running}>
              {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-1 h-4 w-4" />}
              Run now
            </Button>
          </CardTitle>
          <CardDescription className="text-xs">{description}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {loading && <p className="text-xs text-muted-foreground">Loading…</p>}
          {data?.status?.states && (
            <div>
              <h4 className="mb-1 text-xs font-semibold">Signal now</h4>
              <StateTable states={data.status.states} />
            </div>
          )}
          {data && extra?.(data)}
          <div>
            <h4 className="mb-1 text-xs font-semibold">Record since the research start</h4>
            {data?.summary ? (
              <StatsTable rows={[["All", data.summary.all], ...Object.entries(data.summary.by_symbol ?? {})]} />
            ) : (
              <p className="text-xs text-muted-foreground">No trades yet.</p>
            )}
          </div>
          <div>
            <h4 className="mb-1 text-xs font-semibold">Open ({open.length})</h4>
            <TradesTable trades={open} />
          </div>
          <details>
            <summary className="cursor-pointer text-xs font-semibold">Closed ({closed.length})</summary>
            <div className="mt-2">
              <TradesTable trades={closed} />
            </div>
          </details>
        </CardContent>
      </Card>
      <BacktestCard strategy={strategy} gridKeys={gridKeys} defaultStart={defaultStart} />
    </div>
  );
}
