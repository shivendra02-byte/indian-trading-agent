"use client";

import { StrategyPanel } from "@/components/research/StrategyPanel";
import { ShadowNote } from "@/components/research/shared";

type Formation = { month?: string; pairs?: string[]; tested?: number; passed?: number; reason?: string | null };

function FormationLine({ status }: { status: Record<string, unknown> }) {
  const f = status.formation as Formation | undefined;
  if (!f || !f.month) return null;
  return (
    <p className="text-xs text-muted-foreground">
      Pairs for {f.month}: {f.pairs?.length ? f.pairs.join(", ") : "none"} ({f.tested ?? 0} tested, {f.passed ?? 0} passed
      the mean-reversion test{f.reason ? ` — ${f.reason}` : ""}). Entry/exit &quot;price&quot; columns show the q-score.
    </p>
  );
}

export default function ResearchPairsPage() {
  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Pairs (research)</h1>
        <p className="text-sm text-muted-foreground">
          Qureshi &amp; Zaman (2024): pairs picked each month by a maximum-weight matching, so each stock is in at most one
          pair; trades the robust q-score of the spread back towards its median. Plus the GOLDM/SILVERM spread.
        </p>
        <ShadowNote />
      </div>
      <StrategyPanel
        strategy="pairs_stocks"
        description="Monthly: regress log prices over ~2 years for every pair of liquid F&O stocks, keep spreads that pass a Dickey-Fuller test with a 2–40 day half-life, match them so no stock is in two pairs. Enter at |q| ≥ 1.5 (q = (spread − median) / IQR), exit at |q| ≤ 0.25 or a zero cross, stop at |q| ≥ 3.5 (1R), 30-day time exit. Needs ~2 years of daily history (Rule Backtest → download history)."
        defaultStart="2024-06-01"
        extra={(d) => <FormationLine status={d.status} />}
      />
      <StrategyPanel
        strategy="pairs_metals"
        description="SILVERM against GOLDM on the continuous daily series, same q-score rules, traded even when the cointegration test fails (recorded per trade). Lots are matched to the hedge ratio within 25%."
        extra={(d) => <FormationLine status={d.status} />}
      />
    </div>
  );
}
