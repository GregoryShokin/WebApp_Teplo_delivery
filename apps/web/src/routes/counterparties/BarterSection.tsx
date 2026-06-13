import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, LoaderCircle, Wand2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { apiErrorMessage } from "@/lib/api";

import {
  autoSettleBarter,
  getBarterDetail,
  getBarterSuggestions,
  settleBarter,
  type BarterInvoice,
  type BarterSuggestion,
} from "./api";
import { formatRub } from "./shared";

function sumOf(invoices: BarterInvoice[], selected: Set<string>): number {
  return invoices
    .filter((invoice) => selected.has(invoice.id))
    .reduce((total, invoice) => total + invoice.amount, 0);
}

export function BarterSection({
  counterpartyId,
  canOperate,
}: {
  counterpartyId: string;
  canOperate: boolean;
}) {
  const queryClient = useQueryClient();
  const detailQuery = useQuery({
    queryKey: ["cp", "barter", counterpartyId],
    queryFn: () => getBarterDetail(counterpartyId),
  });
  const suggestQuery = useQuery({
    queryKey: ["cp", "barter-suggest", counterpartyId],
    queryFn: () => getBarterSuggestions(counterpartyId),
  });
  const [payableSel, setPayableSel] = useState<Set<string>>(new Set());
  const [receivableSel, setReceivableSel] = useState<Set<string>>(new Set());

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ["cp"] });
    setPayableSel(new Set());
    setReceivableSel(new Set());
  };

  const applySuggestion = (suggestion: BarterSuggestion) => {
    setPayableSel(new Set(suggestion.payable_ids));
    setReceivableSel(new Set(suggestion.receivable_ids));
  };

  const autoMutation = useMutation({
    mutationFn: () => autoSettleBarter(counterpartyId),
    onSuccess: async (result) => {
      await invalidate();
      toast.success(
        result.settled > 0 ? `Авто-зачёт: сведено ${result.settled}` : "Нечего сводить автоматом",
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выполнить авто-зачёт")),
  });

  const settleMutation = useMutation({
    mutationFn: () =>
      settleBarter(counterpartyId, {
        payable_ids: [...payableSel],
        receivable_ids: [...receivableSel],
      }),
    onSuccess: async () => {
      await invalidate();
      toast.success("Зачёт проведён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести зачёт")),
  });

  const detail = detailQuery.data;
  if (!detail) {
    return null;
  }

  const suggestions = suggestQuery.data ?? [];
  const numberById = new Map<string, string>();
  for (const invoice of [...detail.open_payables, ...detail.open_receivables]) {
    numberById.set(invoice.id, invoice.number ?? "—");
  }
  const suggestedIds = new Set(
    suggestions.flatMap((suggestion) => [...suggestion.payable_ids, ...suggestion.receivable_ids]),
  );

  const payableSum = sumOf(detail.open_payables, payableSel);
  const receivableSum = sumOf(detail.open_receivables, receivableSel);
  const canSettle =
    canOperate &&
    payableSel.size > 0 &&
    receivableSel.size > 0 &&
    Math.abs(payableSum - receivableSum) < 0.005;

  return (
    <section className="space-y-3 border-t pt-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Зачёт займов (бартер)</h3>
        {canOperate ? (
          <Button
            size="sm"
            variant="outline"
            disabled={autoMutation.isPending}
            onClick={() => autoMutation.mutate()}
          >
            {autoMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <Wand2 size={15} aria-hidden="true" />
            )}
            Авто-зачёт
          </Button>
        ) : null}
      </div>
      <p className="text-xs text-muted-foreground">
        Займы сводятся по совпадению суммы и номенклатуры (включая сводный возврат на сумму
        нескольких). Уверенные совпадения закрывает авто-зачёт; остальное отметьте вручную с обеих
        сторон — суммы должны совпасть.
      </p>

      {suggestions.length > 0 ? (
        <div className="space-y-1.5">
          <h4 className="text-xs font-medium uppercase text-muted-foreground">Предложенные пары</h4>
          {suggestions.map((suggestion, index) => {
            const payableNumbers = suggestion.payable_ids
              .map((id) => numberById.get(id) ?? "—")
              .join(", ");
            const receivableNumbers = suggestion.receivable_ids
              .map((id) => numberById.get(id) ?? "—")
              .join(", ");
            return (
              <div
                key={index}
                className="flex flex-wrap items-center gap-2 rounded-md border border-dashed p-2 text-xs"
              >
                <span className="tabular-nums font-medium">{formatRub(suggestion.amount)}</span>
                <span className="text-muted-foreground">
                  займы {payableNumbers} ↔ возвраты {receivableNumbers}
                </span>
                <Badge
                  variant="outline"
                  className={suggestion.confident ? "text-emerald-600" : "text-amber-600"}
                >
                  {suggestion.confident ? "уверенно" : "проверьте номенклатуру"}
                </Badge>
                {canOperate ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="ml-auto h-7"
                    onClick={() => applySuggestion(suggestion)}
                  >
                    Выбрать
                  </Button>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2">
        <BarterColumn
          title="Мы должны (займы нам)"
          invoices={detail.open_payables}
          selected={payableSel}
          suggested={suggestedIds}
          onToggle={(id) => setPayableSel((prev) => toggle(prev, id))}
          disabled={!canOperate}
        />
        <BarterColumn
          title="Нам должны (наши возвраты)"
          invoices={detail.open_receivables}
          selected={receivableSel}
          suggested={suggestedIds}
          onToggle={(id) => setReceivableSel((prev) => toggle(prev, id))}
          disabled={!canOperate}
        />
      </div>

      {payableSel.size > 0 || receivableSel.size > 0 ? (
        <div className="flex flex-wrap items-center gap-3 rounded-md bg-muted/50 p-2 text-sm">
          <span className="tabular-nums">
            Выбрано: {formatRub(payableSum)} ↔ {formatRub(receivableSum)}
          </span>
          {Math.abs(payableSum - receivableSum) >= 0.005 ? (
            <span className="text-xs text-amber-600">суммы не совпадают</span>
          ) : null}
          {canOperate ? (
            <Button
              size="sm"
              className="ml-auto"
              disabled={!canSettle || settleMutation.isPending}
              onClick={() => settleMutation.mutate()}
            >
              <Check size={15} aria-hidden="true" />
              Зачесть выбранное
            </Button>
          ) : null}
        </div>
      ) : null}

      {detail.settlements.length > 0 ? (
        <div className="space-y-1">
          <h4 className="text-xs font-medium uppercase text-muted-foreground">Проведённые зачёты</h4>
          {detail.settlements.map((settlement) => (
            <div
              key={settlement.id}
              className="flex flex-wrap items-center gap-2 rounded-md border p-2 text-xs"
            >
              <span className="tabular-nums font-medium">{formatRub(settlement.amount)}</span>
              <span className="text-muted-foreground">
                займы {settlement.payable_numbers.join(", ") || "—"} ↔ возвраты{" "}
                {settlement.receivable_numbers.join(", ") || "—"}
              </span>
              <Badge variant="outline" className="ml-auto">
                {settlement.is_auto ? "авто" : "вручную"}
              </Badge>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function toggle(prev: Set<string>, id: string): Set<string> {
  const next = new Set(prev);
  if (next.has(id)) {
    next.delete(id);
  } else {
    next.add(id);
  }
  return next;
}

function BarterColumn({
  title,
  invoices,
  selected,
  suggested,
  onToggle,
  disabled,
}: {
  title: string;
  invoices: BarterInvoice[];
  selected: Set<string>;
  suggested: Set<string>;
  onToggle: (id: string) => void;
  disabled: boolean;
}) {
  return (
    <div className="space-y-2">
      <h4 className="text-xs font-medium uppercase text-muted-foreground">{title}</h4>
      {invoices.length === 0 ? (
        <p className="text-sm text-muted-foreground">Открытых нет.</p>
      ) : (
        invoices.map((invoice) => (
          <label
            key={invoice.id}
            className={`flex items-start gap-2 rounded-md border p-2 text-sm ${
              suggested.has(invoice.id) ? "border-dashed border-primary/50 bg-primary/5" : ""
            }`}
          >
            <Checkbox
              checked={selected.has(invoice.id)}
              onChange={() => onToggle(invoice.id)}
              disabled={disabled}
              aria-label="Выбрать"
            />
            <span className="min-w-0 flex-1">
              <span className="flex justify-between gap-2">
                <span>счёт {invoice.number ?? "—"}</span>
                <span className="tabular-nums">{formatRub(invoice.amount)}</span>
              </span>
              {invoice.products.length > 0 ? (
                <span className="block truncate text-xs text-muted-foreground">
                  {invoice.products.filter(Boolean).join(", ")}
                </span>
              ) : null}
            </span>
          </label>
        ))
      )}
    </div>
  );
}
