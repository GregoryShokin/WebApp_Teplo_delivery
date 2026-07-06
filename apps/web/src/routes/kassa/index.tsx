import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { usePermissions } from "@/lib/permissions";
import { getKassaPending } from "@/routes/kassa/api";
import { CreateChequeDialog } from "@/routes/kassa/CreateChequeDialog";
import { CreatePaymentChooser, type PaymentKind } from "@/routes/kassa/CreatePaymentChooser";
import { KassaPayoutDialog } from "@/routes/kassa/KassaPayoutDialog";
import { KassaInvoicesTab } from "@/routes/kassa/tabs/invoices";
import { KassaJournalTab } from "@/routes/kassa/tabs/journal";
import { KassaPendingTab } from "@/routes/kassa/tabs/pending";
import { ShiftCloseTab } from "@/routes/kassa/tabs/shift-close";
import { CreateInvoiceDialog } from "@/routes/warehouse/CreateInvoiceDialog";

type DialogMode = "chooser" | PaymentKind | null;
type KassaTab = "shifts" | "invoices" | "journal" | "pending";

export function KassaRoute() {
  const permissions = usePermissions();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<DialogMode>(null);
  const [tab, setTab] = useState<KassaTab>("shifts");

  const canCreateInvoice = permissions.canPerformAction("kassa.invoices.create");
  const canCreateCheque = permissions.canPerformAction("kassa.cheques.create");
  const canPayout = permissions.canPerformAction("kassa.payouts.create");
  const canJournal = permissions.hasPermission("kassa.journal.read");
  const canCreate = canCreateInvoice || canCreateCheque || canPayout;

  // Бейдж-счётчик «К выдаче»: целёвки в кассе + ожидающие разрешения на авансы/займы.
  const pendingQuery = useQuery({
    queryKey: ["kassa", "pending"],
    queryFn: getKassaPending,
    enabled: canPayout,
  });
  const pendingCount = pendingQuery.data?.pending_count ?? 0;

  function invalidateAfterCreate() {
    queryClient.invalidateQueries({ queryKey: ["kassa"] });
    queryClient.invalidateQueries({ queryKey: ["cp"] });
    queryClient.invalidateQueries({ queryKey: ["dds"] });
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Касса"
        description="Закрытие смены iiko, создание платежей и кассовый журнал."
        action={
          canCreate ? (
            <Button onClick={() => setMode("chooser")}>
              <Plus size={16} aria-hidden="true" />
              Создать платёж
            </Button>
          ) : null
        }
      />

      <Tabs value={tab} onValueChange={(value) => setTab(value as KassaTab)}>
        <TabsList>
          <TabsTrigger value="shifts">Смены</TabsTrigger>
          <TabsTrigger value="invoices">Накладные</TabsTrigger>
          {canJournal ? <TabsTrigger value="journal">Кассовый журнал</TabsTrigger> : null}
          {canPayout ? (
            <TabsTrigger value="pending" className="gap-1.5">
              К выдаче
              {pendingCount > 0 ? (
                <Badge className="border-amber-200 bg-amber-50 text-amber-700 tabular-nums">
                  {pendingCount}
                </Badge>
              ) : null}
            </TabsTrigger>
          ) : null}
        </TabsList>
      </Tabs>

      {tab === "shifts" ? (
        <ShiftCloseTab
          canSync={permissions.canPerformAction("kassa.shifts.sync")}
          canPost={permissions.canPerformAction("kassa.shifts.post")}
          canWaive={permissions.canPerformAction("kassa.penalty.waive")}
        />
      ) : null}

      {tab === "invoices" ? <KassaInvoicesTab canPay={canCreateInvoice} /> : null}

      {tab === "journal" && canJournal ? <KassaJournalTab canPayout={canPayout} /> : null}

      {tab === "pending" && canPayout ? <KassaPendingTab /> : null}

      <CreatePaymentChooser
        open={mode === "chooser"}
        onOpenChange={(open) => setMode(open ? "chooser" : null)}
        canInvoice={canCreateInvoice}
        canCheque={canCreateCheque}
        canPayout={canPayout}
        onPick={(kind) => setMode(kind)}
      />

      <CreateInvoiceDialog
        open={mode === "invoice"}
        onOpenChange={(open) => setMode(open ? "invoice" : null)}
        onCreated={invalidateAfterCreate}
        kassaOnly
      />

      <CreateChequeDialog
        open={mode === "cheque"}
        onOpenChange={(open) => setMode(open ? "cheque" : null)}
        onCreated={invalidateAfterCreate}
      />

      <KassaPayoutDialog
        open={mode === "payout"}
        onOpenChange={(open) => setMode(open ? "payout" : null)}
        onSaved={() => {
          // Запись сразу видна в журнале — переключаем на него после создания.
          if (canJournal) {
            setTab("journal");
          }
        }}
      />
    </div>
  );
}
