"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ScanView } from "@/components/scans/ScanView";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getExitWarnings, getReversalScan, runReversalScan } from "@/lib/api";
import { errorMessage } from "@/lib/utils";

type Warning = {
  id: number;
  symbol: string;
  signal_date: string;
  kind: string;
  detail: string;
  daily_stage: string | null;
  alert_status: string | null;
};

function ExitWarnings() {
  const [rows, setRows] = useState<Warning[] | null>(null);

  useEffect(() => {
    getExitWarnings()
      .then((data) => setRows((data as { warnings: Warning[] }).warnings))
      .catch((e) => toast.error(errorMessage(e, "Could not load exit warnings")));
  }, []);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Exit warnings on longs you hold (Minervini top signals)</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        <p className="text-xs text-muted-foreground">
          Climax run, churning, heavy-volume close below the 50-day SMA, 200-day SMA rolling over, distribution
          days. Checked after the close; information only, Telegram off until studied.
        </p>
        {rows === null ? (
          <p className="text-muted-foreground">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="text-muted-foreground">No warnings yet.</p>
        ) : (
          rows.slice(0, 30).map((w) => (
            <p key={w.id}>
              <span className="font-medium">{w.symbol}</span>{" "}
              <span className="text-xs text-muted-foreground">{w.signal_date}</span> ·{" "}
              {w.kind.replace(/_/g, " ")}: {w.detail}
              {w.daily_stage ? <span className="text-xs text-muted-foreground"> · {w.daily_stage}</span> : null}
            </p>
          ))
        )}
      </CardContent>
    </Card>
  );
}

export default function ReversalScanPage() {
  return (
    <div className="space-y-4">
      <ScanView
        title="Trend reversals"
        subtitle="Stretch (2.5 SD) → RSI divergence or swing failure → close above the reversal bar · only in daily Stage 1/2 (no falling knives) · cash, long only · stop beyond the extreme, 2R then breakeven and trail · shadow: tracked in the journal, no AI review or alert · replaces Wolfe Wave · the 1H version for futures and MCX shows in the Futures & MCX scan"
        load={getReversalScan}
        run={runReversalScan}
        emptyText="No scan yet. It runs after the cup & handle scan on the next trading day."
      />
      <ExitWarnings />
    </div>
  );
}
