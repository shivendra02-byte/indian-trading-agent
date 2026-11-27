"use client";

import { useEffect, useState } from "react";
import { Loader2, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getRiskSwitch, runResearchStrategy, runRiskSwitchStudy } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";
import { ShadowNote, num, tone } from "@/components/research/shared";

type Row = { trade_date: string; score: number; state: string; nifty_close: number; components: Record<string, number> };
type Current = { latest: Row | null; rows: Row[]; settings: { off_below: number; on_above: number; drawdown_pct: number; horizon_days: number } };
type Group = { days: number; fall_rate_pct: number | null; avg_fwd_return_pct: number | null };
type Study = {
  by_state: Record<string, Group>;
  by_score: Record<string, Group>;
  days: number;
  base_fall_rate_pct: number | null;
  switches_per_year: number | null;
  horizon_days: number;
  drawdown_pct: number;
  first: string | null;
  last: string | null;
};

function GroupTable({ groups }: { groups: Record<string, Group> }) {
  return (
    <table className="w-full text-xs">
      <thead className="text-muted-foreground">
        <tr>
          <th className="py-1 pr-3 text-left font-normal">Group</th>
          <th className="pr-3 text-right font-normal">Days</th>
          <th className="pr-3 text-right font-normal">Followed by a fall</th>
          <th className="pr-3 text-right font-normal">Avg NIFTY move after</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(groups).map(([k, g]) => (
          <tr key={k} className="border-t">
            <td className="py-1 pr-3 font-medium">{k}</td>
            <td className="pr-3 text-right">{g.days}</td>
            <td className="pr-3 text-right">{num(g.fall_rate_pct, 1)}%</td>
            <td className={`pr-3 text-right ${tone(g.avg_fwd_return_pct)}`}>{num(g.avg_fwd_return_pct)}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ResearchRiskSwitchPage() {
  const [data, setData] = useState<Current | null>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [study, setStudy] = useState<Study | null>(null);
  const [studyBusy, setStudyBusy] = useState(false);
  const [start, setStart] = useState("");

  useEffect(() => {
    let alive = true;
    getRiskSwitch(120)
      .then((res) => {
        if (alive) setData(res as Current);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load the risk switch")));
    return () => {
      alive = false;
    };
  }, [reload]);

  const update = async () => {
    setBusy(true);
    try {
      await runResearchStrategy("risk_switch");
      setReload((r) => r + 1);
    } catch (e) {
      toast.error(errorMessage(e, "Update failed"));
    } finally {
      setBusy(false);
    }
  };

  const runStudy = async () => {
    setStudyBusy(true);
    try {
      setStudy((await runRiskSwitchStudy({ start_date: start || null })) as Study);
    } catch (e) {
      toast.error(errorMessage(e, "Study failed"));
    } finally {
      setStudyBusy(false);
    }
  };

  const latest = data?.latest;
  const recent = data?.rows.slice(-30).reverse() ?? [];

  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Risk Switch (research)</h1>
        <p className="text-sm text-muted-foreground">
          A daily NIFTY drawdown-risk score (rules version of Berzal &amp; García&apos;s risk indicator). It gates nothing — it is
          logged so the study can show whether it would have warned before falls.
        </p>
        <ShadowNote />
      </div>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center justify-between text-sm">
            <span>
              Today:{" "}
              {latest ? (
                <>
                  <Badge variant={latest.state === "DEFENSIVE" ? "destructive" : "default"}>{latest.state}</Badge>{" "}
                  <span className={tone(latest.score)}>score {num(latest.score)}</span>{" "}
                  <span className="font-normal text-muted-foreground">({latest.trade_date})</span>
                </>
              ) : "not computed yet"}
            </span>
            <Button size="sm" variant="outline" onClick={update} disabled={busy}>
              {busy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-1 h-4 w-4" />}
              Update
            </Button>
          </CardTitle>
          <CardDescription className="text-xs">
            Defensive below {data?.settings.off_below ?? 0}, normal again only above {data?.settings.on_above ?? 0.2}{" "}
            (hysteresis). Components scaled −1 … +1; hover a score for them.
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 text-left font-normal">Date</th>
                <th className="pr-3 text-right font-normal">NIFTY</th>
                <th className="pr-3 text-right font-normal">Score</th>
                <th className="pr-3 text-left font-normal">State</th>
                {latest && Object.keys(latest.components).map((k) => (
                  <th key={k} className="pr-3 text-right font-normal">{k.replace("_", " ")}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {recent.map((r) => (
                <tr key={r.trade_date} className="border-t">
                  <td className="py-1 pr-3">{r.trade_date}</td>
                  <td className="pr-3 text-right">{num(r.nifty_close, 0)}</td>
                  <td className={`pr-3 text-right ${tone(r.score)}`}>{num(r.score)}</td>
                  <td className={`pr-3 ${r.state === "DEFENSIVE" ? "text-red-600" : ""}`}>{r.state}</td>
                  {latest && Object.keys(latest.components).map((k) => (
                    <td key={k} className={`pr-3 text-right ${tone(r.components[k])}`}>{num(r.components[k])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
            Did it warn before falls?
            <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="h-8 w-36" />
            <Button size="sm" onClick={runStudy} disabled={studyBusy}>
              {studyBusy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
              Run study
            </Button>
          </CardTitle>
          <CardDescription className="text-xs">
            Share of days followed by a NIFTY fall of {data?.settings.drawdown_pct ?? 5}% or more within{" "}
            {data?.settings.horizon_days ?? 20} sessions, by state and by score. A useful switch has a clearly higher fall
            rate when DEFENSIVE than the base rate, without switching too often.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {study && (
            <>
              <p className="text-xs text-muted-foreground">
                {study.first} → {study.last} · {study.days} days · base rate {num(study.base_fall_rate_pct, 1)}% ·{" "}
                {num(study.switches_per_year, 1)} switches a year
              </p>
              <GroupTable groups={study.by_state} />
              <GroupTable groups={study.by_score} />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
