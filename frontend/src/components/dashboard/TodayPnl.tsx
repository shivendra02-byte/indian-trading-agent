"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Loader2, RefreshCw, Wallet } from "lucide-react";
import { getTodayPnl } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

// GET /api/pnl/today (backend/pnl_today.py)
type Row = {
  book: "mine" | "system";
  id: number;
  name: string;
  segment: string;
  setup?: string;
  direction: string;
  state: "closed" | "awaiting_exit" | "open" | "pending";
  at: string | null;
  price: number | null;
  entry: number | null;
  r: number | null;
  pnl: number | null;
  note?: string | null;
  alert?: "AI approved" | "rules only";
  taken?: boolean;
};
type Book = {
  rows: Row[];
  realised_pnl: number;
  realised_r: number;
  unrealised_pnl: number;
  unrealised_r: number;
  total_pnl: number;
  ended_today: number;
  wins: number;
  open: number;
  pending: number;
};
type Summary = { date: string; as_of: string; mine: Book; system: Book; total: Book };

const STATE_LABEL: Record<Row["state"], string> = {
  closed: "closed",
  awaiting_exit: "stop/target hit",
  open: "open",
  pending: "waiting entry",
};

function rupees(x: number | null | undefined) {
  if (x == null) return "—";
  const sign = x > 0 ? "+" : x < 0 ? "−" : "";
  return `${sign}₹${Math.abs(x).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}
function rr(x: number | null | undefined) {
  if (x == null) return "—";
  return `${x > 0 ? "+" : ""}${x.toFixed(2)}R`;
}
function tone(x: number | null | undefined) {
  if (x == null || x === 0) return "";
  return x > 0 ? "text-emerald-600" : "text-red-600";
}
function time(at: string | null) {
  if (!at) return "";
  return at.length > 10 ? at.replace("T", " ").slice(11, 16) : at.slice(5);
}

function BookBlock({ title, book, href }: { title: string; book: Book; href: string }) {
  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <Link href={href} className="text-sm font-medium hover:underline">
          {title}
        </Link>
        <span className={`text-sm font-semibold ${tone(book.total_pnl)}`}>{rupees(book.total_pnl)}</span>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded border p-2">
          <div className="text-muted-foreground">Realised ({book.ended_today} ended, {book.wins} won)</div>
          <div className={`font-medium ${tone(book.realised_pnl)}`}>
            {rupees(book.realised_pnl)} · {rr(book.realised_r)}
          </div>
        </div>
        <div className="rounded border p-2">
          <div className="text-muted-foreground">Open ({book.open})</div>
          <div className={`font-medium ${tone(book.unrealised_pnl)}`}>
            {rupees(book.unrealised_pnl)} · {rr(book.unrealised_r)}
          </div>
        </div>
      </div>
      {book.rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">Nothing open or ended today.</p>
      ) : (
        <table className="w-full text-xs">
          <tbody>
            {book.rows.map((r) => (
              <tr key={`${r.book}-${r.segment}-${r.id}`} className="border-t align-top" title={r.note ?? undefined}>
                <td className="py-1 pr-2">
                  <div className="font-medium">{r.name}</div>
                  <div className="text-muted-foreground">
                    {r.segment}
                    {r.setup ? ` · ${r.setup.replace(/_/g, " ")}` : ""}
                    {r.alert ? ` · ${r.alert}` : ""}
                    {r.taken ? " · you took it" : ""} ·{" "}
                    <span className={r.direction === "short" ? "text-red-600" : r.direction === "long" ? "text-emerald-600" : ""}>
                      {r.direction}
                    </span>
                  </div>
                </td>
                <td className="py-1 pr-2">
                  <Badge variant={r.state === "closed" ? "secondary" : r.state === "awaiting_exit" ? "destructive" : "outline"} className="text-[10px]">
                    {STATE_LABEL[r.state]}
                  </Badge>
                  <div className="text-muted-foreground">{time(r.at)}</div>
                </td>
                <td className={`py-1 text-right ${tone(r.pnl)}`}>
                  <div>{rupees(r.pnl)}</div>
                  <div>{r.note && r.pnl == null ? <span className="text-amber-600">{r.note}</span> : rr(r.r)}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function TodayPnl() {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(() => {
    getTodayPnl()
      .then((res) => {
        setData(res as Summary);
        setError(null);
      })
      .catch(() => setError("Could not load today's P&L"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
    const t = setInterval(fetchData, 60_000); // trackers update after each bar; refresh every minute
    return () => clearInterval(t);
  }, [fetchData]);

  const reload = () => {
    setLoading(true);
    fetchData();
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <Wallet className="h-4 w-4" /> Today&apos;s P&amp;L
              {data && <span className={`ml-1 ${tone(data.total.total_pnl)}`}>{rupees(data.total.total_pnl)}</span>}
            </CardTitle>
            <CardDescription>
              Yours = My futures trades + journal trades you marked Taken. System = paper trades you were alerted on
              (AI-approved or rules only). A trade moves to realised as soon as it ends{data ? ` · updated ${data.as_of.slice(11, 16)}` : ""}.
            </CardDescription>
          </div>
          <Button size="sm" variant="ghost" onClick={reload} disabled={loading} aria-label="Refresh today's P&L">
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {error && <p className="text-sm text-red-600">{error}</p>}
        {data && (
          <div className="grid gap-6 md:grid-cols-2">
            <BookBlock title="Your trades" book={data.mine} href="/my-trades" />
            <BookBlock title="System paper trades" book={data.system} href="/journal" />
          </div>
        )}
        {data && (
          <p className="mt-3 text-xs text-muted-foreground">
            Total today: realised <b className={tone(data.total.realised_pnl)}>{rupees(data.total.realised_pnl)}</b>{" "}
            ({rr(data.total.realised_r)}) · open <b className={tone(data.total.unrealised_pnl)}>{rupees(data.total.unrealised_pnl)}</b>{" "}
            ({rr(data.total.unrealised_r)}). Stop/target hits on your trades count at the hit price until you enter your
            exit. System ₹ is after charges once closed. A trade you took counts once in the total, as yours (your
            fill and exit, the rules&apos; lot size, no charges).
          </p>
        )}
      </CardContent>
    </Card>
  );
}
