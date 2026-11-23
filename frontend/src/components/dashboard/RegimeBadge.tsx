"use client";

import { useEffect, useState } from "react";
import { getCurrentRegime, getEngineRegime } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { TrendingUp, TrendingDown, Move, Zap, HelpCircle, Loader2, type LucideIcon } from "lucide-react";
import { statusColors, splitStatusColor } from "@/lib/status-colors";

// GET /api/regime/current (backend/market_regime.py classify_regime_for_date).
// An UNKNOWN regime comes back with only date, regime and reasoning.
type CurrentRegime = {
  date: string;
  regime: "BULL" | "BEAR" | "SIDEWAYS" | "HIGH_VOL" | "UNKNOWN";
  reasoning: string;
  nifty_close?: number;
  sma_50?: number;
  sma_200?: number;
  annualized_vol_pct?: number;
  vol_baseline_pct?: number;
};

// GET /api/regime/engine (backend/signals/regime.py explain_current): the regime that gates trades.
type EngineRegime = {
  regime: "STRONG_UP" | "STRONG_DOWN" | "SIDEWAYS" | "HIGH_VOL" | "UNSTABLE" | null;
  reason: string;
  raw?: string;
  date?: string;
  close?: number;
  ema_fast?: number;
  ema_slow?: number;
  ema_fast_period?: number;
  ema_slow_period?: number;
  ema_slow_rising?: boolean;
  adx?: number;
  adx_trend_min?: number;
  adx_sideways_max?: number;
  vol_ratio?: number | null;
  high_vol_ratio?: number;
};

const engineStyles: Record<string, { key: string; label: string; desc: string }> = {
  STRONG_UP: { key: "BULL", label: "STRONG UP", desc: "NSE trades allowed: pullback and breakout longs." },
  STRONG_DOWN: { key: "BEAR", label: "STRONG DOWN", desc: "NSE trades allowed: pullback and breakout shorts." },
  SIDEWAYS: { key: "SIDEWAYS", label: "SIDEWAYS", desc: "NSE trades allowed: range reversals; breakouts only on 2x volume." },
  HIGH_VOL: { key: "HIGH_VOL", label: "HIGH VOL", desc: "NSE trades allowed: with-trend pullbacks at half size." },
  UNSTABLE: { key: "UNKNOWN", label: "UNSTABLE", desc: "No clear regime: no new NSE setups are permitted (MCX uses each commodity's own regime)." },
};

function engineWhy(e: EngineRegime): string {
  if (e.close == null) return e.reason;
  const parts = [
    `NIFTY ${Math.round(e.close)} · EMA${e.ema_fast_period} ${Math.round(e.ema_fast ?? 0)} · EMA${e.ema_slow_period} ${Math.round(e.ema_slow ?? 0)} (${e.ema_slow_rising ? "rising" : "falling"})`,
    `ADX ${e.adx} (trend ≥ ${e.adx_trend_min}, sideways < ${e.adx_sideways_max})`,
  ];
  if (e.vol_ratio != null) parts.push(`vol ${e.vol_ratio}× its average (high ≥ ${e.high_vol_ratio}×)`);
  if (e.regime === "UNSTABLE" && e.reason) parts.push(e.reason);
  return parts.join(" · ");
}

const styles: Record<string, { bg: string; text: string; border: string; icon: LucideIcon; label: string; desc: string }> = {
  BULL: {
    ...splitStatusColor(statusColors.bullish),
    border: "border-green-300 dark:border-green-800",
    icon: TrendingUp,
    label: "BULL",
    desc: "Trend favors longs. Breakout signals reliable.",
  },
  BEAR: {
    ...splitStatusColor(statusColors.bearish),
    border: "border-red-300 dark:border-red-800",
    icon: TrendingDown,
    label: "BEAR",
    desc: "Trend favors shorts. Bounce signals fail more often.",
  },
  SIDEWAYS: {
    ...splitStatusColor(statusColors.amber),
    border: "border-amber-300 dark:border-amber-800",
    icon: Move,
    label: "SIDEWAYS",
    desc: "Range-bound. Mean-reversion works, breakouts fakeout.",
  },
  HIGH_VOL: {
    ...splitStatusColor(statusColors.purple),
    border: "border-purple-300 dark:border-purple-800",
    icon: Zap,
    label: "HIGH VOL",
    desc: "Extreme moves. Reduce size. Most signals less reliable.",
  },
  UNKNOWN: {
    ...splitStatusColor(statusColors.neutral),
    border: "border-gray-300 dark:border-border",
    icon: HelpCircle,
    label: "UNKNOWN",
    desc: "Insufficient data to classify.",
  },
};

export function RegimeBadge() {
  const [data, setData] = useState<CurrentRegime | null>(null);
  const [engine, setEngine] = useState<EngineRegime | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      (getEngineRegime() as Promise<EngineRegime>).catch(() => null),
      (getCurrentRegime() as Promise<CurrentRegime>).catch(() => null),
    ])
      .then(([e, c]) => {
        setEngine(e);
        setData(c);
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <Card className="border-muted">
        <CardContent className="p-3 flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          <span className="text-xs text-muted-foreground">Classifying market regime...</span>
        </CardContent>
      </Card>
    );
  }

  if (!data && !engine) return null;

  // Headline: the trading engine's regime (it decides which setups may trade). Fallback: Top Picks regime.
  const eng = engine?.regime ? engineStyles[engine.regime] : null;
  const style = eng ? styles[eng.key] : styles[data?.regime ?? "UNKNOWN"] || styles.UNKNOWN;
  const Icon = style.icon;

  return (
    <Card className={`${style.border} ${style.bg}`}>
      <CardContent className="p-3">
        <div className="flex items-center gap-3">
          <div className={`p-2 rounded-lg ${style.bg} border ${style.border}`}>
            <Icon className={`h-5 w-5 ${style.text}`} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">
                {eng ? "Market Regime (trading engine)" : "Market Regime"}
              </span>
              <Badge variant="outline" className={`${style.bg} ${style.text} ${style.border}`}>
                {eng ? eng.label : style.label}
              </Badge>
              {engine?.date && <span className="text-xs text-muted-foreground">NIFTY daily {engine.date}</span>}
            </div>
            <p className="text-sm mt-1">{eng ? eng.desc : style.desc}</p>
            {engine && (
              <p className="text-xs text-muted-foreground mt-0.5" title={engine.reason}>
                {engineWhy(engine)}
              </p>
            )}
            {data && (
              <p className="text-xs text-muted-foreground mt-1 truncate" title={data.reasoning}>
                Top Picks regime (50/200 SMA): <span className="font-medium">{data.regime}</span> · {data.reasoning}
              </p>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
