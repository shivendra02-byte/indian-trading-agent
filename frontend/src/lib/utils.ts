import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** The message of a thrown value, or `fallback` when it has none. */
export function errorMessage(error: unknown, fallback: string): string {
  return (error instanceof Error && error.message) || fallback;
}

/** How a setup is shown (25 Nov 2026): a breakout short is a "breakdown"; the stored setup stays "breakout". */
export function setupName(setup: string | null | undefined, direction?: string | null): string {
  if (!setup) return "";
  if (setup === "breakout" && direction === "short") return "breakdown";
  if (setup === "mcx_trend" || setup === "trend_pullback") return "trend pullback";
  return setup.replace(/_/g, " ");
}
