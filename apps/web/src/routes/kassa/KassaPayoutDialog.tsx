import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
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
import { apiErrorMessage } from "@/lib/api";
import { getRegistry } from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";
import {
  createKassaPayout,
  getKassaPayoutArticles,
  getKassaPayoutContext,
  getKassaPayoutEmployees,
  updateKassaPayout,
  type KassaJournalItem,
  type KassaPayoutFlow,
} from "@/routes/kassa/api";

const FLOW_HINTS: Partial<Record<KassaPayoutFlow, string>> = {
  employee_advance:
    "Выдача попадёт в леджер авансов — удержится из ближайшей зарплаты сотрудника.",
  employee_loan:
    "Выдача попадёт в леджер займов — будет удерживаться из зарплаты по рассрочке.",
  supplier_prepayment:
    "Появится дебиторка: поставщик должен привезти товар — накладные будут гаситься против этой предоплаты.",
};

/** Запись журнала, открытая на правку (свои сегодняшние кассовые записи). */
export type KassaPayoutEditTarget = {
  transactionId: string;
  articleId: string | null;
  amount: number;
  comment: string | null;
  employeeId: string | null;
  counterpartyId: string | null;
};

type KassaPayoutDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
  /** null — создание новой выплаты; иначе правка своей сегодняшней записи. */
  editTarget?: KassaPayoutEditTarget | null;
};

