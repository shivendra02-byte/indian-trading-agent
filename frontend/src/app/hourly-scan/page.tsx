"use client";

import { useCallback, useState } from "react";
import { ScanView } from "@/components/scans/ScanView";
import { Button } from "@/components/ui/button";
import { getHourlyScan, runHourlyScan } from "@/lib/api";

type Group = "nse" | "mcx";

const SUBTITLE: Record<Group, string> = {
  nse: "Stock futures (F&O list) and NIFTY/BANKNIFTY futures · 1h entries, 4H + daily context · long and short",
  mcx: "Gold, Silver, Crude and Natural Gas minis · 1h entries, 4H + daily context · long and short",
};

export default function HourlyScanPage() {
  const [group, setGroup] = useState<Group>("nse");
  const load = useCallback(() => getHourlyScan(group), [group]);
  const run = useCallback(() => runHourlyScan(group), [group]);
  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        {(["nse", "mcx"] as Group[]).map((g) => (
          <Button key={g} size="sm" variant={g === group ? "default" : "outline"} onClick={() => setGroup(g)}>
            {g === "nse" ? "NSE futures" : "MCX"}
          </Button>
        ))}
      </div>
      <ScanView
        key={group}
        title={group === "nse" ? "Futures Scan (1h)" : "MCX Scan (1h)"}
        subtitle={SUBTITLE[group]}
        load={load}
        run={run}
        emptyText="No scan yet. Scans run after each completed hourly candle on trading days."
      />
    </div>
  );
}
