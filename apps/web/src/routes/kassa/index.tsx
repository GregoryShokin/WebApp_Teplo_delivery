import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { usePermissions } from "@/lib/permissions";
import { CreateChequeDialog } from "@/routes/kassa/CreateChequeDialog";
import { CreatePaymentChooser, type PaymentKind } from "@/routes/kassa/CreatePaymentChooser";
import { ShiftCloseTab } from "@/routes/kassa/tabs/shift-close";
import { CreateInvoiceDialog } from "@/routes/warehouse/CreateInvoiceDialog";

type DialogMode = "chooser" | PaymentKind | null;

export function KassaRoute() {
  const permissions = usePermissions();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<DialogMode>(null);

  const canCreateInvoice = permissions.canPerformAction("kassa.invoices.create");
  const canCreateCheque = permissions.canPerformAction("kassa.cheques.create");
  const canCreate = canCreateInvoice || canCreateCheque;

  function invalidateAfterCreate() {
    queryClient.invalidateQueries({ queryKey: ["kassa"] });
    queryClient.invalidateQueries({ queryKey: ["cp"] });
    queryClient.invalidateQueries({ queryKey: ["dds"] });
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Касса"
        description="Закрытие смены iiko и создание платежей — накладных и чеков."
        action={
          canCreate ? (
            <Button onClick={() => setMode("chooser")}>
              <Plus size={16} aria-hidden="true" />
              Создать платёж
            </Button>
          ) : null
        }
      />

      <ShiftCloseTab
        canSync={permissions.canPerformAction("kassa.shifts.sync")}
        canPost={permissions.canPerformAction("kassa.shifts.post")}
        canWaive={permissions.canPerformAction("kassa.penalty.waive")}
      />

      <CreatePaymentChooser
        open={mode === "chooser"}
        onOpenChange={(open) => setMode(open ? "chooser" : null)}
        canInvoice={canCreateInvoice}
        canCheque={canCreateCheque}
        onPick={(kind) => setMode(kind)}
      />

      <CreateInvoiceDialog
        open={mode === "invoice"}
        onOpenChange={(open) => setMode(open ? "invoice" : null)}
        onCreated={invalidateAfterCreate}
      />

      <CreateChequeDialog
        open={mode === "cheque"}
        onOpenChange={(open) => setMode(open ? "cheque" : null)}
        onCreated={invalidateAfterCreate}
      />
    </div>
  );
}
