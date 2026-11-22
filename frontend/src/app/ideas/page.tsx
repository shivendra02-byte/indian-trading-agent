"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { addIdea, deleteIdea, listIdeas, refreshIdeas, type IdeaInput } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";
import { OutcomeSummaryTable, num, tone, type OutcomeSummary } from "@/components/scans/OutcomeSummaryTable";

type Idea = {
  id: number;
  idea_date: string;
  symbol: string;
  direction: "long" | "short";
  entry_type: string;
  entry: number | null;
  invalid: number;
  target: number | null;
  source: string;
  pattern: string;
  note: string | null;
  status: string;
  fill_price: number | null;
  exit_reason: string | null;
  result_r: number | null;
  mfe_r: number | null;
};
type IdeaRecord = {
  all: OutcomeSummary;
  by_source: { [k: string]: OutcomeSummary };
  by_pattern: { [k: string]: OutcomeSummary };
  by_direction: { [k: string]: OutcomeSummary };
  counts: { [k: string]: number };
};
type IdeasResponse = { ideas: Idea[]; record: IdeaRecord; patterns: string[] };

const STATUS_TONE: { [k: string]: string } = {
  open: "text-blue-600",
  closed: "",
  waiting: "text-muted-foreground",
  not_filled: "text-muted-foreground",
  invalid: "text-amber-600",
  no_data: "text-amber-600",
};

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

const EMPTY = {
  symbol: "",
  direction: "long" as "long" | "short",
  entry_type: "limit" as "market" | "limit" | "breakout",
  entry: "",
  invalid: "",
  target: "",
  idea_date: "",
  horizon_days: "40",
  source: "",
  pattern: "trendline_bounce",
  note: "",
};

