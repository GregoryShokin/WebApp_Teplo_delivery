import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  LoaderCircle,
  Lock,
  RotateCcw,
  Star,
  Wallet,
} from "lucide-react";
import { toast } from "sonner";

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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { centsFromRubInput, formatCents, isPositiveRubInput } from "@/components/deposits/courier-deposit-utils";
import {
  apiErrorMessage,
  confirmCourierShiftDay,
  createCourierEvaluation,
  getCourierEvaluationCriteria,
  getCourierShiftDay,
  postCourierDepositTransaction,
  putCourierShiftReview,
  unconfirmCourierShiftDay,
  type CourierDepositTransactionType,
  type CourierEvaluationCriterion,
  type CourierShiftDayCourier,
  type CourierShiftDayResponse,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

import { toDateKey } from "./utils";

const CATEGORY_LABELS: Record<"primary" | "secondary", string> = {
  primary: "основной",
  secondary: "доп",
};

export function ShiftInbox() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canEvaluate = permissions.canPerformAction("couriers.evaluations.edit");
  const canDeposit = permissions.canPerformAction("couriers.deposits.edit");
  const canManage = canEvaluate || canDeposit;

  const [dateKey, setDateKey] = useState(() => toDateKey(new Date()));
  const [evalTarget, setEvalTarget] = useState<CourierShiftDayCourier | null>(null);
  const [depositTarget, setDepositTarget] = useState<CourierShiftDayCourier | null>(null);
  const [skipTarget, setSkipTarget] = useState<CourierShiftDayCourier | null>(null);

  const todayKey = toDateKey(new Date());
  const yesterdayKey = toDateKey(addDays(new Date(), -1));
  const evalAllowed = dateKey === todayKey || dateKey === yesterdayKey;

  const shiftDayQuery = useQuery({
    queryKey: ["courier-shift-day", dateKey],
    queryFn: () => getCourierShiftDay(dateKey),
    staleTime: 15_000,
  });

  const data = shiftDayQuery.data;
  const isConfirmed = data?.status === "confirmed";
  const locked = isConfirmed;

  function applySnapshot(snapshot: CourierShiftDayResponse) {
    queryClient.setQueryData(["courier-shift-day", dateKey], snapshot);
  }

  function refreshRelated() {
    void queryClient.invalidateQueries({ queryKey: ["courier-shift-day", dateKey] });
    void queryClient.invalidateQueries({ queryKey: ["courier-deposits"] });
    void queryClient.invalidateQueries({ queryKey: ["evaluations"] });
  }

  const reviewMutation = useMutation({
    mutationFn: ({
      employeeId,
      payload,
    }: {
      employeeId: string;
      payload: Parameters<typeof putCourierShiftReview>[2];
    }) => putCourierShiftReview(dateKey, employeeId, payload),
    onSuccess: (snapshot) => {
      applySnapshot(snapshot);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось обновить смену")),
  });

  const confirmMutation = useMutation({
    mutationFn: () => confirmCourierShiftDay(dateKey),
    onSuccess: (snapshot) => {
      applySnapshot(snapshot);
      toast.success("Смена сохранена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить смену")),
  });

  const unconfirmMutation = useMutation({
    mutationFn: () => unconfirmCourierShiftDay(dateKey),
    onSuccess: (snapshot) => {
      applySnapshot(snapshot);
      toast.success("Смена откатана в черновик");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось откатить смену")),
  });

  const couriers = data?.couriers ?? [];
  const summary = data?.summary ?? { total: 0, ready_count: 0, can_confirm: false };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Button onClick={() => setDateKey(shiftDate(dateKey, -1))} size="icon" variant="outline">
            <ChevronLeft size={16} aria-hidden="true" />
          </Button>
          <div className="min-w-[160px] text-center text-sm font-medium">
            {formatDayLabel(dateKey)}
            {dateKey === todayKey ? <span className="ml-1 text-muted-foreground">· сегодня</span> : null}
          </div>
          <Button
            disabled={dateKey === todayKey}
            onClick={() => setDateKey(shiftDate(dateKey, 1))}
            size="icon"
            variant="outline"
          >
            <ChevronRight size={16} aria-hidden="true" />
          </Button>
          {dateKey !== todayKey ? (
            <Button onClick={() => setDateKey(todayKey)} size="sm" variant="ghost">
              Сегодня
            </Button>
          ) : null}
        </div>

        {canManage ? (
          isConfirmed ? (
            <Button
              disabled={unconfirmMutation.isPending}
              onClick={() => unconfirmMutation.mutate()}
              variant="outline"
            >
              {unconfirmMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <RotateCcw size={16} aria-hidden="true" />
              )}
              Откатить смену
            </Button>
          ) : (
            <Button
              disabled={!summary.can_confirm || confirmMutation.isPending}
              onClick={() => confirmMutation.mutate()}
              title={
                summary.can_confirm
                  ? "Сохранить смену за день"
                  : "Сначала проставьте оценку и депозит у всех курьеров"
              }
            >
              {confirmMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Check size={16} aria-hidden="true" />
              )}
              Сохранить смену
            </Button>
          )
        ) : null}
      </div>

      {isConfirmed ? (
        <div className="flex items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          <Lock size={16} aria-hidden="true" />
          Смена за {formatDayLabel(dateKey)} сохранена
          {data?.confirmed_by_name ? ` — ${data.confirmed_by_name}` : ""}. День закрыт, откатите для правок.
        </div>
      ) : (
        <SummaryLine ready={summary.ready_count} total={summary.total} />
      )}

      {shiftDayQuery.isLoading ? (
        <InboxSkeleton />
      ) : shiftDayQuery.isError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(shiftDayQuery.error, "Не удалось загрузить смену")}
        </div>
      ) : couriers.length === 0 ? (
        <EmptyState
          icon={<Clock3 size={18} aria-hidden="true" />}
          title="Нет курьеров на смене"
          description="За этот день ни один курьер не открывал смену в iiko."
        />
      ) : (
        <div className="space-y-2">
          {couriers.map((courier) => (
            <CourierRow
              key={courier.employee_id}
              courier={courier}
              locked={locked}
              canEvaluate={canEvaluate}
              canDeposit={canDeposit}
              evalAllowed={evalAllowed}
              reviewPending={reviewMutation.isPending}
              onEvaluate={() => setEvalTarget(courier)}
              onDeposit={() => setDepositTarget(courier)}
              onToggleEvalSkip={(value) =>
                reviewMutation.mutate({
                  employeeId: courier.employee_id,
                  payload: { eval_skipped: value },
                })
              }
              onToggleDepositSkip={(value) => {
                if (value) {
                  setSkipTarget(courier);
                } else {
                  reviewMutation.mutate({
                    employeeId: courier.employee_id,
                    payload: { deposit_skipped: false },
                  });
                }
              }}
            />
          ))}
        </div>
      )}

      {data && data.unmatched.length > 0 ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <div className="flex items-center gap-2 font-medium">
            <AlertTriangle size={16} aria-hidden="true" />
            Не сопоставлены со штатом ({data.unmatched.length})
          </div>
          <p className="mt-1 text-amber-700">
            Эти курьеры открыли смену в iiko, но не привязаны к сотруднику. Сопоставьте их в разделе
            «Штат» — до этого их нельзя оценить и они не входят в подтверждение смены.
          </p>
        </div>
      ) : null}

      <EvaluationDialog
        courier={evalTarget}
        dateKey={dateKey}
        onClose={() => setEvalTarget(null)}
        onSaved={refreshRelated}
      />
      <DepositDialog
        courier={depositTarget}
        dateKey={dateKey}
        onClose={() => setDepositTarget(null)}
        onSaved={refreshRelated}
      />
      <DepositSkipDialog
        courier={skipTarget}
        pending={reviewMutation.isPending}
        onClose={() => setSkipTarget(null)}
        onSubmit={(comment) => {
          if (!skipTarget) {
            return;
          }
          reviewMutation.mutate(
            {
              employeeId: skipTarget.employee_id,
              payload: { deposit_skipped: true, deposit_skip_comment: comment },
            },
            { onSuccess: () => setSkipTarget(null) },
          );
        }}
      />
    </div>
  );
}

