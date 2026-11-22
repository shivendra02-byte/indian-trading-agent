"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, Loader2, RefreshCw, Upload } from "lucide-react";
import { toast } from "sonner";
import {
  getOptionChain,
  getOptionDataStatus,
  runOptionUpdate,
  startOptionBackfill,
  uploadOptionFile,
} from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";

type ExchangeStats = { ok: number; missing: number; error: number; first: string | null; last: string | null; rows: number };
type FileRow = { trade_date: string; exchange: string; status: string; rows: number | null; source: string; detail: string | null };
type Progress = {
  status: string;
  job?: string;
  done?: number;
  total?: number;
  current?: string | null;
  seconds_left?: number | null;
  counts?: Record<string, number>;
  errors?: string[];
  message?: string;
};
type Status = {
  enabled: boolean;
  symbols: Record<string, string[]>;
  update_after: string;
  max_history_days: number;
  exchanges: Record<string, ExchangeStats>;
  recent_files: FileRow[];
  vix: { days: number; first: string | null; last: string | null; latest: { trade_date: string; close: number } | null };
  progress: Progress;
};
type Side = { close: number | null; oi: number | null; chg_oi: number | null; volume: number | null; iv: number | null; delta: number | null };
type ChainRow = { strike: number; CE?: Side; PE?: Side };
type Chain = {
  symbol: string;
  date: string | null;
  expiry?: string;
  expiries: string[];
  days_to_expiry?: number;
  forward?: number;
  forward_source?: string;
  spot?: number;
  lot_size?: number;
  vix?: number | null;
  atm_strike?: number | null;
  rows: ChainRow[];
};

const SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"];
const STATUS_TONE: Record<string, string> = {
  ok: "text-emerald-600",
  missing: "text-muted-foreground",
  error: "text-red-600",
};

