"use client";

import { CashUniverseCard } from "@/components/scans/CashUniverseCard";
import { ScanView } from "@/components/scans/ScanView";
import { getCashSwingScan, runCashSwingScan } from "@/lib/api";

export default function SwingScanPage() {
  return (
    <div className="space-y-4">
      <ScanView
        title="Swing Scan"
        subtitle="Cash delivery, long only, F&O stocks + NIFTY 500 · daily setups for 1–7 day holds · runs at 15:45"
        load={getCashSwingScan}
        run={runCashSwingScan}
        emptyText="No scan yet. The first one runs after the close on the next trading day."
      />
      <CashUniverseCard />
    </div>
  );
}
