import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw, Wand2, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { apiErrorMessage } from "@/lib/api";

import {
  cancelDraft,
  getDrafts,
  getRegistry,
  runAutoMatch,
  syncInvoices,
  type PaymentDraft,
} from "../api";
import { DraftStatusBadge, formatDate, formatRub } from "../shared";
import { BankReconcileSection } from "./reconcile";

export function DraftsTab({
  canOperate,
  onOpenCounterparty,
}: {
  canOperate: boolean;
  onOpenCounterparty: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const draftsQuery = useQuery({ queryKey: ["cp", "drafts"], queryFn: () => getDrafts() });
  const registryQuery = useQuery({ queryKey: ["cp", "registry", "all"], queryFn: () => getRegistry() });

  const nameById = useMemo(() => {
    const map = new Map<string, string>();
    (registryQuery.data ?? []).forEach((item) => map.set(item.counterparty_id, item.name));
    return map;
  }, [registryQuery.data]);

  const matchMutation = useMutation({
    mutationFn: runAutoMatch,
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success(
        `Сопоставлено: ${result.matched}` +
          (result.needs_review.length ? `, требует подтверждения: ${result.needs_review.length}` : ""),
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выполнить мэчинг")),
  });

  const syncMutation = useMutation({
    mutationFn: syncInvoices,
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success(
        `Синхронизация iiko: новых ${result.invoices_created ?? 0}, обновлено ${result.invoices_updated ?? 0}`,
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Синхронизация с iiko не удалась")),
  });

  const cancelMutation = useMutation({
    mutationFn: cancelDraft,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Черновик отменён, накладные возвращены к оплате");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить черновик")),
  });

  const columns: Array<DataTableColumn<PaymentDraft>> = [
    {
      key: "counterparty",
      header: "Контрагент",
      className: "min-w-[200px]",
      cell: (draft) => (
        <button
          className="text-left font-medium hover:underline"
          onClick={() => onOpenCounterparty(draft.counterparty_id)}
          type="button"
        >
          {nameById.get(draft.counterparty_id) ?? "—"}
        </button>
      ),
    },
    { key: "document", header: "Документ", cell: (draft) => draft.document_id },
    {
      key: "amount",
      header: "Сумма",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (draft) => formatRub(draft.amount),
    },
    { key: "status", header: "Статус", cell: (draft) => <DraftStatusBadge status={draft.status} /> },
    { key: "created", header: "Создан", cell: (draft) => formatDate(draft.created_at) },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (draft) =>
        canOperate && draft.status !== "paid" ? (
          <Button
            onClick={() => cancelMutation.mutate(draft.id)}
            size="sm"
            variant="ghost"
            title="Отменить черновик"
          >
            <X size={15} aria-hidden="true" />
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          Отправка черновика ≠ оплата. После выписки из банка запустите мэчинг — накладные
          закроются автоматически.
        </p>
        {canOperate ? (
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => syncMutation.mutate()}
              variant="outline"
              disabled={syncMutation.isPending}
            >
              {syncMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <RefreshCw size={16} aria-hidden="true" />
              )}
              Синхронизировать iiko
            </Button>
            <Button onClick={() => matchMutation.mutate()} disabled={matchMutation.isPending}>
              {matchMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Wand2 size={16} aria-hidden="true" />
              )}
              Запустить мэчинг
            </Button>
          </div>
        ) : null}
      </div>

      {canOperate ? <BankReconcileSection /> : null}

      <DataTable
        columns={columns}
        rows={draftsQuery.data ?? []}
        isLoading={draftsQuery.isLoading}
        getRowKey={(draft) => draft.id}
        emptyMessage="Черновиков пока нет"
      />
    </div>
  );
}
