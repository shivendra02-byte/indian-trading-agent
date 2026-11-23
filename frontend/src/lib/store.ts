"use client";

import { create } from "zustand";
import { runAnalysis, connectAnalysisWS, getAnalysisResult, getRecentAnalysisTasks } from "@/lib/api";
import type { WSEvent } from "@/lib/types";

interface AnalysisOptions {
  analysts?: string[];
  max_debate_rounds?: number;
  max_risk_discuss_rounds?: number;
  output_language?: string;
}

interface AnalysisStats {
  llm_calls: number;
  tool_calls: number;
  tokens_in: number;
  tokens_out: number;
  total_tokens: number;
  cost_usd: number;
  cost_inr: number;
  per_model?: Record<string, { input: number; output: number }>;
}

// POST /api/analysis/run → backend/models.AnalysisResponse
type AnalysisStartResponse = { task_id: string; status: string; ticker: string; trade_date: string };

// GET /api/analysis/{task_id} (routers/analysis.get_analysis_result): the saved result when
// finished, else the status plus everything streamed so far.
type AnalysisProgress = {
  reports?: Record<string, string>;
  debates?: Partial<Record<"bull" | "bear", string>>;
  risk_debates?: Partial<Record<"aggressive" | "conservative" | "neutral", string>>;
  heartbeat?: string;
  signal?: string | null;
};
type AnalysisResultResponse = {
  task_id?: string;
  status?: "pending" | "running" | "completed" | "error";
  error?: string | null;
  ticker?: string;
  trade_date?: string;
  signal?: string | null;
  duration_seconds?: number | null;
  stats?: AnalysisStats | string | null;
  progress?: AnalysisProgress;
} & Partial<Record<(typeof REPORT_FIELDS)[number] | DebateField, string | null>>;

// GET /api/analysis/tasks/recent
type RecentTask = { task_id: string; ticker: string; trade_date: string; status: string };

const REPORT_FIELDS = [
  "market_report", "sentiment_report", "news_report", "fundamentals_report",
  "investment_plan", "trader_investment_plan", "final_trade_decision",
] as const;
type DebateField =
  | "bull_history" | "bear_history"
  | "risk_aggressive_history" | "risk_conservative_history" | "risk_neutral_history";

// The running analysis is remembered in the browser so it survives a reload
// (iPad Safari reloads a tab that was in the background while the screen was off).
const LAST_TASK_KEY = "trading-agent:analysis:last-task";
type LastTask = { taskId: string; ticker: string; tradeDate: string };

function saveLastTask(task: LastTask | null) {
  try {
    if (task) localStorage.setItem(LAST_TASK_KEY, JSON.stringify(task));
    else localStorage.removeItem(LAST_TASK_KEY);
  } catch {}
}

function loadLastTask(): LastTask | null {
  try {
    const raw = localStorage.getItem(LAST_TASK_KEY);
    return raw ? (JSON.parse(raw) as LastTask) : null;
  } catch {
    return null;
  }
}

function toStats(raw: AnalysisStats | string | null | undefined): AnalysisStats | null {
  if (!raw) return null;
  try {
    const s = (typeof raw === "string" ? JSON.parse(raw) : raw) as Partial<AnalysisStats>;
    return {
      llm_calls: s.llm_calls || 0,
      tool_calls: s.tool_calls || 0,
      tokens_in: s.tokens_in || 0,
      tokens_out: s.tokens_out || 0,
      total_tokens: s.total_tokens || 0,
      cost_usd: s.cost_usd || 0,
      cost_inr: s.cost_inr || 0,
      per_model: s.per_model,
    };
  } catch {
    return null;
  }
}

function nonEmpty(obj: Record<string, string | null | undefined>): Record<string, string> {
  return Object.fromEntries(Object.entries(obj).filter(([, v]) => !!v)) as Record<string, string>;
}

let resyncTimer: ReturnType<typeof setTimeout> | null = null;

interface AnalysisState {
  taskId: string | null;
  ticker: string;
  tradeDate: string;
  status: "idle" | "running" | "completed" | "error";
  reports: Record<string, string>;
  debates: { bull: string; bear: string };
  riskDebates: { aggressive: string; conservative: string; neutral: string };
  signal: string | null;
  error: string | null;
  duration: number | null;
  ws: WebSocket | null;
  heartbeat: string;
  lastUpdateAt: number;
  stats: AnalysisStats | null;

