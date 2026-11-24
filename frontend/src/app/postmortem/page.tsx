"use client";

import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { getPostmortem, listPostmortems, runPostmortem } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";
import { OutcomeSummaryTable, type OutcomeSummary } from "@/components/scans/OutcomeSummaryTable";

type Stats = {
  n: number;
  sufficient: boolean;
  win_rate?: number;
  avg_r?: number;
  profit_factor?: number | null;
  avg_mfe_r?: number;
  avg_mae_r?: number;
  pnl_rupees?: number;
  sharpe_annual?: number | null;
  sortino_annual?: number | null;
};
type WeekRow = { week_start: string; week_end: string; created_at: string; ai_status: string | null; headline: Stats | null };
type Expectation = {
  run_id: number;
  rule_set: string;
  same_rules_as_live: boolean;
  period: string;
  shortlisted: Stats | null;
  all_trackable: Stats | null;
};
type Summary = {
  week_start: string;
  week_end: string;
  min_sample: number;
  closed: number;
  books: Record<"paper" | "shadow" | "taken" | "all", Stats>;
  previous_4_weeks: { paper: Stats; all: Stats };
  by_setup: Record<string, Stats>;
  by_setup_all: Record<string, Stats>;
  by_regime: Record<string, Stats>;
  by_segment: Record<string, Stats>;
  by_exit: Record<string, Stats>;
  tags: Record<string, number>;
  paper_tags: Record<string, number>;
  tag_labels: Record<string, string>;
  user_marked: { taken: number; skipped: number; unmarked_paper: number };
  your_execution?: {
    taken_with_fill: number;
    taken_with_exit: number;
    avg_entry_diff_r: number | null;
    your_avg_r: number | null;
    rules_avg_r_same_trades: number | null;
    total_deviation_r: number | null;
    exit_better: number;
    exit_worse: number;
    same_as_rules: number;
    skip_notes: { symbol: string; note: string }[];
  };
  ideas?: {
    error?: string;
    closed_this_week?: { symbol: string; direction: string; source: string; pattern: string; exit_reason: string; result_r: number | null }[];
    record_by_source?: Record<string, OutcomeSummary>;
    all?: OutcomeSummary;
  };
  open_at_week_end: { symbol: string; segment: string; setup: string; direction: string; mfe_r: number | null }[];
  backtest_expectation: Record<string, Expectation>;
  adherence: { shortlisted_week: number; max_open_at_once: number; issues: string[] };
};
type Trade = {
  id: number;
  segment: string;
  symbol: string;
  setup: string;
  direction: string;
  regime: string | null;
  book: string;
  exit_ts: string | null;
  exit_reason: string | null;
  pnl_r: number | null;
  mfe_r: number | null;
  tags: string[];
  user_action: string | null;
  user_r?: number;
  deviation_r?: number;
  entry_diff_r?: number;
};
type Postmortem = {
  week_start: string;
  created_at: string;
  summary: Summary;
  trades: Trade[];
  ai_status: string | null;
  ai_model: string | null;
  ai_review: string | null;
};

const BOOK_LABEL: Record<string, string> = {
  paper: "Shortlisted (paper book)",
  shadow: "Shadow (tracked, not picked)",
  taken: "Marked as taken by you",
  all: "All closed records",
};

