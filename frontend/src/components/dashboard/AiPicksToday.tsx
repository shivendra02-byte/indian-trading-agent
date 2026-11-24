"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Loader2, RefreshCw, Sparkles } from "lucide-react";
import { getAiPicksToday } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

// GET /api/journal/ai-today (backend/routers/journal.py ai_today)
type Pick = { name: string; symbol: string; action: string; confidence: number | null; segment: string; at: string | null };
type Response = { date: string; picks: Pick[]; reviewed: number };

/** Today's AI-approved trades: name, buy/sell, AI confidence. Details live in the Trade Journal. */
export function AiPicksToday() {
  const [data, setData] = useState<Response | null>(null);
  const [error, setError] = useState(false);

  const fetchData = useCallback(() => {
    getAiPicksToday()
      .then((res) => {
        setData(res as Response);
        setError(false);
      })
      .catch(() => setError(true));
  }, []);

  useEffect(() => {
    fetchData();
    const t = setInterval(fetchData, 60_000);
    return () => clearInterval(t);
  }, [fetchData]);

  return (
    <Card>
      <CardContent className="space-y-3 p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            <div>
              <p className="font-semibold">Today&apos;s AI picks</p>
              <p className="text-xs text-muted-foreground">
                AI-approved trades · {data ? `${data.reviewed} reviewed today` : "…"}
              </p>
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={fetchData}>
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
        {error && <p className="text-sm text-red-600">Could not load today&apos;s picks.</p>}
        {!data && !error && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
        {data && data.picks.length === 0 && (
          <p className="text-sm text-muted-foreground">No AI-approved trades yet today.</p>
        )}
        {data && data.picks.length > 0 && (
          <div className="divide-y">
            {data.picks.map((p, i) => {
              const sell = p.action === "SELL" || p.action === "BEAR CALL";
              return (
                <div key={`${p.name}-${i}`} className="flex items-center justify-between py-2">
                  <span className="font-medium">{p.name}</span>
                  <span className="flex items-center gap-3">
                    <Badge
                      variant="outline"
                      className={sell ? "border-red-300 text-red-600" : p.action === "BUY" ? "border-green-300 text-green-700" : ""}
                    >
                      {p.action}
                    </Badge>
                    <span className="w-10 text-right text-sm tabular-nums">
                      {p.confidence === null ? "—" : `${p.confidence}%`}
                    </span>
                  </span>
                </div>
              );
            })}
          </div>
        )}
        <Link href="/journal" className="block text-xs text-muted-foreground hover:text-foreground">
          Entry, stop and target in the Trade Journal →
        </Link>
      </CardContent>
    </Card>
  );
}