function fmt(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : value.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

export default function OptionDataPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [start, setStart] = useState(() => isoDaysAgo(365));
  const [end, setEnd] = useState(() => isoDaysAgo(1));
  const [busy, setBusy] = useState(false);
  const [uploadExchange, setUploadExchange] = useState<"NSE" | "BSE">("NSE");
  const [symbol, setSymbol] = useState("NIFTY");
  const [chainDate, setChainDate] = useState("");
  const [expiry, setExpiry] = useState("");
  const [chain, setChain] = useState<Chain | null>(null);
  const [chainLoading, setChainLoading] = useState(true);

  const fetchStatus = useCallback(() => {
    getOptionDataStatus()
      .then((data) => setStatus(data as Status))
      .catch((e) => toast.error(errorMessage(e, "Could not load option data status")))
      .finally(() => setLoading(false));
  }, []);

  const fetchChain = useCallback((sym: string, day: string, exp: string) => {
    getOptionChain(sym, day || undefined, exp || undefined)
      .then((data) => setChain(data as Chain))
      .catch((e) => toast.error(errorMessage(e, "Could not load the option chain")))
      .finally(() => setChainLoading(false));
  }, []);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  useEffect(() => {
    fetchChain(symbol, chainDate, expiry);
  }, [fetchChain, symbol, chainDate, expiry]);

  const running = status?.progress?.status === "running";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(fetchStatus, 3000);
    return () => clearInterval(timer);
  }, [running, fetchStatus]);

  const refresh = () => {
    setLoading(true);
    fetchStatus();
  };

  const backfill = async () => {
    setBusy(true);
    try {
      await startOptionBackfill({ start_date: start, end_date: end });
      toast.success("Download started");
      fetchStatus();
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the download"));
    } finally {
      setBusy(false);
    }
  };

  const update = async () => {
    setBusy(true);
    try {
      await runOptionUpdate();
      toast.success("Update started (last few trading days)");
      setTimeout(fetchStatus, 1500);
    } catch (e) {
      toast.error(errorMessage(e, "Could not start the update"));
    } finally {
      setBusy(false);
    }
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    try {
      const result = (await uploadOptionFile(uploadExchange, file)) as { trade_date: string; rows: number; with_iv: number };
      toast.success(`${uploadExchange} ${result.trade_date}: ${result.rows} rows, ${result.with_iv} with IV`);
      fetchStatus();
    } catch (e) {
      toast.error(errorMessage(e, "Could not import the file"));
    } finally {
      setBusy(false);
    }
  };

  const changeSymbol = (value: string) => {
    setChainLoading(true);
    setExpiry("");
    setSymbol(value);
  };
  const changeDate = (value: string) => {
    setChainLoading(true);
    setExpiry("");
    setChainDate(value);
  };
  const changeExpiry = (value: string) => {
    setChainLoading(true);
    setExpiry(value);
  };

  const progress = status?.progress;
  const progressPct = progress?.total ? Math.round(((progress.done ?? 0) / progress.total) * 100) : 0;

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">Option Data</h1>
          <p className="text-sm text-muted-foreground">
            End-of-day NSE/BSE option files for NIFTY, BANKNIFTY and SENSEX, with India VIX, implied volatility and
            delta (Black-76). Data only: this feeds the option spread backtest; it creates no signals.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} /> Refresh
        </Button>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        {(["NSE", "BSE"] as const).map((ex) => {
          const s = status?.exchanges?.[ex];
          return (
            <Card key={ex}>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  {ex} <span className="text-sm font-normal text-muted-foreground">{status?.symbols?.[ex]?.join(", ")}</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-1 text-sm">
                {s ? (
                  <>
                    <div>{s.ok} days stored{s.first ? ` (${s.first} to ${s.last})` : ""}</div>
                    <div className="text-muted-foreground">
                      {fmt(s.rows, 0)} rows · {s.missing} missing · {s.error} errors
                    </div>
                  </>
                ) : (
                  <div className="text-muted-foreground">No files yet</div>
                )}
              </CardContent>
            </Card>
          );
        })}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">India VIX</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm">
            {status?.vix?.latest ? (
              <>
                <div>
                  {fmt(status.vix.latest.close)} on {status.vix.latest.trade_date}
                </div>
                <div className="text-muted-foreground">{status.vix.days} days stored</div>
              </>
            ) : (
              <div className="text-muted-foreground">No VIX history yet</div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Download</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p className="text-muted-foreground">
            The daily update runs on trading days from {status?.update_after ?? "19:00"} and also fills gaps in the last
            week. For history, download a range once (about 2 seconds per trading day; a year takes ~10 minutes).
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">From</span>
              <input type="date" className="rounded border bg-background px-2 py-1" value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">To</span>
              <input type="date" className="rounded border bg-background px-2 py-1" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
            <Button size="sm" onClick={backfill} disabled={busy || running}>
              <Download className="mr-1 h-4 w-4" /> Download history
            </Button>
            <Button size="sm" variant="outline" onClick={update} disabled={busy || running}>
              <RefreshCw className="mr-1 h-4 w-4" /> Update last week now
            </Button>
          </div>
          {progress && progress.status !== "idle" && (
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                {running && <Loader2 className="h-4 w-4 animate-spin" />}
                <span>
                  {progress.job ?? "download"}: {progress.status}
                  {progress.total ? ` · ${progress.done}/${progress.total} files` : ""}
                  {running && progress.current ? ` · ${progress.current}` : ""}
                  {running && progress.seconds_left ? ` · ~${Math.ceil(progress.seconds_left / 60)} min left` : ""}
                </span>
              </div>
              {progress.total ? (
                <div className="h-2 w-full overflow-hidden rounded bg-muted">
                  <div className="h-2 bg-primary" style={{ width: `${progressPct}%` }} />
                </div>
              ) : null}
              {progress.counts && (
                <div className="text-xs text-muted-foreground">
                  {Object.entries(progress.counts)
                    .filter(([, n]) => n > 0)
                    .map(([k, n]) => `${k} ${n}`)
                    .join(" · ")}
                </div>
              )}
              {progress.message && <div className="text-xs text-red-600">{progress.message}</div>}
              {progress.errors && progress.errors.length > 0 && (
                <details className="text-xs text-red-600">
                  <summary>{progress.errors.length} errors</summary>
                  <ul className="mt-1 space-y-0.5">
                    {progress.errors.map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 border-t pt-3">
            <span className="text-xs text-muted-foreground">
              If the exchange blocks the server, download the day&apos;s F&amp;O bhavcopy (UDiFF) yourself and import it:
            </span>
            <select
              className="rounded border bg-background px-2 py-1"
              value={uploadExchange}
              onChange={(e) => setUploadExchange(e.target.value as "NSE" | "BSE")}
            >
              <option value="NSE">NSE</option>
              <option value="BSE">BSE</option>
            </select>
            <label className="inline-flex cursor-pointer items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-muted">
              <Upload className="h-3 w-3" /> Import file
              <input
                type="file"
                accept=".csv,.zip,.CSV"
                className="hidden"
                disabled={busy}
                onChange={(e) => {
                  void upload(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
            </label>
          </div>
          {status?.recent_files && status.recent_files.length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer text-muted-foreground">Recent files</summary>
              <table className="mt-2 w-full">
                <tbody>
                  {status.recent_files.map((f) => (
                    <tr key={`${f.trade_date}-${f.exchange}`} className="border-t">
                      <td className="px-2 py-1">{f.trade_date}</td>
                      <td className="px-2 py-1">{f.exchange}</td>
                      <td className={`px-2 py-1 ${STATUS_TONE[f.status] ?? ""}`}>{f.status}</td>
                      <td className="px-2 py-1 text-right">{f.rows ?? "—"}</td>
                      <td className="px-2 py-1 text-muted-foreground">{f.source}</td>
                      <td className="px-2 py-1 text-muted-foreground">{f.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Stored option chain</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Index</span>
              <select className="rounded border bg-background px-2 py-1" value={symbol} onChange={(e) => changeSymbol(e.target.value)}>
                {SYMBOLS.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Date (blank = latest)</span>
              <input type="date" className="rounded border bg-background px-2 py-1" value={chainDate} onChange={(e) => changeDate(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Expiry</span>
              <select
                className="rounded border bg-background px-2 py-1"
                value={chain?.expiry ?? ""}
                onChange={(e) => changeExpiry(e.target.value)}
                disabled={!chain?.expiries?.length}
              >
                {(chain?.expiries ?? []).map((x) => (
                  <option key={x} value={x}>{x}</option>
                ))}
              </select>
            </label>
            {chainLoading && <Loader2 className="h-4 w-4 animate-spin" />}
          </div>
          {!chain || !chain.date || chain.rows.length === 0 ? (
            <p className="text-muted-foreground">No stored data for {symbol}{chainDate ? ` on ${chainDate}` : ""}. Download history above.</p>
          ) : (
            <>
              <div className="flex flex-wrap gap-2 text-xs">
                <Badge variant="outline">{chain.date}</Badge>
                <Badge variant="outline">{chain.days_to_expiry} days to expiry</Badge>
                <Badge variant="outline">Spot {fmt(chain.spot)}</Badge>
                <Badge variant="outline" title={`Forward from ${chain.forward_source}`}>
                  Forward {fmt(chain.forward)}
                </Badge>
                <Badge variant="outline">Lot {fmt(chain.lot_size, 0)}</Badge>
                <Badge variant="outline">VIX {fmt(chain.vix)}</Badge>
              </div>
              <div className="max-h-[32rem] overflow-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-background text-muted-foreground">
                    <tr>
                      {["CE OI", "CE vol", "CE close", "CE IV", "CE Δ", "Strike", "PE Δ", "PE IV", "PE close", "PE vol", "PE OI"].map((h) => (
                        <th key={h} className={`px-2 py-1 font-medium ${h === "Strike" ? "text-center" : "text-right"}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {chain.rows.map((r) => (
                      <tr key={r.strike} className={`border-t ${r.strike === chain.atm_strike ? "bg-amber-50 dark:bg-amber-950/30" : ""}`}>
                        <td className="px-2 py-1 text-right">{fmt(r.CE?.oi, 0)}</td>
                        <td className="px-2 py-1 text-right">{fmt(r.CE?.volume, 0)}</td>
                        <td className="px-2 py-1 text-right">{fmt(r.CE?.close)}</td>
                        <td className="px-2 py-1 text-right">{pct(r.CE?.iv)}</td>
                        <td className="px-2 py-1 text-right">{fmt(r.CE?.delta)}</td>
                        <td className="px-2 py-1 text-center font-medium">{fmt(r.strike, 0)}</td>
                        <td className="px-2 py-1 text-right">{fmt(r.PE?.delta)}</td>
                        <td className="px-2 py-1 text-right">{pct(r.PE?.iv)}</td>
                        <td className="px-2 py-1 text-right">{fmt(r.PE?.close)}</td>
                        <td className="px-2 py-1 text-right">{fmt(r.PE?.volume, 0)}</td>
                        <td className="px-2 py-1 text-right">{fmt(r.PE?.oi, 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
