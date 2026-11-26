"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw, Upload } from "lucide-react";
import { toast } from "sonner";
import { getCashUniverse, refreshNifty500, uploadNifty500, uploadSurveillance } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/utils";

// Shape from backend/marketdata/cash_universe.py summary() (asked 26 Nov 2026)
type Summary = {
  symbols: number;
  refreshed_on: string | null;
  universe: number;
  fno: number;
  nifty500_extra: number;
  surveillance: { status: string; trade_date?: string };
  skipped_surveillance: string[];
};

function UploadButton({ label, onFile, disabled }: { label: string; onFile: (f: File) => void; disabled: boolean }) {
  return (
    <label className={`inline-flex ${disabled ? "pointer-events-none opacity-50" : "cursor-pointer"}`}>
      <input
        type="file"
        accept=".csv,text/csv"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFile(f);
          e.target.value = "";
        }}
      />
      <span className="inline-flex items-center rounded-md border px-2 py-1 text-xs hover:bg-muted">
        <Upload className="mr-1 h-3 w-3" /> {label}
      </span>
    </label>
  );
}

export function CashUniverseCard() {
  const [s, setS] = useState<Summary | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchData = useCallback(() => {
    getCashUniverse()
      .then((d) => setS(d as Summary))
      .catch(() => setS(null));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      const res = (await fn()) as { status?: string; errors?: string[] };
      if (res.status === "error") toast.error(`Download failed: ${(res.errors || []).join("; ")} — upload the file instead`);
      else toast.success(ok);
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Stocks scanned: F&amp;O + NIFTY 500</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {s ? (
          <>
            <p>
              {s.universe} stocks: {s.fno} F&amp;O + {s.nifty500_extra} more from the NIFTY 500 (list of {s.symbols}
              {s.refreshed_on ? `, updated ${s.refreshed_on}` : ", not downloaded yet"}). NIFTY 500 stocks outside F&amp;O need
              ₹20 cr daily turnover and use daily bars only.
            </p>
            <p className="text-xs text-muted-foreground">
              ASM/GSM surveillance list:{" "}
              {s.surveillance.status === "ok"
                ? `${s.surveillance.trade_date} · ${s.skipped_surveillance.length} stocks skipped${
                    s.skipped_surveillance.length ? ` (${s.skipped_surveillance.slice(0, 12).join(", ")}${s.skipped_surveillance.length > 12 ? "…" : ""})` : ""
                  }`
                : "not available — nothing is skipped until it is downloaded or uploaded"}
            </p>
          </>
        ) : (
          <p className="text-muted-foreground">Loading…</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" disabled={busy} onClick={() => act(refreshNifty500, "NIFTY 500 list updated")}>
            {busy ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <RefreshCw className="mr-1 h-3 w-3" />} Download NIFTY 500 list
          </Button>
          <UploadButton label="Upload NIFTY 500 CSV" disabled={busy} onFile={(f) => act(() => uploadNifty500(f), "NIFTY 500 list stored")} />
          <UploadButton
            label="Upload ASM/GSM file (REG1_IND)"
            disabled={busy}
            onFile={(f) => act(() => uploadSurveillance(f), "Surveillance list stored")}
          />
        </div>
      </CardContent>
    </Card>
  );
}
