"use client";

import { StrategyPanel } from "@/components/research/StrategyPanel";
import { ShadowNote } from "@/components/research/shared";

export default function ResearchHourlyPage() {
  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Hourly Research (live market)</h1>
        <p className="text-sm text-muted-foreground">
          Research strategies that trade during market hours on 1H / 4H bars — MCX minis, NIFTY, BANKNIFTY and the 40 most
          liquid F&amp;O stocks. Every trade has a stop and a target; exits as in the live futures books (breakeven at 1R,
          then a 2 ATR trailing stop). They run after every completed hourly bar and write to the Research Journal.
        </p>
        <ShadowNote />
      </div>
      <StrategyPanel
        strategy="htf_pullback"
        description="The papers' idea: the slow trend picks the direction, the hourly chart only times the entry. Direction = daily EWMA 20 vs 250 (beyond 0.5 daily ATR). Entry when a 1H bar has touched the 1H EMA20 in the last 6 bars and the next close breaks the last 3 bars' high (low) and the EMA; once per pullback. Stop beyond the pullback ± 0.25 ATR (1–3 ATR), target 3R, fills at the next 1H open."
        defaultStart="2025-12-01"
      />
      <StrategyPanel
        strategy="trend_4h"
        description="Sepp & Lucic's EWMA crossover on 4H bars: a fresh cross of EWMA 20 over 100 beyond 0.5 × 4H ATR. Stop 2.5 × 4H ATR, target 3R, breakeven at 1R then a 2 × 4H ATR trail behind the best 1H close; time exit after 120 hourly bars."
        defaultStart="2025-12-01"
      />
    </div>
  );
}
