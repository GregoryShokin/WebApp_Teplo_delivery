import { Wallet } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { usePermissions } from "@/lib/permissions";
import { ActivePaymentsModal } from "@/routes/finance/ActivePaymentsModal";

/**
 * Вторая плавающая кнопка — активные платежи. Открывает модалку со всеми
 * незакрытыми платежами платёжного контура (готовы к оплате / к отправке в банк /
 * резервы Сейфа и Кассы). Стоит стопкой над кнопкой «Создать» («+»).
 * Видна тем, у кого есть доступ к контуру платежей (section «counterparties»).
 */
export function PaymentsFab() {
  const permissions = usePermissions();
  const [open, setOpen] = useState(false);

  if (!permissions.canOpenSection("counterparties")) {
    return null;
  }

  return (
    <>
      <Button
        aria-label="Активные платежи"
        className="fixed bottom-6 right-6 z-40 h-14 w-14 rounded-full shadow-lg"
        size="icon"
        title="Активные платежи"
        type="button"
        variant="secondary"
        onClick={() => setOpen(true)}
      >
        <Wallet size={24} aria-hidden="true" />
      </Button>
      <ActivePaymentsModal open={open} onOpenChange={setOpen} />
    </>
  );
}
