"use client";

import { useCallback, useState } from "react";
import { ScanView } from "@/components/scans/ScanView";
import { Button } from "@/components/ui/button";
import { getHourlyScan, getOrbScan, runHourlyScan, runOrbScan } from "@/lib/api";

type Group = "nse" | "orb" | "mcx";

const SUBTITLE: Record<Group, string> = {
  nse: "Stock futures (F&O list) and NIFTY/BANKNIFTY futures · 1h entries, 4H + daily context · long and short",
  orb: "Opening range breakout on the most liquid stock futures · 30-minute range, 10-minute signals · exit 15:15 the same day",
  mcx: "Gold, Silver, Crude and Natural Gas minis · 1h entries, 4H + daily context · long and short",
};
const LABEL: Record<Group, string> = { nse: "NSE futures", orb: "Stock ORB (intraday)", mcx: "MCX" };
const TITLE: Record<Group, string> = {
  nse: "Futures Scan (1h)",
  orb: "Opening Range Breakout (10m)",
  mcx: "MCX Scan (1h)",
};

export default function HourlyScanPage() {
  const [group, setGroup] = useState<Group>("nse");
  const load = useCallback(() => (group === "orb" ? getOrbScan() : getHourlyScan(group)), [group]);
  const run = useCallback(() => (group === "orb" ? runOrbScan() : runHourlyScan(group)), [group]);
  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        {(["nse", "orb", "mcx"] as Group[]).map((g) => (
          <Button key={g} size="sm" variant={g === group ? "default" : "outline"} onClick={() => setGroup(g)}>
            {LABEL[g]}
          </Button>
        ))}
      </div>
      <ScanView
        key={group}
        title={TITLE[group]}
        subtitle={SUBTITLE[group]}
        load={load}
        run={run}
        emptyText={
          group === "orb"
            ? "No ORB scan yet. It runs after each 10-minute candle from 09:55 to 14:50 on trading days."
            : "No scan yet. Scans run after each completed hourly candle on trading days."
        }
      />
    </div>
  );
}
