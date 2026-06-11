import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Switch } from "@/components/ui/switch";
import { apiErrorMessage } from "@/lib/api";

import { confirmMatch, getMatchSuggestions } from "../api";
import { formatRub } from "../shared";

type RowState = { selected: boolean; enrich: boolean };

export function BankReconcileSection() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["cp", "match-suggestions"], queryFn: getMatchSuggestions });
  const suggestions = query.data ?? [];
  const [rows, setRows] = useState<Record<string, RowState>>({});

  useEffect(() => {
    const next: Record<string, RowState> = {};
    suggestions.forEach((item) => {
      next[item.invoice_id] = { selected: item.confident, enrich: true };
    });
    setRows(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data]);

  const confirmMutation = useMutation({
    mutationFn: async () => {
      let paid = 0;
      let enriched = 0;
      for (const item of suggestions) {
        const state = rows[item.invoice_id];
        if (!state?.selected) {
          continue;
        }
        try {
          const result = await confirmMatch({
            invoice_id: item.invoice_id,
            bank_operation_id: item.candidates[0].bank_operation_id,
            enrich: state.enrich,
          });
          if (result.payment_status === "paid") {
            paid += 1;
          }
          if (result.enriched) {
            enriched += 1;
          }
        } catch {
          // skip rows the backend rejects (e.g. operation already used)
        }
      }
      return { paid, enriched };
    },
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success(`Сверено: оплачено ${result.paid}, реквизиты обновлены ${result.enriched}`);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выполнить сверку")),
  });

  if (query.isLoading || suggestions.length === 0) {
    return null;
  }

  const selectedCount = Object.values(rows).filter((state) => state.selected).length;

  return (
    <div className="space-y-3 rounded-md border border-sky-200 bg-sky-50/40 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Сверка с T-Bank по суммам ({suggestions.length})</h3>
        <Button
          size="sm"
          disabled={selectedCount === 0 || confirmMutation.isPending}
          onClick={() => confirmMutation.mutate()}
        >
          {confirmMutation.isPending ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : (
            <Check size={16} aria-hidden="true" />
          )}
          Подтвердить выбранные ({selectedCount})
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Совпадения по точной сумме платежа в T-Bank. Подтверждение пометит накладную
        оплаченной и — при включённом тумблере — подставит официальное имя, ИНН и реквизиты
        получателя из банка. Карточные операции отфильтрованы.
      </p>
      <div className="grid gap-2">
        {suggestions.map((item) => {
          const candidate = item.candidates[0];
          const state = rows[item.invoice_id] ?? { selected: false, enrich: true };
          return (
            <div
              key={item.invoice_id}
              className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border bg-card p-2 text-sm"
            >
              <Checkbox
                checked={state.selected}
                onChange={() =>
                  setRows((prev) => ({
                    ...prev,
                    [item.invoice_id]: { ...state, selected: !state.selected },
                  }))
                }
                aria-label="Выбрать"
              />
              <span className="min-w-0">
                <span className="text-muted-foreground">{item.counterparty_name}</span>
                {" · счёт "}
                {item.invoice_number ?? "—"}
                {" · "}
                <span className="tabular-nums">{formatRub(item.invoice_amount)}</span>
              </span>
              <ArrowRight size={14} aria-hidden="true" className="text-muted-foreground" />
              <span className="font-medium">{candidate.official_name ?? "—"}</span>
              {candidate.inn ? <Badge variant="outline">ИНН {candidate.inn}</Badge> : null}
              {item.candidates.length > 1 ? (
                <span className="text-xs text-muted-foreground">
                  ({item.candidates.length} платежа)
                </span>
              ) : null}
              <label className="ml-auto flex items-center gap-2 text-xs">
                <Switch
                  checked={state.enrich}
                  onCheckedChange={(value) =>
                    setRows((prev) => ({
                      ...prev,
                      [item.invoice_id]: { ...state, enrich: value },
                    }))
                  }
                />
                обновить имя и реквизиты
              </label>
            </div>
          );
        })}
      </div>
    </div>
  );
}
