"use client";

import { StrategyPanel } from "@/components/research/StrategyPanel";
import { ShadowNote } from "@/components/research/shared";

export default function ResearchTrendPage() {
  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Trend Book (research)</h1>
        <p className="text-sm text-muted-foreground">
          Slow trend following on daily bars — GOLDM, SILVERM, CRUDEOILM, NATGASMINI, NIFTY, BANKNIFTY. From Sepp &amp;
          Lucic, &quot;The Science and Practice of Trend-Following Systems&quot; (2026).
        </p>
        <ShadowNote />
      </div>
      <StrategyPanel
        strategy="trend_ewma"
        description="Long when the 20-day EWMA is above the 250-day EWMA by a buffer of ATR (short mirrored). Size fixed at entry; stop 5 ATR, trailing behind each close. Exit only when the stop is hit AND the signal is off. Fills at the next day's open. Expect a low win rate and a few large winners."
        gridKeys={["fast", "slow", "entry_buffer_atr"]}
      />
      <StrategyPanel
        strategy="tsmom"
        description="Time-series momentum: the average sign of the last 10 blocks of 10 days of volatility-scaled returns (-1 … +1). Long at +0.2 or more, short at -0.2 or less, decided once a week; a 5 ATR trailing stop is the safety exit and defines 1R."
        gridKeys={["blocks", "block_days", "entry_threshold"]}
      />
    </div>
  );
}
