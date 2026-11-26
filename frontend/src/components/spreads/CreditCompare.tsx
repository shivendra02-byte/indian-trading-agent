"use client";

import { useState } from "react";
import { Loader2, Scale } from "lucide-react";
import { toast } from "sonner";
import { compareSpreadCredit } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/utils";

// Shapes from backend/options/spread_research.py compare_min_credit() (asked 26 Nov 2026)
type S = {
  trades: number;
  win_rate?: number;
  avg_r?: number;
  total_r?: number;
  max_drawdown_r?: number;
  profit_factor?: number | null;
  total_inr?: number;
  avg_inr?: number;
  avg_cost_points?: number;
};
type AddedTrade = {
  symbol: string;
  signal_date: string;
  strategy: string;
  credit: number;
  width: number;
  credit_pct: number | null;
  exit_date: string;
  exit_reason: string;
  pnl_r: number;
  pnl_inr: number;
};
type Row = {
  ratio: number;
  singles: S;
  all: S;
  singles_by_period: { train?: S; test?: S };
  added_vs?: number;
  added?: S;
  added_trades?: AddedTrade[];
};
type Result = { status: string; split_date: string | null; ratios: Row[]; notes: string[] };

const pct = (r: number) => `${Math.round(r * 1000) / 10}%`;
const n = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : x.toFixed(d));
const inr = (x: number | null | undefined) => (x === null || x === undefined ? "—" : `₹${Math.round(x).toLocaleString("en-IN")}`);

function StatCells({ s }: { s: S | undefined }) {
  if (!s || !s.trades) {
    return (
      <>
        <td className="px-2 py-1 text-right">0</td>
        <td className="px-2 py-1 text-right text-muted-foreground" colSpan={6}>—</td>
      </>
    );
  }
  const tone = (s.avg_r ?? 0) > 0 ? "text-green-600" : (s.avg_r ?? 0) < 0 ? "text-red-600" : "";
  return (
    <>
      <td className="px-2 py-1 text-right">{s.trades}</td>
      <td className="px-2 py-1 text-right">{n(s.win_rate, 1)}%</td>
      <td className={`px-2 py-1 text-right font-medium ${tone}`}>{n(s.avg_r, 3)}</td>
      <td className="px-2 py-1 text-right">{inr(s.avg_inr)}</td>
      <td className="px-2 py-1 text-right">{n(s.profit_factor)}</td>
      <td className="px-2 py-1 text-right">{n(s.max_drawdown_r)}</td>
      <td className="px-2 py-1 text-right">{inr(s.total_inr)}</td>
    </>
  );
}

const HEAD = ["Trades", "Win %", "Avg R", "Avg ₹", "PF", "Max DD R", "Total ₹"];

export function CreditCompare() {
  const [form, setForm] = useState({ start_date: "2024-06-01", end_date: "2026-11-25", split_date: "2025-12-01", ratios: "10, 12, 15" });
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Result | null>(null);

  const run = async () => {
    const ratios = form.ratios
      .split(",")
      .map((x) => parseFloat(x.trim()) / 100)
      .filter((x) => x > 0 && x < 1);
    if (ratios.length < 2) {
      toast.error("Enter at least two minimum credits, e.g. 10, 12, 15");
      return;
    }
    setBusy(true);
    try {
      const out = (await compareSpreadCredit({
        start_date: form.start_date,
        end_date: form.end_date,
        split_date: form.split_date || null,
        ratios,
      })) as Result;
      setRes(out);
    } catch (e) {
      toast.error(errorMessage(e, "Comparison failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Scale className="h-4 w-4" /> Minimum credit comparison (bull put / bear call)
        </CardTitle>
        <CardDescription>
          The live spread rules re-run on stored option chains at each minimum credit (% of the width), with costs. The live
          setting is 12%.
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
            <span className="block text-xs text-muted-foreground">Minimum credits (%)</span>
            <Input value={form.ratios} onChange={(e) => setForm({ ...form, ratios: e.target.value })} className="w-32" />
          </label>
          <Button size="sm" disabled={busy} onClick={run}>
            {busy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Scale className="mr-1 h-4 w-4" />} Compare
          </Button>
        </div>

        {res && (
          <div className="space-y-4 text-xs">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="text-muted-foreground">
                  <tr className="border-b">
                    <th className="px-2 py-1 text-left">Minimum credit · bull put / bear call</th>
                    {HEAD.map((h) => (
                      <th key={h} className="px-2 py-1 text-right font-normal">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {res.ratios.map((r) => (
                    <tr key={r.ratio} className="border-b">
                      <td className="px-2 py-1 font-medium">
                        {pct(r.ratio)}
                        {Math.abs(r.ratio - 0.12) < 1e-6 ? " (live)" : ""}
                      </td>
                      <StatCells s={r.singles} />
                    </tr>
                  ))}
                  {res.split_date &&
                    res.ratios.flatMap((r) =>
                      (["train", "test"] as const).map((p) => (
                        <tr key={`${r.ratio}-${p}`} className="border-b text-muted-foreground">
                          <td className="px-2 py-1">
                            {pct(r.ratio)} · {p === "train" ? `before ${res.split_date}` : `from ${res.split_date}`}
                          </td>
                          <StatCells s={r.singles_by_period[p]} />
                        </tr>
                      )),
                    )}
                </tbody>
              </table>
            </div>

            {res.ratios.some((r) => r.added) && (
              <div className="overflow-x-auto">
                <p className="mb-1 font-medium">Trades a lower minimum adds (the ones that decide it)</p>
                <table className="w-full">
                  <thead className="text-muted-foreground">
                    <tr className="border-b">
                      <th className="px-2 py-1 text-left">Added</th>
                      {HEAD.map((h) => (
                        <th key={h} className="px-2 py-1 text-right font-normal">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {res.ratios
                      .filter((r) => r.added)
                      .map((r) => (
                        <tr key={`added-${r.ratio}`} className="border-b">
                          <td className="px-2 py-1">
                            {pct(r.ratio)} over {pct(r.added_vs ?? 0)}
                          </td>
                          <StatCells s={r.added} />
                        </tr>
                      ))}
                  </tbody>
                </table>
                {res.ratios
                  .filter((r) => r.added_trades && r.added_trades.length)
                  .map((r) => (
                    <details key={`list-${r.ratio}`} className="mt-2">
                      <summary className="cursor-pointer text-muted-foreground">
                        {r.added_trades!.length} trades added at {pct(r.ratio)} (last 50)
                      </summary>
                      <table className="mt-1 w-full">
                        <tbody>
                          {r.added_trades!.map((t, i) => (
                            <tr key={`${t.symbol}-${t.signal_date}-${i}`} className="border-b last:border-0">
                              <td className="px-2 py-0.5">{t.signal_date}</td>
                              <td className="px-2 py-0.5">{t.symbol}</td>
                              <td className="px-2 py-0.5">{t.strategy.replace("_", " ")}</td>
                              <td className="px-2 py-0.5 text-right">
                                {n(t.credit, 1)} / {t.width} ({t.credit_pct ?? "—"}%)
                              </td>
                              <td className="px-2 py-0.5">{t.exit_reason.replace("_", " ")}</td>
                              <td className={`px-2 py-0.5 text-right ${t.pnl_r > 0 ? "text-green-600" : "text-red-600"}`}>
                                {n(t.pnl_r)}R · {inr(t.pnl_inr)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </details>
                  ))}
              </div>
            )}

            <ul className="list-disc space-y-0.5 pl-4 text-muted-foreground">
              {res.notes.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
