"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { ThemeToggle } from "@/components/theme/ThemeToggle";
import { SystemControl } from "@/components/layout/SystemControl";
import { logout } from "@/lib/api";
import { toast } from "sonner";
import {
  Home,
  Radar,
  CandlestickChart,
  Award,
  FlaskConical,
  History,
  Settings,
  TrendingUp,
  PieChart,
  LogOut,
  ClipboardCheck,
  Layers,
  GitCompareArrows,
  Spline,
  Lightbulb,
  Crosshair,
  Waves,
  Rocket,
  Scale,
  type LucideIcon,
  Menu,
  X,
  Zap,
} from "lucide-react";

type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  hint?: string;
};

type NavGroup = {
  title?: string;
  items: NavItem[];
};

// The old upstream tools (Top Picks, Market Scan, Strategies, News, Deep Analysis, Performance,
// Top Picks Tracker, Learning Insights, Signal Performance, Verdict/Confidence Calibration,
// Shadow Trades, Memory Admin, AI Backtest, My Trades) are off the sidebar (23 Nov 2026) but their
// pages still open by URL.
const navGroups: NavGroup[] = [
  {
    items: [
      { href: "/", label: "Today", icon: Home, hint: "Your daily workflow" },
    ],
  },
  {
    title: "CASH (AFTER THE CLOSE)",
    items: [
      { href: "/swing-scan", label: "Swing Scan", icon: TrendingUp, hint: "Daily cash swing setups (F&O stocks)" },
      { href: "/cup-handle-scan", label: "Cup & Handle", icon: Award, hint: "O'Neil breakouts, ~8-week holds" },
      { href: "/wolfe-scan", label: "Wolfe Wave", icon: Waves, hint: "5-point wedge reversals, AI-reviewed" },
    ],
  },
  {
    title: "FUTURES",
    items: [
      { href: "/hourly-scan", label: "Futures & MCX Scan", icon: Radar, hint: "Hourly futures, MCX and ORB setups" },
      { href: "/momentum", label: "Momentum Book", icon: Rocket, hint: "Weekly strongest/weakest F&O stocks" },
      { href: "/my-trades", label: "My Futures Trades", icon: Crosshair, hint: "Your trades with a trailing stop" },
    ],
  },
  {
    title: "OPTIONS",
    items: [
      { href: "/option-spreads", label: "Option Spreads", icon: Scale, hint: "Index credit spreads, AI-reviewed" },
      { href: "/nifty-options", label: "NIFTY Option Buying", icon: Zap, hint: "5-min call/put signals (shadow)" },
      { href: "/options-data", label: "Option Data", icon: Layers, hint: "Option history, IV, delta" },
    ],
  },
  {
    title: "ALERTS",
    items: [
      { href: "/divergence", label: "RSI Divergence", icon: GitCompareArrows, hint: "Daily reversal warnings + their record" },
      { href: "/chart-patterns", label: "Chart Patterns", icon: Spline, hint: "Trendlines, channels, triangles" },
      { href: "/ideas", label: "Ideas", icon: Lightbulb, hint: "Track ideas from X / chats by source" },
    ],
  },
  {
    title: "REVIEW",
    items: [
      { href: "/journal", label: "Trade Journal", icon: History, hint: "Every signal, AI decision, outcome" },
      { href: "/postmortem", label: "Weekly Post-mortem", icon: ClipboardCheck, hint: "Saturday review of the week" },
      { href: "/rule-backtests", label: "Rule Backtest", icon: FlaskConical, hint: "Compare rule versions on history" },
    ],
  },
  {
    title: "TOOLS",
    items: [
      { href: "/equity-portfolio-analysis", label: "Equity Portfolio", icon: PieChart, hint: "Kite holdings review" },
      { href: "/charts", label: "Charts", icon: CandlestickChart, hint: "Candlestick charts" },
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  // Phones and portrait tablets: the sidebar is a drawer opened from the top bar.
  const [open, setOpen] = useState(false);
  const current = navGroups.flatMap((g) => g.items).find(
    (i) => pathname === i.href || (i.href !== "/" && pathname.startsWith(i.href)),
  );

  const signOut = async () => {
    try {
      await logout();
      router.replace("/login");
      router.refresh();
    } catch {
      toast.error("Unable to sign out. Please try again.");
    }
  };

  return (
    <>
    <header className="lg:hidden fixed inset-x-0 top-0 z-40 flex h-12 items-center gap-3 border-b border-border bg-card/95 px-3 backdrop-blur">
      <button
        type="button"
        aria-label="Open menu"
        onClick={() => setOpen(true)}
        className="-ml-1 rounded-md p-2 text-foreground hover:bg-accent"
      >
        <Menu className="h-5 w-5" />
      </button>
      <TrendingUp className="h-5 w-5 text-green-500" />
      <span className="truncate font-semibold">{current?.label ?? "Trading Agent"}</span>
    </header>
    {open && (
      <div className="lg:hidden fixed inset-0 z-40 bg-black/40" aria-hidden="true" onClick={() => setOpen(false)} />
    )}
    <aside
      className={`fixed left-0 top-0 h-full w-72 max-w-[85vw] lg:w-64 bg-card border-r border-border flex flex-col z-50 overflow-y-auto lg:overflow-visible transition-transform duration-200 ${
        open ? "translate-x-0" : "-translate-x-full"
      } lg:translate-x-0`}
    >
      <button
        type="button"
        aria-label="Close menu"
        onClick={() => setOpen(false)}
        className="lg:hidden absolute right-2 top-2 rounded-md p-2 text-muted-foreground hover:bg-accent"
      >
        <X className="h-5 w-5" />
      </button>
      <div className="p-6 border-b border-border">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-6 w-6 text-green-500" />
          <div>
            <h1 className="font-bold text-lg">Trading Agent</h1>
            <p className="text-xs text-muted-foreground">NSE/BSE - Indian Market</p>
          </div>
        </div>
        <div className="mt-4">
          <SystemControl />
        </div>
      </div>

      <nav className="flex-1 p-3 space-y-4 lg:overflow-y-auto">
        {navGroups.map((group, gi) => (
          <div key={gi}>
            {group.title && (
              <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider px-3 mb-1.5">
                {group.title}
              </p>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => {
                const isActive = pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href));
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    onClick={() => setOpen(false)}
                    className={`flex items-center gap-3 px-3 py-2.5 lg:py-2 rounded-lg text-sm group ${
                      isActive
                        ? "bg-accent text-accent-foreground font-medium"
                        : "text-muted-foreground hover:text-foreground hover:bg-accent/50"
                    }`}
                  >
                    <item.icon className="h-4 w-4 flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div>{item.label}</div>
                      {item.hint && !isActive && (
                        <div className="text-[10px] text-muted-foreground/70 group-hover:text-muted-foreground truncate">
                          {item.hint}
                        </div>
                      )}
                    </div>
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="p-4 border-t border-border space-y-3">
        <div>
          <p className="text-[10px] text-muted-foreground mb-1.5">Appearance</p>
          <ThemeToggle />
        </div>
        <button
          type="button"
          onClick={signOut}
          className="flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          <LogOut className="h-3.5 w-3.5" /> Sign out
        </button>
        <div className="text-[10px] text-muted-foreground">
          <p>Powered by Claude + LangGraph</p>
          <p className="mt-0.5">Data: yfinance (NSE)</p>
        </div>
      </div>
    </aside>
    </>
  );
}
