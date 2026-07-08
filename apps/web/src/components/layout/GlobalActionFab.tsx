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
import { EmployeePayoutDialog } from "@/routes/dds/EmployeePayoutDialog";
import { NewPaymentDialog } from "@/routes/dds/NewPaymentDialog";

// Пресет построчного конструктора: пункт FAB предвыбирает статью первой строки.
const PRESET_SUPPLIER_PREPAYMENT = "advance_to_supplier";

/**
 * Кроссстраничная плавающая кнопка «+»: всплывающее меню действий над кнопкой (стопкой).
 * Пункты показываются по правам; кнопка скрыта, если ни одно действие недоступно.
 * Все пункты ведут в единое окно «Новый платёж» (драйвер — статья ДДС).
 */
export function GlobalActionFab() {
  const permissions = usePermissions();
  const canNewPayment = permissions.canPerformAction("payments.create");
  const canPrepay = permissions.canPerformAction("invoices.normal.pay");
  const canEmployeePayout = permissions.canPerformAction("payroll.employee_payouts.create");

  const [paymentOpen, setPaymentOpen] = useState(false);
  const [payoutOpen, setPayoutOpen] = useState(false);
  const [presetArticleCode, setPresetArticleCode] = useState<string | null>(null);

  if (!canNewPayment) {
    return null;
  }

  function openPayment(preset: string | null) {
    setPresetArticleCode(preset);
    setPaymentOpen(true);
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
          <DropdownMenuItem onSelect={() => openPayment(null)}>
            Создать новый платёж
          </DropdownMenuItem>
          {canPrepay ? (
            <DropdownMenuItem onSelect={() => openPayment(PRESET_SUPPLIER_PREPAYMENT)}>
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

      <NewPaymentDialog
        onOpenChange={setPaymentOpen}
        open={paymentOpen}
        presetArticleCode={presetArticleCode}
      />
      <EmployeePayoutDialog onOpenChange={setPayoutOpen} open={payoutOpen} />
    </>
  );
}
