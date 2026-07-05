import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { usePermissions } from "@/lib/permissions";
import { CreateChequeDialog } from "@/routes/kassa/CreateChequeDialog";
import { CreatePaymentChooser, type PaymentKind } from "@/routes/kassa/CreatePaymentChooser";
import { KassaPayoutDialog } from "@/routes/kassa/KassaPayoutDialog";
import { KassaInvoicesTab } from "@/routes/kassa/tabs/invoices";
import { KassaJournalTab } from "@/routes/kassa/tabs/journal";
import { ShiftCloseTab } from "@/routes/kassa/tabs/shift-close";
import { CreateInvoiceDialog } from "@/routes/warehouse/CreateInvoiceDialog";

type DialogMode = "chooser" | PaymentKind | null;
type KassaTab = "shifts" | "invoices" | "journal";

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
