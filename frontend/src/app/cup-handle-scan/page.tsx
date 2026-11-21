"use client";

import { ScanView } from "@/components/scans/ScanView";
import { getCupHandleScan, runCupHandleScan } from "@/lib/api";

export default function CupHandleScanPage() {
  return (
    <ScanView
      title="Cup & Handle"
      subtitle="William O'Neil breakouts · cash delivery, long only, F&O-list stocks · stop 8%, target 20%, up to ~8 weeks · runs after the close"
      load={getCupHandleScan}
      run={runCupHandleScan}
      emptyText="No scan yet. It runs after the swing scan on the next trading day."
    />
  );
}
