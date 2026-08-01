import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import { apiErrorMessage } from "@/lib/api";
import { todayIso } from "@/lib/date";

import {
  closeServiceAgreement,
  createServiceAgreement,
  getExpenseArticles,
  getServiceAgreements,
} from "./api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

const dateFormat = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

function fmtDate(value: string | null): string {
  if (!value) return "—";
  return dateFormat.format(new Date(`${value}T00:00:00`));
}

/**
 * Договоры услуг: сколько контрагент берёт в месяц и ждём ли от него документы.
 *
 * Это единственный способ сказать системе «услуга идёт каждый месяц, бумаги не будет, долг
 * считай сам» — по договору ночная джоба заводит обязательство и признаёт расход. Раньше такой
 * договор заводился только SQL, то есть механикой нельзя было пользоваться.
 *
 * Строк может быть несколько: у АО «АЙКО» две лицензии со своими ставками. Смена ставки —
 * закрытие строки и заведение новой (кнопка «Закрыть»), а не правка суммы: иначе уже
 * начисленные месяцы задним числом пересчитались бы по новой цене.
 */
export function ServiceAgreementsSection({
  counterpartyId,
  canAdmin,
}: {
  counterpartyId: string;
  canAdmin: boolean;
}) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [amount, setAmount] = useState("");
  const [articleId, setArticleId] = useState("");
  const [documentsMode, setDocumentsMode] = useState("informal");
  const [startedOn, setStartedOn] = useState(todayIso().slice(0, 8) + "01");

  const agreements = useQuery({
    queryKey: ["cp", "service-agreements", counterpartyId],
    queryFn: () => getServiceAgreements(counterpartyId),
  });
  const articles = useQuery({
    queryKey: ["cp", "expense-articles"],
    queryFn: getExpenseArticles,
    enabled: adding,
  });

  const reset = () => {
    setTitle("");
    setAmount("");
    setArticleId("");
    setDocumentsMode("informal");
    setAdding(false);
  };

  const create = useMutation({
    mutationFn: () =>
      createServiceAgreement(counterpartyId, {
        title: title.trim(),
        monthly_amount: Number(amount),
        dds_article_id: articleId,
        documents_mode: documentsMode,
        started_on: startedOn,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp", "service-agreements"] });
      await queryClient.invalidateQueries({ queryKey: ["accounting"] });
      toast.success("Договор заведён — начисление пойдёт с конца месяца");
      reset();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось завести договор")),
  });

  const close = useMutation({
    mutationFn: (id: string) => closeServiceAgreement(counterpartyId, id, todayIso()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp", "service-agreements"] });
      toast.success("Договор закрыт сегодняшним днём");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось закрыть договор")),
  });

  const rows = agreements.data ?? [];
  const canSave = Boolean(title.trim() && Number(amount) > 0 && articleId && startedOn);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">Договоры услуг</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Ежемесячная ставка. Если документов не будет — система сама начислит долг за каждый
            месяц и признает расход, не дожидаясь бумаг.
          </p>
        </div>
        {canAdmin && !adding ? (
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            Добавить договор
          </Button>
        ) : null}
      </div>

      {rows.length === 0 && !adding ? (
        <p className="text-sm text-muted-foreground">Договоров нет.</p>
      ) : null}

      <div className="space-y-2">
        {rows.map((row) => (
          <div
            key={row.id}
            className={`flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm ${
              row.is_active ? "" : "opacity-60"
            }`}
          >
            <span className="font-medium">{row.title}</span>
            <Badge variant="secondary">{money.format(row.monthly_amount)}/мес</Badge>
            <Badge variant="outline">
              {row.documents_mode === "official" ? "ждём документы" : "без документов"}
            </Badge>
            {!row.accrual_enabled ? <Badge variant="outline">начисление выключено</Badge> : null}
            <span className="text-xs text-muted-foreground">
              {row.dds_article_name ?? "без статьи"} · с {fmtDate(row.started_on)}
              {row.ended_on ? ` по ${fmtDate(row.ended_on)}` : ""}
            </span>
            {canAdmin && row.is_active ? (
              <Button
                size="sm"
                variant="ghost"
                className="ml-auto"
                disabled={close.isPending}
                onClick={() => close.mutate(row.id)}
              >
                Закрыть
              </Button>
            ) : null}
          </div>
        ))}
      </div>

      {adding ? (
        <div className="grid gap-3 rounded-md border p-3 sm:grid-cols-2">
          <div className="grid gap-1.5">
            <Label className="text-xs">Что оказывают</Label>
            <Input
              value={title}
              placeholder="Бухгалтерское обслуживание"
              onChange={(event) => setTitle(event.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Сумма в месяц, ₽</Label>
            <Input
              type="number"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Статья расхода</Label>
            <ArticleCombobox
              articles={articles.data ?? []}
              value={articleId}
              onChange={setArticleId}
            />
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Документы</Label>
            <Select value={documentsMode} onValueChange={setDocumentsMode}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="informal">Не будет — считаем долг сами</SelectItem>
                <SelectItem value="official">Приходят — ждём первичку</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs">Действует с</Label>
            <Input
              type="date"
              value={startedOn}
              onChange={(event) => setStartedOn(event.target.value)}
            />
          </div>
          <div className="flex items-end gap-2">
            <Button disabled={!canSave || create.isPending} onClick={() => create.mutate()}>
              Завести
            </Button>
            <Button variant="ghost" onClick={reset}>
              Отмена
            </Button>
          </div>
          {documentsMode === "official" ? (
            <p className="text-xs text-muted-foreground sm:col-span-2">
              Начисление по такому договору не делается: расход придёт с документом, а второе
              начисление удвоило бы его.
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