export default function IdeasPage() {
  const [data, setData] = useState<IdeasResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ ...EMPTY, idea_date: today() });

  const fetchData = useCallback(() => {
    listIdeas()
      .then((res) => setData(res as IdeasResponse))
      .catch((e) => toast.error(errorMessage(e, "Could not load ideas")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const set = (key: keyof typeof EMPTY, value: string) => setForm((f) => ({ ...f, [key]: value }));
  const n = (v: string) => (v.trim() === "" ? null : Number(v));

  const submit = async () => {
    setSaving(true);
    try {
      const payload: IdeaInput = {
        symbol: form.symbol,
        direction: form.direction,
        entry_type: form.entry_type,
        entry: form.entry_type === "market" ? null : n(form.entry),
        invalid: Number(form.invalid),
        target: n(form.target),
        idea_date: form.idea_date || undefined,
        horizon_days: Number(form.horizon_days) || undefined,
        source: form.source,
        pattern: form.pattern,
        note: form.note,
      };
      const idea = (await addIdea(payload)) as Idea;
      toast.success(`${idea.symbol} added (${idea.status.replace("_", " ")})`);
      setForm({ ...EMPTY, idea_date: today(), source: form.source, pattern: form.pattern });
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not add the idea"));
    } finally {
      setSaving(false);
    }
  };

  const refresh = async () => {
    setLoading(true);
    try {
      await refreshIdeas();
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Refresh failed"));
      setLoading(false);
    }
  };

  const remove = async (id: number) => {
    try {
      await deleteIdea(id);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not delete"));
    }
  };

  const rec = data?.record;
  const input = "rounded border bg-background px-2 py-1";

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">Ideas</h1>
          <p className="text-sm text-muted-foreground">
            Log trade ideas you see on X, in chats or on your own charts. Each is followed on daily prices like a shadow
            trade, so after 30–50 ideas you can see which sources and patterns actually work. Ideas are also summarised in
            the weekly post-mortem.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={refresh} disabled={loading}>
          <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} /> Update prices
        </Button>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Add an idea</CardTitle>
          <CardDescription>
            &quot;At or better&quot; waits for the price to come to your entry; &quot;on a break&quot; enters when price
            goes through it; &quot;next open&quot; takes the next day&apos;s open. It must fill within 5 trading days.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Symbol</span>
              <input className={`${input} w-32 uppercase`} placeholder="UNOMINDA" value={form.symbol} onChange={(e) => set("symbol", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Direction</span>
              <select className={input} value={form.direction} onChange={(e) => set("direction", e.target.value)}>
                <option value="long">Long</option>
                <option value="short">Short</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Entry</span>
              <select className={input} value={form.entry_type} onChange={(e) => set("entry_type", e.target.value)}>
                <option value="limit">At or better</option>
                <option value="breakout">On a break</option>
                <option value="market">Next open</option>
              </select>
            </label>
            {form.entry_type !== "market" && (
              <label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Entry price</span>
                <input className={`${input} w-24`} inputMode="decimal" value={form.entry} onChange={(e) => set("entry", e.target.value)} />
              </label>
            )}
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Invalid (stop)</span>
              <input className={`${input} w-24`} inputMode="decimal" value={form.invalid} onChange={(e) => set("invalid", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Target (optional)</span>
              <input className={`${input} w-24`} inputMode="decimal" value={form.target} onChange={(e) => set("target", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Idea date</span>
              <input type="date" className={input} value={form.idea_date} onChange={(e) => set("idea_date", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Hold up to (days)</span>
              <input className={`${input} w-16`} inputMode="numeric" value={form.horizon_days} onChange={(e) => set("horizon_days", e.target.value)} />
            </label>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Source</span>
              <input className={`${input} w-44`} placeholder="@Thekalal" value={form.source} onChange={(e) => set("source", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Pattern</span>
              <select className={input} value={form.pattern} onChange={(e) => set("pattern", e.target.value)}>
                {(data?.patterns ?? [form.pattern]).map((p) => (
                  <option key={p} value={p}>{p.replace(/_/g, " ")}</option>
                ))}
              </select>
            </label>
            <label className="flex min-w-[12rem] flex-1 flex-col gap-1">
              <span className="text-xs text-muted-foreground">Note</span>
              <input className={input} value={form.note} onChange={(e) => set("note", e.target.value)} />
            </label>
            <Button size="sm" onClick={submit} disabled={saving || !form.symbol || !form.invalid}>
              {saving ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Plus className="mr-1 h-4 w-4" />} Add idea
            </Button>
          </div>
        </CardContent>
      </Card>

      {rec && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Record</CardTitle>
            <CardDescription>
              Closed ideas, in R (1R = entry to invalid level). * = fewer than 30: too few to judge.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2 text-xs">
              {Object.entries(rec.counts).map(([k, v]) => (
                <Badge key={k} variant="outline">
                  {k.replace("_", " ")}: {v}
                </Badge>
              ))}
            </div>
            <OutcomeSummaryTable rows={[["All closed ideas", rec.all]]} showMoves={false} />
            <OutcomeSummaryTable rows={Object.entries(rec.by_source)} showMoves={false} />
            <OutcomeSummaryTable rows={Object.entries(rec.by_pattern)} showMoves={false} />
            <OutcomeSummaryTable rows={Object.entries(rec.by_direction)} showMoves={false} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Ideas</CardTitle>
        </CardHeader>
        <CardContent>
          {!data || data.ideas.length === 0 ? (
            <p className="text-sm text-muted-foreground">No ideas yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    {["Date", "Symbol", "Dir", "Entry", "Invalid", "Target", "Source", "Pattern", "Status", "Filled", "Result", "Best", ""].map((h) => (
                      <th key={h} className="px-2 py-1 text-left font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.ideas.map((i) => (
                    <tr key={i.id} className="border-t" title={i.note ?? undefined}>
                      <td className="px-2 py-1">{i.idea_date}</td>
                      <td className="px-2 py-1 font-medium">{i.symbol}</td>
                      <td className={`px-2 py-1 ${i.direction === "long" ? "text-emerald-600" : "text-red-600"}`}>{i.direction}</td>
                      <td className="px-2 py-1">{i.entry_type === "market" ? "next open" : `${i.entry_type === "breakout" ? "break " : ""}${num(i.entry)}`}</td>
                      <td className="px-2 py-1">{num(i.invalid)}</td>
                      <td className="px-2 py-1">{num(i.target)}</td>
                      <td className="px-2 py-1">{i.source}</td>
                      <td className="px-2 py-1">{i.pattern.replace(/_/g, " ")}</td>
                      <td className={`px-2 py-1 ${STATUS_TONE[i.status] ?? ""}`}>
                        {i.status === "closed" ? i.exit_reason : i.status.replace("_", " ")}
                      </td>
                      <td className="px-2 py-1">{num(i.fill_price)}</td>
                      <td className={`px-2 py-1 ${tone(i.result_r)}`}>{num(i.result_r, 2, "R")}</td>
                      <td className="px-2 py-1">{num(i.mfe_r, 1, "R")}</td>
                      <td className="px-2 py-1">
                        <button className="text-red-600" onClick={() => remove(i.id)} aria-label={`Delete ${i.symbol} idea`}>
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
