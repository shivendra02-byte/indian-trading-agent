"use client";

import { ScanView } from "@/components/scans/ScanView";
import { getCashSwingScan, runCashSwingScan } from "@/lib/api";

export default function SwingScanPage() {
  return (
    <ScanView
      title="Swing Scan"
      subtitle="Cash delivery, long only, F&O-list stocks · daily setups for 1–7 day holds · runs at 15:45"
      load={getCashSwingScan}
      run={runCashSwingScan}
      emptyText="No scan yet. The first one runs after the close on the next trading day."
    />
  );
}
