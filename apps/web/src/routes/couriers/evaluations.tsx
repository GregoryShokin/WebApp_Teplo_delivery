import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Filter,
  Globe,
  MessageSquare,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  Trash2,
  UserRoundCheck,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
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
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  apiErrorStatus,
  createCourierEvaluation,
  deleteCourierEvaluation,
  getCourierEvaluationCriteria,
  getCourierEvaluations,
  getEmployees,
  patchCourierEvaluation,
  type CourierEvaluation,
  type CourierEvaluationCriterion,
  type CourierEvaluationSource,
  type Employee,
} from "@/lib/api";
import { getAuthSnapshot, subscribeAuth, type AuthUser } from "@/lib/auth";
import { cn } from "@/lib/utils";

type PeriodPreset = "today" | "week" | "current-month" | "previous-month" | "custom";
type ScoreTone = "positive" | "neutral" | "negative";
type FormMode = "create" | "edit";

type EvaluationFormState = {
  courierId: string;
  criterionId: string;
  comment: string;
  evaluatedAt: string;
};

const PAGE_SIZE = 20;
const EDIT_WINDOW_MINUTES = 30;
const QUICK_CREATE_FLAG = "courierEvaluation.openForm";

const sourceLabels: Record<CourierEvaluationSource, string> = {
  web: "web",
  telegram: "telegram",
  api: "api",
};

const sourceOptions: Array<{ value: CourierEvaluationSource; label: string }> = [
  { value: "web", label: "web" },
  { value: "telegram", label: "telegram" },
  { value: "api", label: "api" },
];

