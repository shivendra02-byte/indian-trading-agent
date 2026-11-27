"use client";

import { useEffect, useState } from "react";
import { Download, Loader2, Play } from "lucide-react";
import { toast } from "sonner";
import { downloadResearchHistory, getResearchHistory, getTrendDiagnostic } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";
import { ShadowNote, num, tone } from "@/components/research/shared";
import { LabExtras } from "@/components/research/LabExtras";

type Series = { symbol: string; bars: number; first: string | null; last: string | null; continuous: boolean };
type History = { series: Series[]; progress: { status: string; done?: number; total?: number | null; current?: string | null; errors?: string[]; message?: string } };
type SharpeBySpan = Record<string, { gross: number | null; net: number | null }>;
type DiagRow = {
  symbol: string;
  bars: number;
  first?: string;
  last?: string;
  drift_sharpe?: number | null;
  acf_bands?: Record<string, number>;
  sepp_sharpe?: SharpeBySpan;
  verdict?: string;
  note?: string;
};
type Diagnostic = {
  spans: number[];
  bands: string[];
  instruments: DiagRow[];
  fno_stocks: { stocks: number; median_sepp_sharpe: SharpeBySpan; share_trending: number; best: DiagRow[] } | null;
  notes: string[];
};

function DiagTable({ rows, spans, bands }: { rows: DiagRow[]; spans: number[]; bands: string[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 text-left font-normal">Instrument</th>
            <th className="pr-3 text-right font-normal">Bars</th>
            <th className="pr-3 text-left font-normal">Verdict</th>
            {spans.map((s) => <th key={s} className="pr-3 text-right font-normal">Sharpe {s}d (net)</th>)}
            {bands.map((b) => <th key={b} className="pr-3 text-right font-normal">ACF lags {b}</th>)}
            <th className="pr-3 text-right font-normal">Buy-hold Sharpe</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.symbol} className="border-t">
              <td className="py-1 pr-3 font-medium">{r.symbol}</td>
              <td className="pr-3 text-right">{r.bars}</td>
              <td className="pr-3">
                {r.verdict ? (
                  <Badge variant={r.verdict === "trends" ? "default" : "outline"}>{r.verdict}</Badge>
                ) : <span className="text-muted-foreground">{r.note}</span>}
              </td>
              {spans.map((s) => {
                const v = r.sepp_sharpe?.[String(s)];
                return (
                  <td key={s} className={`pr-3 text-right ${tone(v?.net)}`} title={`gross ${num(v?.gross)}`}>
                    {num(v?.net)}
                  </td>
                );
              })}
              {bands.map((b) => (
                <td key={b} className={`pr-3 text-right ${tone(r.acf_bands?.[b])}`}>{num(r.acf_bands?.[b], 3)}</td>
              ))}
              <td className="pr-3 text-right">{num(r.drift_sharpe)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ResearchLabPage() {
  const [history, setHistory] = useState<History | null>(null);
  const [poll, setPoll] = useState(0);
  const [diag, setDiag] = useState<Diagnostic | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    getResearchHistory()
      .then((res) => {
        if (alive) setHistory(res as History);
      })
      .catch((e) => toast.error(errorMessage(e, "Could not load history status")));
    return () => {
      alive = false;
    };
  }, [poll]);

  const downloading = history?.progress.status === "running";
  useEffect(() => {
    if (!downloading) return;
    const t = setTimeout(() => setPoll((p) => p + 1), 3000);
    return () => clearTimeout(t);
  }, [downloading, poll]);

  const download = async () => {
    try {
      const res = (await downloadResearchHistory()) as { status: string };
      toast.success(res.status === "busy" ? "Already downloading" : "History download started");
      setPoll((p) => p + 1);
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the download"));
    }
  };

  const runDiag = async () => {
    setDiagLoading(true);
    try {
      setDiag((await getTrendDiagnostic(true)) as Diagnostic);
    } catch (e) {
      toast.error(errorMessage(e, "Diagnostic failed"));
    } finally {
      setDiagLoading(false);
    }
  };

  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Research Lab</h1>
        <p className="text-sm text-muted-foreground">Studies that are not trade books. Nothing here changes a live rule.</p>
        <ShadowNote />
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center justify-between text-sm">
            Long daily history
            <Button size="sm" variant="outline" onClick={download} disabled={downloading}>
              {downloading ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Download className="mr-1 h-4 w-4" />}
              Download research history
            </Button>
          </CardTitle>
          <CardDescription className="text-xs">
            The trend books need 250+ days of warm-up and years of history. This downloads up to 10 years of daily bars
            from Kite (MCX continuous series, NIFTY/BANKNIFTY index). Stock pairs and cash momentum use the Rule
            Backtest page&apos;s history download.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-xs">
          {downloading && (
            <p>Downloading {history?.progress.current} ({history?.progress.done}/{history?.progress.total ?? "?"})…</p>
          )}
          {history?.progress.status === "error" && <p className="text-red-600">{history.progress.message}</p>}
          <div className="flex flex-wrap gap-2">
            {history?.series.map((s) => (
              <Badge key={s.symbol} variant="outline">
                {s.symbol}: {s.bars} bars {s.first ? `from ${s.first}` : ""}
              </Badge>
            ))}
          </div>
          {!!history?.progress.errors?.length && (
            <p className="text-amber-600">Errors: {history.progress.errors.join("; ")}</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center justify-between text-sm">
            Which markets trend?
            <Button size="sm" onClick={runDiag} disabled={diagLoading}>
              {diagLoading ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
              Run
            </Button>
          </CardTitle>
          <CardDescription className="text-xs">
            Sepp &amp; Lucic: a trend system earns from positive autocorrelation of volatility-scaled returns at 1–3 month
            lags. Sharpe of their continuous EWMA trend signal per span, after the research cost model.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {diag && (
            <>
              <DiagTable rows={diag.instruments} spans={diag.spans} bands={diag.bands} />
              {diag.fno_stocks && (
                <div className="space-y-1 text-xs">
                  <p>
                    F&amp;O stocks ({diag.fno_stocks.stocks} with 500+ bars): {diag.fno_stocks.share_trending}% trend;
                    median net Sharpe by span:{" "}
                    {diag.spans.map((s) => `${s}d ${num(diag.fno_stocks?.median_sepp_sharpe[String(s)]?.net)}`).join(" · ")}
                  </p>
                  <details>
                    <summary className="cursor-pointer text-muted-foreground">10 strongest trending stocks (120-day span)</summary>
                    <DiagTable rows={diag.fno_stocks.best} spans={diag.spans} bands={diag.bands} />
                  </details>
                </div>
              )}
              <ul className="list-disc pl-4 text-[11px] text-muted-foreground">
                {diag.notes.map((n) => <li key={n}>{n}</li>)}
              </ul>
            </>
          )}
        </CardContent>
      </Card>

      <LabExtras />
    </div>
  );
}
