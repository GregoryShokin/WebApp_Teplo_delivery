import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, LoaderCircle, RefreshCw, Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
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
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  buildShiftLedger,
  getShiftLedger,
  patchShiftLedgerEntry,
  type EmployeeCategory,
  type ShiftLedgerAvailableRole,
  type ShiftLedgerEntry,
} from "@/lib/api";
import { EMPLOYEE_CATEGORY_LABELS, PAYROLL_ROLE_LABELS } from "@/lib/i18n/employee";
import { cn } from "@/lib/utils";

type Draft = {
  payroll_role: string;
  category: EmployeeCategory | "";
};

const sourceLabels: Record<ShiftLedgerEntry["source"], string> = {
  schedule: "График",
  fallback_primary: "Основная роль",
  manual_correction: "Ручная правка",
};

export function PayrollDailyLedgerRoute() {
  const queryClient = useQueryClient();
  const [workDate, setWorkDate] = useState(todayIsoDate);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  const ledgerQuery = useQuery({
    queryKey: ["shift-ledger", workDate],
    queryFn: () => getShiftLedger(workDate),
  });

  const rows = useMemo(
    () => [...(ledgerQuery.data ?? [])].sort(compareLedgerRows),
    [ledgerQuery.data],
  );

  useEffect(() => {
    setDrafts((current) => {
      const next: Record<string, Draft> = {};
      for (const row of rows) {
        next[row.id] = current[row.id] ?? draftFromRow(row);
      }
      return next;
    });
  }, [rows]);

  const buildMutation = useMutation({
    mutationFn: () => buildShiftLedger(workDate),
    onSuccess: async () => {
      toast.success("Смены обновлены из iiko");
      await queryClient.invalidateQueries({ queryKey: ["shift-ledger", workDate] });
    },
    onError: (error) => toast.error((error as Error).message),
  });

  const patchMutation = useMutation({
    mutationFn: ({ id, draft }: { id: string; draft: Draft }) =>
      patchShiftLedgerEntry(id, {
        payroll_role: draft.payroll_role,
      }),
    onSuccess: async () => {
      toast.success("Смена скорректирована");
      await queryClient.invalidateQueries({ queryKey: ["shift-ledger", workDate] });
    },
    onError: (error) => toast.error((error as Error).message),
  });

  const columns: Array<DataTableColumn<ShiftLedgerEntry>> = [
    {
      key: "employee",
      header: "Имя",
      cell: (row) => (
        <div className="min-w-[220px]">
          <div className="font-medium">{row.employee_name}</div>
          <div className="text-xs text-muted-foreground">{row.employee_iiko_id}</div>
        </div>
      ),
    },
    {
      key: "opened",
      header: "Открытие смены",
      cell: (row) => formatDateTime(row.opened_at),
      className: "whitespace-nowrap tabular-nums",
    },
    {
      key: "closed",
      header: "Закрытие",
      cell: (row) => formatDateTime(row.closed_at),
      className: "whitespace-nowrap tabular-nums",
    },
    {
      key: "role",
      header: "Payroll-роль",
      cell: (row) => {
        const draft = drafts[row.id] ?? draftFromRow(row);
        const availableRoles = row.available_roles ?? [];

        if (availableRoles.length === 0) {
          return (
            <div className="flex min-w-[220px] flex-wrap items-center gap-2">
              <Badge className="border-amber-200 bg-amber-50 text-amber-800" variant="outline">
                Не назначены роли в Штате
              </Badge>
              <Button
                className="h-8 px-2"
                onClick={() => navigateToStaff(row.employee_id)}
                size="sm"
                type="button"
                variant="outline"
              >
                <ExternalLink size={14} aria-hidden="true" />
                Перейти в Штат
              </Button>
            </div>
          );
        }

        if (availableRoles.length === 1) {
          return <ReadOnlyValue>{roleLabel(availableRoles[0].payroll_role)}</ReadOnlyValue>;
        }

        return (
          <Select
            value={draft.payroll_role || undefined}
            onValueChange={(value) => updateDraftFromRole(row, value)}
          >
            <SelectTrigger className="h-9 min-w-[170px] bg-background">
              <SelectValue placeholder="Выбрать" />
            </SelectTrigger>
            <SelectContent>
              {availableRoles.map((role) => (
                <SelectItem key={role.payroll_role} value={role.payroll_role}>
                  {roleLabel(role.payroll_role)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        );
      },
    },
    {
      key: "category",
      header: "Категория",
      cell: (row) => {
        const draft = drafts[row.id] ?? draftFromRow(row);
        return <ReadOnlyValue>{categoryLabel(draft.category)}</ReadOnlyValue>;
      },
    },
    {
      key: "source",
      header: "Источник",
      cell: (row) => sourceLabels[row.source],
      className: "whitespace-nowrap",
    },
    {
      key: "resolution",
      header: "Резолюция",
      cell: (row) =>
        row.status === "needs_employee_setup" ? (
          <Badge className="border-amber-200 bg-amber-50 text-amber-800" variant="outline">
            Нет ролей в Штате
          </Badge>
        ) : row.is_resolved ? (
          <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700" variant="outline">
            Готово
          </Badge>
        ) : (
          <Badge className="border-amber-200 bg-amber-50 text-amber-800" variant="outline">
            Требует выбора
          </Badge>
        ),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (row) => {
        const draft = drafts[row.id] ?? draftFromRow(row);
        const canSave = canSaveDraft(row, draft);
        const isSaving = patchMutation.isPending && patchMutation.variables?.id === row.id;
        return (
          <Button
            disabled={!canSave || isSaving}
            onClick={() => patchMutation.mutate({ id: row.id, draft })}
            size="icon"
            title="Сохранить"
            variant="outline"
          >
            {isSaving ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Save size={16} aria-hidden="true" />
            )}
          </Button>
        );
      },
    },
  ];

  function updateDraft(id: string, patch: Partial<Draft>) {
    setDrafts((current) => ({
      ...current,
      [id]: {
        ...(current[id] ?? { payroll_role: "", category: "" }),
        ...patch,
      },
    }));
  }

  function updateDraftFromRole(row: ShiftLedgerEntry, payrollRole: string) {
    const assignment = findAvailableRole(row.available_roles, payrollRole);
    updateDraft(row.id, {
      payroll_role: payrollRole,
      category: assignment?.category ?? "",
    });
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Учёт смен"
        action={
          <Button onClick={() => buildMutation.mutate()} disabled={buildMutation.isPending}>
            {buildMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            Обновить из iiko
          </Button>
        }
      />

      <section className="flex flex-col gap-3 rounded-lg border bg-card p-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="grid gap-2">
          <Label htmlFor="shift-ledger-date">Дата</Label>
          <Input
            className="w-full sm:w-[180px]"
            id="shift-ledger-date"
            onChange={(event) => setWorkDate(event.target.value)}
            type="date"
            value={workDate}
          />
        </div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <AlertTriangle className="h-4 w-4 text-amber-600" aria-hidden="true" />
          <span>{unresolvedCount(rows)} требуют ручного выбора</span>
        </div>
      </section>

      {rows.length === 0 && !ledgerQuery.isLoading ? (
        <EmptyState
          icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
          title="На эту дату нет открытых смен"
        />
      ) : (
        <DataTable
          columns={columns}
          emptyMessage="На эту дату нет открытых смен"
          getRowKey={(row) => row.id}
          isLoading={ledgerQuery.isLoading}
          rowClassName={(row) =>
            cn(!row.is_resolved && "bg-amber-50/80 hover:bg-amber-50 dark:bg-amber-950/20")
          }
          rows={rows}
        />
      )}
    </div>
  );
}

function draftFromRow(row: ShiftLedgerEntry): Draft {
  const singleAssignment = row.available_roles?.length === 1 ? row.available_roles[0] : null;
  const payrollRole = row.payroll_role ?? singleAssignment?.payroll_role ?? "";
  const assignment = findAvailableRole(row.available_roles, payrollRole);
  return {
    payroll_role: payrollRole,
    category: assignment?.category ?? row.category ?? singleAssignment?.category ?? "",
  };
}

function canSaveDraft(row: ShiftLedgerEntry, draft: Draft) {
  const assignment = findAvailableRole(row.available_roles, draft.payroll_role);
  return (
    Boolean(assignment && draft.category) &&
    (!row.is_resolved || draft.payroll_role !== (row.payroll_role ?? ""))
  );
}

function findAvailableRole(
  availableRoles: ShiftLedgerAvailableRole[] | undefined,
  payrollRole: string,
) {
  return availableRoles?.find((role) => role.payroll_role === payrollRole);
}

function roleLabel(payrollRole: string) {
  return PAYROLL_ROLE_LABELS[payrollRole as keyof typeof PAYROLL_ROLE_LABELS] ?? payrollRole;
}

function categoryLabel(category: EmployeeCategory | "") {
  return category ? EMPLOYEE_CATEGORY_LABELS[category] : "—";
}

function ReadOnlyValue({ children }: { children: string }) {
  return <span className="inline-flex min-h-9 items-center text-sm">{children}</span>;
}

function navigateToStaff(employeeId: string) {
  window.history.pushState({}, "", `/staff?employee=${encodeURIComponent(employeeId)}`);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function todayIsoDate() {
  const now = new Date();
  const timezoneOffsetMs = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - timezoneOffsetMs).toISOString().slice(0, 10);
}

function compareLedgerRows(left: ShiftLedgerEntry, right: ShiftLedgerEntry) {
  return (
    Date.parse(left.opened_at) - Date.parse(right.opened_at) ||
    left.employee_name.localeCompare(right.employee_name, "ru")
  );
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function unresolvedCount(rows: ShiftLedgerEntry[]) {
  return rows.filter((row) => !row.is_resolved).length;
}
