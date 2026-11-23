// In-memory cache of the last GET /api/recommend/ result per (universe, minSignals).
// Shared by the Today tab's Top Picks card and the /recommendations page, so
// "View all" shows the same picks straight away instead of an empty page.
// Lives for the browser tab (client-side navigation); a full reload starts empty.

const MAX_AGE_MS = 15 * 60 * 1000;

type Entry = { data: unknown; at: number };

const cache = new Map<string, Entry>();

const key = (universe: string, minSignals: number) => `${universe}:${minSignals}`;

export function setCachedRecommendations(universe: string, minSignals: number, data: unknown) {
  cache.set(key(universe, minSignals), { data, at: Date.now() });
}

export function getCachedRecommendations<T>(universe: string, minSignals: number): { data: T; at: Date } | null {
  const entry = cache.get(key(universe, minSignals));
  if (!entry || Date.now() - entry.at > MAX_AGE_MS) return null;
  return { data: entry.data as T, at: new Date(entry.at) };
}
