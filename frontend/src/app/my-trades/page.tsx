"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";
import {
  addMyTrade,
  closeMyTrade,
  deleteMyTrade,
  getMyTradeContracts,
  listMyTrades,
  refreshMyTrades,
  type MyTradeInput,
} from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";

type Trade = {
  id: number;
  symbol: string;
  exchange: string;
  contract: string;
  expiry: string;
  direction: "long" | "short";
  lots: number;
  point_value: number;
  entry_price: number;
  entry_time: string;
  initial_stop: number;
  target: number | null;
  interval: string;
  trail_atr: number;
  breakeven_at_r: number;
  status: "open" | "stop_hit" | "target_hit" | "closed";
  current_stop: number | null;
  stop_stage: string | null;
  atr: number | null;
  last_close: number | null;
  last_bar_ts: string | null;
  open_r: number | null;
  open_pnl: number | null;
  hit_time: string | null;
  hit_price: number | null;
  exit_price: number | null;
  realised_r: number | null;
  realised_pnl: number | null;
  note: string | null;
  best_close: number | null;
  data_note: string | null;
};
type Settings = { interval: string; trail_atr: number; breakeven_at_r: number; trail_start_r: number };
type ListResponse = { trades: Trade[]; settings: Settings };
type Contract = { contract: string; expiry: string; lot_size: number; active: boolean; point_value: number };

const STAGE: Record<string, string> = {
  initial: "initial stop",
  breakeven: "at breakeven",
  trailing: "trailing",
  stop: "initial stop",
  gap_stop: "gapped through stop",
  breakeven_stop: "breakeven stop",
  trail_stop: "trailing stop",
  target: "target",
};

