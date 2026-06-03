import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  LoaderCircle,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
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
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { InventoryGroupBadge } from "@/routes/payroll/inventory-positions";
import {
  apiErrorMessage,
  applyInventoryAudit,
  cancelInventoryAudit,
  computeInventoryAudit,
  createManualInventoryAudit,
  getIikoCandidates,
  getInventoryAudit,
  getInventoryAudits,
  getInventoryPositions,
  importInventoryAuditFromIiko,
  type InventoryAudit,
  type InventoryAllocationGroup,
  type InventoryAuditStatus,
  type InventoryEmployeePenalty,
  type InventoryGroupSnapshot,
  type IikoCandidate,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type InventoryAuditsRouteProps = {
  onNavigate: (path: string) => void;
};

type ManualRow = {
  position_id: string;
  shortage_amount: string;
};

type ConfirmationTarget = { action: "apply" | "cancel"; audit: InventoryAudit } | null;

const statusLabels: Record<InventoryAuditStatus, string> = {
  draft: "Черновик",
  applied: "Применён",
  cancelled: "Отменён",
};

const groupOrder: Array<InventoryAllocationGroup> = ["chefs", "common", "admins"];

export function InventoryAuditsRoute({ onNavigate }: InventoryAuditsRouteProps) {
  void onNavigate;
  const queryClient = useQueryClient();
  const month = currentMonthRange();
  const [dateFrom, setDateFrom] = useState(month.start);
  const [dateTo, setDateTo] = useState(month.end);
  const [statusFilter, setStatusFilter] = useState<InventoryAuditStatus | "all">("all");
  const [selectedAuditId, setSelectedAuditId] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [importDate, setImportDate] = useState(previousMondayKey());
  const [iikoCandidates, setIikoCandidates] = useState<IikoCandidate[]>([]);
  const [selectedDocumentId, setSelectedDocumentId] = useState("");
  const [manualDate, setManualDate] = useState(todayKey());
  const [manualRows, setManualRows] = useState<ManualRow[]>([
    { position_id: "", shortage_amount: "" },
  ]);
  const [confirmation, setConfirmation] = useState<ConfirmationTarget>(null);

  const auditsQuery = useQuery({
    queryKey: ["inventory-audits", dateFrom, dateTo, statusFilter],
    queryFn: () => getInventoryAudits({ dateFrom, dateTo, status: statusFilter }),
  });
  const positionsQuery = useQuery({
    queryKey: ["inventory-positions", "active"],
    queryFn: () => getInventoryPositions(false),
  });
  const selectedAuditQuery = useQuery({
    queryKey: ["inventory-audit", selectedAuditId],
    queryFn: () => getInventoryAudit(selectedAuditId ?? ""),
    enabled: Boolean(selectedAuditId),
  });

  const candidatesMutation = useMutation({
    mutationFn: getIikoCandidates,
    onSuccess: (candidates) => {
      setIikoCandidates(candidates);
      setSelectedDocumentId(candidates.length === 1 ? candidates[0].document_id : "");
      if (!candidates.length) {
        toast.info("Документов инвентаризации за эту дату не найдено");
      }
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось найти документы инвентаризации")),
  });

  const importMutation = useMutation({
    mutationFn: importInventoryAuditFromIiko,
    onSuccess: async (audit) => {
      toast.success("Ревизия импортирована из iiko");
      setImportOpen(false);
      setIikoCandidates([]);
      setSelectedDocumentId("");
      setSelectedAuditId(audit.id);
      await invalidateInventory(queryClient, audit.id);
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Документ инвентаризации не найден в iiko за эту дату")),
  });

  const manualMutation = useMutation({
    mutationFn: createManualInventoryAudit,
    onSuccess: async (audit) => {
      toast.success("Ревизия сохранена как черновик");
      setManualOpen(false);
      setSelectedAuditId(audit.id);
      setManualRows([{ position_id: "", shortage_amount: "" }]);
      await invalidateInventory(queryClient, audit.id);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить ревизию")),
  });

  const computeMutation = useMutation({
    mutationFn: computeInventoryAudit,
    onSuccess: async (_computation, auditId) => {
      toast.success("Расчёт обновлён");
      await invalidateInventory(queryClient, auditId);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось пересчитать ревизию")),
  });

  const applyMutation = useMutation({
    mutationFn: applyInventoryAudit,
    onSuccess: async (audit) => {
      toast.success(
        `Штрафы применены к payroll-периоду с ${formatDate(
          payrollPeriodStartForAudit(audit.business_date),
        )}`,
      );
      setConfirmation(null);
      await Promise.all([
        invalidateInventory(queryClient, audit.id),
        queryClient.invalidateQueries({ queryKey: ["payroll-adjustments"] }),
      ]);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось применить ревизию")),
  });

  const cancelMutation = useMutation({
    mutationFn: cancelInventoryAudit,
    onSuccess: async (audit) => {
      toast.success("Ревизия отменена");
      setConfirmation(null);
      await Promise.all([
        invalidateInventory(queryClient, audit.id),
        queryClient.invalidateQueries({ queryKey: ["payroll-adjustments"] }),
      ]);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить ревизию")),
  });

  const audits = auditsQuery.data ?? [];
  const selectedAudit = selectedAuditQuery.data ?? null;
  const positions = positionsQuery.data ?? [];

  const columns: Array<DataTableColumn<InventoryAudit>> = [
    {
      key: "date",
      header: "Дата",
      cell: (audit) => <div className="min-w-[96px] font-medium">{formatDate(audit.business_date)}</div>,
    },
    {
      key: "shortage",
      header: "Недостача",
      className: "tabular-nums",
      cell: (audit) => formatMoney(audit.total_shortage_amount),
    },
    {
      key: "penalty",
      header: "Штраф",
      className: "font-medium tabular-nums",
      cell: (audit) => formatMoney(audit.total_penalty_amount),
    },
    {
      key: "employees",
      header: "Сотруд.",
      className: "tabular-nums",
      cell: (audit) => (audit.employee_count ? String(audit.employee_count) : "—"),
    },
    {
      key: "status",
      header: "Статус",
      cell: (audit) => <AuditStatusBadge status={audit.status} />,
    },
    {
      key: "actions",
      header: "Действия",
      className: "text-right",
      cell: (audit) => (
        <div className="flex flex-wrap justify-end gap-2">
          <Button onClick={() => setSelectedAuditId(audit.id)} size="sm" variant="outline">
            <Search size={15} aria-hidden="true" />
            Открыть
          </Button>
          {audit.status === "draft" ? (
            <Button onClick={() => setConfirmation({ action: "apply", audit })} size="sm">
              <Check size={15} aria-hidden="true" />
              Применить
            </Button>
          ) : null}
          {audit.status !== "cancelled" ? (
            <Button
              onClick={() => setConfirmation({ action: "cancel", audit })}
              size="sm"
              variant="outline"
            >
              <Trash2 size={15} aria-hidden="true" />
              Отменить
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Ревизии</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Штрафы по недостачам из iiko и ручных ревизий.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => handleImportOpenChange(true)} type="button">
            <UploadCloud size={16} aria-hidden="true" />
            Импорт из iiko
          </Button>
          <Button onClick={() => setManualOpen(true)} type="button" variant="outline">
            <Plus size={16} aria-hidden="true" />
            Вручную
          </Button>
        </div>
      </div>

      <section className="grid gap-3 rounded-lg border bg-card p-3 md:grid-cols-[150px_150px_160px_1fr] md:items-end">
        <Label className="grid gap-2">
          <span>С даты</span>
          <Input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
        </Label>
        <Label className="grid gap-2">
          <span>По дату</span>
          <Input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
        </Label>
        <Label className="grid gap-2">
          <span>Статус</span>
          <Select
            onValueChange={(value) => setStatusFilter(value as InventoryAuditStatus | "all")}
            value={statusFilter}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              <SelectItem value="draft">Черновик</SelectItem>
              <SelectItem value="applied">Применён</SelectItem>
              <SelectItem value="cancelled">Отменён</SelectItem>
            </SelectContent>
          </Select>
        </Label>
      </section>

      {auditsQuery.isLoading ? (
        <div className="rounded-lg border bg-card px-4 py-8 text-sm text-muted-foreground">
          Загрузка ревизий...
        </div>
      ) : audits.length ? (
        <DataTable columns={columns} rows={audits} getRowKey={(audit) => audit.id} />
      ) : (
        <EmptyState title="Ревизий нет" description="За выбранный период ничего не найдено." />
      )}

      {selectedAudit ? (
        <AuditDetail
          audit={selectedAudit}
          isComputing={computeMutation.isPending}
          onApply={(audit) => setConfirmation({ action: "apply", audit })}
          onCancel={(audit) => setConfirmation({ action: "cancel", audit })}
          onCompute={(audit) => computeMutation.mutate(audit.id)}
        />
      ) : null}

      <Dialog open={importOpen} onOpenChange={handleImportOpenChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Импорт из iiko</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
              <Label className="grid gap-2">
                <span>Дата ревизии</span>
                <Input
                  type="date"
                  value={importDate}
                  onChange={(event) => {
                    setImportDate(event.target.value);
                    setIikoCandidates([]);
                    setSelectedDocumentId("");
                  }}
                />
              </Label>
              <Button
                disabled={candidatesMutation.isPending}
                onClick={() => candidatesMutation.mutate(importDate)}
                type="button"
                variant="outline"
              >
                {candidatesMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <Search size={16} aria-hidden="true" />
                )}
                Найти документы
              </Button>
            </div>

            {iikoCandidates.length ? (
              <div className="space-y-3">
                <div className="text-sm font-medium">
                  Найдено документов: {iikoCandidates.length}
                </div>
                <div className="space-y-2">
                  {iikoCandidates.map((candidate) => (
                    <label
                      className="grid cursor-pointer grid-cols-[20px_1fr] gap-3 rounded-md border p-3 text-sm hover:bg-muted/35"
                      key={candidate.document_id}
                    >
                      <input
                        checked={selectedDocumentId === candidate.document_id}
                        className="mt-1 h-4 w-4"
                        name="iiko-document"
                        onChange={() => setSelectedDocumentId(candidate.document_id)}
                        type="radio"
                      />
                      <span>
                        <span className="font-medium">
                          {candidate.document_num ? `#${candidate.document_num}` : candidate.document_id}
                        </span>
                        <span>
                          {" "}
                          — {candidate.items_count} позиций, недостача{" "}
                          {formatMoney(candidate.total_shortage)}
                        </span>
                        <span className="mt-1 block text-muted-foreground">
                          из них в активном whitelist: {candidate.matched_active_count}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button onClick={() => handleImportOpenChange(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={importMutation.isPending || !selectedDocumentId}
              onClick={() =>
                importMutation.mutate({
                  business_date: importDate,
                  document_id: selectedDocumentId,
                })
              }
              type="button"
            >
              {importMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <UploadCloud size={16} aria-hidden="true" />
              )}
              Импортировать
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={manualOpen} onOpenChange={setManualOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Ревизия вручную</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <Label className="grid max-w-[180px] gap-2">
              <span>Дата</span>
              <Input type="date" value={manualDate} onChange={(event) => setManualDate(event.target.value)} />
            </Label>
            <div className="space-y-2">
              {manualRows.map((row, index) => (
                <div className="grid gap-2 sm:grid-cols-[1fr_140px_auto]" key={index}>
                  <Select
                    onValueChange={(position_id) => updateManualRow(index, { position_id })}
                    value={row.position_id || "none"}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Позиция" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">Позиция</SelectItem>
                      {positions.map((position) => (
                        <SelectItem key={position.id} value={position.id}>
                          {position.display_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Input
                    inputMode="decimal"
                    onChange={(event) =>
                      updateManualRow(index, { shortage_amount: event.target.value })
                    }
                    placeholder="Сумма"
                    value={row.shortage_amount}
                  />
                  <Button
                    onClick={() =>
                      setManualRows((rows) => rows.filter((_item, itemIndex) => itemIndex !== index))
                    }
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 size={16} aria-hidden="true" />
                  </Button>
                </div>
              ))}
            </div>
            <Button
              onClick={() =>
                setManualRows((rows) => [...rows, { position_id: "", shortage_amount: "" }])
              }
              type="button"
              variant="outline"
            >
              <Plus size={16} aria-hidden="true" />
              Добавить строку
            </Button>
          </div>
          <DialogFooter>
            <Button onClick={() => setManualOpen(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={manualMutation.isPending}
              onClick={() =>
                manualMutation.mutate({
                  business_date: manualDate,
                  items: manualRows
                    .filter((row) => row.position_id && row.shortage_amount)
                    .map((row) => ({
                      position_id: row.position_id,
                      shortage_amount: row.shortage_amount,
                    })),
                })
              }
              type="button"
            >
              {manualMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Plus size={16} aria-hidden="true" />
              )}
              Сохранить как черновик
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={Boolean(confirmation)} onOpenChange={(open) => !open && setConfirmation(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmation?.action === "apply" ? "Применить штрафы" : "Отменить ревизию"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmation ? confirmationText(confirmation) : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Назад</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (!confirmation) {
                  return;
                }
                if (confirmation.action === "apply") {
                  applyMutation.mutate(confirmation.audit.id);
                } else {
                  cancelMutation.mutate(confirmation.audit.id);
                }
              }}
            >
              Подтвердить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );

  function updateManualRow(index: number, patch: Partial<ManualRow>) {
    setManualRows((rows) =>
      rows.map((row, itemIndex) => {
        if (itemIndex !== index) {
          return row;
        }
        const next = { ...row, ...patch };
        if (next.position_id === "none") {
          next.position_id = "";
        }
        return next;
      }),
    );
  }

  function handleImportOpenChange(open: boolean) {
    setImportOpen(open);
    setIikoCandidates([]);
    setSelectedDocumentId("");
    if (open) {
      setImportDate(previousMondayKey());
    }
  }
}

function AuditDetail({
  audit,
  isComputing,
  onApply,
  onCancel,
  onCompute,
}: {
  audit: InventoryAudit;
  isComputing: boolean;
  onApply: (audit: InventoryAudit) => void;
  onCancel: (audit: InventoryAudit) => void;
  onCompute: (audit: InventoryAudit) => void;
}) {
  const snapshot = audit.computation_snapshot;
  const groups = snapshot?.groups ?? {};
  const penalties = snapshot?.employee_penalties ?? [];
  const period = snapshot?.period;
  const items = audit.items ?? [];
  const skippedCount = audit.items_skipped_count ?? 0;
  const totalShortageIiko = audit.total_shortage_iiko ?? audit.total_shortage_amount;
  const totalShortageConsidered =
    audit.total_shortage_considered ?? audit.total_shortage_amount;
  const payrollPeriodStart = payrollPeriodStartForAudit(audit.business_date);

  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold tracking-normal">
              Ревизия {formatDate(audit.business_date)}
            </h3>
            <AuditStatusBadge status={audit.status} />
          </div>
          <div className="mt-1 text-sm text-muted-foreground">
            Предыдущая ревизия:{" "}
            {audit.previous_audit_date ? formatDate(audit.previous_audit_date) : "—"}
            {period?.start && period?.end ? ` · Период ${formatDate(period.start)} — ${formatDate(period.end)}` : ""}
          </div>
          <div className="mt-1 text-sm text-muted-foreground">
            Штрафы попадут в payroll-период с {formatDate(payrollPeriodStart)}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button disabled={isComputing} onClick={() => onCompute(audit)} variant="outline">
            {isComputing ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            Перерасчёт
          </Button>
          {audit.status === "draft" ? (
            <Button onClick={() => onApply(audit)}>
              <Check size={16} aria-hidden="true" />
              Применить
            </Button>
          ) : null}
          {audit.status !== "cancelled" ? (
            <Button onClick={() => onCancel(audit)} variant="outline">
              <RotateCcw size={16} aria-hidden="true" />
              Отменить
            </Button>
          ) : null}
        </div>
      </div>

      <div className="grid gap-2 text-sm sm:max-w-[720px]">
        <div className="grid gap-2 sm:grid-cols-[190px_minmax(0,1fr)] sm:items-baseline">
          <span className="font-medium">Недостача всего:</span>
          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="text-lg font-semibold tabular-nums">
              {formatMoney(totalShortageIiko)}
            </span>
            <span className="text-muted-foreground">
              {audit.source === "iiko" ? "(из iiko)" : "(все строки)"}
            </span>
          </span>
        </div>
        <div className="grid gap-2 sm:grid-cols-[190px_minmax(0,1fr)] sm:items-baseline">
          <InlineTooltip content="Учитываются только позиции, которые активированы в Исходных → Ревизии и имеют группу распределения.">
            <span className="font-medium">Учитываемая в штрафе:</span>
          </InlineTooltip>
          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="text-lg font-semibold tabular-nums">
              {formatMoney(totalShortageConsidered)}
            </span>
            <span className="text-muted-foreground">
              ({formatItemsCount(items.length, "активная позиция", "активные позиции", "активных позиций")}
              {skippedCount > 0 ? `; ${skippedCount} скрыто как неактивные` : ""})
            </span>
          </span>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,520px)]">
        <div className="space-y-4">
          <PanelTitle title="Позиции" />
          {items.length ? (
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full min-w-[560px] text-sm">
                <thead>
                  <tr className="border-b bg-muted/35">
                    <th className="p-3 text-left font-medium">Позиция</th>
                    <th className="p-3 text-left font-medium">Группа</th>
                    <th className="p-3 text-right font-medium">Сумма</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.id} className="border-b last:border-b-0">
                      <td className="p-3">
                        <div className="font-medium">{item.product_name_snapshot}</div>
                      </td>
                      <td className="p-3">
                        <InventoryGroupBadge group={item.allocation_group} />
                      </td>
                      <td className="p-3 text-right tabular-nums">{formatMoney(item.shortage_amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="rounded-md border px-3 py-4 text-sm text-muted-foreground">
              Ни одна позиция документа не активна в whitelist. Активируйте нужные в Исходных → Ревизии.
            </div>
          )}
        </div>

        <div className="space-y-4">
          <PanelTitle title="Расчёт штрафов" />
          {snapshot ? (
            <>
              <GroupSnapshotTable groups={groups} />
              <PanelTitle title="Распределение" />
              {penalties.length ? (
                <div className="rounded-md border">
                  {penalties.map((penalty) => (
                    <EmployeePenaltyRow key={penalty.employee_id} penalty={penalty} />
                  ))}
                </div>
              ) : (
                <div className="rounded-md border px-3 py-4 text-sm text-muted-foreground">
                  Штрафов к распределению нет
                </div>
              )}
            </>
          ) : (
            <div className="rounded-md border px-3 py-4 text-sm text-muted-foreground">
              Нажмите «Перерасчёт», чтобы сформировать снимок.
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function GroupSnapshotTable({ groups }: { groups: Record<string, InventoryGroupSnapshot> }) {
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full min-w-[520px] text-sm">
        <thead>
          <tr className="border-b bg-muted/35">
            <th className="p-3 text-left font-medium">Группа</th>
            <th className="p-3 text-right font-medium">Σ нед.</th>
            <th className="p-3 text-left font-medium">Порог</th>
            <th className="p-3 text-right font-medium">%</th>
            <th className="p-3 text-right font-medium">Штраф</th>
          </tr>
        </thead>
        <tbody>
          {groupOrder.map((group) => {
            const snapshot = groups[group];
            if (!snapshot) {
              return null;
            }
            return (
              <tr key={group} className="border-b last:border-b-0">
                <td className="p-3">
                  <InventoryGroupBadge group={group} />
                </td>
                <td className="p-3 text-right tabular-nums">
                  {formatMoney(snapshot.total_shortage ?? snapshot.sum ?? "0")}
                </td>
                <td className="p-3">{snapshot.threshold ?? "—"}</td>
                <td className="p-3 text-right tabular-nums">
                  {Number(snapshot.rate_percent ?? 0).toLocaleString("ru-RU", {
                    maximumFractionDigits: 0,
                  })}
                  %
                </td>
                <td className="p-3 text-right font-medium tabular-nums">
                  {formatMoney(snapshot.penalty ?? "0")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function EmployeePenaltyRow({ penalty }: { penalty: InventoryEmployeePenalty }) {
  return (
    <div className="grid grid-cols-[1fr_auto] gap-3 border-b px-3 py-2 text-sm last:border-b-0">
      <div>
        <div className="font-medium">{penalty.full_name}</div>
        <div className="text-xs text-muted-foreground">{penalty.position ?? "—"}</div>
      </div>
      <div className="font-medium tabular-nums">{formatMoney(penalty.amount)}</div>
    </div>
  );
}

function AuditStatusBadge({ status }: { status: InventoryAuditStatus }) {
  return (
    <Badge
      className={cn(
        status === "applied" && "bg-emerald-100 text-emerald-900 hover:bg-emerald-100",
        status === "cancelled" && "bg-muted text-muted-foreground hover:bg-muted",
      )}
      variant={status === "draft" ? "outline" : "secondary"}
    >
      {statusLabels[status]}
    </Badge>
  );
}

function PanelTitle({ title }: { title: string }) {
  return <h4 className="text-sm font-semibold tracking-normal">{title}</h4>;
}

function InlineTooltip({ children, content }: { children: ReactNode; content: string }) {
  return (
    <span className="group relative inline-flex w-fit">
      {children}
      <span
        className="pointer-events-none absolute left-0 top-full z-50 mt-2 hidden w-80 rounded-md border bg-popover px-3 py-2 text-left text-xs leading-5 text-popover-foreground shadow-lg group-focus-within:block group-hover:block"
        role="tooltip"
      >
        {content}
      </span>
    </span>
  );
}

function confirmationText(target: { action: "apply" | "cancel"; audit: InventoryAudit }) {
  if (target.action === "cancel") {
    return "Связанные штрафы по этой ревизии будут удалены из премий и штрафов.";
  }
  return `Применить штрафы ${target.audit.employee_count || "—"} сотрудникам на ${formatMoney(
    target.audit.total_penalty_amount,
  )}? Это создаст записи в премиях и штрафах.`;
}

async function invalidateInventory(
  queryClient: ReturnType<typeof useQueryClient>,
  auditId?: string,
) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["inventory-audits"] }),
    auditId ? queryClient.invalidateQueries({ queryKey: ["inventory-audit", auditId] }) : null,
  ]);
}

function currentMonthRange() {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), 1);
  const end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
  return { start: dateKey(start), end: dateKey(end) };
}

function todayKey() {
  return dateKey(new Date());
}

export function previousMonday(today: Date): Date {
  const dow = today.getDay();
  let daysSinceMonday: number;
  if (dow === 0) {
    daysSinceMonday = 6;
  } else if (dow === 1) {
    daysSinceMonday = 0;
  } else {
    daysSinceMonday = dow - 1;
  }
  return addDays(today, -daysSinceMonday);
}

function previousMondayKey(today: Date = new Date()) {
  return dateKey(previousMonday(today));
}

function payrollPeriodStartForAudit(businessDate: string) {
  return dateKey(addDays(parseDateKey(businessDate), 1));
}

function addDays(value: Date, days: number) {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

function parseDateKey(value: string) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}

function dateKey(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(new Date(`${value}T00:00:00`));
}

function formatMoney(value: string | number | null | undefined) {
  const amount = Number(value ?? 0);
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(amount);
}

function formatItemsCount(count: number, one: string, few: string, many: string) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) {
    return `${count} ${one}`;
  }
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
    return `${count} ${few}`;
  }
  return `${count} ${many}`;
}