function StatsRows({ rows }: { rows: [string, Stats | null | undefined][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            {["Group", "Trades", "Win", "Avg R", "PF", "₹", "Sharpe", "Sortino"].map((h) => (
              <th key={h} className={`px-2 py-1 font-medium ${h === "Group" ? "text-left" : "text-right"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, s]) => (
            <tr key={label} className="border-t">
              <td className="px-2 py-1">{label}</td>
              {!s || s.n === 0 ? (
                <td colSpan={7} className="px-2 py-1 text-right text-muted-foreground">no closed trades</td>
              ) : (
                <>
                  <td className="px-2 py-1 text-right">
                    {s.n}
                    {!s.sufficient && <span className="text-amber-600" title="Too few trades to conclude anything"> *</span>}
                  </td>
                  <td className="px-2 py-1 text-right">{s.win_rate}%</td>
                  <td className={`px-2 py-1 text-right ${(s.avg_r ?? 0) < 0 ? "text-red-600" : "text-emerald-600"}`}>{s.avg_r}R</td>
                  <td className="px-2 py-1 text-right">{s.profit_factor ?? "—"}</td>
                  <td className="px-2 py-1 text-right">{s.pnl_rupees?.toLocaleString("en-IN") ?? "—"}</td>
                  <td className="px-2 py-1 text-right">{s.sharpe_annual ?? "—"}</td>
                  <td className="px-2 py-1 text-right">{s.sortino_annual ?? "—"}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Monday..Sunday of the current week, local date (asked 24 Nov 2026: check a week mid-way).
function currentWeek(): { start: string; end: string } {
  const d = new Date();
  const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
  const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
  const iso = (x: Date) =>
    `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`;
  return { start: iso(monday), end: iso(sunday) };
}

export default function PostmortemPage() {
  const [thisWeek] = useState(currentWeek);
  const [weeks, setWeeks] = useState<WeekRow[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [pm, setPm] = useState<Postmortem | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [withAi, setWithAi] = useState(true);

  const fetchWeek = useCallback((week: string) => {
    getPostmortem(week)
      .then((r) => setPm(r as Postmortem))
      .catch((e) => {
        setPm(null);
        const msg = errorMessage(e, "Could not load that week");
        if (msg.includes("no post-mortem")) toast.message("Not built yet. Press \"Run this week so far\" to see the trades closed so far.");
        else toast.error(msg);
      })
      .finally(() => setLoading(false));
  }, []);

  const fetchWeeks = useCallback(() => {
    listPostmortems()
      .then((r) => {
        const stored = (r as { weeks: WeekRow[] }).weeks;
        const list = stored.some((w) => w.week_start === thisWeek.start)
          ? stored
          : [{ week_start: thisWeek.start, week_end: thisWeek.end, created_at: "", ai_status: null, headline: null }, ...stored];
        setWeeks(list);
        // open the newest stored report; this week shows once it has been run
        const first = stored[0]?.week_start ?? list[0].week_start;
        setSelected(first);
        fetchWeek(first);
      })
      .catch((e) => {
        toast.error(errorMessage(e, "Could not load post-mortems"));
        setLoading(false);
      });
  }, [fetchWeek, thisWeek]);

  useEffect(() => {
    fetchWeeks();
  }, [fetchWeeks]);

  const pick = (week: string) => {
    setSelected(week);
    setLoading(true);
    fetchWeek(week);
  };

  const runNow = async () => {
    setBusy(true);
    try {
      const r = (await runPostmortem({ week_start: selected || undefined, with_ai: withAi })) as {
        week_start: string;
        closed: number;
        ai_status: string;
      };
      toast.success(`Week of ${r.week_start}: ${r.closed} closed trades · AI ${r.ai_status}`);
      setLoading(true);
      fetchWeeks();
    } catch (e) {
      toast.error(errorMessage(e, "Post-mortem failed"));
    } finally {
      setBusy(false);
    }
  };

  const s = pm?.summary;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Weekly post-mortem</h1>
        <p className="text-sm text-muted-foreground">
          Runs every Saturday at 08:00 on the week&apos;s closed trades. It explains and proposes; it never changes a rule.
          Groups marked * have fewer than 30 trades: read them as anecdotes, not evidence.
        </p>
      </div>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-6 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">Week</span>
            <select className="rounded border bg-background px-2 py-1" value={selected} onChange={(e) => pick(e.target.value)}>
              {weeks.length === 0 && <option value="">No post-mortems yet</option>}
              {weeks.map((w) => (
                <option key={w.week_start} value={w.week_start}>
                  {w.week_start} → {w.week_end}
                  {w.week_start === thisWeek.start ? (w.created_at ? " · this week so far" : " · this week (not run yet)") : ""}
                  {w.headline?.n ? ` · ${w.headline.n} paper trades, ${w.headline.avg_r}R avg` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 pb-1 text-xs">
            <input type="checkbox" checked={withAi} onChange={(e) => setWithAi(e.target.checked)} />
            Include the AI review (one Claude call)
          </label>
          <Button size="sm" onClick={runNow} disabled={busy}>
            {busy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
            {selected === thisWeek.start ? (pm ? "Update this week so far" : "Run this week so far") : "Re-run this week"}
          </Button>
        </CardContent>
      </Card>

      {loading ? (
        <div className="flex justify-center py-10"><Loader2 className="h-6 w-6 animate-spin" /></div>
      ) : !s || !pm ? (
        <p className="text-sm text-muted-foreground">No post-mortem yet. The first one runs on Saturday, or use the button above.</p>
      ) : (
        <>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">
                Week of {s.week_start} · {s.closed} closed records
                <span className="font-normal text-muted-foreground"> · generated {pm.created_at}</span>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <StatsRows rows={[
                ...(Object.keys(BOOK_LABEL) as (keyof typeof BOOK_LABEL)[]).map(
                  (k) => [BOOK_LABEL[k], s.books[k as keyof Summary["books"]]] as [string, Stats],
                ),
                ["Previous 4 weeks: shortlisted", s.previous_4_weeks.paper],
                ...Object.entries(s.backtest_expectation).map(([seg, e]) => [
                  `Backtest #${e.run_id} ${seg} (${e.rule_set}${e.same_rules_as_live ? "" : ", older rules"}): shortlisted`,
                  e.shortlisted,
                ] as [string, Stats | null]),
              ]} />
              <p className="text-xs text-muted-foreground">
                You marked {s.user_marked.taken} taken and {s.user_marked.skipped} skipped;{" "}
                {s.user_marked.unmarked_paper} shortlisted trades are unmarked (mark them on the Trade Journal page).
              </p>
            </CardContent>
          </Card>

          {s.your_execution && s.your_execution.taken_with_fill > 0 && (
            <Card>
              <CardHeader className="pb-2"><CardTitle className="text-sm">Your execution vs the rules</CardTitle></CardHeader>
              <CardContent className="space-y-1 text-xs">
                <p>
                  {s.your_execution.taken_with_fill} trades with your fill · entry vs plan{" "}
                  {s.your_execution.avg_entry_diff_r ?? "—"}R on average (+ = you paid more than planned).
                </p>
                {s.your_execution.taken_with_exit > 0 ? (
                  <p>
                    {s.your_execution.taken_with_exit} trades with your exit: your average {s.your_execution.your_avg_r}R vs the
                    rules&apos; {s.your_execution.rules_avg_r_same_trades}R on the same trades (total difference{" "}
                    <span className={(s.your_execution.total_deviation_r ?? 0) < 0 ? "text-red-600" : "text-emerald-600"}>
                      {s.your_execution.total_deviation_r}R
                    </span>
                    ). Your exit was better {s.your_execution.exit_better}×, worse {s.your_execution.exit_worse}×, about the same{" "}
                    {s.your_execution.same_as_rules}×.
                  </p>
                ) : (
                  <p className="text-muted-foreground">Add your exit price on the Trade Journal to compare exits.</p>
                )}
                {s.your_execution.skip_notes.length > 0 && (
                  <p className="text-muted-foreground">
                    Why you skipped: {s.your_execution.skip_notes.map((n) => `${n.symbol}: ${n.note}`).join(" · ")}
                  </p>
                )}
              </CardContent>
            </Card>
          )}

          {s.ideas && !s.ideas.error && (s.ideas.all?.n ?? 0) > 0 && (
            <Card>
              <CardHeader className="pb-2"><CardTitle className="text-sm">Ideas from other sources</CardTitle></CardHeader>
              <CardContent className="space-y-2 text-xs">
                {(s.ideas.closed_this_week ?? []).length > 0 && (
                  <p>
                    Closed this week:{" "}
                    {(s.ideas.closed_this_week ?? [])
                      .map((i) => `${i.symbol} ${i.direction} (${i.source}, ${i.exit_reason}) ${i.result_r ?? "—"}R`)
                      .join(" · ")}
                  </p>
                )}
                <OutcomeSummaryTable rows={Object.entries(s.ideas.record_by_source ?? {})} showMoves={false} />
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-sm">Why trades won or lost</CardTitle></CardHeader>
            <CardContent>
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1 text-left font-medium">Cause</th>
                    <th className="px-2 py-1 text-left font-medium">Rule</th>
                    <th className="px-2 py-1 text-right font-medium">Shortlisted</th>
                    <th className="px-2 py-1 text-right font-medium">All</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(s.tag_labels).map(([tag, label]) => (
                    <tr key={tag} className="border-t">
                      <td className="px-2 py-1 font-medium">{tag.replaceAll("_", " ")}</td>
                      <td className="px-2 py-1 text-muted-foreground">{label}</td>
                      <td className="px-2 py-1 text-right">{s.paper_tags[tag] ?? 0}</td>
                      <td className="px-2 py-1 text-right">{s.tags[tag] ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-sm">Breakdown</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              <p className="text-xs font-medium">By setup (shortlisted)</p>
              <StatsRows rows={Object.entries(s.by_setup)} />
              <p className="text-xs font-medium">By regime (all)</p>
              <StatsRows rows={Object.entries(s.by_regime)} />
              <p className="text-xs font-medium">By segment (all)</p>
              <StatsRows rows={Object.entries(s.by_segment)} />
              <p className="text-xs font-medium">By exit (all)</p>
              <StatsRows rows={Object.entries(s.by_exit)} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-sm">Rules followed?</CardTitle></CardHeader>
            <CardContent className="space-y-1 text-xs">
              <p>{s.adherence.shortlisted_week} shortlisted this week · at most {s.adherence.max_open_at_once} paper positions open at once.</p>
              {s.adherence.issues.length === 0 ? (
                <p className="text-emerald-600">No limit was broken.</p>
              ) : (
                <ul className="list-disc pl-5 text-red-600">
                  {s.adherence.issues.map((i) => <li key={i}>{i}</li>)}
                </ul>
              )}
              {s.open_at_week_end.length > 0 && (
                <p className="text-muted-foreground">
                  Still open: {s.open_at_week_end.map((o) => `${o.symbol} ${o.setup} ${o.direction}`).join(", ")}
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">
                AI review <Badge variant="outline" className="ml-1">{pm.ai_status ?? "—"}</Badge>
                {pm.ai_model && <span className="ml-2 font-normal text-muted-foreground">{pm.ai_model}</span>}
              </CardTitle>
            </CardHeader>
            <CardContent className="prose prose-sm max-w-none dark:prose-invert">
              {pm.ai_review ? <ReactMarkdown>{pm.ai_review}</ReactMarkdown> : (
                <p className="text-sm text-muted-foreground">No AI review for this week.</p>
              )}
              <p className="text-xs text-muted-foreground">
                Proposals are experiments to backtest, not changes. Nothing is applied without a backtest and your approval.
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-sm">Trades</CardTitle></CardHeader>
            <CardContent className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-muted/50 text-left">
                  <tr>
                    {["Exit", "Symbol", "Setup", "Book", "Regime", "Exit reason", "R", "MFE", "Causes", "You", "Your R"].map((h) => (
                      <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {pm.trades.map((t) => (
                    <tr key={t.id} className="border-t">
                      <td className="px-2 py-1">{t.exit_ts?.slice(0, 16).replace("T", " ")}</td>
                      <td className="px-2 py-1 font-medium">{t.symbol}</td>
                      <td className="px-2 py-1">{t.setup} {t.direction === "short" ? "↓" : "↑"}</td>
                      <td className="px-2 py-1">{t.book}</td>
                      <td className="px-2 py-1">{t.regime ?? "—"}</td>
                      <td className="px-2 py-1">{t.exit_reason}</td>
                      <td className={`px-2 py-1 ${(t.pnl_r ?? 0) < 0 ? "text-red-600" : "text-emerald-600"}`}>{t.pnl_r}</td>
                      <td className="px-2 py-1">{t.mfe_r}</td>
                      <td className="px-2 py-1">{t.tags.map((g) => g.replaceAll("_", " ")).join(", ")}</td>
                      <td className="px-2 py-1">{t.user_action ?? "—"}</td>
                      <td className="px-2 py-1">
                        {t.user_r !== undefined ? `${t.user_r} (${(t.deviation_r ?? 0) >= 0 ? "+" : ""}${t.deviation_r})` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