export function CourierEvaluationsRoute() {
  const queryClient = useQueryClient();
  const [auth, setAuth] = useState(getAuthSnapshot);
  const [period, setPeriod] = useState<PeriodPreset>("current-month");
  const [customFrom, setCustomFrom] = useState(startOfCurrentMonth());
  const [customTo, setCustomTo] = useState(todayInput());
  const [courierFilter, setCourierFilter] = useState<string[]>([]);
  const [authorFilter, setAuthorFilter] = useState<string[]>([]);
  const [criterionFilter, setCriterionFilter] = useState<string[]>([]);
  const [sourceFilter, setSourceFilter] = useState<string[]>([]);
  const [onlyMine, setOnlyMine] = useState(false);
  const [page, setPage] = useState(1);
  const [nowMs, setNowMs] = useState(Date.now());
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [formMode, setFormMode] = useState<FormMode>("create");
  const [editingEvaluation, setEditingEvaluation] = useState<CourierEvaluation | null>(null);
  const [form, setForm] = useState<EvaluationFormState>(emptyForm());
  const [permissionBlocked, setPermissionBlocked] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<CourierEvaluation | null>(null);

  useEffect(() => {
    const unsubscribe = subscribeAuth(setAuth);
    return () => {
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 10_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const openFromQuickAction = () => {
      window.sessionStorage.removeItem(QUICK_CREATE_FLAG);
      openCreateForm();
    };

    if (window.sessionStorage.getItem(QUICK_CREATE_FLAG) === "1") {
      openFromQuickAction();
    }

    window.addEventListener("courier-evaluation:create", openFromQuickAction);
    return () => window.removeEventListener("courier-evaluation:create", openFromQuickAction);
  }, []);

  const dateRange = useMemo(
    () => getDateRange(period, customFrom, customTo),
    [customFrom, customTo, period],
  );

  const criteriaQuery = useQuery({
    queryKey: ["evaluation-criteria"],
    queryFn: getCourierEvaluationCriteria,
  });
  const evaluationsQuery = useQuery({
    queryKey: ["evaluations", dateRange.from, dateRange.to],
    queryFn: () => getCourierEvaluations({ from: dateRange.from, to: dateRange.to }),
  });
  const employeesQuery = useQuery({
    queryKey: ["employees", "courier-evaluations-roster"],
    queryFn: () => getEmployees({ status: "all" }),
  });

  const employees = useMemo(() => employeesQuery.data ?? [], [employeesQuery.data]);
  const criteria = useMemo(() => criteriaQuery.data ?? [], [criteriaQuery.data]);
  const evaluations = useMemo(() => evaluationsQuery.data ?? [], [evaluationsQuery.data]);
  const employeeById = useMemo(() => indexById(employees), [employees]);
  const criterionById = useMemo(() => indexCriteriaById(criteria), [criteria]);
  const activeCouriers = useMemo(
    () =>
      employees
        .filter((employee) => employee.position === "Курьер" && employee.status === "active")
        .sort(byFullName),
    [employees],
  );
  const authorOptions = useMemo(() => getAuthorOptions(evaluations, employees), [employees, evaluations]);
  const currentEmployee = useMemo(
    () => resolveCurrentEmployee(auth.user, employees),
    [auth.user, employees],
  );
  const canCreate = Boolean(currentEmployee && isAdminCashier(currentEmployee));
  const hasForbiddenRead =
    apiErrorStatus(criteriaQuery.error) === 403 ||
    apiErrorStatus(evaluationsQuery.error) === 403 ||
    apiErrorStatus(employeesQuery.error) === 403;

  const filteredEvaluations = useMemo(() => {
    const mineId = currentEmployee?.id;
    return evaluations.filter((evaluation) => {
      if (courierFilter.length > 0 && !courierFilter.includes(evaluation.courier_employee_id)) {
        return false;
      }
      if (authorFilter.length > 0 && !authorFilter.includes(evaluation.created_by)) {
        return false;
      }
      if (
        criterionFilter.length > 0 &&
        !criterionFilter.includes(String(evaluation.criterion_id))
      ) {
        return false;
      }
      if (sourceFilter.length > 0 && !sourceFilter.includes(evaluation.source)) {
        return false;
      }
      if (onlyMine && (!mineId || evaluation.created_by !== mineId)) {
        return false;
      }
      return true;
    });
  }, [
    authorFilter,
    courierFilter,
    criterionFilter,
    currentEmployee?.id,
    evaluations,
    onlyMine,
    sourceFilter,
  ]);

  const totalPages = Math.max(1, Math.ceil(filteredEvaluations.length / PAGE_SIZE));
  const visibleEvaluations = filteredEvaluations.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  useEffect(() => {
    setPage(1);
  }, [authorFilter, courierFilter, criterionFilter, dateRange.from, dateRange.to, onlyMine, sourceFilter]);

  useEffect(() => {
    setPage((current) => Math.min(current, totalPages));
  }, [totalPages]);

  const createMutation = useMutation({
    mutationFn: createCourierEvaluation,
    onSuccess: () => {
      toast.success("Отличие зафиксировано");
      setForm(emptyForm());
      setPermissionBlocked(false);
      setIsFormOpen(false);
      void invalidateEvaluationQueries(queryClient);
    },
    onError: (error) => {
      if (apiErrorStatus(error) === 403) {
        setPermissionBlocked(true);
        toast.error("Только администраторы смены могут ставить оценки");
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось зафиксировать отличие"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: Parameters<typeof patchCourierEvaluation>[1] }) =>
      patchCourierEvaluation(id, payload),
    onSuccess: () => {
      toast.success("Оценка обновлена");
      setIsFormOpen(false);
      setEditingEvaluation(null);
      setPermissionBlocked(false);
      void invalidateEvaluationQueries(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось обновить оценку"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteCourierEvaluation(id),
    onSuccess: () => {
      toast.success("Оценка удалена");
      setPendingDelete(null);
      void invalidateEvaluationQueries(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось удалить оценку"));
    },
  });

  function openCreateForm() {
    setFormMode("create");
    setEditingEvaluation(null);
    setForm(emptyForm());
    setPermissionBlocked(false);
    setIsFormOpen(true);
  }

  function openEditForm(evaluation: CourierEvaluation) {
    setFormMode("edit");
    setEditingEvaluation(evaluation);
    setForm({
      courierId: evaluation.courier_employee_id,
      criterionId: String(evaluation.criterion_id),
      comment: evaluation.comment ?? "",
      evaluatedAt: evaluation.evaluated_at,
    });
    setPermissionBlocked(false);
    setIsFormOpen(true);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.criterionId || !form.evaluatedAt) {
      toast.error("Заполните критерий и дату эпизода");
      return;
    }
    if (form.comment.length > 500) {
      toast.error("Комментарий не должен превышать 500 символов");
      return;
    }

    if (formMode === "create") {
      if (!form.courierId) {
        toast.error("Выберите курьера");
        return;
      }
      createMutation.mutate({
        courier_employee_id: form.courierId,
        criterion_id: Number(form.criterionId),
        evaluated_at: form.evaluatedAt,
        comment: cleanComment(form.comment),
        source: "web",
      });
      return;
    }

    if (editingEvaluation?.id == null) {
      toast.error("Оценка не найдена");
      return;
    }
    updateMutation.mutate({
      id: editingEvaluation.id,
      payload: {
        criterion_id: Number(form.criterionId),
        evaluated_at: form.evaluatedAt,
        comment: cleanComment(form.comment),
      },
    });
  }

  const isInitialLoading =
    criteriaQuery.isLoading || evaluationsQuery.isLoading || employeesQuery.isLoading;
  const isMutating = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Оценки курьеров"
        description="Журнал отличий курьеров от администраторов смены."
        action={
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
            <Button
              className="h-12 justify-center text-base sm:h-11"
              disabled={hasForbiddenRead}
              onClick={openCreateForm}
              size="lg"
            >
              <Plus aria-hidden="true" />
              Поставить отличие
            </Button>
            <Button
              className="h-11"
              onClick={() => {
                void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
                void queryClient.invalidateQueries({ queryKey: ["evaluation-criteria"] });
                void queryClient.invalidateQueries({ queryKey: ["employees"] });
              }}
              variant="outline"
            >
              <RefreshCw aria-hidden="true" />
              Обновить
            </Button>
          </div>
        }
      />

      {hasForbiddenRead ? (
        <EmptyState
          icon={<UserRoundCheck className="h-5 w-5" aria-hidden="true" />}
          title="Недостаточно прав"
          description="Эту страницу могут открывать только просматривать. Поставить оценку могут только администраторы смены (Кассир + admin)."
        />
      ) : (
        <>
          <FiltersPanel
            activeCouriers={activeCouriers}
            authorFilter={authorFilter}
            authorOptions={authorOptions}
            criteria={criteria}
            criterionFilter={criterionFilter}
            courierFilter={courierFilter}
            customFrom={customFrom}
            customTo={customTo}
            onAuthorFilterChange={setAuthorFilter}
            onCriterionFilterChange={setCriterionFilter}
            onCourierFilterChange={setCourierFilter}
            onCustomFromChange={setCustomFrom}
            onCustomToChange={setCustomTo}
            onOnlyMineChange={setOnlyMine}
            onPeriodChange={setPeriod}
            onSourceFilterChange={setSourceFilter}
            onlyMine={onlyMine}
            period={period}
            sourceFilter={sourceFilter}
            canFilterMine={Boolean(currentEmployee)}
          />

          {isInitialLoading ? (
            <EvaluationSkeletons />
          ) : visibleEvaluations.length === 0 ? (
            <EmptyState
              icon={<Filter className="h-5 w-5" aria-hidden="true" />}
              title="Оценки не найдены"
              description="Попробуйте изменить фильтры или поставьте первую оценку."
              action={
                <Button disabled={!canCreate} onClick={openCreateForm}>
                  <Plus aria-hidden="true" />
                  Поставить отличие
                </Button>
              }
            />
          ) : (
            <section className="space-y-3" aria-label="Лента оценок">
              {visibleEvaluations.map((evaluation) => (
                <EvaluationCard
                  criterion={criterionById.get(evaluation.criterion_id)}
                  courier={employeeById.get(evaluation.courier_employee_id)}
                  evaluation={evaluation}
                  isMine={currentEmployee?.id === evaluation.created_by}
                  key={evaluation.id ?? `${evaluation.created_by}-${evaluation.created_at}`}
                  nowMs={nowMs}
                  onDelete={() => setPendingDelete(evaluation)}
                  onEdit={() => openEditForm(evaluation)}
                  author={employeeById.get(evaluation.created_by)}
                />
              ))}
            </section>
          )}

          <PaginationFooter
            page={page}
            pageSize={PAGE_SIZE}
            total={filteredEvaluations.length}
            totalPages={totalPages}
            onPageChange={setPage}
          />
        </>
      )}

      <EvaluationFormSheet
        activeCouriers={activeCouriers}
        canCreate={canCreate}
        criteria={criteria}
        form={form}
        formMode={formMode}
        isMutating={isMutating}
        onFormChange={setForm}
        onOpenChange={setIsFormOpen}
        onSubmit={handleSubmit}
        open={isFormOpen}
        permissionBlocked={permissionBlocked}
      />

      <AlertDialog open={pendingDelete !== null} onOpenChange={(open) => !open && setPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить эту оценку?</AlertDialogTitle>
            <AlertDialogDescription>
              Действие необратимо. Оценка будет скрыта из журнала через soft-delete.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (pendingDelete?.id != null) {
                  deleteMutation.mutate(pendingDelete.id);
                }
              }}
            >
              <Trash2 aria-hidden="true" />
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function FiltersPanel({
  activeCouriers,
  authorFilter,
  authorOptions,
  canFilterMine,
  criteria,
  criterionFilter,
  courierFilter,
  customFrom,
  customTo,
  onAuthorFilterChange,
  onCriterionFilterChange,
  onCourierFilterChange,
  onCustomFromChange,
  onCustomToChange,
  onOnlyMineChange,
  onPeriodChange,
  onSourceFilterChange,
  onlyMine,
  period,
  sourceFilter,
}: {
  activeCouriers: Employee[];
  authorFilter: string[];
  authorOptions: Array<{ value: string; label: string }>;
  canFilterMine: boolean;
  criteria: CourierEvaluationCriterion[];
  criterionFilter: string[];
  courierFilter: string[];
  customFrom: string;
  customTo: string;
  onAuthorFilterChange: (value: string[]) => void;
  onCriterionFilterChange: (value: string[]) => void;
  onCourierFilterChange: (value: string[]) => void;
  onCustomFromChange: (value: string) => void;
  onCustomToChange: (value: string) => void;
  onOnlyMineChange: (value: boolean) => void;
  onPeriodChange: (value: PeriodPreset) => void;
  onSourceFilterChange: (value: string[]) => void;
  onlyMine: boolean;
  period: PeriodPreset;
  sourceFilter: string[];
}) {
  const criterionOptions = criteria.map((criterion) => ({
    value: String(criterion.id),
    label: `${criterion.label} ${formatScore(criterion.score)}`,
  }));

  return (
    <section className="space-y-3 rounded-md border bg-card p-3 shadow-none">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <Filter className="h-4 w-4" aria-hidden="true" />
        Фильтры
      </div>

      <div className="flex flex-wrap gap-2">
        {periodPresetOptions.map((option) => (
          <Button
            className="h-9"
            key={option.value}
            onClick={() => onPeriodChange(option.value)}
            size="sm"
            type="button"
            variant={period === option.value ? "default" : "outline"}
          >
            {option.label}
          </Button>
        ))}
      </div>

      {period === "custom" ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="grid gap-2">
            <Label htmlFor="evaluation-filter-from">С даты</Label>
            <Input
              id="evaluation-filter-from"
              onChange={(event) => onCustomFromChange(event.target.value)}
              type="date"
              value={customFrom}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="evaluation-filter-to">По дату</Label>
            <Input
              id="evaluation-filter-to"
              onChange={(event) => onCustomToChange(event.target.value)}
              type="date"
              value={customTo}
            />
          </div>
        </div>
      ) : null}

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <MultiSelectFilter
          label="Курьер"
          onChange={onCourierFilterChange}
          options={activeCouriers.map((employee) => ({ value: employee.id, label: employee.full_name }))}
          placeholder="Все курьеры"
          value={courierFilter}
        />
        <MultiSelectFilter
          label="Автор-админ"
          onChange={onAuthorFilterChange}
          options={authorOptions}
          placeholder="Все авторы"
          value={authorFilter}
        />
        <MultiSelectFilter
          label="Критерий"
          onChange={onCriterionFilterChange}
          options={criterionOptions}
          placeholder="Все критерии"
          value={criterionFilter}
        />
        <MultiSelectFilter
          label="Источник"
          onChange={onSourceFilterChange}
          options={sourceOptions}
          placeholder="Все источники"
          value={sourceFilter}
        />
      </div>

      <div className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2">
        <Label className="text-sm" htmlFor="only-mine">
          Только мои оценки
        </Label>
        <Switch
          checked={onlyMine}
          disabled={!canFilterMine}
          id="only-mine"
          onCheckedChange={onOnlyMineChange}
        />
      </div>
    </section>
  );
}

function MultiSelectFilter({
  label,
  onChange,
  options,
  placeholder,
  value,
}: {
  label: string;
  onChange: (value: string[]) => void;
  options: Array<{ value: string; label: string }>;
  placeholder: string;
  value: string[];
}) {
  const selectedLabels = options
    .filter((option) => value.includes(option.value))
    .map((option) => option.label);
  const triggerLabel =
    selectedLabels.length === 0
      ? placeholder
      : selectedLabels.length === 1
        ? selectedLabels[0]
        : `Выбрано: ${selectedLabels.length}`;

  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button className="h-11 justify-between overflow-hidden px-3" variant="outline">
            <span className="truncate">{triggerLabel}</span>
            <ChevronRight className="h-4 w-4 opacity-60" aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-[min(22rem,calc(100vw-2rem))]">
          <DropdownMenuLabel>{label}</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {options.length === 0 ? (
            <div className="px-2 py-3 text-sm text-muted-foreground">Нет вариантов</div>
          ) : (
            options.map((option) => (
              <DropdownMenuCheckboxItem
                checked={value.includes(option.value)}
                key={option.value}
                onCheckedChange={() => {
                  onChange(toggleValue(value, option.value));
                }}
                onSelect={(event) => event.preventDefault()}
              >
                <span className="truncate">{option.label}</span>
              </DropdownMenuCheckboxItem>
            ))
          )}
          {value.length > 0 ? (
            <>
              <DropdownMenuSeparator />
              <Button className="h-8 w-full" onClick={() => onChange([])} size="sm" variant="ghost">
                Сбросить
              </Button>
            </>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

function EvaluationCard({
  author,
  criterion,
  courier,
  evaluation,
  isMine,
  nowMs,
  onDelete,
  onEdit,
}: {
  author?: Employee;
  criterion?: CourierEvaluationCriterion;
  courier?: Employee;
  evaluation: CourierEvaluation;
  isMine: boolean;
  nowMs: number;
  onDelete: () => void;
  onEdit: () => void;
}) {
  const editable = isMine && isEditWindowOpen(evaluation, nowMs);
  const countdown = editCountdownText(evaluation, nowMs);
  const score = evaluation.score_snapshot;
  const tone = scoreTone(score);

  return (
    <article className="rounded-md border bg-card p-4 shadow-none">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="min-w-0 text-lg font-semibold leading-6">{courier?.full_name ?? "Курьер не найден"}</h2>
            <ScoreBadge label={criterion?.label ?? `Критерий #${evaluation.criterion_id}`} score={score} />
            <Badge className={scoreBadgeClass(tone)} variant="outline">
              {formatScore(score)}
            </Badge>
          </div>
          {evaluation.comment ? (
            <p className="max-w-3xl whitespace-pre-wrap text-sm leading-6 text-foreground">
              {evaluation.comment}
            </p>
          ) : null}
        </div>
        {editable ? (
          <div className="grid grid-cols-2 gap-2 sm:flex sm:shrink-0">
            <Button onClick={onEdit} size="sm" variant="outline">
              <Pencil aria-hidden="true" />
              Изменить
            </Button>
            <Button onClick={onDelete} size="sm" variant="destructive">
              <Trash2 aria-hidden="true" />
              Удалить
            </Button>
          </div>
        ) : null}
      </div>

      <div className="mt-4 flex flex-col gap-2 text-sm text-muted-foreground sm:flex-row sm:flex-wrap sm:items-center">
        <span className="inline-flex items-center gap-1.5">
          <SourceIcon source={evaluation.source} />
          {author?.full_name ?? "Автор не найден"} · {sourceLabels[evaluation.source] ?? evaluation.source}
        </span>
        <span className="hidden sm:inline">·</span>
        <span>
          {formatDate(evaluation.evaluated_at)} · создано {formatDateTime(evaluation.created_at)}
        </span>
      </div>

      <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
        <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
        {isMine ? (
          editable ? (
            <span>{countdown}</span>
          ) : (
            <span>Окно редактирования закрыто</span>
          )
        ) : (
          <span>Только автор может изменить оценку</span>
        )}
      </div>
    </article>
  );
}

function ScoreBadge({ label, score }: { label: string; score: number }) {
  return (
    <Badge className={scoreBadgeClass(scoreTone(score))} variant="outline">
      <span className="truncate">{label}</span>
    </Badge>
  );
}

function SourceIcon({ source }: { source: CourierEvaluationSource }) {
  const Icon = source === "telegram" ? MessageSquare : source === "api" ? Plug : Globe;
  return <Icon className="h-4 w-4" aria-hidden="true" />;
}

function EvaluationFormSheet({
  activeCouriers,
  canCreate,
  criteria,
  form,
  formMode,
  isMutating,
  onFormChange,
  onOpenChange,
  onSubmit,
  open,
  permissionBlocked,
}: {
  activeCouriers: Employee[];
  canCreate: boolean;
  criteria: CourierEvaluationCriterion[];
  form: EvaluationFormState;
  formMode: FormMode;
  isMutating: boolean;
  onFormChange: (value: EvaluationFormState) => void;
  onOpenChange: (value: boolean) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  open: boolean;
  permissionBlocked: boolean;
}) {
  const today = todayInput();
  const yesterday = addDaysInput(new Date(), -1);
  const canSubmit =
    formMode === "edit" ||
    (canCreate && !permissionBlocked && Boolean(form.courierId) && Boolean(form.criterionId));

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="flex w-full flex-col overflow-y-auto p-0 sm:max-w-xl" side="right">
        <form className="flex min-h-full flex-col" onSubmit={onSubmit}>
          <SheetHeader className="border-b px-5 py-5 text-left">
            <SheetTitle>
              {formMode === "edit" ? "Изменить отличие" : "Поставить отличие"}
            </SheetTitle>
            <SheetDescription>
              Курьер, критерий и дата эпизода. Комментарий можно оставить пустым.
            </SheetDescription>
          </SheetHeader>

          <div className="grid flex-1 gap-5 px-5 py-5">
            {!canCreate && formMode === "create" ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                Только администраторы смены могут ставить оценки.
              </div>
            ) : null}
            {permissionBlocked ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                Только администраторы смены могут ставить оценки.
              </div>
            ) : null}

            <div className="grid gap-2">
              <Label htmlFor="evaluation-courier">Курьер</Label>
              <Select
                disabled={formMode === "edit"}
                onValueChange={(courierId) => onFormChange({ ...form, courierId })}
                value={form.courierId}
              >
                <SelectTrigger className="h-12" id="evaluation-courier">
                  <SelectValue placeholder="Выберите курьера" />
                </SelectTrigger>
                <SelectContent className="max-h-[70vh]">
                  {activeCouriers.map((employee) => (
                    <SelectItem key={employee.id} value={employee.id}>
                      {employee.full_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="evaluation-criterion">Критерий</Label>
              <Select
                onValueChange={(criterionId) => onFormChange({ ...form, criterionId })}
                value={form.criterionId}
              >
                <SelectTrigger className="h-12" id="evaluation-criterion">
                  <SelectValue placeholder="Выберите критерий" />
                </SelectTrigger>
                <SelectContent className="max-h-[70vh]">
                  <CriterionSelectGroup criteria={criteria} label="Зелёные" tone="positive" />
                  <SelectSeparator />
                  <CriterionSelectGroup criteria={criteria} label="Серые" tone="neutral" />
                  <SelectSeparator />
                  <CriterionSelectGroup criteria={criteria} label="Красные" tone="negative" />
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-2">
              <div className="flex items-center justify-between gap-3">
                <Label htmlFor="evaluation-comment">Комментарий</Label>
                <span className="text-xs text-muted-foreground">{form.comment.length}/500</span>
              </div>
              <Textarea
                className="min-h-28 resize-none"
                id="evaluation-comment"
                maxLength={500}
                onChange={(event) => onFormChange({ ...form, comment: event.target.value })}
                placeholder="Например: быстро взял сложный заказ"
                value={form.comment}
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="evaluation-date">Дата эпизода</Label>
              <div className="relative">
                <CalendarDays
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  className="h-12 pl-9"
                  id="evaluation-date"
                  max={today}
                  min={yesterday}
                  onChange={(event) => onFormChange({ ...form, evaluatedAt: event.target.value })}
                  type="date"
                  value={form.evaluatedAt}
                />
              </div>
            </div>
          </div>

          <div className="sticky bottom-0 grid gap-2 border-t bg-background px-5 py-4 sm:grid-cols-2">
            <Button
              className="h-12"
              disabled={!canSubmit || isMutating}
              type="submit"
            >
              {formMode === "edit" ? "Сохранить" : "Зафиксировать отличие"}
            </Button>
            <Button className="h-12" onClick={() => onOpenChange(false)} type="button" variant="outline">
              Отмена
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}

function CriterionSelectGroup({
  criteria,
  label,
  tone,
}: {
  criteria: CourierEvaluationCriterion[];
  label: string;
  tone: ScoreTone;
}) {
  const options = criteria
    .filter((criterion) => scoreTone(criterion.score) === tone)
    .sort((first, second) => {
      if (tone === "neutral") {
        return first.display_order - second.display_order;
      }
      return Math.abs(second.score) - Math.abs(first.score) || first.display_order - second.display_order;
    });

  return (
    <SelectGroup>
      <SelectLabel>{label}</SelectLabel>
      {options.map((criterion) => (
        <SelectItem key={criterion.id} value={String(criterion.id)}>
          <span className="inline-flex w-full items-center justify-between gap-3">
            <span className="truncate">{criterion.label}</span>
            <span className={cn("text-xs font-semibold", scoreTextClass(scoreTone(criterion.score)))}>
              {formatScore(criterion.score)}
            </span>
          </span>
        </SelectItem>
      ))}
    </SelectGroup>
  );
}

function EvaluationSkeletons() {
  return (
    <section className="space-y-3" aria-label="Загрузка оценок">
      {Array.from({ length: 4 }).map((_, index) => (
        <div className="rounded-md border bg-card p-4" key={index}>
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-3">
              <Skeleton className="h-6 w-56" />
              <Skeleton className="h-5 w-72" />
            </div>
            <Skeleton className="h-9 w-28" />
          </div>
          <Skeleton className="mt-5 h-5 w-full max-w-xl" />
          <Skeleton className="mt-3 h-4 w-80" />
        </div>
      ))}
    </section>
  );
}

function PaginationFooter({
  onPageChange,
  page,
  pageSize,
  total,
  totalPages,
}: {
  onPageChange: (page: number) => void;
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
}) {
  if (total === 0) {
    return null;
  }
  const from = (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);

  return (
    <footer className="flex flex-col gap-3 rounded-md border bg-card px-4 py-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
      <span>
        Показано {from}-{to} из {total}
      </span>
      <div className="flex items-center gap-2">
        <Button
          disabled={page <= 1}
          onClick={() => onPageChange(Math.max(1, page - 1))}
          size="sm"
          variant="outline"
        >
          <ChevronLeft aria-hidden="true" />
          Назад
        </Button>
        <span className="min-w-16 text-center">
          {page} / {totalPages}
        </span>
        <Button
          disabled={page >= totalPages}
          onClick={() => onPageChange(Math.min(totalPages, page + 1))}
          size="sm"
          variant="outline"
        >
          Вперёд
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
    </footer>
  );
}

const periodPresetOptions: Array<{ value: PeriodPreset; label: string }> = [
  { value: "today", label: "Сегодня" },
  { value: "week", label: "Неделя" },
  { value: "current-month", label: "Текущий месяц" },
  { value: "previous-month", label: "Прошлый месяц" },
  { value: "custom", label: "Custom" },
];

function emptyForm(): EvaluationFormState {
  return {
    courierId: "",
    criterionId: "",
    comment: "",
    evaluatedAt: todayInput(),
  };
}

function invalidateEvaluationQueries(queryClient: ReturnType<typeof useQueryClient>) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["evaluations"] }),
    queryClient.invalidateQueries({ queryKey: ["evaluation-criteria"] }),
  ]);
}

function indexById(employees: Employee[]) {
  return new Map(employees.map((employee) => [employee.id, employee]));
}

function indexCriteriaById(criteria: CourierEvaluationCriterion[]) {
  return new Map(criteria.map((criterion) => [criterion.id, criterion]));
}

function resolveCurrentEmployee(user: AuthUser | null, employees: Employee[]) {
  if (!user) {
    return null;
  }
  return (
    employees.find((employee) => employee.id === user.id) ??
    employees.find((employee) => normalizeName(employee.full_name) === normalizeName(user.full_name)) ??
    null
  );
}

function isAdminCashier(employee: Employee) {
  if (employee.position !== "Кассир") {
    return false;
  }
  return employee.assignments.some((assignment) =>
    ["admin", "administrator"].includes(String(assignment.payroll_role)),
  );
}

function getAuthorOptions(evaluations: CourierEvaluation[], employees: Employee[]) {
  const employeeById = indexById(employees);
  const authorIds = new Set(evaluations.map((evaluation) => evaluation.created_by));
  const knownAuthors = Array.from(authorIds).map((id) => {
    const employee = employeeById.get(id);
    return {
      value: id,
      label: employee?.full_name ?? `Автор ${id.slice(0, 8)}`,
    };
  });
  return knownAuthors.sort((first, second) => first.label.localeCompare(second.label, "ru"));
}

function byFullName(first: Employee, second: Employee) {
  return first.full_name.localeCompare(second.full_name, "ru");
}

function toggleValue(values: string[], value: string) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function scoreTone(score: number): ScoreTone {
  if (score > 0) {
    return "positive";
  }
  if (score < 0) {
    return "negative";
  }
  return "neutral";
}

function scoreBadgeClass(tone: ScoreTone) {
  if (tone === "positive") {
    return "border-emerald-200 bg-emerald-50 text-emerald-800";
  }
  if (tone === "negative") {
    return "border-rose-200 bg-rose-50 text-rose-800";
  }
  return "border-slate-200 bg-slate-50 text-slate-700";
}

function scoreTextClass(tone: ScoreTone) {
  if (tone === "positive") {
    return "text-emerald-700";
  }
  if (tone === "negative") {
    return "text-rose-700";
  }
  return "text-slate-600";
}

function formatScore(score: number) {
  if (score > 0) {
    return `+${score}`;
  }
  if (score < 0) {
    return `−${Math.abs(score)}`;
  }
  return "0";
}

function isEditWindowOpen(evaluation: CourierEvaluation, nowMs: number) {
  if (!evaluation.created_at) {
    return false;
  }
  return nowMs < Date.parse(evaluation.created_at) + EDIT_WINDOW_MINUTES * 60_000;
}

function editCountdownText(evaluation: CourierEvaluation, nowMs: number) {
  if (!evaluation.created_at) {
    return "Окно редактирования закрыто";
  }
  const closesAt = Date.parse(evaluation.created_at) + EDIT_WINDOW_MINUTES * 60_000;
  const minutesLeft = Math.max(0, Math.ceil((closesAt - nowMs) / 60_000));
  return `Можно изменить ещё ${minutesLeft} мин`;
}

function getDateRange(period: PeriodPreset, customFrom: string, customTo: string) {
  const now = new Date();
  if (period === "today") {
    const today = toDateInput(now);
    return { from: today, to: today };
  }
  if (period === "week") {
    return { from: toDateInput(startOfWeek(now)), to: toDateInput(now) };
  }
  if (period === "previous-month") {
    const first = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    const last = new Date(now.getFullYear(), now.getMonth(), 0);
    return { from: toDateInput(first), to: toDateInput(last) };
  }
  if (period === "custom") {
    return { from: customFrom, to: customTo };
  }
  return { from: startOfCurrentMonth(), to: toDateInput(now) };
}

function startOfWeek(value: Date) {
  const date = new Date(value);
  const day = date.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  date.setDate(date.getDate() + diff);
  return date;
}

function startOfCurrentMonth() {
  const now = new Date();
  return toDateInput(new Date(now.getFullYear(), now.getMonth(), 1));
}

function todayInput() {
  return toDateInput(new Date());
}

function addDaysInput(value: Date, days: number) {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return toDateInput(date);
}

function toDateInput(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium" }).format(new Date(value));
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

function cleanComment(value: string) {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function normalizeName(value: string) {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase("ru-RU");
}
