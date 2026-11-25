"use client";

import { ScanView } from "@/components/scans/ScanView";
import { getWolfeScan, runWolfeScan } from "@/lib/api";

export default function WolfeScanPage() {
  return (
    <ScanView
      title="Wolfe Wave (retired)"
      subtitle="Retired 25 Nov 2026, replaced by Trend reversals · bullish 5-point wedge reversals · cash, long only, F&O-list stocks · target the 1–4 line (≥ 2R), breakeven then trail, up to 20 days · AI-reviewed · runs after the close"
      load={getWolfeScan}
      run={runWolfeScan}
      emptyText="No scan yet. It runs after the cup & handle scan on the next trading day."
    />
  );
}
