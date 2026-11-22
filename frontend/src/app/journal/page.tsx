"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getJournal, getLearningReport, markJournalAction, trackJournal } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";

type Stats = {
  n: number;
  sufficient: boolean;
  win_rate?: number;
  avg_r?: number;
  profit_factor?: number | null;
  avg_mfe_r?: number;
  avg_mae_r?: number;
  pnl_rupees?: number;
};
type Ranked = (Stats & { group: string }) | null;
type Report = {
  min_sample: number;
  records: number;
  closed: number;
  overall: Stats;
  q1_best_setup_trending: { by_setup: Record<string, Stats>; best: Ranked };
  q2_worst_setup_sideways: { by_setup: Record<string, Stats>; worst: Ranked };
  q3_volume_confirmation: { passed_1_5x: Stats; failed_1_5x: Stats; by_volume_ratio: Record<string, Stats> };
  q4_rsi_filter: { rsi_turn_passed: Stats; rsi_turn_failed: Stats };
  q5_agents: { note: string };
  q6_rejected_but_profitable: {
    by_decision: Record<string, Stats>;
    profitable_count: number;
    top_examples: { symbol: string; signal_date: string; setup: string; decision: string; reject_reason: string | null; pnl_r: number }[];
  };
  trailing_vs_fixed: { fixed: Stats; trailing: Stats };
};
type JournalRecord = {
  id: number;
  symbol: string;
  segment: string;
  signal_date: string;
  setup: string;
  direction: string;
  regime: string | null;
  score: number | null;
  decision: string;
  reject_reason: string | null;
  entry: number;
  stop: number;
  target: number;
  track_status: string;
  exit_reason: string | null;
  pnl_r: number | null;
  mfe_r: number | null;
  mae_r: number | null;
  pnl_rupees: number | null;
  user_action?: "taken" | "skipped" | null;
  user_fill_price?: number | null;
  user_exit_price?: number | null;
  user_note?: string | null;
};

// What you did with a shortlisted signal (execution is manual). Feeds the weekly post-mortem,
// which compares your fill and exit with the rules' version of the same trade.
function ActionCell({ r }: { r: JournalRecord }) {
  const [action, setAction] = useState<string>(r.user_action ?? "");
  const [fill, setFill] = useState<string>(r.user_fill_price ? String(r.user_fill_price) : "");
  const [exit, setExit] = useState<string>(r.user_exit_price ? String(r.user_exit_price) : "");
  const [note, setNote] = useState<string>(r.user_note ?? "");
  const save = async (next: { action?: string; fill?: string; exit?: string; note?: string } = {}) => {
    const a = next.action ?? action;
    const f = next.fill ?? fill;
    const x = next.exit ?? exit;
    const n = next.note ?? note;
    try {
      await markJournalAction(r.id, {
        action: a ? (a as "taken" | "skipped") : null,
        fill_price: a === "taken" && f ? Number(f) : null,
        exit_price: a === "taken" && x ? Number(x) : null,
        note: a && n ? n : null,
      });
    } catch (e) {
      toast.error(errorMessage(e, "Could not save"));
    }
  };
  if (!["shortlisted", "approved"].includes(r.decision)) return <span className="text-muted-foreground">—</span>;
  const input = "rounded border bg-background px-1 py-0.5";
  return (
    <div className="flex flex-wrap items-center gap-1">
      <select
        className={input}
        value={action}
        onChange={(e) => {
          setAction(e.target.value);
          void save({ action: e.target.value });
        }}
      >
        <option value="">—</option>
        <option value="taken">Taken</option>
        <option value="skipped">Skipped</option>
      </select>
      {action === "taken" && (
        <>
          <input type="number" step="0.05" placeholder="your fill" className={`w-20 ${input}`} value={fill}
            onChange={(e) => setFill(e.target.value)} onBlur={() => void save()} />
          <input type="number" step="0.05" placeholder="your exit" className={`w-20 ${input}`} value={exit}
            onChange={(e) => setExit(e.target.value)} onBlur={() => void save()} />
        </>
      )}
      {action && (
        <input type="text" maxLength={500} placeholder={action === "skipped" ? "why skipped?" : "note"}
          className={`w-32 ${input}`} value={note} onChange={(e) => setNote(e.target.value)} onBlur={() => void save()} />
      )}
    </div>
  );
}

