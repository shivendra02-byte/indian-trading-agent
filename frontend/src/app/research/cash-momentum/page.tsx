"use client";

import { StrategyPanel } from "@/components/research/StrategyPanel";
import { ShadowNote, num, rupees, tone } from "@/components/research/shared";

type Book = { return_pct?: number; cagr_pct?: number | null; max_drawdown_pct?: number; sharpe_daily?: number | null; nifty_return_pct?: number | null };

function BookLine({ status }: { status: Record<string, unknown> }) {
  const book = status.book as Book | undefined;
  return (
    <p className="text-xs text-muted-foreground">
      Universe {String(status.universe ?? "—")} stocks · NIFTY filter{" "}
      <b>{status.index_filter_on === true ? "ON (new buys allowed)" : status.index_filter_on === false ? "OFF (no new buys)" : "—"}</b>
      {book && book.return_pct !== undefined && (
        <>
          {" "}· book since the research start: <span className={tone(book.return_pct)}>{num(book.return_pct, 1)}%</span>{" "}
          (NIFTY {num(book.nifty_return_pct, 1)}%), max drawdown {num(book.max_drawdown_pct, 1)}%, cash {rupees(status.cash as number)}
        </>
      )}
    </p>
  );
}

export default function ResearchCashMomentumPage() {
  return (
    <div className="space-y-6 p-4 md:p-6">
      <div>
        <h1 className="text-xl font-semibold">Cash Momentum (research)</h1>
        <p className="text-sm text-muted-foreground">
          Andreas Clenow&apos;s &quot;Stocks on the Move&quot; (quoted in Berzal &amp; García, 2024) on F&amp;O + NIFTY 500 stocks.
        </p>
        <ShadowNote />
      </div>
      <StrategyPanel
        strategy="cash_momentum"
        description="Weekly (Wednesday): rank stocks by the 90-day exponential regression slope × R². Hold the top 20% while above their 100-day SMA with no gap over 15%; new buys only while NIFTY is above its 200-day SMA. Shares = capital × 0.10% ÷ ATR(20), at most 10% of capital each; fills at the next open; no stop-loss. R = ₹ P&L ÷ ₹10,000."
        defaultStart="2024-06-01"
        extra={(d) => <BookLine status={d.status} />}
      />
    </div>
  );
}
