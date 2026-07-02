import { Plus } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { usePermissions } from "@/lib/permissions";
import { CreatePrepaymentDialog } from "@/routes/counterparties/CreatePrepaymentDialog";
import { CreateEmployeePayoutDialog } from "@/routes/payroll/create-employee-payout-dialog";

/**
 * Кроссстраничная плавающая кнопка «+»: всплывающее меню действий над кнопкой (стопкой).
 * Пункты показываются по правам; кнопка скрыта, если ни одно действие недоступно.
 */
export function GlobalActionFab() {
  const permissions = usePermissions();
  const canPrepay = permissions.canPerformAction("invoices.normal.pay");
  const canEmployeePayout = permissions.canPerformAction("payroll.employee_payouts.create");

  const [prepayOpen, setPrepayOpen] = useState(false);
  const [payoutOpen, setPayoutOpen] = useState(false);

  if (!canPrepay && !canEmployeePayout) {
    return null;
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            aria-label="Создать"
            className="fixed bottom-6 right-6 z-40 h-14 w-14 rounded-full shadow-lg"
            size="icon"
            title="Создать"
            type="button"
          >
            <Plus size={24} aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-60" side="top" sideOffset={12}>
          <DropdownMenuLabel>Создать</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem disabled>
            Создать новый платёж
            <span className="ml-auto text-xs text-muted-foreground">скоро</span>
          </DropdownMenuItem>
          {canPrepay ? (
            <DropdownMenuItem onSelect={() => setPrepayOpen(true)}>
              Создать аванс поставщику
            </DropdownMenuItem>
          ) : null}
          {canEmployeePayout ? (
            <DropdownMenuItem onSelect={() => setPayoutOpen(true)}>
              Создать выплату сотруднику
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>

      {canPrepay ? (
        <CreatePrepaymentDialog onOpenChange={setPrepayOpen} open={prepayOpen} />
      ) : null}
      {canEmployeePayout ? (
        <CreateEmployeePayoutDialog onOpenChange={setPayoutOpen} open={payoutOpen} />
      ) : null}
    </>
  );
}