  start: (ticker: string, tradeDate: string, options?: AnalysisOptions) => Promise<void>;
  /** Catch up with an analysis after the page lost it (reload, screen off, dropped WebSocket). */
  resume: (taskId?: string) => Promise<void>;
  reset: () => void;
}

export const useAnalysisStore = create<AnalysisState>((set, get) => {
  // Open the live stream for a task. If it drops while the analysis is still running
  // (screen off, network blip), catch up from the server a few seconds later.
  const listen = (taskId: string) => {
    const old = get().ws;
    if (old) {
      old.onclose = null;
      try { old.close(); } catch {}
    }
    const ws = connectAnalysisWS(taskId, (event: WSEvent) => {
      if (get().taskId !== taskId) return;
      const state = get();
      switch (event.type) {
        case "heartbeat":
          set({ heartbeat: event.last_activity || `Processing chunk #${event.chunk}`, lastUpdateAt: Date.now() });
          break;
        case "report":
          set({ reports: { ...state.reports, [event.section!]: event.content! }, lastUpdateAt: Date.now() });
          break;
        case "debate":
          set({ debates: { ...state.debates, [event.side!]: event.content! }, lastUpdateAt: Date.now() });
          break;
        case "risk_debate":
          set({ riskDebates: { ...state.riskDebates, [event.side!]: event.content! }, lastUpdateAt: Date.now() });
          break;
        case "signal":
          set({ signal: event.decision!, lastUpdateAt: Date.now() });
          break;
        case "stats":
          set({ stats: toStats(event as unknown as AnalysisStats), lastUpdateAt: Date.now() });
          break;
        case "complete":
          ws.onclose = null;
          ws.close();
          set({
            status: "completed",
            duration: event.duration_seconds ?? null,
            ws: null,
            heartbeat: "Complete",
            stats: toStats(event.stats as AnalysisStats | undefined) ?? get().stats,
          });
          break;
        case "error":
          ws.onclose = null;
          ws.close();
          set({ status: "error", error: event.message ?? "Unknown error", ws: null });
          break;
      }
    });
    const onClose = ws.onclose;
    ws.onclose = (ev) => {
      onClose?.call(ws, ev);
      if (get().ws !== ws) return;
      set({ ws: null });
      if (get().status === "running" && get().taskId === taskId) {
        if (resyncTimer) clearTimeout(resyncTimer);
        resyncTimer = setTimeout(() => { void get().resume(taskId); }, 3000);
      }
    };
    set({ ws });
  };

  return {
  taskId: null,
  ticker: "",
  tradeDate: "",
  status: "idle",
  reports: {},
  debates: { bull: "", bear: "" },
  riskDebates: { aggressive: "", conservative: "", neutral: "" },
  signal: null,
  error: null,
  duration: null,
  ws: null,
  heartbeat: "",
  lastUpdateAt: 0,
  stats: null,

  start: async (ticker: string, tradeDate: string, options: AnalysisOptions = {}) => {
    // Close existing WS if any
    const existingWs = get().ws;
    if (existingWs) {
      existingWs.onclose = null;
      try { existingWs.close(); } catch {}
    }
    if (resyncTimer) clearTimeout(resyncTimer);

    set({
      taskId: null,
      ticker,
      tradeDate,
      status: "running",
      reports: {},
      debates: { bull: "", bear: "" },
      riskDebates: { aggressive: "", conservative: "", neutral: "" },
      signal: null,
      error: null,
      duration: null,
      ws: null,
      heartbeat: "Initializing pipeline...",
      lastUpdateAt: Date.now(),
      stats: null,
    });

    try {
      const result = (await runAnalysis({
        ticker,
        trade_date: tradeDate,
        analysts: options.analysts,
        max_debate_rounds: options.max_debate_rounds,
        max_risk_discuss_rounds: options.max_risk_discuss_rounds,
        output_language: options.output_language,
      })) as AnalysisStartResponse;
      const taskId = result.task_id;

      saveLastTask({ taskId, ticker, tradeDate });
      set({ taskId });
      listen(taskId);
    } catch (e) {
      set({ status: "error", error: (e as Error).message });
    }
  },

  resume: async (taskIdArg?: string) => {
    let taskId = taskIdArg ?? get().taskId ?? loadLastTask()?.taskId ?? null;
    if (!taskId) {
      // Nothing remembered in this browser: pick up an analysis still running on the server.
      try {
        const recent = (await getRecentAnalysisTasks()) as RecentTask[];
        taskId = recent.find((t) => t.status === "running" || t.status === "pending")?.task_id ?? null;
      } catch {}
      if (!taskId) return;
    }
    let res: AnalysisResultResponse;
    try {
      res = (await getAnalysisResult(taskId)) as AnalysisResultResponse;
    } catch {
      // Backend unreachable (iPad just woke up, VPN reconnecting): try again shortly.
      if (get().status === "running") {
        if (resyncTimer) clearTimeout(resyncTimer);
        resyncTimer = setTimeout(() => { void get().resume(taskId!); }, 5000);
      }
      return;
    }
    if (!res || (res as { error?: string }).error === "Analysis not found") {
      if (loadLastTask()?.taskId === taskId) saveLastTask(null);
      if (get().taskId === taskId && get().status === "running") {
        set({
          status: "error", ws: null,
          error: "This analysis is no longer on the server (the backend restarted before it finished). Run it again.",
        });
      }
      return;
    }
    const remembered = loadLastTask();
    const ticker = res.ticker || (remembered?.taskId === taskId ? remembered.ticker : "") || get().ticker;
    const tradeDate = res.trade_date || (remembered?.taskId === taskId ? remembered.tradeDate : "") || get().tradeDate;
    const status = res.status ?? (res.signal !== undefined ? "completed" : "running");

    if (status === "completed") {
      const ws = get().ws;
      if (ws) { ws.onclose = null; try { ws.close(); } catch {} }
      set({
        taskId, ticker, tradeDate, status: "completed", ws: null, error: null, heartbeat: "Complete",
        reports: nonEmpty(Object.fromEntries(REPORT_FIELDS.map((f) => [f, res[f]]))),
        debates: { bull: res.bull_history || "", bear: res.bear_history || "" },
        riskDebates: {
          aggressive: res.risk_aggressive_history || "",
          conservative: res.risk_conservative_history || "",
          neutral: res.risk_neutral_history || "",
        },
        signal: res.signal ?? null,
        duration: res.duration_seconds ?? null,
        stats: toStats(res.stats),
      });
      return;
    }
    if (status === "error") {
      const ws = get().ws;
      if (ws) { ws.onclose = null; try { ws.close(); } catch {} }
      set({ taskId, ticker, tradeDate, status: "error", ws: null, error: res.error || "The analysis failed." });
      return;
    }
    // Still running: show what has been produced so far (keeping anything newer that
    // already arrived on the stream) and reopen the stream.
    const p = res.progress ?? {};
    const same = get().taskId === taskId;
    const cur = get();
    set({
      taskId, ticker, tradeDate, status: "running", error: null,
      reports: { ...(p.reports ?? {}), ...(same ? cur.reports : {}) },
      debates: {
        bull: (same && cur.debates.bull) || p.debates?.bull || "",
        bear: (same && cur.debates.bear) || p.debates?.bear || "",
      },
      riskDebates: {
        aggressive: (same && cur.riskDebates.aggressive) || p.risk_debates?.aggressive || "",
        conservative: (same && cur.riskDebates.conservative) || p.risk_debates?.conservative || "",
        neutral: (same && cur.riskDebates.neutral) || p.risk_debates?.neutral || "",
      },
      signal: (same && cur.signal) || p.signal || null,
      heartbeat: p.heartbeat || (same ? cur.heartbeat : "") || "Reconnected, waiting for the next step...",
      lastUpdateAt: Date.now(),
    });
    if (!get().ws || get().ws!.readyState > WebSocket.OPEN) listen(taskId);
  },

  reset: () => {
    if (resyncTimer) clearTimeout(resyncTimer);
    saveLastTask(null);
    const ws = get().ws;
    if (ws) {
      ws.onclose = null;
      try { ws.close(); } catch {}
    }
    set({
      taskId: null,
      ticker: "",
      tradeDate: "",
      status: "idle",
      reports: {},
      debates: { bull: "", bear: "" },
      riskDebates: { aggressive: "", conservative: "", neutral: "" },
      signal: null,
      error: null,
      duration: null,
      ws: null,
      heartbeat: "",
      lastUpdateAt: 0,
      stats: null,
    });
  },
};
});