function fmt(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function tone(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v > 0 ? "text-emerald-600" : v < 0 ? "text-red-600" : "";
}

function nowLocal(): string {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

export default function MyTradesPage() {
  const [data, setData] = useState<ListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [contracts, setContracts] = useState<Contract[]>([]);
  const [exits, setExits] = useState<Record<number, string>>({});
  const [form, setForm] = useState({
    exchange: "NFO" as "NFO" | "BFO" | "MCX",
    symbol: "",
    contract: "",
    direction: "long" as "long" | "short",
    lots: "1",
    entry_price: "",
    entry_time: nowLocal(),
    initial_stop: "",
    target: "",
    trail_atr: "",
    interval: "60minute" as "10minute" | "60minute",
    note: "",
  });

  const fetchData = useCallback(() => {
    listMyTrades()
      .then((res) => setData(res as ListResponse))
      .catch((e) => toast.error(errorMessage(e, "Could not load your trades")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const set = (key: keyof typeof form, value: string) => setForm((f) => ({ ...f, [key]: value }));

  const loadContracts = async (symbol: string, exchange: "NFO" | "BFO" | "MCX") => {
    if (!symbol) return;
    try {
      const res = (await getMyTradeContracts(symbol, exchange)) as { contracts: Contract[] };
      setContracts(res.contracts);
      const active = res.contracts.find((c) => c.active) ?? res.contracts[0];
      set("contract", active?.contract ?? "");
      if (res.contracts.length === 0) toast.warning("No unexpired futures found (is the Kite login done today?)");
    } catch (e) {
      toast.error(errorMessage(e, "Could not load contracts"));
    }
  };

  const findContracts = () => loadContracts(form.symbol, form.exchange);

  // Index futures in one tap (asked 24 Nov 2026): SENSEX trades on BSE (BFO).
  const pickIndex = (symbol: string) => {
    const exchange = symbol === "SENSEX" ? "BFO" : "NFO";
    setForm((f) => ({ ...f, symbol, exchange }));
    loadContracts(symbol, exchange);
  };

  const submit = async () => {
    setSaving(true);
    try {
      const payload: MyTradeInput = {
        symbol: form.symbol,
        exchange: form.exchange,
        contract: form.contract || undefined,
        direction: form.direction,
        lots: Number(form.lots),
        entry_price: Number(form.entry_price),
        entry_time: form.entry_time || undefined,
        initial_stop: Number(form.initial_stop),
        target: form.target ? Number(form.target) : null,
        interval: form.exchange === "MCX" ? "60minute" : form.interval,
        trail_atr: form.trail_atr ? Number(form.trail_atr) : undefined,
        note: form.note || undefined,
      };
      const t = (await addMyTrade(payload)) as Trade;
      toast.success(`${t.contract} ${t.direction} added, stop ${fmt(t.current_stop)}`);
      if (t.data_note) toast.warning(t.data_note);
      setForm((f) => ({ ...f, symbol: "", contract: "", entry_price: "", initial_stop: "", target: "", note: "", entry_time: nowLocal() }));
      setContracts([]);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not add the trade"));
    } finally {
      setSaving(false);
    }
  };

  const refresh = async () => {
    setLoading(true);
    try {
      const res = (await refreshMyTrades()) as { fetch_problems?: Record<string, string> };
      const problems = Object.values(res.fetch_problems ?? {});
      if (problems.length) toast.warning(problems[0]);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Refresh failed"));
      setLoading(false);
    }
  };

  const close = async (t: Trade) => {
    const price = Number(exits[t.id]);
    if (!price) {
      toast.warning("Enter your exit price first");
      return;
    }
    try {
      await closeMyTrade(t.id, price);
      toast.success(`${t.contract} closed`);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not close the trade"));
    }
  };

  const remove = async (t: Trade) => {
    try {
      await deleteMyTrade(t.id);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Could not delete"));
    }
  };

  const input = "rounded border bg-background px-2 py-1";
  const s = data?.settings;

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold">My Futures Trades</h1>
          <p className="text-sm text-muted-foreground">
            Enter the futures trades you take. After every {s?.interval === "10minute" ? "10-minute" : "hourly"} bar the
            system works out your trailing stop: to breakeven once a bar closes {s?.breakeven_at_r ?? 1}R in your favour,
            then {s?.trail_atr ?? 2} ATR behind the best close. It never loosens. Telegram tells you when the stop moves
            or is hit; you place and move the actual stop order.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={refresh} disabled={loading}>
          <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} /> Recalculate
        </Button>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Add a trade</CardTitle>
          <CardDescription>
            1R = entry to initial stop. The bar your entry falls in is skipped; following starts from the next bar.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Exchange</span>
              <select className={input} value={form.exchange} onChange={(e) => set("exchange", e.target.value)}>
                <option value="NFO">NSE F&amp;O</option>
                <option value="BFO">BSE F&amp;O (SENSEX)</option>
                <option value="MCX">MCX</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Underlying</span>
              <input
                className={`${input} w-32 uppercase`}
                placeholder={form.exchange === "MCX" ? "GOLDM" : form.exchange === "BFO" ? "SENSEX" : "RELIANCE"}
                value={form.symbol}
                onChange={(e) => set("symbol", e.target.value)}
                onBlur={findContracts}
              />
            </label>
            <Button size="sm" variant="outline" onClick={findContracts} disabled={!form.symbol}>
              <Search className="mr-1 h-4 w-4" /> Contracts
            </Button>
            <div className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Index futures</span>
              <div className="flex gap-1">
                {["NIFTY", "BANKNIFTY", "SENSEX"].map((idx) => (
                  <Button key={idx} size="sm" variant={form.symbol === idx ? "default" : "outline"} className="h-8 px-2 text-xs" onClick={() => pickIndex(idx)}>
                    {idx}
                  </Button>
                ))}
              </div>
            </div>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Contract</span>
              <select className={input} value={form.contract} onChange={(e) => set("contract", e.target.value)} disabled={!contracts.length}>
                {contracts.length === 0 && <option value="">—</option>}
                {contracts.map((c) => (
                  <option key={c.contract} value={c.contract}>
                    {c.contract} (lot {c.lot_size}){c.active ? " · active" : ""}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Side</span>
              <select className={input} value={form.direction} onChange={(e) => set("direction", e.target.value)}>
                <option value="long">Long</option>
                <option value="short">Short</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Lots</span>
              <input className={`${input} w-16`} inputMode="numeric" value={form.lots} onChange={(e) => set("lots", e.target.value)} />
            </label>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Entry price</span>
              <input className={`${input} w-24`} inputMode="decimal" value={form.entry_price} onChange={(e) => set("entry_price", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Entry time</span>
              <input type="datetime-local" className={input} value={form.entry_time} onChange={(e) => set("entry_time", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Initial stop</span>
              <input className={`${input} w-24`} inputMode="decimal" value={form.initial_stop} onChange={(e) => set("initial_stop", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Target (optional)</span>
              <input className={`${input} w-24`} inputMode="decimal" value={form.target} onChange={(e) => set("target", e.target.value)} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">Trail ATR</span>
              <input className={`${input} w-16`} inputMode="decimal" placeholder={String(s?.trail_atr ?? 2)} value={form.trail_atr} onChange={(e) => set("trail_atr", e.target.value)} />
            </label>
            {form.exchange !== "MCX" && (
              <label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Trail on</span>
                <select className={input} value={form.interval} onChange={(e) => set("interval", e.target.value)}>
                  <option value="60minute">1-hour bars</option>
                  <option value="10minute">10-minute bars</option>
                </select>
              </label>
            )}
            <label className="flex min-w-[10rem] flex-1 flex-col gap-1">
              <span className="text-xs text-muted-foreground">Note</span>
              <input className={input} value={form.note} onChange={(e) => set("note", e.target.value)} />
            </label>
            <Button size="sm" onClick={submit} disabled={saving || !form.symbol || !form.entry_price || !form.initial_stop}>
              {saving ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Plus className="mr-1 h-4 w-4" />} Add trade
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Your trades</CardTitle>
        </CardHeader>
        <CardContent>
          {!data || data.trades.length === 0 ? (
            <p className="text-sm text-muted-foreground">No trades yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    {["Contract", "Side", "Lots", "Entry", "Initial stop", "Stop now", "Last close", "Open", "Status", "Your exit", ""].map((h) => (
                      <th key={h} className="px-2 py-1 text-left font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.trades.map((t) => (
                    <tr key={t.id} className="border-t align-top" title={t.note ?? undefined}>
                      <td className="px-2 py-1 font-medium">
                        {t.contract}
                        <div className="text-muted-foreground">{t.entry_time.replace("T", " ").slice(0, 16)}</div>
                        <div className="text-muted-foreground">
                          {t.interval === "10minute" ? "10-min" : "1-hour"} bars · trail {t.trail_atr} ATR
                          {t.atr ? ` (${fmt(t.atr)} pts)` : ""}
                        </div>
                      </td>
                      <td className={`px-2 py-1 ${t.direction === "long" ? "text-emerald-600" : "text-red-600"}`}>{t.direction}</td>
                      <td className="px-2 py-1">{t.lots}</td>
                      <td className="px-2 py-1">{fmt(t.entry_price)}</td>
                      <td className="px-2 py-1">{fmt(t.initial_stop)}</td>
                      <td className="px-2 py-1">
                        {t.status === "closed" ? "—" : <b>{fmt(t.current_stop)}</b>}
                        {t.stop_stage && t.status !== "closed" && (
                          <div>
                            <Badge variant="outline" className="mt-0.5 text-[10px]">{STAGE[t.stop_stage] ?? t.stop_stage}</Badge>
                          </div>
                        )}
                        {t.best_close != null && t.status !== "closed" && (
                          <div className="text-muted-foreground" title="The trail follows the best close since entry">
                            best close {fmt(t.best_close)}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-1">
                        {t.last_close == null && t.data_note ? (
                          <span className="text-amber-600">{t.data_note}</span>
                        ) : (
                          fmt(t.last_close)
                        )}
                        {t.last_bar_ts && (
                          <div className="text-muted-foreground" title="Start time of the last completed bar">
                            bar {t.last_bar_ts.slice(5, 10).split("-").reverse().join("/")} {t.last_bar_ts.slice(11, 16)}
                          </div>
                        )}
                      </td>
                      <td className={`px-2 py-1 ${tone(t.status === "closed" ? t.realised_r : t.open_r)}`}>
                        {t.status === "closed"
                          ? `${fmt(t.realised_r)}R · ₹${fmt(t.realised_pnl, 0)}`
                          : `${fmt(t.open_r)}R · ₹${fmt(t.open_pnl, 0)}`}
                      </td>
                      <td className="px-2 py-1">
                        {t.status === "open" && <Badge variant="outline">open</Badge>}
                        {t.status === "stop_hit" && (
                          <Badge variant="destructive">stop hit {fmt(t.hit_price)}</Badge>
                        )}
                        {t.status === "target_hit" && <Badge>target {fmt(t.hit_price)}</Badge>}
                        {t.hit_time && t.status !== "open" && (
                          <div className="text-muted-foreground">at {t.hit_time.replace("T", " ").slice(5, 16)}</div>
                        )}
                        {t.status === "closed" && <span className="text-muted-foreground">closed at {fmt(t.exit_price)}</span>}
                      </td>
                      <td className="px-2 py-1">
                        {t.status !== "closed" && (
                          <div className="flex items-center gap-1">
                            <input
                              className={`${input} w-20`}
                              inputMode="decimal"
                              placeholder="price"
                              value={exits[t.id] ?? ""}
                              onChange={(e) => setExits((x) => ({ ...x, [t.id]: e.target.value }))}
                            />
                            <Button size="sm" variant="outline" onClick={() => close(t)}>
                              Close
                            </Button>
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-1">
                        <button className="text-red-600" onClick={() => remove(t)} aria-label={`Delete ${t.contract} trade`}>
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
