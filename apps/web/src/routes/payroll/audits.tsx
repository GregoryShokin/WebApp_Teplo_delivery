import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Info,
  LoaderCircle,
  MoreVertical,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
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
  patchInventoryAuditEmployeeExclusion,
  patchInventoryAuditItem,
  restoreInventoryAuditDraft,
  type InventoryAudit,
  type InventoryEmployeeRecipient,
  type InventoryAuditItem,
  type InventoryAllocationGroup,
  type InventoryAuditStatus,
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

type ConfirmationTarget =
  | { action: "apply" | "cancel" | "restore"; audit: InventoryAudit }
  | null;
type OverrideTarget =
  | { item: InventoryAuditItem; nextOverride: string | null; label: string }
  | null;
type MoveTarget = { item: InventoryAuditItem; value: string } | null;
type ExclusionTarget = InventoryEmployeeRecipient | null;
type ItemAmountFilter = "all" | "shortages" | "surpluses";
type AuditItemDisplayRow =
  | { type: "item"; item: InventoryAuditItem }
  | { type: "summary"; summary: NonNullable<InventoryAudit["swap_groups"]>[number] };

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
  const auditDetailRef = useRef<HTMLDivElement | null>(null);

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

  const restoreMutation = useMutation({
    mutationFn: restoreInventoryAuditDraft,
    onSuccess: async (audit) => {
      toast.success("Ревизия возвращена в черновик");
      setConfirmation(null);
      await invalidateInventory(queryClient, audit.id);
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось вернуть ревизию в черновик")),
  });

  const audits = auditsQuery.data ?? [];
  const selectedAudit = selectedAuditQuery.data ?? null;
  const selectedAuditError = selectedAuditQuery.error
    ? apiErrorMessage(selectedAuditQuery.error, "Не удалось открыть ревизию")
    : null;
  const positions = positionsQuery.data ?? [];

  useEffect(() => {
    if (!selectedAuditId) {
      return;
    }
    window.setTimeout(() => {
      auditDetailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }, [selectedAuditId]);

  useEffect(() => {
    if (selectedAuditError) {
      toast.error(selectedAuditError);
    }
  }, [selectedAuditError]);

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
          <Button onClick={() => openAudit(audit.id)} size="sm" variant="outline">
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
          {audit.status === "cancelled" ? (
            <Button
              onClick={() => setConfirmation({ action: "restore", audit })}
              size="sm"
              variant="outline"
            >
              <RotateCcw size={15} aria-hidden="true" />
              В черновик
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

      {selectedAuditId ? (
        <div ref={auditDetailRef} className="scroll-mt-6">
          {selectedAuditQuery.isFetching && !selectedAudit ? (
            <div className="flex items-center gap-2 rounded-lg border bg-card px-4 py-8 text-sm text-muted-foreground">
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              Загрузка ревизии...
            </div>
          ) : null}
          {selectedAuditError ? (
            <div className="flex flex-col gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between">
              <div>{selectedAuditError}</div>
              <Button onClick={() => selectedAuditQuery.refetch()} type="button" variant="outline">
                Повторить
              </Button>
            </div>
          ) : null}
          {selectedAudit ? (
            <AuditDetail
              audit={selectedAudit}
              isComputing={computeMutation.isPending}
              onApply={(audit) => setConfirmation({ action: "apply", audit })}
              onCancel={(audit) => setConfirmation({ action: "cancel", audit })}
              onCompute={(audit) => computeMutation.mutate(audit.id)}
              onRestore={(audit) => setConfirmation({ action: "restore", audit })}
            />
          ) : null}
        </div>
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
              {confirmation ? confirmationTitle(confirmation) : ""}
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
                } else if (confirmation.action === "cancel") {
                  cancelMutation.mutate(confirmation.audit.id);
                } else {
                  restoreMutation.mutate(confirmation.audit.id);
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

  function openAudit(auditId: string) {
    setSelectedAuditId(auditId);
    window.setTimeout(() => {
      auditDetailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }
}

function AuditDetail({
  audit,
  isComputing,
  onApply,
  onCancel,
  onCompute,
  onRestore,
}: {
  audit: InventoryAudit;
  isComputing: boolean;
  onApply: (audit: InventoryAudit) => void;
  onCancel: (audit: InventoryAudit) => void;
  onCompute: (audit: InventoryAudit) => void;
  onRestore: (audit: InventoryAudit) => void;
}) {
  const queryClient = useQueryClient();
  const [overrideTarget, setOverrideTarget] = useState<OverrideTarget>(null);
  const [moveTarget, setMoveTarget] = useState<MoveTarget>(null);
  const [exclusionTarget, setExclusionTarget] = useState<ExclusionTarget>(null);
  const [exclusionReason, setExclusionReason] = useState("");
  const [itemAmountFilter, setItemAmountFilter] = useState<ItemAmountFilter>("all");
  const overrideMutation = useMutation({
    mutationFn: ({
      item,
      nextOverride,
    }: {
      item: InventoryAuditItem;
      nextOverride: string | null;
    }) =>
      patchInventoryAuditItem(audit.id, item.id, {
        swap_group_override: nextOverride,
      }),
    onSuccess: async () => {
      toast.success("Override сохранён");
      setOverrideTarget(null);
      setMoveTarget(null);
      await invalidateInventory(queryClient, audit.id);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить override")),
  });
  const exclusionMutation = useMutation({
    mutationFn: ({
      employeeId,
      excluded,
      reason,
    }: {
      employeeId: string;
      excluded: boolean;
      reason?: string | null;
    }) =>
      patchInventoryAuditEmployeeExclusion(audit.id, employeeId, {
        excluded,
        reason,
      }),
    onSuccess: async (_audit, variables) => {
      toast.success(variables.excluded ? "Сотрудник исключён из расчёта" : "Сотрудник возвращён");
      setExclusionTarget(null);
      setExclusionReason("");
      await invalidateInventory(queryClient, audit.id);
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось обновить распределение")),
  });
  const snapshot = audit.computation_snapshot;
  const groups = snapshot?.groups ?? {};
  const penalties = snapshot?.employee_penalties ?? [];
  const recipientRows: InventoryEmployeeRecipient[] = snapshot?.employee_recipients?.length
    ? snapshot.employee_recipients
    : penalties.map((penalty) => ({
        ...penalty,
        is_excluded: false,
        exclusion_reason: null,
      }));
  const period = snapshot?.period;
  const items = audit.items ?? [];
  const swapGroups = audit.swap_groups ?? snapshot?.swap_groups ?? [];
  const filteredItems = useMemo(
    () =>
      items.filter((item) => {
        const amount = Number(item.amount);
        if (itemAmountFilter === "shortages") {
          return amount < 0;
        }
        if (itemAmountFilter === "surpluses") {
          return amount > 0;
        }
        return true;
      }),
    [itemAmountFilter, items],
  );
  const visibleSwapGroups = itemAmountFilter === "all" ? swapGroups : [];
  const displayRows = buildAuditItemRows(filteredItems, visibleSwapGroups);
  const shortageItemsCount = items.filter((item) => Number(item.amount) < 0).length;
  const surplusItemsCount = items.filter((item) => Number(item.amount) > 0).length;
  const skippedCount = audit.items_skipped_count ?? 0;
  const totalShortageIiko = audit.total_shortage_iiko ?? audit.total_shortage_amount;
  const totalShortageConsidered =
    audit.total_shortage_considered ?? audit.total_shortage_amount;
  const payrollPeriodStart = payrollPeriodStartForAudit(audit.business_date);
  const isFinalized = audit.status === "applied";
  const isOverrideDisabled = audit.status !== "draft" || overrideMutation.isPending;
  const isExclusionDisabled = audit.status !== "draft" || exclusionMutation.isPending;

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
        <div className="flex flex-col gap-2 sm:items-end">
          <div className="text-right">
            <div className="text-xs text-muted-foreground">Штраф</div>
            <div className="text-xl font-semibold tabular-nums">
              {formatMoney(audit.total_penalty_amount)}
            </div>
          </div>
          <div className="flex flex-wrap justify-end gap-2">
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
              <Trash2 size={16} aria-hidden="true" />
              Отменить
            </Button>
          ) : null}
          {audit.status === "cancelled" ? (
            <Button onClick={() => onRestore(audit)} variant="outline">
              <RotateCcw size={16} aria-hidden="true" />
              В черновик
            </Button>
          ) : null}
          </div>
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
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <PanelTitle title="Позиции" />
            <div className="flex rounded-md border p-1">
              <FilterButton
                active={itemAmountFilter === "all"}
                label={`Все ${items.length}`}
                onClick={() => setItemAmountFilter("all")}
              />
              <FilterButton
                active={itemAmountFilter === "shortages"}
                label={`Недостачи ${shortageItemsCount}`}
                onClick={() => setItemAmountFilter("shortages")}
              />
              <FilterButton
                active={itemAmountFilter === "surpluses"}
                label={`Излишки ${surplusItemsCount}`}
                onClick={() => setItemAmountFilter("surpluses")}
              />
            </div>
          </div>
          {items.length ? (
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full min-w-[760px] text-sm">
                <thead>
                  <tr className="border-b bg-muted/35">
                    <th className="p-3 text-left font-medium">Позиция</th>
                    <th className="p-3 text-left font-medium">Группа</th>
                    <th className="p-3 text-right font-medium">Сумма</th>
                    <th className="p-3 text-right font-medium">Действия</th>
                  </tr>
                </thead>
                <tbody>
                  {displayRows.length ? (
                    displayRows.map((row) =>
                    row.type === "item" ? (
                      <AuditItemRow
                        disabled={isOverrideDisabled}
                        finalized={isFinalized}
                        item={row.item}
                        key={row.item.id}
                        loading={overrideMutation.isPending}
                        onExclude={(item) =>
                          setOverrideTarget({
                            item,
                            nextOverride: "",
                            label: "Исключить из группы",
                          })
                        }
                        onMove={(item) =>
                          setMoveTarget({
                            item,
                            value: item.swap_group ?? item.swap_group_default ?? "",
                          })
                        }
                      />
                    ) : (
                      <SwapGroupSummaryRow key={`summary-${row.summary.group}`} summary={row.summary} />
                    ),
                    )
                  ) : (
                    <tr>
                      <td className="p-3 text-sm text-muted-foreground" colSpan={4}>
                        По выбранному фильтру строк нет
                      </td>
                    </tr>
                  )}
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
              {recipientRows.length ? (
                <div className="rounded-md border">
                  {recipientRows.map((recipient) => (
                    <EmployeePenaltyRow
                      disabled={isExclusionDisabled}
                      key={recipient.employee_id}
                      loading={
                        exclusionMutation.isPending &&
                        exclusionMutation.variables?.employeeId === recipient.employee_id
                      }
                      onIncludedChange={(included) => {
                        if (included) {
                          exclusionMutation.mutate({
                            employeeId: recipient.employee_id,
                            excluded: false,
                          });
                          return;
                        }
                        setExclusionTarget(recipient);
                        setExclusionReason(recipient.exclusion_reason ?? "");
                      }}
                      recipient={recipient}
                    />
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

      <Dialog open={Boolean(moveTarget)} onOpenChange={(open) => !open && setMoveTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Перенести в группу</DialogTitle>
          </DialogHeader>
          <div className="grid gap-2">
            <Label>
              <span>Группа пересорта</span>
              <Input
                className="mt-2"
                maxLength={64}
                onChange={(event) =>
                  setMoveTarget((target) =>
                    target ? { ...target, value: event.target.value.slice(0, 64) } : target,
                  )
                }
                value={moveTarget?.value ?? ""}
              />
            </Label>
          </div>
          <DialogFooter>
            <Button onClick={() => setMoveTarget(null)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!moveTarget?.value.trim()}
              onClick={() => {
                if (!moveTarget) {
                  return;
                }
                setOverrideTarget({
                  item: moveTarget.item,
                  nextOverride: moveTarget.value.trim(),
                  label: "Перенести в группу",
                });
                setMoveTarget(null);
              }}
              type="button"
            >
              Продолжить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(exclusionTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setExclusionTarget(null);
            setExclusionReason("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Исключить из расчёта</DialogTitle>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="text-sm font-medium">{exclusionTarget?.full_name}</div>
            <Label className="grid gap-2">
              <span>Причина</span>
              <Textarea
                maxLength={500}
                onChange={(event) => setExclusionReason(event.target.value.slice(0, 500))}
                value={exclusionReason}
              />
            </Label>
          </div>
          <DialogFooter>
            <Button
              disabled={exclusionMutation.isPending}
              onClick={() => {
                setExclusionTarget(null);
                setExclusionReason("");
              }}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button
              disabled={exclusionMutation.isPending || !exclusionReason.trim()}
              onClick={() => {
                if (!exclusionTarget) {
                  return;
                }
                exclusionMutation.mutate({
                  employeeId: exclusionTarget.employee_id,
                  excluded: true,
                  reason: exclusionReason.trim(),
                });
              }}
              type="button"
            >
              {exclusionMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Исключить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={Boolean(overrideTarget)}
        onOpenChange={(open) => !open && setOverrideTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{overrideTarget?.label ?? "Изменить группу"}</AlertDialogTitle>
            <AlertDialogDescription>
              Это изменит расчёт штрафа. Применить?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Назад</AlertDialogCancel>
            <AlertDialogAction
              disabled={overrideMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (!overrideTarget) {
                  return;
                }
                overrideMutation.mutate({
                  item: overrideTarget.item,
                  nextOverride: overrideTarget.nextOverride,
                });
              }}
            >
              {overrideMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Применить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function AuditItemRow({
  disabled,
  finalized,
  item,
  loading,
  onExclude,
  onMove,
}: {
  disabled: boolean;
  finalized: boolean;
  item: InventoryAuditItem;
  loading: boolean;
  onExclude: (item: InventoryAuditItem) => void;
  onMove: (item: InventoryAuditItem) => void;
}) {
  const hasSwapGroup = Boolean(item.swap_group);
  const canEditSwapGroup = Boolean(
    item.swap_group || item.swap_group_default || item.has_swap_group_override,
  );
  return (
    <tr
      className={cn(
        "border-b last:border-b-0",
        hasSwapGroup && "bg-slate-50",
      )}
    >
      <td className="p-3">
        <div className="flex items-center gap-2">
          {item.swap_group ? <Badge variant="secondary">{item.swap_group}</Badge> : null}
          <div className="font-medium">{item.product_name_snapshot}</div>
          {item.has_swap_group_override ? (
            <InlineTooltip
              content={`Override на этой ревизии: исходная группа ${
                item.swap_group_default ?? "—"
              } → ${item.swap_group_override ? item.swap_group_override : "исключено"}`}
            >
              <Info size={14} aria-hidden="true" className="text-muted-foreground" />
            </InlineTooltip>
          ) : null}
        </div>
      </td>
      <td className="p-3">
        <InventoryGroupBadge group={item.allocation_group} />
      </td>
      <td className="p-3 text-right tabular-nums">{formatSignedMoney(item.amount)}</td>
      <td className="p-3 text-right">
        {canEditSwapGroup ? (
          <span
            title={
              finalized
                ? "Ревизия зафиксирована. Сначала отмените фиксацию для изменений"
                : undefined
            }
          >
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button disabled={disabled} size="icon" type="button" variant="ghost">
                  {loading ? (
                    <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
                  ) : (
                    <MoreVertical size={15} aria-hidden="true" />
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => onExclude(item)}>
                  Исключить из группы (только для этой ревизии)
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => onMove(item)}>
                  Перенести в группу…
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
    </tr>
  );
}

function SwapGroupSummaryRow({
  summary,
}: {
  summary: NonNullable<InventoryAudit["swap_groups"]>[number];
}) {
  const net = Number(summary.net_amount);
  const covered = net >= 0;
  return (
    <tr className={cn("border-b", covered ? "bg-emerald-50 text-emerald-900" : "bg-white")}>
      <td className="p-3 font-medium" colSpan={2}>
        {covered
          ? `${summary.group} (пересорт): покрыто излишком ${formatSignedMoney(
              summary.net_amount,
            )} — штрафа нет`
          : `${summary.group} (пересорт): ${formatSignedMoney(summary.net_amount)}`}
        <span className="ml-2 text-xs text-muted-foreground">
          {summary.allocation_group ?? "—"}
        </span>
      </td>
      <td className="p-3 text-right font-semibold tabular-nums">
        {covered ? "0 ₽" : formatMoney(summary.effective_shortage ?? summary.net_amount)}
      </td>
      <td className="p-3" />
    </tr>
  );
}

function buildAuditItemRows(
  items: InventoryAuditItem[],
  swapGroups: NonNullable<InventoryAudit["swap_groups"]>,
): AuditItemDisplayRow[] {
  const rows: AuditItemDisplayRow[] = [];
  const summaryByGroup = new Map(swapGroups.map((summary) => [summary.group, summary]));
  const itemsByGroup = new Map<string, InventoryAuditItem[]>();
  const ungrouped: InventoryAuditItem[] = [];
  for (const item of items) {
    if (!item.swap_group) {
      ungrouped.push(item);
      continue;
    }
    const groupItems = itemsByGroup.get(item.swap_group) ?? [];
    groupItems.push(item);
    itemsByGroup.set(item.swap_group, groupItems);
  }
  for (const [group, groupItems] of itemsByGroup.entries()) {
    groupItems.forEach((item) => rows.push({ type: "item", item }));
    const summary = summaryByGroup.get(group);
    if (summary) {
      rows.push({ type: "summary", summary });
    }
  }
  ungrouped.forEach((item) => rows.push({ type: "item", item }));
  return rows;
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

function EmployeePenaltyRow({
  disabled,
  loading,
  onIncludedChange,
  recipient,
}: {
  disabled: boolean;
  loading: boolean;
  onIncludedChange: (included: boolean) => void;
  recipient: InventoryEmployeeRecipient;
}) {
  const included = !recipient.is_excluded;
  return (
    <div
      className={cn(
        "grid gap-3 border-b px-3 py-2 text-sm last:border-b-0 sm:grid-cols-[1fr_auto_auto] sm:items-center",
        !included && "bg-muted/35 text-muted-foreground",
      )}
    >
      <div className="min-w-0">
        <div className="font-medium text-foreground">{recipient.full_name}</div>
        <div className="text-xs text-muted-foreground">{recipient.position ?? "—"}</div>
        {!included && recipient.exclusion_reason ? (
          <div className="mt-1 text-xs text-muted-foreground">
            Исключён: {recipient.exclusion_reason}
          </div>
        ) : null}
      </div>
      <div className="font-medium tabular-nums sm:text-right">
        {formatMoney(recipient.amount)}
      </div>
      <div className="flex items-center gap-2 sm:justify-end">
        {loading ? <LoaderCircle className="animate-spin" size={15} aria-hidden="true" /> : null}
        <span className="text-xs text-muted-foreground">Учитывать</span>
        <Switch
          checked={included}
          disabled={disabled}
          onCheckedChange={onIncludedChange}
          title={disabled ? "Изменения доступны только в черновике" : undefined}
        />
      </div>
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

function FilterButton({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className={cn(
        "h-8 rounded-sm px-3 text-sm transition-colors",
        active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
      )}
      onClick={onClick}
      type="button"
    >
      {label}
    </button>
  );
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

function confirmationTitle(target: NonNullable<ConfirmationTarget>) {
  if (target.action === "apply") {
    return "Применить штрафы";
  }
  if (target.action === "restore") {
    return "Вернуть в черновик";
  }
  return "Отменить ревизию";
}

function confirmationText(target: NonNullable<ConfirmationTarget>) {
  if (target.action === "cancel") {
    return "Связанные штрафы по этой ревизии будут удалены из премий и штрафов.";
  }
  if (target.action === "restore") {
    return "Ревизия снова станет черновиком. После этого её можно пересчитать и применить заново.";
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

function previousMonday(today: Date): Date {
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

function formatSignedMoney(value: string | number | null | undefined) {
  const amount = Number(value ?? 0);
  if (amount <= 0) {
    return formatMoney(amount);
  }
  return `+${formatMoney(amount)}`;
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
