"use client";

import { ScanView } from "@/components/scans/ScanView";
import { getVcpScan, runVcpScan } from "@/lib/api";

export default function VcpScanPage() {
  return (
    <ScanView
      title="VCP (Minervini)"
      subtitle="Trend template + volatility contraction breakout · cash, long only, F&O-list stocks · stop under the last contraction (≤ 8%), target 20% (≥ 2R), breakeven then trail · shadow: tracked in the journal, no AI review or alert · runs after the close"
      load={getVcpScan}
      run={runVcpScan}
      emptyText="No scan yet. It runs after the cup & handle scan on the next trading day."
    />
  );
}