function StatLine({ label, s }: { label: string; s: Stats }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      {s.n === 0 ? (
        <span className="text-muted-foreground">no closed trades yet</span>
      ) : (
        <span>
          n={s.n} · win {s.win_rate}% · avg {s.avg_r}R
          {!s.sufficient && <span className="ml-1 text-amber-600">(too few)</span>}
        </span>
      )}
    </div>
  );
}

function Question({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">{children}</CardContent>
    </Card>
  );
}

const DECISIONS = ["", "shortlisted", "approved", "rejected_panel", "not_selected", "below_score", "soft_rejected"];

export default function JournalPage() {
  const [report, setReport] = useState<Report | null>(null);
  const [records, setRecords] = useState<JournalRecord[]>([]);
  const [decision, setDecision] = useState("");
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback((dec: string) => {
    Promise.all([getLearningReport(), getJournal({ decision: dec || undefined, limit: 300 })])
      .then((res) => {
        const [rep, jr] = res as [Report, { records: JournalRecord[] }];
        setReport(rep);
        setRecords(jr.records);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load the journal")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData("");
  }, [fetchData]);

  const changeDecision = (value: string) => {
    setDecision(value);
    setLoading(true);
    fetchData(value);
  };

  const refresh = async () => {
    setLoading(true);
    try {
      await trackJournal();
    } catch (e) {
      toast.error(errorMessage(e, "Tracking failed"));
    }
    fetchData(decision);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-2xl font-bold">Trade Journal</h1>
          <p className="text-sm text-muted-foreground">
            Every signal — taken or rejected — followed to its outcome. The learning engine only recommends; it never changes rules.
          </p>
        </div>
        <Button className="ml-auto" variant="outline" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className="mr-1 h-4 w-4" /> Update outcomes
        </Button>
      </div>

      {loading && !report ? (
        <Loader2 className="h-6 w-6 animate-spin" />
      ) : report ? (
        <>
          <Card>
            <CardContent className="grid gap-3 pt-6 text-sm sm:grid-cols-4">
              <div><p className="text-xs text-muted-foreground">Records</p><p className="text-lg font-semibold">{report.records}</p></div>
              <div><p className="text-xs text-muted-foreground">Closed</p><p className="text-lg font-semibold">{report.closed}</p></div>
              <div><p className="text-xs text-muted-foreground">Win rate · avg R</p><p className="text-lg font-semibold">{report.overall.n ? `${report.overall.win_rate}% · ${report.overall.avg_r}R` : "—"}</p></div>
              <div><p className="text-xs text-muted-foreground">Fixed vs trailing (avg R)</p><p className="text-lg font-semibold">{report.trailing_vs_fixed.fixed.avg_r ?? "—"} vs {report.trailing_vs_fixed.trailing.avg_r ?? "—"}</p></div>
              <p className="text-xs text-muted-foreground sm:col-span-4">
                Conclusions need at least {report.min_sample} closed trades per group; smaller groups are marked &quot;too few&quot;.
              </p>
            </CardContent>
          </Card>

          <div className="grid gap-4 md:grid-cols-2">
            <Question title="1. Best setup in trending markets">
              {Object.entries(report.q1_best_setup_trending.by_setup).map(([k, v]) => <StatLine key={k} label={k} s={v} />)}
              {report.q1_best_setup_trending.best && <p className="text-xs font-medium">Best so far: {report.q1_best_setup_trending.best.group}</p>}
            </Question>
            <Question title="2. Weakest setup in sideways markets">
              {Object.entries(report.q2_worst_setup_sideways.by_setup).map(([k, v]) => <StatLine key={k} label={k} s={v} />)}
              {report.q2_worst_setup_sideways.worst && <p className="text-xs font-medium">Weakest so far: {report.q2_worst_setup_sideways.worst.group}</p>}
            </Question>
            <Question title="3. Does 1.5× volume help breakouts?">
              <StatLine label="Volume ≥ 1.5×" s={report.q3_volume_confirmation.passed_1_5x} />
              <StatLine label="Volume < 1.5× (shadow)" s={report.q3_volume_confirmation.failed_1_5x} />
            </Question>
            <Question title="4. Does the RSI filter help pullbacks?">
              <StatLine label="RSI turn passed" s={report.q4_rsi_filter.rsi_turn_passed} />
              <StatLine label="RSI turn failed (shadow)" s={report.q4_rsi_filter.rsi_turn_failed} />
            </Question>
            <Question title="5. Which AI agent is most useful?">
              <p className="text-xs text-muted-foreground">{report.q5_agents.note}</p>
            </Question>
            <Question title="6. Rejected trades that would have made money">
              {Object.entries(report.q6_rejected_but_profitable.by_decision).map(([k, v]) => <StatLine key={k} label={k} s={v} />)}
              <p className="text-xs">{report.q6_rejected_but_profitable.profitable_count} rejected ideas closed in profit.</p>
            </Question>
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <h2 className="font-semibold">Records</h2>
              <select
                className="ml-auto rounded-md border bg-background px-2 py-1 text-xs"
                value={decision}
                onChange={(e) => changeDecision(e.target.value)}
              >
                {DECISIONS.map((d) => <option key={d} value={d}>{d || "all decisions"}</option>)}
              </select>
            </div>
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full text-xs">
                <thead className="bg-muted/50 text-left">
                  <tr>
                    {["Date", "Symbol", "Setup", "Regime", "Score", "Decision", "Entry / Stop / Target", "Status", "Exit", "R", "MFE / MAE", "₹", "You"].map((h) => (
                      <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {records.map((r) => (
                    <tr key={r.id} className="border-t">
                      <td className="px-2 py-1">{r.signal_date}</td>
                      <td className="px-2 py-1 font-medium">{r.symbol}</td>
                      <td className="px-2 py-1">{r.setup} {r.direction === "short" ? "↓" : "↑"}</td>
                      <td className="px-2 py-1">{r.regime}</td>
                      <td className="px-2 py-1">{r.score?.toFixed(0)}</td>
                      <td className="px-2 py-1" title={r.reject_reason ?? ""}><Badge variant="outline">{r.decision}</Badge></td>
                      <td className="px-2 py-1">{r.entry} / {r.stop} / {r.target}</td>
                      <td className="px-2 py-1">{r.track_status}</td>
                      <td className="px-2 py-1">{r.exit_reason ?? "—"}</td>
                      <td className={`px-2 py-1 ${r.pnl_r !== null && r.pnl_r < 0 ? "text-red-600" : "text-green-600"}`}>{r.pnl_r ?? "—"}</td>
                      <td className="px-2 py-1">{r.mfe_r ?? "—"} / {r.mae_r ?? "—"}</td>
                      <td className="px-2 py-1">{r.pnl_rupees?.toLocaleString("en-IN") ?? "—"}</td>
                      <td className="px-2 py-1"><ActionCell r={r} /></td>
                    </tr>
                  ))}
                  {records.length === 0 && (
                    <tr><td colSpan={13} className="px-2 py-4 text-center text-muted-foreground">No records yet — the first scan runs after Monday&apos;s close.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-muted-foreground">
              ₹ figures use notional sizing (₹10,000 risk, max ₹2.5L per stock) and delivery charges, for every record — shadow trades included.
            </p>
          </div>
        </>
      ) : null}
    </div>
  );
}
