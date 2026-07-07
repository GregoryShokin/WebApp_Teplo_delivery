import type { ReactNode } from "react";
import { ArrowDownToLine, Banknote, FileText, Receipt } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export type PaymentKind = "invoice" | "cheque" | "payout" | "payin";

type CreatePaymentChooserProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  canInvoice: boolean;
  canCheque: boolean;
  canPayout: boolean;
  onPick: (kind: PaymentKind) => void;
};

export function CreatePaymentChooser({
  open,
  onOpenChange,
  canInvoice,
  canCheque,
  canPayout,
  onPick,
}: CreatePaymentChooserProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Создать операцию</DialogTitle>
          <DialogDescription>Выберите, что создать.</DialogDescription>
        </DialogHeader>

        <div className="grid gap-3">
          {canInvoice ? (
            <ChoiceCard
              icon={<FileText size={20} aria-hidden="true" />}
              title="Накладная"
              description="Обязательство к оплате поставщику (с разнесением и отправкой в банк)."
              onClick={() => onPick("invoice")}
            />
          ) : null}
          {canCheque ? (
            <ChoiceCard
              icon={<Receipt size={20} aria-hidden="true" />}
              title="Чек"
              description="Уже оплаченная картой покупка — уходит в ДДС по выбранной статье."
              onClick={() => onPick("cheque")}
            />
          ) : null}
          {canPayout ? (
            <ChoiceCard
              icon={<Banknote size={20} aria-hidden="true" />}
              title="Выплата из кассы"
              description="Выдача наличных по разрешённой статье: аренда, авансы, расчёты."
              onClick={() => onPick("payout")}
            />
          ) : null}
          {canPayout ? (
            <ChoiceCard
              icon={<ArrowDownToLine size={20} aria-hidden="true" />}
              title="Внести деньги"
              description="Приход наличных в кассу: за масло, взнос собственника, деньги от партнёров."
              onClick={() => onPick("payin")}
            />
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ChoiceCard({
  icon,
  title,
  description,
  onClick,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-start gap-3 rounded-lg border p-4 text-left transition-colors hover:border-primary hover:bg-accent"
    >
      <span className="mt-0.5 text-muted-foreground">{icon}</span>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-foreground">{title}</span>
        <span className="mt-0.5 block text-sm text-muted-foreground">{description}</span>
      </span>
    </button>
  );
}
