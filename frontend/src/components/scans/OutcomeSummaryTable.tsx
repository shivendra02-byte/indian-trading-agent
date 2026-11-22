export type OutcomeSummary = {
  n: number;
  win_rate?: number;
  hit_1r?: number;
  hit_2r?: number;
  avg_r?: number;
  pf?: number | null;
  small_sample?: boolean;
  avg_ret_5?: number | null;
  avg_ret_10?: number | null;
  avg_ret_20?: number | null;
};

export function num(v: number | null | undefined, digits = 2, suffix = ""): string {
  return v === null || v === undefined ? "—" : `${v.toFixed(digits)}${suffix}`;
}

export function tone(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v > 0 ? "text-emerald-600" : v < 0 ? "text-red-600" : "";
}

/** Record of tracked alerts or ideas: one row per group, * = fewer than 30. */
export function OutcomeSummaryTable({ rows, showMoves = true }: { rows: [string, OutcomeSummary][]; showMoves?: boolean }) {
  const heads = ["Group", "Count", "Ended +", "Reached 1R", "Reached 2R", "Avg", "PF", ...(showMoves ? ["10-day move", "20-day move"] : [])];
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr>
            {heads.map((h) => (
              <th key={h} className={`px-2 py-1 font-medium ${h === "Group" ? "text-left" : "text-right"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, s]) => (
            <tr key={name} className="border-t">
              <td className="px-2 py-1">{name}</td>
              <td className="px-2 py-1 text-right">
                {s.n}
                {s.small_sample ? <span className="text-amber-500"> *</span> : null}
              </td>
              <td className="px-2 py-1 text-right">{num(s.win_rate, 1, "%")}</td>
              <td className="px-2 py-1 text-right">{num(s.hit_1r, 1, "%")}</td>
              <td className="px-2 py-1 text-right">{num(s.hit_2r, 1, "%")}</td>
              <td className={`px-2 py-1 text-right ${tone(s.avg_r)}`}>{num(s.avg_r, 3, "R")}</td>
              <td className="px-2 py-1 text-right">{s.pf ?? "—"}</td>
              {showMoves && <td className={`px-2 py-1 text-right ${tone(s.avg_ret_10)}`}>{num(s.avg_ret_10, 2, "%")}</td>}
              {showMoves && <td className={`px-2 py-1 text-right ${tone(s.avg_ret_20)}`}>{num(s.avg_ret_20, 2, "%")}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
