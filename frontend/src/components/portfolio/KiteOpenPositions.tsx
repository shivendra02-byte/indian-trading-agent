"use client";

import { useState } from "react";
import { getKiteOpenPositions } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "sonner";
import { Activity, Loader2, RefreshCw } from "lucide-react";
import { errorMessage } from "@/lib/utils";

// One row of GET /api/positions/kite-open (backend/brokers/kite.py normalize_positions).
type OpenPosition = {
  tradingsymbol: string;
  exchange: string;
  product: string | null;
  side: "long" | "short";
  quantity: number;
  overnight_quantity: number;
  multiplier: number;
  average_price: number;
  last_price: number;
  pnl: number;
  unrealised: number;
  realised: number;
  m2m: number;
  exposure: number;
};

type OpenPositionsResponse = {
  positions: OpenPosition[];
  count: number;
  total_pnl: number;
  total_unrealised: number;
  fetched_at: string;
};

const money = (v: number) => `Rs.${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
const tone = (v: number) => (v > 0 ? "text-green-600" : v < 0 ? "text-red-600" : "");

/** Live F&O / intraday / MCX positions from Kite (read-only, fetched on demand, never stored). */
export function KiteOpenPositions({ kiteConnected }: { kiteConnected: boolean }) {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<OpenPositionsResponse | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      setData((await getKiteOpenPositions()) as OpenPositionsResponse);
    } catch (e) {
      toast.error(errorMessage(e, "Could not fetch positions from Kite"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-lg font-semibold">Open positions</h2>
          <span className="text-xs text-muted-foreground">
            F&amp;O, intraday and MCX from Kite · read-only
            {data ? ` · fetched ${new Date(data.fetched_at).toLocaleTimeString()}` : ""}
          </span>
        </div>
        <Button size="sm" onClick={load} disabled={!kiteConnected || loading}>
          {loading ? <Loader2 className="h-3 w-3 mr-1 animate-spin" /> : <RefreshCw className="h-3 w-3 mr-1" />}
          Fetch positions from Kite
        </Button>
      </div>

      {!kiteConnected && (
        <p className="text-xs text-muted-foreground">Kite is not connected for today. Log in to Kite to see open positions.</p>
      )}

      {data && (
        <Card>
          <CardContent className="p-0">
            {data.count === 0 ? (
              <p className="p-6 text-center text-sm text-muted-foreground">No open positions in Kite right now.</p>
            ) : (
              <>
                <div className="flex flex-wrap gap-4 px-4 pt-3 text-sm">
                  <span>{data.count} open</span>
                  <span>
                    P&amp;L <span className={`font-medium ${tone(data.total_pnl)}`}>{money(data.total_pnl)}</span>
                  </span>
                  <span>
                    Unrealised <span className={`font-medium ${tone(data.total_unrealised)}`}>{money(data.total_unrealised)}</span>
                  </span>
                </div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Instrument</TableHead>
                      <TableHead>Product</TableHead>
                      <TableHead className="text-right">Qty</TableHead>
                      <TableHead className="text-right">Avg</TableHead>
                      <TableHead className="text-right">LTP</TableHead>
                      <TableHead className="text-right">Exposure</TableHead>
                      <TableHead className="text-right">P&amp;L</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.positions.map((p) => (
                      <TableRow key={`${p.exchange}:${p.tradingsymbol}:${p.product ?? ""}`}>
                        <TableCell>
                          <span className="font-medium">{p.tradingsymbol}</span>{" "}
                          <span className="text-xs text-muted-foreground">{p.exchange}</span>{" "}
                          <Badge variant="outline" className={`text-xs ${p.side === "long" ? "text-green-700" : "text-red-700"}`}>
                            {p.side}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs">{p.product ?? "—"}</TableCell>
                        <TableCell className="text-right">{p.quantity}</TableCell>
                        <TableCell className="text-right">{p.average_price.toFixed(2)}</TableCell>
                        <TableCell className="text-right">{p.last_price.toFixed(2)}</TableCell>
                        <TableCell className="text-right">{money(p.exposure)}</TableCell>
                        <TableCell className={`text-right font-medium ${tone(p.pnl)}`}>{money(p.pnl)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
