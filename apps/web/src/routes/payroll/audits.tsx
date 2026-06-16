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
  DialogDescription,
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { InventoryGroupBadge } from "@/routes/payroll/inventory-positions";
import {
  apiErrorMessage,
  applyInventoryAudit,
  cancelDeferredCharge,
  cancelInventoryAudit,
  computeInventoryAudit,
  createDeferredCharge,
  createManualInventoryAudit,
  getAllInventoryAuditExclusions,
  getDeferredChargePayoutOptions,
  getIikoCandidates,
  getInventoryAudit,
  getInventoryAuditDeferredOnPayout,
  getInventoryAuditPayoutOptions,
  getInventoryAudits,
  getInventoryPositions,
  getSettings,
  importInventoryAuditFromIiko,
  listDeferredCharges,
  patchInventoryAudit,
  patchInventoryAuditEmployeeExclusion,
  patchInventoryAuditItem,
  patchInventoryAuditItemExclusion,
  restoreInventoryAuditDraft,
  type DeferredCharge,
  type DeferredChargeStatus,
  type InventoryAudit,
  type InventoryDeferredOnPayoutEmployee,
  type InventoryPayoutOption,
  type InventoryAuditExclusionLogRow,
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

type ConfirmationTarget = { action: "apply" | "cancel" | "restore"; audit: InventoryAudit } | null;
type OverrideTarget = {
  item: InventoryAuditItem;
  nextOverride: string | null;
  label: string;
} | null;
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
  const [subTab, setSubTab] = useState<"audits" | "items" | "employees" | "deferred">("audits");
  const payrollSettingsQuery = useQuery({
    queryKey: ["settings", "payroll"],
    queryFn: () => getSettings("payroll"),
    staleTime: 60_000,
  });
  const deferredEnabled =
    payrollSettingsQuery.data?.find((s) => s.key === "payroll.deferred_charges_enabled")?.value ===
    true;
  useEffect(() => {
    if (!deferredEnabled && subTab === "deferred") {
      setSubTab("audits");
    }
  }, [deferredEnabled, subTab]);
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
          audit.penalty_work_date_effective ?? payrollPeriodStartForAudit(audit.business_date),
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
      cell: (audit) => (
        <div className="min-w-[96px] font-medium">{formatDate(audit.business_date)}</div>
      ),
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
              <RotateCcw size={15} aria-hidden="true" />В черновик
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      <div>
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Ревизии</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Штрафы по недостачам из iiko и ручных ревизий.
          </p>
        </div>
      </div>

      <Tabs
        className="space-y-4"
        onValueChange={(value) => setSubTab(value as typeof subTab)}
        value={subTab}
      >
        <TabsList>
          <TabsTrigger value="audits">Ревизии</TabsTrigger>
          <TabsTrigger value="items">Позиции</TabsTrigger>
          <TabsTrigger value="employees">Сотрудники</TabsTrigger>
          {deferredEnabled ? (
            <TabsTrigger value="deferred">Распределённые</TabsTrigger>
          ) : null}
        </TabsList>

        <TabsContent className="space-y-5" value="audits">
          <section className="grid gap-3 rounded-lg border bg-card p-3 md:grid-cols-[150px_150px_160px_1fr] md:items-end">
            <Label className="grid gap-2">
              <span>С даты</span>
              <Input
                onChange={(event) => setDateFrom(event.target.value)}
                type="date"
                value={dateFrom}
              />
            </Label>
            <Label className="grid gap-2">
              <span>По дату</span>
              <Input
                onChange={(event) => setDateTo(event.target.value)}
                type="date"
                value={dateTo}
              />
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
            <div className="flex flex-wrap gap-2 md:justify-end">
              <Button onClick={() => handleImportOpenChange(true)} type="button">
                <UploadCloud size={16} aria-hidden="true" />
                Импорт из iiko
              </Button>
              <Button onClick={() => setManualOpen(true)} type="button" variant="outline">
                <Plus size={16} aria-hidden="true" />
                Вручную
              </Button>
            </div>
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
                  <Button
                    onClick={() => selectedAuditQuery.refetch()}
                    type="button"
                    variant="outline"
                  >
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
        </TabsContent>

        <TabsContent value="items">
          <ExclusionLogTab deferredEnabled={deferredEnabled} kind="items" />
        </TabsContent>

        <TabsContent value="employees">
          <ExclusionLogTab deferredEnabled={deferredEnabled} kind="employees" />
        </TabsContent>

        {deferredEnabled ? (
          <TabsContent value="deferred">
            <DeferredChargesTab />
          </TabsContent>
        ) : null}
      </Tabs>

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
                          {candidate.document_num
                            ? `#${candidate.document_num}`
                            : candidate.document_id}
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
              <Input
                type="date"
                value={manualDate}
                onChange={(event) => setManualDate(event.target.value)}
              />
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
                      setManualRows((rows) =>
                        rows.filter((_item, itemIndex) => itemIndex !== index),
                      )
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

      <AlertDialog
        open={Boolean(confirmation)}
        onOpenChange={(open) => !open && setConfirmation(null)}
      >
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

function ExclusionLogTab({
  kind,
  deferredEnabled,
}: {
  kind: "items" | "employees";
  deferredEnabled: boolean;
}) {
  const [auditDateFrom, setAuditDateFrom] = useState<string>("");
  const [auditDateTo, setAuditDateTo] = useState<string>("");
  const [deferredTarget, setDeferredTarget] = useState<InventoryAuditExclusionLogRow | null>(null);
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["inventory-exclusions", { auditDateFrom, auditDateTo }],
    queryFn: () =>
      getAllInventoryAuditExclusions({
        audit_date_from: auditDateFrom || undefined,
        audit_date_to: auditDateTo || undefined,
      }),
  });

  const rows = kind === "items" ? (query.data?.items ?? []) : (query.data?.employees ?? []);

  const itemReturnMutation = useMutation({
    mutationFn: ({ auditId, itemId }: { auditId: string; itemId: string }) =>
      patchInventoryAuditItemExclusion(auditId, itemId, { excluded: false, reason: null }),
    onSuccess: async (_audit, variables) => {
      toast.success("Позиция возвращена в расчёт");
      await invalidateInventory(queryClient, variables.auditId);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось вернуть позицию")),
  });

  const employeeReturnMutation = useMutation({
    mutationFn: ({ auditId, employeeId }: { auditId: string; employeeId: string }) =>
      patchInventoryAuditEmployeeExclusion(auditId, employeeId, { excluded: false }),
    onSuccess: async (_audit, variables) => {
      toast.success("Сотрудник возвращён в расчёт");
      await invalidateInventory(queryClient, variables.auditId);
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось вернуть сотрудника в расчёт")),
  });

  return (
    <>
      <section className="mt-4 space-y-4 rounded-lg border bg-card p-4">
        <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <PanelTitle title={kind === "items" ? "Исключённые позиции" : "Исключённые сотрудники"} />
          <div className="grid gap-2 md:grid-cols-2 md:gap-3">
            <Label>
              <span className="text-xs text-muted-foreground">Ревизии с</span>
              <Input
                className="mt-1"
                onChange={(event) => setAuditDateFrom(event.target.value)}
                type="date"
                value={auditDateFrom}
              />
            </Label>
            <Label>
              <span className="text-xs text-muted-foreground">по</span>
              <Input
                className="mt-1"
                onChange={(event) => setAuditDateTo(event.target.value)}
                type="date"
                value={auditDateTo}
              />
            </Label>
          </div>
        </div>

        {query.isLoading ? (
          <div className="px-3 py-4 text-sm text-muted-foreground">Загрузка...</div>
        ) : rows.length === 0 ? (
          <div className="rounded-md border px-3 py-6 text-center text-sm text-muted-foreground">
            {kind === "items" ? "Исключённых позиций нет." : "Исключённых сотрудников нет."}
          </div>
        ) : (
          <div className="overflow-x-auto rounded-md border">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b bg-muted/35">
                  <th className="w-[110px] px-2 py-2 text-left font-medium">Дата ревизии</th>
                  <th className="px-2 py-2 text-left font-medium">
                    {kind === "items" ? "Позиция" : "Сотрудник"}
                  </th>
                  <th className="px-2 py-2 text-left font-medium">Причина</th>
                  <th className="w-[130px] px-2 py-2 text-left font-medium">Когда / кто</th>
                  <th className="w-[110px] px-2 py-2 text-right font-medium">Действие</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const canReturn = row.audit_status === "draft";
                  const pending =
                    kind === "items"
                      ? itemReturnMutation.isPending
                      : employeeReturnMutation.isPending;
                  return (
                    <tr className="border-b last:border-b-0" key={row.id}>
                      <td className="px-2 py-2 align-top">
                        {row.audit_business_date ? formatDate(row.audit_business_date) : "—"}
                        {row.audit_status ? (
                          <div className="mt-1 text-xs">
                            <AuditStatusBadge status={row.audit_status} />
                          </div>
                        ) : null}
                      </td>
                      <td className="px-2 py-2 align-top">
                        {kind === "items" ? (
                          <>
                            <div className="font-medium">{row.product_name ?? "—"}</div>
                            {row.amount ? (
                              <div className="text-xs text-muted-foreground tabular-nums">
                                {formatSignedMoney(row.amount)}
                              </div>
                            ) : null}
                          </>
                        ) : (
                          <>
                            <div className="font-medium">{row.employee_name ?? "—"}</div>
                            {row.employee_position ? (
                              <div className="text-xs text-muted-foreground">
                                {row.employee_position}
                              </div>
                            ) : null}
                          </>
                        )}
                      </td>
                      <td className="px-2 py-2 align-top text-muted-foreground">{row.reason}</td>
                      <td className="px-2 py-2 align-top text-xs text-muted-foreground">
                        {row.created_at ? formatDateTime(row.created_at) : "—"}
                        <div>{row.created_by_name ?? "—"}</div>
                      </td>
                      <td className="px-2 py-2 text-right align-top">
                        {kind === "items" ? (
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Button disabled={pending} size="sm" type="button" variant="ghost">
                                Действия
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem
                                disabled={!canReturn || !row.item_id}
                                onSelect={() => {
                                  if (row.item_id) {
                                    itemReturnMutation.mutate({
                                      auditId: row.audit_id,
                                      itemId: row.item_id,
                                    });
                                  }
                                }}
                              >
                                Вернуть в расчёт
                              </DropdownMenuItem>
                              {deferredEnabled ? (
                                <DropdownMenuItem onSelect={() => setDeferredTarget(row)}>
                                  Распределить штраф…
                                </DropdownMenuItem>
                              ) : null}
                            </DropdownMenuContent>
                          </DropdownMenu>
                        ) : (
                          <Button
                            disabled={!canReturn || pending}
                            onClick={() => {
                              if (row.employee_id) {
                                employeeReturnMutation.mutate({
                                  auditId: row.audit_id,
                                  employeeId: row.employee_id,
                                });
                              }
                            }}
                            size="sm"
                            title={
                              !canReturn ? "Вернуть можно только в черновике ревизии" : undefined
                            }
                            type="button"
                            variant="ghost"
                          >
                            Вернуть
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <DeferredChargeDialog row={deferredTarget} onClose={() => setDeferredTarget(null)} />
    </>
  );
}

function DeferredChargeDialog({
  onClose,
  row,
}: {
  onClose: () => void;
  row: InventoryAuditExclusionLogRow | null;
}) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState<string>("");
  const [splitsCount, setSplitsCount] = useState<number>(3);
  const [startPeriodStart, setStartPeriodStart] = useState<string>("");
  const [reason, setReason] = useState<string>("");

  const payoutOptionsQuery = useQuery({
    queryKey: ["deferred-charge-payout-options"],
    queryFn: getDeferredChargePayoutOptions,
    enabled: row !== null,
  });
  const payoutOptions: InventoryPayoutOption[] = payoutOptionsQuery.data ?? [];
  const firstOpenOption = payoutOptions.find((option) => !option.locked) ?? null;

  useEffect(() => {
    if (!row) {
      return;
    }
    setAmount("");
    setSplitsCount(3);
    setReason(row.reason ?? "");
    setStartPeriodStart("");
  }, [row]);

  // дефолт — первая открытая выплата, когда опции загрузились
  useEffect(() => {
    if (row && !startPeriodStart && firstOpenOption) {
      setStartPeriodStart(firstOpenOption.period_start);
    }
  }, [row, startPeriodStart, firstOpenOption]);

  const startOption =
    payoutOptions.find((option) => option.period_start === startPeriodStart) ?? null;
  // выплаты, в которые сядут доли: старт + 7×k
  const targetPayouts = startOption
    ? Array.from({ length: splitsCount }, (_, index) =>
        dateKey(addDays(parseDateKey(startOption.payout_date), 7 * index)),
      )
    : [];

  const amountNumber = Number(amount);
  const isValid =
    row !== null &&
    Boolean(row.item_id) &&
    Number.isFinite(amountNumber) &&
    amountNumber > 0 &&
    splitsCount >= 1 &&
    splitsCount <= 24 &&
    startPeriodStart.length > 0 &&
    reason.trim().length > 0;

  const mutation = useMutation({
    mutationFn: () =>
      createDeferredCharge({
        source_audit_id: row!.audit_id,
        source_item_id: row!.item_id!,
        total_penalty_amount: amount,
        splits_count: splitsCount,
        start_period_start: startPeriodStart,
        reason: reason.trim(),
      }),
    onSuccess: async () => {
      toast.success(`Штраф распределён на ${splitsCount} ${pluralizeRuns(splitsCount)}`);
      onClose();
      await queryClient.invalidateQueries({ queryKey: ["deferred-charges"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось распределить штраф")),
  });

  return (
    <Dialog open={row !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Распределить штраф по расчётам ЗП</DialogTitle>
          <DialogDescription>
            Штраф делится равными долями по {splitsCount} {pluralizeRuns(splitsCount)} начиная с
            выбранной выплаты, далее подряд по неделям.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {row ? (
            <div className="rounded-md border bg-muted/40 p-3 text-sm">
              <div className="font-medium">{row.product_name ?? "—"}</div>
              <div className="text-xs text-muted-foreground">
                Ревизия {row.audit_business_date ? formatDate(row.audit_business_date) : "—"} ·
                Исключённая сумма {row.amount ? formatSignedMoney(row.amount) : "—"}
              </div>
            </div>
          ) : null}
          <Label className="grid gap-2">
            <span>Сумма штрафа, ₽</span>
            <Input
              disabled={mutation.isPending}
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              value={amount}
            />
          </Label>
          <Label className="grid gap-2">
            <span>Количество долей ({splitsCount})</span>
            <Input
              disabled={mutation.isPending}
              max={24}
              min={1}
              onChange={(event) =>
                setSplitsCount(Math.max(1, Math.min(24, Number(event.target.value) || 1)))
              }
              type="number"
              value={splitsCount}
            />
          </Label>
          <Label className="grid gap-2">
            <span>Стартовая выплата</span>
            <Select
              disabled={mutation.isPending || payoutOptionsQuery.isLoading}
              onValueChange={setStartPeriodStart}
              value={startPeriodStart}
            >
              <SelectTrigger>
                <SelectValue placeholder="Выберите выплату" />
              </SelectTrigger>
              <SelectContent>
                {payoutOptions.map((option) => (
                  <SelectItem
                    disabled={option.locked}
                    key={option.period_start}
                    value={option.period_start}
                  >
                    Выплата {formatDate(option.payout_date)}
                    {option.locked ? " — закрыта" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          {targetPayouts.length > 0 ? (
            <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs leading-5 text-muted-foreground">
              Доли уйдут в выплаты: {targetPayouts.map((payout) => formatDate(payout)).join(", ")}.
              Получателей бэк определит автоматически по сотрудникам в сменах за период ревизии для
              группы исключённой позиции.
            </div>
          ) : null}
          <Label className="grid gap-2">
            <span>Причина</span>
            <Textarea
              disabled={mutation.isPending}
              maxLength={500}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Например: распределить недостачу за май на 3 ближайшие зарплаты"
              rows={3}
              value={reason}
            />
          </Label>
        </div>
        <DialogFooter>
          <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={!isValid || mutation.isPending}
            onClick={() => mutation.mutate()}
            type="button"
          >
            Распределить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeferredChargesTab() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<DeferredChargeStatus | "all">("all");
  const [cancelTarget, setCancelTarget] = useState<DeferredCharge | null>(null);
  const [detailsTarget, setDetailsTarget] = useState<DeferredCharge | null>(null);

  const query = useQuery({
    queryKey: ["deferred-charges", { statusFilter }],
    queryFn: () => listDeferredCharges(statusFilter === "all" ? {} : { status: statusFilter }),
  });

  const cancelMutation = useMutation({
    mutationFn: (chargeId: string) => cancelDeferredCharge(chargeId),
    onSuccess: async () => {
      toast.success("Распределённый штраф отменён");
      setCancelTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["deferred-charges"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить распределение")),
  });

  const rows = query.data ?? [];
  const appliedCancelSplits = cancelTarget ? deferredChargeAppliedSplits(cancelTarget) : 0;

  return (
    <section className="mt-4 space-y-4 rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <PanelTitle title="Распределённые штрафы" />
        <div className="flex flex-wrap rounded-md border p-1">
          <FilterButton
            active={statusFilter === "all"}
            label="Все"
            onClick={() => setStatusFilter("all")}
          />
          <FilterButton
            active={statusFilter === "pending"}
            label="Ожидает"
            onClick={() => setStatusFilter("pending")}
          />
          <FilterButton
            active={statusFilter === "partially_applied"}
            label="Частично"
            onClick={() => setStatusFilter("partially_applied")}
          />
          <FilterButton
            active={statusFilter === "applied"}
            label="Применён"
            onClick={() => setStatusFilter("applied")}
          />
          <FilterButton
            active={statusFilter === "cancelled"}
            label="Отменён"
            onClick={() => setStatusFilter("cancelled")}
          />
        </div>
      </div>

      {query.isLoading ? (
        <div className="px-3 py-4 text-sm text-muted-foreground">Загрузка...</div>
      ) : rows.length === 0 ? (
        <div className="rounded-md border px-3 py-6 text-center text-sm text-muted-foreground">
          Распределённых штрафов нет.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full min-w-[900px] text-sm">
            <thead>
              <tr className="border-b bg-muted/35">
                <th className="w-[120px] px-2 py-2 text-left font-medium">Создан</th>
                <th className="px-2 py-2 text-left font-medium">Получатели</th>
                <th className="w-[110px] px-2 py-2 text-left font-medium">Ревизия</th>
                <th className="w-[100px] px-2 py-2 text-right font-medium">Сумма</th>
                <th className="w-[110px] px-2 py-2 text-center font-medium">Доли</th>
                <th className="w-[140px] px-2 py-2 text-left font-medium">Статус</th>
                <th className="px-2 py-2 text-left font-medium">Причина</th>
                <th className="w-[170px] px-2 py-2 text-right font-medium">Действие</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((charge) => {
                const canCancel =
                  charge.status === "pending" || charge.status === "partially_applied";
                const recipientNames = charge.recipients
                  .map((recipient) => recipient.employee_name ?? recipient.employee_id)
                  .join(", ");
                const totalSplits = deferredChargeTotalSplits(charge);
                return (
                  <tr className="border-b last:border-b-0" key={charge.id}>
                    <td className="px-2 py-2 align-top text-xs text-muted-foreground">
                      {charge.created_at ? formatDateTime(charge.created_at) : "—"}
                      <div>{charge.created_by_name ?? "—"}</div>
                    </td>
                    <td className="px-2 py-2 align-top">
                      <InlineTooltip content={recipientNames || "Получателей нет"}>
                        <button
                          className="text-left underline-offset-2 hover:underline"
                          onClick={() => setDetailsTarget(charge)}
                          type="button"
                        >
                          {formatItemsCount(
                            charge.recipients.length,
                            "получатель",
                            "получателя",
                            "получателей",
                          )}
                        </button>
                      </InlineTooltip>
                    </td>
                    <td className="px-2 py-2 align-top text-xs">
                      {charge.source_audit_date ? formatDate(charge.source_audit_date) : "—"}
                      <div className="text-muted-foreground">{charge.source_item_name ?? "—"}</div>
                    </td>
                    <td className="px-2 py-2 text-right align-top tabular-nums">
                      {formatMoney(charge.total_penalty_amount)}
                    </td>
                    <td className="px-2 py-2 text-center align-top">
                      {deferredChargeAppliedSplits(charge)}/{totalSplits}
                    </td>
                    <td className="px-2 py-2 align-top">
                      <DeferredChargeStatusBadge status={charge.status} />
                    </td>
                    <td className="px-2 py-2 align-top text-muted-foreground">{charge.reason}</td>
                    <td className="px-2 py-2 text-right align-top">
                      <Button
                        onClick={() => setDetailsTarget(charge)}
                        size="sm"
                        type="button"
                        variant="ghost"
                      >
                        Детали
                      </Button>
                      <Button
                        disabled={!canCancel || cancelMutation.isPending}
                        onClick={() => setCancelTarget(charge)}
                        size="sm"
                        type="button"
                        variant="ghost"
                      >
                        Отменить
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <DeferredChargeDetailsDialog charge={detailsTarget} onClose={() => setDetailsTarget(null)} />

      <AlertDialog
        open={cancelTarget !== null}
        onOpenChange={(open) => !open && setCancelTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Отменить распределение штрафа?</AlertDialogTitle>
            <AlertDialogDescription>
              {appliedCancelSplits > 0
                ? "Уже применённые доли не откатываются — они остаются в payroll-расчётах. Остановится только применение будущих долей."
                : "Этот распределённый штраф ещё не применялся — он будет полностью отменён."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={cancelMutation.isPending}>Не отменять</AlertDialogCancel>
            <AlertDialogAction
              disabled={cancelMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (cancelTarget) {
                  cancelMutation.mutate(cancelTarget.id);
                }
              }}
            >
              Отменить распределение
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function DeferredChargeDetailsDialog({
  charge,
  onClose,
}: {
  charge: DeferredCharge | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={charge !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Детали распределения</DialogTitle>
          <DialogDescription>
            {charge
              ? `${formatMoney(charge.total_penalty_amount)} · ${formatItemsCount(
                  charge.recipients.length,
                  "получатель",
                  "получателя",
                  "получателей",
                )} · ${charge.splits_count} ${pluralizeRuns(charge.splits_count)}`
              : ""}
          </DialogDescription>
        </DialogHeader>
        {charge ? (
          <div className="space-y-3">
            <div className="text-sm">
              <div className="font-medium">{charge.source_item_name ?? "Позиция ревизии"}</div>
              <div className="text-xs text-muted-foreground">
                Ревизия {charge.source_audit_date ? formatDate(charge.source_audit_date) : "—"} ·
                Группа {allocationGroupLabel(charge.allocation_group)}
              </div>
            </div>
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full min-w-[680px] text-sm">
                <thead>
                  <tr className="border-b bg-muted/35">
                    <th className="px-2 py-2 text-left font-medium">Получатель</th>
                    <th className="w-[120px] px-2 py-2 text-right font-medium">Доля</th>
                    <th className="w-[110px] px-2 py-2 text-center font-medium">Осталось</th>
                    <th className="w-[170px] px-2 py-2 text-left font-medium">Статус</th>
                  </tr>
                </thead>
                <tbody>
                  {charge.recipients.map((recipient) => (
                    <tr className="border-b last:border-b-0" key={recipient.id}>
                      <td className="px-2 py-2 align-top">
                        <div>{recipient.employee_name ?? recipient.employee_id}</div>
                        <div className="text-xs text-muted-foreground">
                          {recipient.employee_position ?? "—"}
                        </div>
                        <div className="mt-1 text-xs text-muted-foreground">
                          {recipient.splits
                            .map((split) => {
                              const label = charge.start_period_start
                                ? formatDate(
                                    dateKey(
                                      addDays(
                                        parseDateKey(charge.start_period_start),
                                        7 * split.split_index,
                                      ),
                                    ),
                                  )
                                : `${split.split_index}`;
                              return split.applied_at
                                ? `${label}: применена`
                                : `${label}: ${formatMoney(split.amount)}`;
                            })
                            .join(" · ")}
                        </div>
                      </td>
                      <td className="px-2 py-2 text-right align-top tabular-nums">
                        {formatMoney(recipient.per_split_amount)}
                      </td>
                      <td className="px-2 py-2 text-center align-top">
                        {recipient.splits_remaining}/{charge.splits_count}
                      </td>
                      <td className="px-2 py-2 align-top">
                        {recipient.collapsed_at
                          ? "Схлопнут при увольнении"
                          : recipient.splits_remaining === 0
                            ? "Завершён"
                            : "В графике"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
        <DialogFooter>
          <Button onClick={onClose} type="button" variant="outline">
            Закрыть
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
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
  const [itemExclusionTarget, setItemExclusionTarget] = useState<{
    item: InventoryAuditItem;
    itemId: string;
    productName: string;
    nextExcluded: boolean;
  } | null>(null);
  const [itemExclusionReason, setItemExclusionReason] = useState("");
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
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось обновить распределение")),
  });
  const itemExclusionMutation = useMutation({
    mutationFn: ({
      itemId,
      excluded,
      reason,
    }: {
      itemId: string;
      excluded: boolean;
      reason?: string | null;
    }) => patchInventoryAuditItemExclusion(audit.id, itemId, { excluded, reason }),
    onSuccess: async (_audit, variables) => {
      toast.success(
        variables.excluded ? "Позиция исключена из расчёта" : "Позиция возвращена в расчёт",
      );
      await invalidateInventory(queryClient, audit.id);
      setItemExclusionTarget(null);
      setItemExclusionReason("");
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось обновить исключение позиции")),
  });
  const payoutOptionsQuery = useQuery({
    queryKey: ["inventory-audit-payout-options", audit.id],
    queryFn: () => getInventoryAuditPayoutOptions(audit.id),
    enabled: audit.status === "draft",
  });
  const penaltyDateMutation = useMutation({
    mutationFn: (override: string | null) =>
      patchInventoryAudit(audit.id, { penalty_work_date_override: override }),
    onSuccess: async () => {
      toast.success("Дата списания обновлена");
      await invalidateInventory(queryClient, audit.id);
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось перенести дату списания")),
  });
  const deferredOnPayoutQuery = useQuery({
    queryKey: ["inventory-audit-deferred-on-payout", audit.id],
    queryFn: () => getInventoryAuditDeferredOnPayout(audit.id),
  });
  const deferredOnPayout = deferredOnPayoutQuery.data ?? {
    total: "0",
    charges: [],
    by_employee: [],
  };
  const deferredByEmployee = new Map<string, InventoryDeferredOnPayoutEmployee>(
    deferredOnPayout.by_employee.map((row) => [row.employee_id, row]),
  );
  const deductionTotal = Number(audit.total_penalty_amount) + Number(deferredOnPayout.total);
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
  const totalShortageConsidered = audit.total_shortage_considered ?? audit.total_shortage_amount;
  const payrollPeriodStart =
    audit.penalty_work_date_effective ?? payrollPeriodStartForAudit(audit.business_date);
  const hasOverride = audit.penalty_work_date_override != null;
  const payoutOptions: InventoryPayoutOption[] = payoutOptionsQuery.data ?? [];
  const selectedPayout =
    payoutOptions.find(
      (option) =>
        payrollPeriodStart >= option.period_start && payrollPeriodStart <= option.period_end,
    ) ?? null;
  const isFinalized = audit.status !== "draft";
  const isOverrideDisabled =
    audit.status !== "draft" || overrideMutation.isPending || itemExclusionMutation.isPending;
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
            {period?.start && period?.end
              ? ` · Период ${formatDate(period.start)} — ${formatDate(period.end)}`
              : ""}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span>
              Штраф спишется в выплату{" "}
              {formatDate(selectedPayout ? selectedPayout.payout_date : payrollPeriodStart)}
              {hasOverride ? " · перенесено" : ""}
            </span>
            {audit.status === "draft" ? (
              <Select
                value={selectedPayout?.period_start ?? ""}
                onValueChange={(value) => {
                  const option = payoutOptions.find((item) => item.period_start === value);
                  if (option) {
                    penaltyDateMutation.mutate(option.override_value);
                  }
                }}
                disabled={penaltyDateMutation.isPending || payoutOptionsQuery.isLoading}
              >
                <SelectTrigger className="h-7 w-auto min-w-[15rem] text-xs">
                  <SelectValue placeholder="Перенести списание…" />
                </SelectTrigger>
                <SelectContent>
                  {payoutOptions.map((option) => (
                    <SelectItem
                      key={option.period_start}
                      value={option.period_start}
                      disabled={option.locked}
                    >
                      Выплата {formatDate(option.payout_date)}
                      {option.is_default ? " (по умолчанию)" : ""}
                      {option.locked ? " — закрыт" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
          </div>
          {deferredOnPayout.charges.length > 0 ? (
            <div className="mt-2 rounded-md border border-dashed bg-muted/20 px-3 py-2 text-xs leading-5 text-muted-foreground">
              <div className="font-medium text-foreground/80">
                Распределённые штрафы на эту выплату
              </div>
              {deferredOnPayout.charges.map((row) => (
                <div key={`${row.charge_id}-${row.split_index}`}>
                  Ревизия {row.source_audit_date ? formatDate(row.source_audit_date) : "—"}
                  {row.source_item_name ? ` (${row.source_item_name})` : ""}: доля{" "}
                  {row.split_index}/{row.splits_count} — {formatMoney(row.total_amount)} на{" "}
                  {row.recipient_count} сотр.{row.applied ? " · применена" : " · ожидает"}
                </div>
              ))}
            </div>
          ) : null}
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
                <RotateCcw size={16} aria-hidden="true" />В черновик
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
              (
              {formatItemsCount(
                items.length,
                "активная позиция",
                "активные позиции",
                "активных позиций",
              )}
              {skippedCount > 0 ? `; ${skippedCount} скрыто как неактивные` : ""})
            </span>
          </span>
        </div>
        {Number(deferredOnPayout.total) > 0 ? (
          <div className="grid gap-2 sm:grid-cols-[190px_minmax(0,1fr)] sm:items-baseline">
            <span className="font-medium">Сумма списания:</span>
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-lg font-semibold tabular-nums">
                {formatMoney(deductionTotal.toFixed(2))}
              </span>
              <InlineTooltip
                content={`Штраф ревизии ${formatMoney(
                  audit.total_penalty_amount,
                )} + распределённое на эту выплату ${formatMoney(deferredOnPayout.total)}`}
              >
                <Info size={14} aria-hidden="true" className="text-muted-foreground" />
              </InlineTooltip>
            </span>
          </div>
        ) : null}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(320px,440px)]">
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
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-b bg-muted/35">
                    <th className="p-3 text-left font-medium">Позиция</th>
                    <th className="px-2 py-3 text-left font-medium w-[110px]">Группа</th>
                    <th className="px-2 py-3 text-right font-medium w-[110px]">Сумма</th>
                    <th className="px-2 py-3 text-center font-medium w-[64px]">Учёт</th>
                    <th className="px-2 py-3 text-right font-medium w-[56px]">Действия</th>
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
                          loading={overrideMutation.isPending || itemExclusionMutation.isPending}
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
                          onToggleInclude={(item, nextExcluded) => {
                            setItemExclusionTarget({
                              item,
                              itemId: item.id,
                              productName: item.product_name_snapshot,
                              nextExcluded,
                            });
                            setItemExclusionReason(item.exclusion_reason ?? "");
                          }}
                        />
                      ) : (
                        <SwapGroupSummaryRow
                          key={`summary-${row.summary.group}`}
                          summary={row.summary}
                        />
                      ),
                    )
                  ) : (
                    <tr>
                      <td className="p-3 text-sm text-muted-foreground" colSpan={5}>
                        По выбранному фильтру строк нет
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="rounded-md border px-3 py-4 text-sm text-muted-foreground">
              Ни одна позиция документа не активна в whitelist. Активируйте нужные в Исходных →
              Ревизии.
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
                      deferred={deferredByEmployee.get(recipient.employee_id)}
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
        open={itemExclusionTarget !== null}
        onOpenChange={(open) => {
          if (!open) {
            setItemExclusionTarget(null);
            setItemExclusionReason("");
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {itemExclusionTarget?.nextExcluded
                ? "Исключить позицию из расчёта"
                : "Вернуть позицию в расчёт"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {itemExclusionTarget?.nextExcluded ? (
                <>
                  Позиция <b>{itemExclusionTarget.productName}</b> не попадёт в недостачи и штрафы
                  этой ревизии. Укажите причину исключения — это обязательно и будет зафиксировано в
                  журнале.
                </>
              ) : (
                <>
                  Позиция <b>{itemExclusionTarget?.productName}</b> снова будет учтена в расчёте
                  штрафа.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {itemExclusionTarget?.nextExcluded ? (
            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="item-exclusion-reason">
                Причина исключения
              </label>
              <Textarea
                id="item-exclusion-reason"
                value={itemExclusionReason}
                onChange={(event) => setItemExclusionReason(event.target.value)}
                placeholder="Например: брак на приёмке, списано документом N…"
                rows={3}
                maxLength={500}
              />
            </div>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={itemExclusionMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={
                itemExclusionMutation.isPending ||
                (itemExclusionTarget?.nextExcluded === true &&
                  itemExclusionReason.trim().length === 0)
              }
              onClick={(event) => {
                event.preventDefault();
                if (!itemExclusionTarget) {
                  return;
                }
                itemExclusionMutation.mutate({
                  itemId: itemExclusionTarget.itemId,
                  excluded: itemExclusionTarget.nextExcluded,
                  reason: itemExclusionTarget.nextExcluded ? itemExclusionReason.trim() : null,
                });
              }}
            >
              {itemExclusionTarget?.nextExcluded ? "Исключить" : "Вернуть"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(overrideTarget)}
        onOpenChange={(open) => !open && setOverrideTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{overrideTarget?.label ?? "Изменить группу"}</AlertDialogTitle>
            <AlertDialogDescription>Это изменит расчёт штрафа. Применить?</AlertDialogDescription>
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
  onToggleInclude,
}: {
  disabled: boolean;
  finalized: boolean;
  item: InventoryAuditItem;
  loading: boolean;
  onExclude: (item: InventoryAuditItem) => void;
  onMove: (item: InventoryAuditItem) => void;
  onToggleInclude: (item: InventoryAuditItem, nextExcluded: boolean) => void;
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
        item.is_excluded && "opacity-60 line-through",
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
      <td className="px-2 py-3">
        <InventoryGroupBadge group={item.allocation_group} />
      </td>
      <td className="px-2 py-3 text-right tabular-nums">{formatSignedMoney(item.amount)}</td>
      <td className="px-2 py-3 text-center">
        <InlineTooltip
          content={
            item.is_excluded
              ? `Исключена: ${item.exclusion_reason ?? "без комментария"}`
              : "Учитывается в расчёте штрафа. Выключи, чтобы исключить."
          }
        >
          <Switch
            aria-checked={!item.is_excluded}
            aria-label="Учитывать позицию в расчёте"
            checked={!item.is_excluded}
            disabled={disabled || loading || finalized}
            onCheckedChange={(next) => onToggleInclude(item, !next)}
          />
        </InlineTooltip>
      </td>
      <td className="px-2 py-3 text-right">
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
  deferred,
  disabled,
  loading,
  onIncludedChange,
  recipient,
}: {
  deferred?: InventoryDeferredOnPayoutEmployee;
  disabled: boolean;
  loading: boolean;
  onIncludedChange: (included: boolean) => void;
  recipient: InventoryEmployeeRecipient;
}) {
  const included = !recipient.is_excluded;
  const hasDeferred = Boolean(deferred) && Number(deferred?.total ?? 0) > 0;
  const displayedAmount = hasDeferred
    ? (Number(recipient.amount) + Number(deferred?.total ?? 0)).toFixed(2)
    : recipient.amount;
  const deferredTooltip =
    hasDeferred && deferred
      ? `${formatMoney(recipient.amount)} — штраф ревизии${deferred.items
          .map(
            (item) =>
              `; +${formatMoney(item.amount)} распределённый штраф (ревизия ${
                item.source_audit_date ? formatDate(item.source_audit_date) : "—"
              }${item.source_item_name ? `, ${item.source_item_name}` : ""}, доля ${
                item.split_index
              }/${item.splits_count})`,
          )
          .join("")}`
      : "";
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
      <div className="flex items-center gap-1 font-medium tabular-nums sm:justify-end">
        {formatMoney(displayedAmount)}
        {hasDeferred ? (
          <InlineTooltip align="right" content={deferredTooltip}>
            <Info size={13} aria-hidden="true" className="text-muted-foreground" />
          </InlineTooltip>
        ) : null}
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

function DeferredChargeStatusBadge({ status }: { status: DeferredChargeStatus }) {
  const label =
    status === "pending"
      ? "Ожидает"
      : status === "partially_applied"
        ? "Частично применён"
        : status === "applied"
          ? "Применён"
          : "Отменён";
  const variant: "default" | "secondary" | "outline" =
    status === "applied" ? "default" : status === "cancelled" ? "outline" : "secondary";
  return <Badge variant={variant}>{label}</Badge>;
}

function deferredChargeTotalSplits(charge: DeferredCharge) {
  return charge.splits_count * charge.recipients.length;
}

function deferredChargeAppliedSplits(charge: DeferredCharge) {
  const remaining = charge.recipients.reduce(
    (sum, recipient) => sum + recipient.splits_remaining,
    0,
  );
  return Math.max(0, deferredChargeTotalSplits(charge) - remaining);
}

function allocationGroupLabel(group: InventoryAllocationGroup) {
  if (group === "chefs") {
    return "повара";
  }
  if (group === "admins") {
    return "кассиры";
  }
  return "общая";
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

function InlineTooltip({
  align = "left",
  children,
  content,
}: {
  align?: "left" | "right";
  children: ReactNode;
  content: string;
}) {
  return (
    <span className="group relative inline-flex w-fit">
      {children}
      <span
        className={cn(
          "pointer-events-none absolute top-full z-50 mt-2 hidden max-w-[min(20rem,calc(100vw-2rem))] rounded-md border bg-popover px-3 py-2 text-left text-xs leading-5 text-popover-foreground shadow-lg group-focus-within:block group-hover:block",
          align === "right" ? "right-0" : "left-0",
        )}
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
    queryClient.invalidateQueries({ queryKey: ["inventory-exclusions"] }),
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

function formatDateTime(value: string) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return formatDate(value);
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
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

function pluralizeRuns(count: number) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) {
    return "расчёт";
  }
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) {
    return "расчёта";
  }
  return "расчётов";
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
