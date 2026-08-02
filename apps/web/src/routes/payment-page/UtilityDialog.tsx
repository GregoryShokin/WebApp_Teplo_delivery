import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiErrorMessage, getUtilityAccounts } from "@/lib/api";
import { formatRub } from "@/routes/counterparties/shared";

import { confirmUtilityIntake, fetchIntakeFileUrl, type PaymentIntake } from "./api";
import { DocumentPreview } from "./DocumentPreview";

function Field({
  id,
  label,
  value,
  onChange,
  type = "text",
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  hint?: string;
}) {
  return (
    <div className="grid gap-1">
      {/* Подпись связана с полем явно: иначе она подпись только на вид — ни экранный диктор,
          ни клик по тексту до поля не доберутся. */}
      <Label htmlFor={id} className="text-xs text-muted-foreground">
        {label}
      </Label>
      <Input id={id} type={type} value={value} onChange={(e) => onChange(e.target.value)} />
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

/**
 * Разбор коммунальной платёжки: поток, период и ДВЕ суммы.
 *
 * Своё окно, а не поля в общем разборе счёта, потому что вопросы другие. У обычного счёта
 * оператор правит контрагента и банковские реквизиты; здесь контрагент известен из потока и
 * менять его в момент разбора нельзя — деньги уходят арендодателю, а не ресурсной организации,
 * напечатанной в квитанции.
 *
 * Сумм две, потому что в бумаге их две: у воды расход месяца и платёж совпадают, у акта за факт
 * нет — из потребления с потерями вычтен ранее внесённый аванс. Признать расход по сумме
 * платёжки значит потерять из прибыли ровно ту часть, что оплачена вперёд.
 */
export function UtilityDialog({
  intake,
  onClose,
}: {
  intake: PaymentIntake;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [fileUrl, setFileUrl] = useState<string | null>(null);
  const [accountId, setAccountId] = useState(intake.utility_account_id ?? "");
  const [periodStart, setPeriodStart] = useState(intake.service_period_start ?? "");
  const [periodEnd, setPeriodEnd] = useState(intake.service_period_end ?? "");
  const [payable, setPayable] = useState(
    intake.utility_payable_amount ?? intake.amount ?? "",
  );
  // Аванс — документ БЕЗ расхода. Отдельный переключатель, а не «оставьте поле пустым»: разница
  // между «расхода нет» и «расход забыли ввести» стоит месяца прибыли.
  const [isAdvance, setIsAdvance] = useState(intake.utility_act_kind === "advance");
  const [expense, setExpense] = useState(
    intake.utility_expense_amount ?? intake.utility_payable_amount ?? intake.amount ?? "",
  );

  const accountsQuery = useQuery({
    queryKey: ["utility-accounts", "active"],
    queryFn: () => getUtilityAccounts(true),
  });
  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data]);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    if (intake.has_pdf) {
      fetchIntakeFileUrl(intake.id)
        .then((u) => {
          if (cancelled) {
            URL.revokeObjectURL(u);
            return;
          }
          url = u;
          setFileUrl(u);
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [intake.id, intake.has_pdf]);

  const mutation = useMutation({
    mutationFn: () =>
      confirmUtilityIntake(intake.id, {
        utility_account_id: accountId,
        period_start: periodStart,
        period_end: periodEnd,
        expense_amount: isAdvance ? null : expense.trim(),
        payable_amount: payable.trim(),
      }),
    onSuccess: (item) => {
      void queryClient.invalidateQueries({ queryKey: ["payment-page", "intakes"] });
      toast.success(
        item.status === "duplicate"
          ? "За этот месяц документ уже заведён — строка помечена дублем"
          : "Проведено: счёт готов к оплате",
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести")),
  });

  const canConfirm =
    accountId !== "" &&
    periodStart !== "" &&
    periodEnd !== "" &&
    payable.trim() !== "" &&
    (isAdvance || expense.trim() !== "");

  const busy = mutation.isPending;
  const difference =
    !isAdvance && expense.trim() && payable.trim()
      ? Number(expense) - Number(payable)
      : 0;

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-5xl">
        <DialogHeader>
          <DialogTitle>
            Коммунальная платёжка{intake.utility_kind_label ? ` · ${intake.utility_kind_label}` : ""}
          </DialogTitle>
          <DialogDescription>
            Сверьте суммы со снимком. Счёт уйдёт в очередь оплат, расход встанет в месяц
            потребления и попадёт в прибыль той точки, за которой закреплён поток.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 md:grid-cols-2">
          <DocumentPreview url={fileUrl} mime={intake.attachment_mime} hasFile={intake.has_pdf} />

          <div className="grid max-h-[60vh] gap-3 overflow-auto pr-1">
            {intake.utility_blocking.length > 0 ? (
              <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
                <div className="font-medium">Разбор не завершён</div>
                <ul className="mt-1 list-inside list-disc">
                  {intake.utility_blocking.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {intake.utility_hints.length > 0 ? (
              <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
                {intake.utility_hints.join(". ")}
              </div>
            ) : null}

            <div className="grid gap-1">
              <Label className="text-xs text-muted-foreground">Поток (помещение и ресурс)</Label>
              <Select value={accountId} onValueChange={setAccountId}>
                <SelectTrigger>
                  <SelectValue placeholder="Выберите поток…" />
                </SelectTrigger>
                <SelectContent>
                  {accounts.map((account) => (
                    <SelectItem key={account.id} value={account.id}>
                      {account.kind_label} · {account.location_name} → {account.counterparty_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Из потока берутся получатель денег, статья расхода и помещение. В самой квитанции
                их нет: договор с ресурсником заключал арендодатель.
              </p>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              <Field
                id="utility-period-start"
                label="Период с"
                type="date"
                value={periodStart}
                onChange={setPeriodStart}
              />
              <Field
                id="utility-period-end"
                label="Период по"
                type="date"
                value={periodEnd}
                onChange={setPeriodEnd}
              />
            </div>
            <p className="-mt-2 text-xs text-muted-foreground">
              Месяц ПОТРЕБЛЕНИЯ, а не дата бумажки: расчёт за июнь, принесённый в июле, обязан
              лечь в июнь.
            </p>

            <div className="flex gap-2">
              <Button
                type="button"
                size="sm"
                variant={!isAdvance ? "default" : "outline"}
                onClick={() => setIsAdvance(false)}
              >
                За факт
              </Button>
              <Button
                type="button"
                size="sm"
                variant={isAdvance ? "default" : "outline"}
                onClick={() => setIsAdvance(true)}
              >
                Аванс
              </Button>
            </div>

            <Field
              id="utility-payable"
              label="К оплате, ₽"
              value={payable}
              onChange={setPayable}
              hint="Столько уйдёт деньгами."
            />
            {isAdvance ? (
              <p className="text-xs text-muted-foreground">
                Аванс расхода не несёт: его признает будущий акт за факт, а уплаченное до тех пор
                числится дебиторкой.
              </p>
            ) : (
              <Field
                id="utility-expense"
                label="Расход за период, ₽"
                value={expense}
                onChange={setExpense}
                hint={
                  difference > 0
                    ? `Больше платежа на ${formatRub(difference)} — на столько зачтён ранее внесённый аванс.`
                    : "У воды и газа совпадает с суммой к оплате."
                }
              />
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!canConfirm || busy}>
            {busy ? "Проводим…" : "Провести"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
