"use client";

import { useCallback, useEffect, useState } from "react";
import { DatabaseBackup, Download, Loader2, Send } from "lucide-react";
import { toast } from "sonner";
import { backupDownloadUrl, listBackups, runBackup } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { errorMessage } from "@/lib/utils";

type BackupRow = { name: string; kind: "essentials" | "full"; created_at: string; bytes: number };
type BackupList = {
  enabled: boolean;
  folder: string;
  schedule: string;
  last_essentials_at: string | null;
  backups: BackupRow[];
};
type RunResult = { name: string; bytes: number; telegram?: { status: string; reason?: string } };

function size(bytes: number): string {
  return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`;
}

export function BackupsCard() {
  const [data, setData] = useState<BackupList | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const fetchData = useCallback(() => {
    listBackups()
      .then((res) => setData(res as BackupList))
      .catch((e) => toast.error(errorMessage(e, "Could not load backups")));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const run = async (kind: "essentials" | "full", toTelegram: boolean) => {
    setBusy(toTelegram ? "telegram" : kind);
    try {
      const result = (await runBackup({ kind, to_telegram: toTelegram })) as RunResult;
      const tg = result.telegram;
      if (tg && tg.status !== "sent") {
        toast.warning(`Backup saved (${size(result.bytes)}), Telegram: ${tg.reason ?? tg.status}`);
      } else {
        toast.success(`Backup saved (${size(result.bytes)})${tg ? " and sent to Telegram" : ""}`);
      }
      fetchData();
    } catch (e) {
      toast.error(errorMessage(e, "Backup failed"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-lg">
          <DatabaseBackup className="h-5 w-5" /> Backups
        </CardTitle>
        <CardDescription>
          {data?.schedule ?? "Weekly"}. <b>Essentials</b> (journal, trades, post-mortems, backtests, settings; no
          candles or option prices; API keys and credentials blank) is also sent to your Telegram chat as the
          off-server copy. <b>Full</b> (everything, credentials included) stays on the server: download it to keep a
          complete copy. Keeps 8 essentials and 2 full backups.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {data && !data.enabled && <Badge variant="outline">Automatic backups are off on this server</Badge>}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => run("essentials", true)} disabled={busy !== null}>
            {busy === "telegram" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Send className="mr-1 h-4 w-4" />}
            Back up now + send to Telegram
          </Button>
          <Button size="sm" variant="outline" onClick={() => run("full", false)} disabled={busy !== null}>
            {busy === "full" ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <DatabaseBackup className="mr-1 h-4 w-4" />}
            Full backup now
          </Button>
        </div>
        {data && data.backups.length === 0 && <p className="text-muted-foreground">No backups yet.</p>}
        {data && data.backups.length > 0 && (
          <table className="w-full text-xs">
            <tbody>
              {data.backups.map((b) => (
                <tr key={b.name} className="border-t">
                  <td className="py-1 pr-2">{b.created_at.replace("T", " ").slice(0, 16)}</td>
                  <td className="py-1 pr-2">
                    <Badge variant="outline">{b.kind}</Badge>
                  </td>
                  <td className="py-1 pr-2 text-right">{size(b.bytes)}</td>
                  <td className="py-1 text-right">
                    <a className="inline-flex items-center gap-1 text-primary hover:underline" href={backupDownloadUrl(b.name)}>
                      <Download className="h-3 w-3" /> Download
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="text-xs text-muted-foreground">
          Restore: stop the backend (<code>sudo systemctl stop ita-backend</code>), put{" "}
          <code>trading_agent.db</code>{" "}
          from the zip in <code>~/.tradingagents/</code>, then start it again. Each zip has a README with the steps.
        </p>
      </CardContent>
    </Card>
  );
}
