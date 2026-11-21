"use client";

import { useCallback, useEffect, useState } from "react";
import { Pause, Play } from "lucide-react";
import { toast } from "sonner";
import { getMarketCalendarStatus, getPauseState, getSchedulerStatus, setPauseState } from "@/lib/api";
import { errorMessage } from "@/lib/utils";

type PauseState = { paused: boolean; since: string | null; reason: string | null };
type ExchangeStatus = {
  exchange: string;
  open_now: boolean;
  holiday_today: string | null;
  next_closure: { date: string; name: string | null; closed: string } | null;
};
type CalendarStatus = { exchanges: ExchangeStatus[] };
type SchedulerStatus = {
  enabled: boolean;
  running: boolean;
  waiting_for: string | null;
  last_bar_synced: Record<string, string>;
  last_results: Record<string, { status?: string }>;
};

function dataLine(s: SchedulerStatus): { text: string; warn: boolean } {
  if (!s.enabled || !s.running) return { text: "Data scheduler off", warn: true };
  if (s.waiting_for === "Kite login for today") return { text: "Waiting for today's Kite login", warn: true };
  if (s.waiting_for) return { text: `Data: waiting (${s.waiting_for})`, warn: false };
  const failed = Object.values(s.last_results).some((r) => r.status === "error");
  const latest = Object.values(s.last_bar_synced).sort().at(-1);
  if (failed) return { text: "Data: last sync had errors", warn: true };
  return { text: latest ? `Data up to ${latest.slice(11, 16)}` : "Data: starting", warn: false };
}

const REFRESH_MS = 60_000;

/** Pause/Resume switch and NSE/MCX open-closed status, shown on every page. */
export function SystemControl() {
  const [pause, setPause] = useState<PauseState | null>(null);
  const [calendar, setCalendar] = useState<CalendarStatus | null>(null);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchData = useCallback(() => {
    getPauseState()
      .then((data) => setPause(data as PauseState))
      .catch(() => setPause(null));
    getMarketCalendarStatus()
      .then((data) => setCalendar(data as CalendarStatus))
      .catch(() => setCalendar(null));
    getSchedulerStatus()
      .then((data) => setScheduler(data as SchedulerStatus))
      .catch(() => setScheduler(null));
  }, []);

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, REFRESH_MS);
    return () => clearInterval(timer);
  }, [fetchData]);

  const toggle = async () => {
    if (!pause) return;
    const next = !pause.paused;
    if (next && !window.confirm("Pause the system? New scans, signals and AI calls stop until you resume.")) return;
    setBusy(true);
    try {
      setPause((await setPauseState(next, next ? "manual" : undefined)) as PauseState);
      toast.success(next ? "System paused" : "System resumed");
    } catch (e) {
      toast.error(errorMessage(e, "Could not change the pause state"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      {pause?.paused && (
        <div className="rounded-md bg-red-600 px-2 py-1 text-center text-xs font-bold text-white">
          PAUSED{pause.since ? ` since ${pause.since.slice(11, 16)}` : ""}
        </div>
      )}
      <button
        type="button"
        onClick={toggle}
        disabled={!pause || busy}
        className={`flex w-full items-center justify-center gap-2 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
          pause?.paused
            ? "border-green-600 text-green-700 hover:bg-green-50 dark:text-green-400 dark:hover:bg-green-950"
            : "border-red-600 text-red-700 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
        }`}
      >
        {pause?.paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
        {pause?.paused ? "Resume system" : "Pause system"}
      </button>
      {calendar && (
        <div className="space-y-0.5 text-[11px]">
          {calendar.exchanges.map((ex) => (
            <div key={ex.exchange} className="flex items-center justify-between gap-2">
              <span className="font-medium">{ex.exchange}</span>
              <span className={ex.open_now ? "text-green-600" : "text-muted-foreground"}>
                {ex.open_now ? "Open" : ex.holiday_today ? `Holiday: ${ex.holiday_today}` : "Closed"}
              </span>
            </div>
          ))}
          {scheduler && (() => {
            const line = dataLine(scheduler);
            return (
              <p className={`text-[10px] ${line.warn ? "font-medium text-amber-600" : "text-muted-foreground"}`}>
                {line.text}
              </p>
            );
          })()}
          {calendar.exchanges.map((ex) =>
            ex.next_closure ? (
              <p key={`${ex.exchange}-next`} className="text-[10px] text-muted-foreground">
                Next {ex.exchange} closure: {ex.next_closure.date.slice(5)}
                {ex.next_closure.closed !== "full" ? ` (${ex.next_closure.closed})` : ""}
              </p>
            ) : null,
          )}
        </div>
      )}
    </div>
  );
}