/** Форма-хамелеон «Выплата из кассы»: состав полей зависит от выбранной статьи. */
export function KassaPayoutDialog({
  open,
  onOpenChange,
  onSaved,
  editTarget = null,
}: KassaPayoutDialogProps) {
  const queryClient = useQueryClient();
  const [articleId, setArticleId] = useState("");
  const [amount, setAmount] = useState("");
  const [comment, setComment] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [counterpartyId, setCounterpartyId] = useState("");

  const isEdit = editTarget !== null;

  const articlesQuery = useQuery({
    queryKey: ["kassa", "payout-articles"],
    queryFn: getKassaPayoutArticles,
    enabled: open,
  });
  const contextQuery = useQuery({
    queryKey: ["kassa", "payout-context"],
    queryFn: getKassaPayoutContext,
    enabled: open,
  });

  const article = useMemo(
    () => (articlesQuery.data ?? []).find((item) => item.id === articleId) ?? null,
    [articlesQuery.data, articleId],
  );
  const flow: KassaPayoutFlow | null = article?.flow ?? null;
  const needsEmployee = flow === "employee_advance" || flow === "employee_loan";
  const needsSupplier = flow === "supplier_prepayment";

  const employeesQuery = useQuery({
    queryKey: ["kassa", "payout-employees"],
    queryFn: getKassaPayoutEmployees,
    enabled: open && needsEmployee,
  });
  const suppliersQuery = useQuery({
    queryKey: ["kassa", "payout-suppliers"],
    queryFn: () => getRegistry({ kassa_only: true }),
    enabled: open && needsSupplier,
  });

  // Предзаполнение при открытии: правка — из записи журнала, создание — с чистого листа.
  useEffect(() => {
    if (!open) {
      return;
    }
    setArticleId(editTarget?.articleId ?? "");
    setAmount(editTarget ? String(editTarget.amount) : "");
    setComment(editTarget?.comment ?? "");
    setEmployeeId(editTarget?.employeeId ?? "");
    setCounterpartyId(editTarget?.counterpartyId ?? "");
  }, [open, editTarget]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const payload = {
        article_id: articleId,
        amount: Number(amount),
        comment: comment.trim() || null,
        employee_id: needsEmployee ? employeeId || null : null,
        counterparty_id: needsSupplier ? counterpartyId || null : null,
      };
      return editTarget
        ? updateKassaPayout(editTarget.transactionId, payload)
        : createKassaPayout(payload);
    },
    onSuccess: () => {
      toast.success(isEdit ? "Выплата исправлена" : "Выплата проведена — запись в журнале");
      void queryClient.invalidateQueries({ queryKey: ["kassa"] });
      void queryClient.invalidateQueries({ queryKey: ["dds"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      onOpenChange(false);
      onSaved();
    },
    onError: (error) =>
      toast.error(
        apiErrorMessage(error, isEdit ? "Не удалось исправить выплату" : "Не удалось провести выплату"),
      ),
  });

  const amountNumber = Number(amount);
  const balance = contextQuery.data?.balance;
  const overBalance =
    typeof balance === "number" && Number.isFinite(amountNumber) && amountNumber > balance;
  const canSave =
    Boolean(articleId) &&
    Number.isFinite(amountNumber) &&
    amountNumber > 0 &&
    (!needsEmployee || Boolean(employeeId)) &&
    (!needsSupplier || Boolean(counterpartyId)) &&
    !saveMutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Изменить выплату из кассы" : "Выплата из кассы"}</DialogTitle>
          <DialogDescription>
            {contextQuery.data
              ? `Счёт: ${contextQuery.data.wallet_name} · в кассе ${formatRub(contextQuery.data.balance)}`
              : "Выдача наличных по разрешённой статье. Дата операции — сегодня."}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Статья</Label>
            <Select value={articleId} onValueChange={setArticleId}>
              <SelectTrigger>
                <SelectValue
                  placeholder={
                    articlesQuery.isLoading ? "Загрузка…" : "Выберите статью выплаты"
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {(articlesQuery.data ?? []).map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {!articlesQuery.isLoading && (articlesQuery.data ?? []).length === 0 ? (
              <p className="text-xs text-muted-foreground">
                Нет разрешённых статей — флаг «Доступна в кассе» включает владелец в каталоге
                статей ДДС.
              </p>
            ) : null}
          </div>

          {needsEmployee ? (
            <div className="grid gap-2">
              <Label>Сотрудник</Label>
              <Select value={employeeId} onValueChange={setEmployeeId}>
                <SelectTrigger>
                  <SelectValue
                    placeholder={employeesQuery.isLoading ? "Загрузка…" : "Выберите сотрудника"}
                  />
                </SelectTrigger>
                <SelectContent>
                  {(employeesQuery.data ?? []).map((employee) => (
                    <SelectItem key={employee.id} value={employee.id}>
                      {employee.full_name}
                      {employee.position ? ` · ${employee.position}` : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}

          {needsSupplier ? (
            <div className="grid gap-2">
              <Label>Поставщик</Label>
              <Select value={counterpartyId} onValueChange={setCounterpartyId}>
                <SelectTrigger>
                  <SelectValue
                    placeholder={suppliersQuery.isLoading ? "Загрузка…" : "Выберите поставщика"}
                  />
                </SelectTrigger>
                <SelectContent>
                  {(suppliersQuery.data ?? []).map((supplier) => (
                    <SelectItem key={supplier.counterparty_id} value={supplier.counterparty_id}>
                      {supplier.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!suppliersQuery.isLoading && (suppliersQuery.data ?? []).length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Нет поставщиков с флагом «Активен в Кассе» — его включает владелец в реестре
                  контрагентов.
                </p>
              ) : null}
            </div>
          ) : null}

          {flow && FLOW_HINTS[flow] ? (
            <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
              {FLOW_HINTS[flow]}
            </p>
          ) : null}

          <div className="grid gap-2">
            <Label>Сумма, ₽</Label>
            <Input
              type="number"
              min="0"
              step="0.01"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
            {overBalance ? (
              <p className="text-xs font-medium text-amber-600">
                Сумма больше учётного остатка кассы ({formatRub(balance)}) — выплата пройдёт,
                но остаток уйдёт в минус. Проверьте сумму.
              </p>
            ) : null}
          </div>

          <div className="grid gap-2">
            <Label>Комментарий</Label>
            <Input
              value={comment}
              placeholder="Назначение выплаты"
              onChange={(event) => setComment(event.target.value)}
            />
          </div>

          {isEdit && (needsEmployee || needsSupplier) ? (
            <p className="text-xs text-muted-foreground">
              Правка аванса или предоплаты проводится как отмена и новая выдача — у записи в
              журнале появится новое время создания.
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Отмена
          </Button>
          <Button disabled={!canSave} onClick={() => saveMutation.mutate()}>
            {saveMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            {isEdit ? "Сохранить" : "Выплатить"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Собрать цель правки из строки журнала (только для editable-записей). */
export function editTargetFromJournalItem(item: KassaJournalItem): KassaPayoutEditTarget {
  return {
    transactionId: item.id,
    articleId: item.article_id,
    amount: item.amount,
    comment: item.comment ?? item.purpose,
    employeeId: item.employee_id,
    counterpartyId: item.counterparty_id,
  };
}