function SummaryLine({ ready, total }: { ready: number; total: number }) {
  if (total === 0) {
    return null;
  }
  const left = total - ready;
  return (
    <div className="text-sm text-muted-foreground">
      Готово {ready} из {total}
      {left > 0 ? ` — у ${left} не проставлены оценка и/или депозит` : " — можно сохранять"}
    </div>
  );
}

function CourierRow({
  courier,
  locked,
  canEvaluate,
  canDeposit,
  evalAllowed,
  reviewPending,
  onEvaluate,
  onDeposit,
  onToggleEvalSkip,
  onToggleDepositSkip,
}: {
  courier: CourierShiftDayCourier;
  locked: boolean;
  canEvaluate: boolean;
  canDeposit: boolean;
  evalAllowed: boolean;
  reviewPending: boolean;
  onEvaluate: () => void;
  onDeposit: () => void;
  onToggleEvalSkip: (value: boolean) => void;
  onToggleDepositSkip: (value: boolean) => void;
}) {
  const tone = locked
    ? "border-border opacity-70"
    : courier.ready
      ? "border-emerald-200"
      : "border-amber-200 bg-amber-50/60";

  return (
    <div className={cn("rounded-lg border bg-card p-3", tone)}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-[170px] flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium">{courier.full_name}</span>
            {courier.category ? (
              <Badge className="rounded-md" variant="secondary">
                {CATEGORY_LABELS[courier.category]}
              </Badge>
            ) : null}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">{shiftSummary(courier)}</div>
        </div>

        <ReadinessChips courier={courier} />

        <div className="flex items-center gap-2">
          <Button
            disabled={locked || !canEvaluate || courier.eval_skipped || !evalAllowed}
            onClick={onEvaluate}
            size="sm"
            title={!evalAllowed ? "Оценку можно ставить только за сегодня или вчера" : undefined}
            variant="outline"
          >
            <Star size={15} aria-hidden="true" />
            Оценить
          </Button>
          <Button
            disabled={locked || !canDeposit || courier.deposit_collected || courier.deposit_skipped}
            onClick={onDeposit}
            size="sm"
            title={courier.deposit_collected ? "Депозит собран" : undefined}
            variant="outline"
          >
            <Wallet size={15} aria-hidden="true" />
            Депозит
          </Button>
        </div>
      </div>

      {!locked ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 border-t pt-2 text-xs text-muted-foreground">
          <label className="flex items-center gap-2">
            <Switch
              checked={courier.eval_skipped}
              disabled={!canEvaluate || courier.eval_present || reviewPending}
              onCheckedChange={onToggleEvalSkip}
            />
            Не оценивать
          </label>
          <label className="flex items-center gap-2">
            <Switch
              checked={courier.deposit_skipped}
              disabled={
                !canDeposit || courier.deposit_collected || courier.deposit_present || reviewPending
              }
              onCheckedChange={onToggleDepositSkip}
            />
            Не собирать депозит
          </label>
          {courier.deposit_skipped && courier.deposit_skip_comment ? (
            <span className="italic">«{courier.deposit_skip_comment}»</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ReadinessChips({ courier }: { courier: CourierShiftDayCourier }) {
  const evalChip = courier.eval_present
    ? {
        tone: "ok" as const,
        text: courier.latest_eval_label
          ? `${courier.latest_eval_label} ${formatScore(courier.latest_eval_score)}`
          : "оценка",
      }
    : courier.eval_skipped
      ? { tone: "skip" as const, text: "без оценки" }
      : { tone: "todo" as const, text: "нет оценки" };

  const depositChip = courier.deposit_collected
    ? { tone: "ok" as const, text: "депозит собран" }
    : courier.deposit_present
      ? { tone: "ok" as const, text: `депозит ${formatCents(courier.deposit_balance_cents)}` }
      : courier.deposit_skipped
        ? { tone: "skip" as const, text: "без депозита" }
        : { tone: "todo" as const, text: "нет депозита" };

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <StatusChip icon={<Star size={12} aria-hidden="true" />} tone={evalChip.tone} text={evalChip.text} />
      <StatusChip icon={<Wallet size={12} aria-hidden="true" />} tone={depositChip.tone} text={depositChip.text} />
    </div>
  );
}

function StatusChip({
  icon,
  text,
  tone,
}: {
  icon: ReactNode;
  text: string;
  tone: "ok" | "skip" | "todo";
}) {
  const cls =
    tone === "ok"
      ? "border-emerald-200 bg-emerald-50 text-emerald-800"
      : tone === "skip"
        ? "border-slate-200 bg-slate-50 text-slate-600"
        : "border-amber-200 bg-amber-50 text-amber-800";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium",
        cls,
      )}
    >
      {tone === "ok" ? <Check size={12} aria-hidden="true" /> : icon}
      {text}
    </span>
  );
}

function EvaluationDialog({
  courier,
  dateKey,
  onClose,
  onSaved,
}: {
  courier: CourierShiftDayCourier | null;
  dateKey: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [criterionId, setCriterionId] = useState<number | null>(null);
  const [comment, setComment] = useState("");

  const criteriaQuery = useQuery({
    queryKey: ["evaluation-criteria"],
    queryFn: getCourierEvaluationCriteria,
    enabled: courier !== null,
  });

  const criteria = useMemo(
    () =>
      (criteriaQuery.data ?? [])
        .filter((criterion) => criterion.code !== "no_distinctions")
        .sort((a, b) => b.score - a.score || a.display_order - b.display_order),
    [criteriaQuery.data],
  );

  const mutation = useMutation({
    mutationFn: () => {
      if (!courier || criterionId === null) {
        throw new Error("Выберите критерий");
      }
      return createCourierEvaluation({
        courier_employee_id: courier.employee_id,
        criterion_id: criterionId,
        evaluated_at: dateKey,
        comment: comment.trim() || null,
        source: "web",
      });
    },
    onSuccess: () => {
      toast.success("Оценка сохранена");
      onSaved();
      handleClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить оценку")),
  });

  function handleClose() {
    setCriterionId(null);
    setComment("");
    onClose();
  }

  return (
    <Dialog open={courier !== null} onOpenChange={(open) => !open && handleClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Оценка курьера</DialogTitle>
          <DialogDescription>{courier ? courier.full_name : ""}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-wrap gap-2">
          {criteria.map((criterion) => (
            <button
              className={cn(
                "rounded-md border px-2.5 py-1.5 text-sm transition-colors",
                criterionId === criterion.id
                  ? "border-primary ring-1 ring-primary"
                  : "hover:bg-muted",
                criterionScoreClass(criterion.score),
              )}
              key={criterion.id}
              onClick={() => setCriterionId(criterion.id)}
              type="button"
            >
              {criterion.label} {formatScore(criterion.score)}
            </button>
          ))}
        </div>

        <Label className="grid gap-2">
          <span>Комментарий (необязательно)</span>
          <Textarea
            onChange={(event) => setComment(event.target.value)}
            placeholder="Например: быстро взял сложный заказ"
            value={comment}
          />
        </Label>

        <DialogFooter>
          <Button onClick={handleClose} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={criterionId === null || mutation.isPending}
            onClick={() => mutation.mutate()}
            type="button"
          >
            {mutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Check size={16} aria-hidden="true" />
            )}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const DEPOSIT_TYPE_LABELS: Record<CourierDepositTransactionType, string> = {
  top_up: "Пополнение",
  return: "Возврат",
  forfeit: "Списание",
};

function DepositDialog({
  courier,
  dateKey,
  onClose,
  onSaved,
}: {
  courier: CourierShiftDayCourier | null;
  dateKey: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [type, setType] = useState<CourierDepositTransactionType>("top_up");
  const [amount, setAmount] = useState("");
  const [comment, setComment] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const amountValid = isPositiveRubInput(amount);
  const commentValid = type !== "forfeit" || comment.trim().length > 0;

  const mutation = useMutation({
    mutationFn: () => {
      const cents = centsFromRubInput(amount);
      if (!courier || cents === null || cents <= 0) {
        throw new Error("Проверьте сумму");
      }
      return postCourierDepositTransaction(courier.employee_id, {
        amount_cents: cents,
        comment: comment.trim() || null,
        transaction_date: dateKey,
        transaction_type: type,
      });
    },
    onSuccess: () => {
      toast.success("Операция создана");
      onSaved();
      handleClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать операцию")),
  });

  function handleClose() {
    setType("top_up");
    setAmount("");
    setComment("");
    setSubmitted(false);
    onClose();
  }

  function submit() {
    setSubmitted(true);
    if (!amountValid || !commentValid) {
      return;
    }
    mutation.mutate();
  }

  return (
    <Dialog open={courier !== null} onOpenChange={(open) => !open && handleClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Депозит</DialogTitle>
          <DialogDescription>{courier ? courier.full_name : ""}</DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <Label className="grid gap-2">
            <span>Тип операции</span>
            <Select onValueChange={(value) => setType(value as CourierDepositTransactionType)} value={type}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="top_up">Пополнение</SelectItem>
                <SelectItem value="return">Возврат</SelectItem>
                <SelectItem value="forfeit">Списание</SelectItem>
              </SelectContent>
            </Select>
          </Label>

          <Label className="grid gap-2">
            <span>Сумма ₽</span>
            <Input
              min={0}
              onChange={(event) => setAmount(event.target.value)}
              type="number"
              value={amount}
            />
            {submitted && !amountValid ? (
              <span className="text-sm text-destructive">Введите положительную сумму.</span>
            ) : null}
          </Label>

          <Label className="grid gap-2">
            <span>{type === "forfeit" ? "Причина" : "Комментарий"}</span>
            <Textarea
              onChange={(event) => setComment(event.target.value)}
              placeholder={type === "forfeit" ? "Укажите причину списания" : "Необязательно"}
              value={comment}
            />
            {submitted && !commentValid ? (
              <span className="text-sm text-destructive">Для списания нужна причина.</span>
            ) : null}
          </Label>
        </div>

        <DialogFooter>
          <Button onClick={handleClose} type="button" variant="outline">
            Отмена
          </Button>
          <Button disabled={mutation.isPending} onClick={submit} type="button">
            {mutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Check size={16} aria-hidden="true" />
            )}
            {DEPOSIT_TYPE_LABELS[type]}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DepositSkipDialog({
  courier,
  pending,
  onClose,
  onSubmit,
}: {
  courier: CourierShiftDayCourier | null;
  pending: boolean;
  onClose: () => void;
  onSubmit: (comment: string) => void;
}) {
  const [comment, setComment] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const valid = comment.trim().length > 0;

  function handleClose() {
    setComment("");
    setSubmitted(false);
    onClose();
  }

  return (
    <Dialog open={courier !== null} onOpenChange={(open) => !open && handleClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Не собирать депозит</DialogTitle>
          <DialogDescription>
            {courier ? `${courier.full_name}: укажите причину` : ""}
          </DialogDescription>
        </DialogHeader>

        <Label className="grid gap-2">
          <span>Причина (обязательно)</span>
          <Textarea
            onChange={(event) => setComment(event.target.value)}
            placeholder="Например: новичок, депозит со следующей смены"
            value={comment}
          />
          {submitted && !valid ? (
            <span className="text-sm text-destructive">Укажите причину.</span>
          ) : null}
        </Label>

        <DialogFooter>
          <Button onClick={handleClose} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={pending}
            onClick={() => {
              setSubmitted(true);
              if (valid) {
                onSubmit(comment.trim());
              }
            }}
            type="button"
          >
            {pending ? <LoaderCircle className="animate-spin" size={16} aria-hidden="true" /> : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function InboxSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 4 }).map((_, index) => (
        <Skeleton className="h-16 w-full" key={index} />
      ))}
    </div>
  );
}

function shiftSummary(courier: CourierShiftDayCourier) {
  if (courier.shifts.length === 0) {
    return "—";
  }
  const open = courier.shifts.some((shift) => shift.is_open);
  const first = courier.shifts[0];
  const openedTime = formatTime(first.opened_at);
  if (open) {
    return `на линии · с ${openedTime}`;
  }
  const last = courier.shifts[courier.shifts.length - 1];
  const closedTime = last.closed_at ? formatTime(last.closed_at) : "—";
  return `смена закрыта · ${openedTime}–${closedTime}`;
}

function formatTime(iso: string) {
  return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(
    new Date(iso),
  );
}

function formatDayLabel(dateKey: string) {
  return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "long" }).format(
    new Date(`${dateKey}T00:00:00`),
  );
}

function formatScore(score: number | null) {
  if (score === null) {
    return "";
  }
  if (score > 0) {
    return `+${score}`;
  }
  if (score < 0) {
    return `−${Math.abs(score)}`;
  }
  return "0";
}

function criterionScoreClass(score: number) {
  if (score > 0) {
    return "text-emerald-700";
  }
  if (score < 0) {
    return "text-rose-700";
  }
  return "text-slate-600";
}

function addDays(value: Date, days: number) {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

function shiftDate(dateKey: string, days: number) {
  return toDateKey(addDays(new Date(`${dateKey}T00:00:00`), days));
}
