/** Страница «Учёт ОС»: реестр карточек, свод по категориям, история начислений.
 *
 * Карточка = ОДНА физическая единица (три ларя — три карточки), поэтому реестр длинный, а
 * поиск — главный способ в нём ориентироваться. Ищем клиентом по уже загруженному ответу:
 * дебаунса в проекте нет нигде, а весь реестр влезает в один запрос.
 *
 * Удаления карточки нет по замыслу: объект с историей начислений выбывает сменой статуса.
 */
import { useMemo, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Boxes, Pencil, Plus, RefreshCw } from "lucide-react";
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
import { Card, CardContent } from "@/components/ui/card";
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
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { InfoHint } from "@/components/ui-app/InfoHint";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { apiErrorMessage, apiErrorStatus } from "@/lib/api";
import { todayIso } from "@/lib/date";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

import {
  type AssetStatus,
  type FixedAsset,
  type Money,
  closeDepreciationMonth,
  correctDepreciation,
  createFixedAsset,
  getAssetCategories,
  getFixedAsset,
  getFixedAssets,
  getFixedAssetsSummary,
} from "./api";

const ALL = "all";
const QUERY_ROOT = "fixed-assets";

const rubFormatter = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});
const rubExactFormatter = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Деньги приходят из pydantic-Decimal строкой — приводим в одном месте. */
function toNumber(value: Money | null | undefined): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function money(value: Money | null | undefined): string {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? rubFormatter.format(numeric) : "—";
}

function moneyExact(value: Money | null | undefined): string {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? rubExactFormatter.format(numeric) : "—";
}

/** Дата приходит как `YYYY-MM-DD`. Якорь `T00:00:00` обязателен: без него `new Date` считает
 *  строку за UTC и показывает предыдущий день. */
function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const anchored = value.length === 10 ? `${value}T00:00:00` : value;
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short" }).format(new Date(anchored));
}

function formatMonth(value: string | null | undefined): string {
  if (!value) return "—";
  const anchored = value.length === 10 ? `${value}T00:00:00` : value;
  return new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric" }).format(
    new Date(anchored),
  );
}

/** Первое число прошедшего месяца — период, который закрывает ночная джоба. */
function previousMonthIso(): string {
  const now = new Date(`${todayIso()}T00:00:00`);
  const first = new Date(now.getFullYear(), now.getMonth(), 1);
  first.setDate(0);
  return `${first.getFullYear()}-${String(first.getMonth() + 1).padStart(2, "0")}-01`;
}

const STATUS_CLASSES: Record<AssetStatus, string> = {
  in_use: "border-emerald-200 bg-emerald-50 text-emerald-700",
  in_storage: "border-sky-200 bg-sky-50 text-sky-700",
  not_working: "border-amber-200 bg-amber-50 text-amber-700",
  disposed: "border-zinc-200 bg-zinc-50 text-zinc-600",
  sold: "border-zinc-200 bg-zinc-50 text-zinc-600",
};

const STATUS_OPTIONS: Array<{ value: AssetStatus; label: string }> = [
  { value: "in_use", label: "В работе" },
  { value: "in_storage", label: "На складе" },
  { value: "not_working", label: "Не работает" },
  { value: "disposed", label: "Списан" },
  { value: "sold", label: "Продан" },
];

function LoadingBlock({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton className="h-10 w-full" key={index} />
      ))}
    </div>
  );
}

function ErrorBlock({
  error,
  onRetry,
  fallback,
}: {
  error: unknown;
  onRetry?: () => void;
  fallback?: string;
}) {
  // 403 бэкенд отдаёт по-английски («Insufficient permission») — показывать это владельцу нельзя.
  const message =
    apiErrorStatus(error) === 403
      ? "Недостаточно прав для этого раздела"
      : apiErrorMessage(error, fallback ?? "Не удалось загрузить данные");
  return (
    <Card className="border-rose-200 bg-rose-50/60 shadow-none">
      <CardContent className="flex flex-col gap-3 p-5 text-sm text-rose-900 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-2">
          <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{message}</span>
        </div>
        {onRetry ? (
          <Button onClick={onRetry} size="sm" variant="outline">
            <RefreshCw aria-hidden="true" />
            Повторить
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}

function MetricCard({
  title,
  value,
  hint,
  accent,
}: {
  title: string;
  value: string;
  hint?: string;
  accent?: string;
}) {
  return (
    <Card className="shadow-none">
      <CardContent className="flex flex-col gap-1 p-4">
        <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {title}
        </div>
        <div className={cn("text-xl font-semibold tabular-nums", accent)}>{value}</div>
        {hint ? <div className="text-xs text-muted-foreground">{hint}</div> : null}
      </CardContent>
    </Card>
  );
}

function SummarySection() {
  const summaryQuery = useQuery({
    queryKey: [QUERY_ROOT, "summary"],
    queryFn: getFixedAssetsSummary,
  });

  if (summaryQuery.isLoading) return <LoadingBlock rows={2} />;
  if (summaryQuery.isError) {
    return (
      <ErrorBlock
        error={summaryQuery.error}
        fallback="Не удалось загрузить свод по основным средствам"
        onRetry={() => void summaryQuery.refetch()}
      />
    );
  }

  const summary = summaryQuery.data;
  if (!summary) return null;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          hint={`${summary.count} карточек в работе`}
          title="Первоначальная стоимость"
          value={money(summary.initial_cost)}
        />
        <MetricCard
          accent="text-rose-700"
          title="Накоплено амортизации"
          value={money(summary.accumulated)}
        />
        <MetricCard
          accent="text-sky-700"
          hint="идёт в баланс, строки 1–11"
          title="Остаточная стоимость"
          value={money(summary.residual)}
        />
        <MetricCard
          hint={
            summary.last_closed_month
              ? `последнее закрытие — ${formatMonth(summary.last_closed_month)}`
              : "ни один месяц ещё не закрыт"
          }
          title="Амортизация в месяц"
          value={money(summary.monthly_amount)}
        />
      </div>

      <div className="overflow-hidden rounded-lg border bg-card">
        <table className="w-full caption-bottom text-sm">
          <thead>
            <tr className="bg-muted">
              <th className="h-10 px-4 text-left text-xs font-semibold uppercase">Категория</th>
              <th className="h-10 px-4 text-right text-xs font-semibold uppercase">Единиц</th>
              <th className="h-10 px-4 text-right text-xs font-semibold uppercase">
                Первоначальная
              </th>
              <th className="h-10 px-4 text-right text-xs font-semibold uppercase">Накоплено</th>
              <th className="h-10 px-4 text-right text-xs font-semibold uppercase">Остаточная</th>
              <th className="h-10 px-4 text-right text-xs font-semibold uppercase">В месяц</th>
            </tr>
          </thead>
          <tbody>
            {summary.by_category.map((row) => (
              <tr className="border-t" key={row.category_id ?? "none"}>
                <td className="px-4 py-2">{row.category_name}</td>
                <td className="px-4 py-2 text-right tabular-nums">{row.count}</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(row.initial_cost)}</td>
                <td className="px-4 py-2 text-right tabular-nums text-rose-700">
                  {money(row.accumulated)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums text-sky-700">
                  {money(row.residual)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{money(row.monthly_amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CloseMonthButton() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [periodMonth, setPeriodMonth] = useState(previousMonthIso);

  const mutation = useMutation({
    mutationFn: () => closeDepreciationMonth(periodMonth),
    onSuccess: (result) => {
      setOpen(false);
      if (result.entries === 0) {
        toast.info(`За ${formatMonth(result.period_month)} начислять нечего — месяц уже закрыт`);
      } else {
        toast.success(
          `Начислено за ${formatMonth(result.period_month)}: ` +
            `${result.entries} объектов на ${moneyExact(result.amount)}`,
        );
      }
      void queryClient.invalidateQueries({ queryKey: [QUERY_ROOT] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось закрыть месяц"));
    },
  });

  return (
    <>
      <Button onClick={() => setOpen(true)} variant="outline">
        <RefreshCw aria-hidden="true" />
        Закрыть месяц
      </Button>
      <AlertDialog onOpenChange={setOpen} open={open}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Начислить амортизацию за месяц</AlertDialogTitle>
            <AlertDialogDescription>
              Обычно это делает планировщик 1-го числа. Ручной запуск нужен, когда он не
              отработал. Повторять безопасно: уже посчитанные месяцы не задваиваются, а суммы,
              поправленные вручную, остаются нетронутыми.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="grid gap-2">
            <Label htmlFor="close-month-period">Месяц</Label>
            <Input
              id="close-month-period"
              onChange={(event) => setPeriodMonth(`${event.target.value}-01`)}
              type="month"
              value={periodMonth.slice(0, 7)}
            />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={mutation.isPending}
              onClick={(event) => {
                // Без этого Radix закроет диалог до ответа сервера, и владелец не увидит,
                // что запрос ещё идёт.
                event.preventDefault();
                mutation.mutate();
              }}
            >
              {mutation.isPending ? "Считаем…" : "Начислить"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function CreateAssetDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [cost, setCost] = useState("");
  const [categoryId, setCategoryId] = useState(ALL);
  const [brandModel, setBrandModel] = useState("");
  const [commissionedOn, setCommissionedOn] = useState(todayIso);
  const [note, setNote] = useState("");

  const categoriesQuery = useQuery({
    queryKey: [QUERY_ROOT, "categories"],
    queryFn: getAssetCategories,
  });

  const mutation = useMutation({
    mutationFn: () =>
      createFixedAsset({
        name: name.trim(),
        initial_cost: cost,
        category_id: categoryId === ALL ? null : categoryId,
        brand_model: brandModel.trim() || null,
        commissioned_on: commissionedOn || null,
        valued_on: commissionedOn || null,
        note: note.trim() || null,
      }),
    onSuccess: (asset) => {
      toast.success(`Карточка заведена, инвентарный номер ${asset.inventory_number ?? "—"}`);
      void queryClient.invalidateQueries({ queryKey: [QUERY_ROOT] });
      onClose();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось завести карточку"));
    },
  });

  const ready = name.trim().length > 0 && Number(cost) > 0;

  return (
    <Dialog onOpenChange={(open) => (open ? undefined : onClose())} open>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Новая карточка ОС</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="asset-name">Наименование</Label>
            <Input
              id="asset-name"
              onChange={(event) => setName(event.target.value)}
              placeholder="Печь для пиццы электрическая"
              value={name}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <Label htmlFor="asset-cost">Стоимость, ₽</Label>
              <Input
                id="asset-cost"
                inputMode="decimal"
                onChange={(event) => setCost(event.target.value)}
                placeholder="95000"
                value={cost}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="asset-commissioned">Дата ввода</Label>
              <Input
                id="asset-commissioned"
                onChange={(event) => setCommissionedOn(event.target.value)}
                type="date"
                value={commissionedOn}
              />
            </div>
          </div>
          <div className="grid gap-2">
            <Label>Категория</Label>
            <Select onValueChange={setCategoryId} value={categoryId}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Без категории</SelectItem>
                {(categoriesQuery.data ?? []).map((category) => (
                  <SelectItem key={category.id} value={category.id}>
                    {category.name} · {Math.round(category.useful_life_months / 12)} лет
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Срок службы берётся из категории. Без категории объект не будет амортизироваться.
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="asset-brand">Бренд и модель</Label>
            <Input
              id="asset-brand"
              onChange={(event) => setBrandModel(event.target.value)}
              placeholder="ITPIZZA MS44"
              value={brandModel}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="asset-note">Примечание</Label>
            <Textarea
              id="asset-note"
              onChange={(event) => setNote(event.target.value)}
              rows={2}
              value={note}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Инвентарный номер присвоится автоматически.
          </p>
        </div>
        <DialogFooter>
          <Button disabled={mutation.isPending} onClick={onClose} variant="outline">
            Отмена
          </Button>
          <Button disabled={!ready || mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Сохраняем…" : "Завести"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CorrectionDialog({
  assetId,
  periodMonth,
  currentAmount,
  onClose,
}: {
  assetId: string;
  periodMonth: string;
  currentAmount: Money;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState(String(toNumber(currentAmount)));
  const [note, setNote] = useState("");

  const mutation = useMutation({
    mutationFn: () =>
      correctDepreciation(assetId, {
        period_month: periodMonth,
        amount,
        note: note.trim() || null,
      }),
    onSuccess: () => {
      toast.success(`Начисление за ${formatMonth(periodMonth)} поправлено`);
      void queryClient.invalidateQueries({ queryKey: [QUERY_ROOT] });
      onClose();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось поправить начисление"));
    },
  });

  return (
    <Dialog onOpenChange={(open) => (open ? undefined : onClose())} open>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Правка начисления за {formatMonth(periodMonth)}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            Месяц уже закрыт. Правка изменит остаточную стоимость этого месяца и всех
            последующих — они пересчитаются заново.
          </div>
          <div className="grid gap-2">
            <Label htmlFor="correction-amount">Сумма, ₽</Label>
            <Input
              id="correction-amount"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              value={amount}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="correction-note">Причина</Label>
            <Textarea
              id="correction-note"
              onChange={(event) => setNote(event.target.value)}
              placeholder="Печь запущена только 20 августа"
              rows={2}
              value={note}
            />
          </div>
        </div>
        <DialogFooter>
          <Button disabled={mutation.isPending} onClick={onClose} variant="outline">
            Отмена
          </Button>
          <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Сохраняем…" : "Поправить"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AssetSheet({
  assetId,
  canEdit,
  onClose,
}: {
  assetId: string | null;
  canEdit: boolean;
  onClose: () => void;
}) {
  const [correcting, setCorrecting] = useState<{ period: string; amount: Money } | null>(null);
  const cardQuery = useQuery({
    queryKey: [QUERY_ROOT, "card", assetId],
    queryFn: () => getFixedAsset(assetId as string),
    enabled: Boolean(assetId),
  });

  const asset = cardQuery.data;

  return (
    <>
      <Sheet
        onOpenChange={(open) => {
          if (!open) onClose();
        }}
        open={Boolean(assetId)}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle>{asset?.name ?? "Основное средство"}</SheetTitle>
            <SheetDescription>
              {asset
                ? `${asset.inventory_number ?? "без номера"}${
                    asset.brand_model ? ` · ${asset.brand_model}` : ""
                  }`
                : "Загрузка…"}
            </SheetDescription>
          </SheetHeader>

          {cardQuery.isError ? (
            <ErrorBlock error={cardQuery.error} fallback="Не удалось загрузить карточку" />
          ) : null}

          {asset ? (
            <Tabs className="mt-4" defaultValue="card">
              <TabsList>
                <TabsTrigger value="card">Карточка</TabsTrigger>
                <TabsTrigger value="history">
                  История начислений ({asset.entries.length})
                </TabsTrigger>
              </TabsList>

              <TabsContent className="space-y-4" value="card">
                <div className="grid gap-3 sm:grid-cols-2">
                  <MetricCard
                    title="Первоначальная"
                    value={moneyExact(asset.initial_cost)}
                  />
                  <MetricCard
                    accent="text-sky-700"
                    title="Остаточная"
                    value={moneyExact(asset.residual)}
                  />
                </div>
                <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
                  <Row label="Статус" value={asset.status_title} />
                  <Row label="Категория" value={asset.category_name ?? "—"} />
                  <Row
                    label="Срок службы"
                    value={
                      asset.useful_life_months ? `${asset.useful_life_months} мес` : "не задан"
                    }
                  />
                  <Row
                    hint="Первоначальная стоимость, делённая на срок службы. Ноль — если объект выбыл, ещё не введён в эксплуатацию, без срока или уже самортизирован."
                    label="Амортизация в месяц"
                    value={asset.depreciating ? moneyExact(asset.monthly_amount) : "не идёт"}
                  />
                  <Row label="Накоплено" value={moneyExact(asset.accumulated)} />
                  <Row label="Дата ввода" value={formatDate(asset.commissioned_on)} />
                  <Row
                    label="Оценка"
                    value={
                      asset.valuation_basis === "market"
                        ? `рыночная на ${formatDate(asset.valued_on)}`
                        : "по сумме платежа"
                    }
                  />
                  <Row label="Помещение" value={asset.location_name ?? asset.location ?? "—"} />
                  <Row label="Источник" value={asset.source_ref ?? "—"} />
                </dl>
                {asset.note ? (
                  <div className="rounded-md border bg-muted/40 p-3 text-sm">{asset.note}</div>
                ) : null}
                {asset.review_status === "requires_owner_review" ? (
                  <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                    Требует решения владельца
                    {asset.review_reason ? `: ${asset.review_reason}` : ""}
                  </div>
                ) : null}
              </TabsContent>

              <TabsContent value="history">
                {asset.entries.length === 0 ? (
                  <EmptyState
                    description="Начисления появятся после первого закрытия месяца."
                    title="Начислений пока нет"
                  />
                ) : (
                  <div className="overflow-hidden rounded-lg border bg-card">
                    <table className="w-full caption-bottom text-sm">
                      <thead>
                        <tr className="bg-muted">
                          <th className="h-10 px-4 text-left text-xs font-semibold uppercase">
                            Месяц
                          </th>
                          <th className="h-10 px-4 text-right text-xs font-semibold uppercase">
                            Начислено
                          </th>
                          <th className="h-10 px-4 text-right text-xs font-semibold uppercase">
                            Остаток после
                          </th>
                          <th className="h-10 w-24 px-4" />
                        </tr>
                      </thead>
                      <tbody>
                        {asset.entries.map((entry) => (
                          <tr className="border-t" key={entry.period_month}>
                            <td className="px-4 py-2">
                              <div>{formatMonth(entry.period_month)}</div>
                              {entry.is_manual ? (
                                <div className="text-xs text-amber-700">
                                  правка вручную
                                  {entry.corrected_at ? ` · ${formatDate(entry.corrected_at)}` : ""}
                                  {entry.note ? ` · ${entry.note}` : ""}
                                </div>
                              ) : null}
                            </td>
                            <td className="px-4 py-2 text-right tabular-nums">
                              {moneyExact(entry.amount)}
                            </td>
                            <td className="px-4 py-2 text-right tabular-nums text-sky-700">
                              {moneyExact(entry.residual_after)}
                            </td>
                            <td className="px-4 py-2 text-right">
                              {canEdit ? (
                                <Button
                                  onClick={() =>
                                    setCorrecting({
                                      period: entry.period_month,
                                      amount: entry.amount,
                                    })
                                  }
                                  size="sm"
                                  variant="ghost"
                                >
                                  <Pencil aria-hidden="true" />
                                  Поправить
                                </Button>
                              ) : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </TabsContent>
            </Tabs>
          ) : null}
        </SheetContent>
      </Sheet>

      {correcting && assetId ? (
        <CorrectionDialog
          assetId={assetId}
          currentAmount={correcting.amount}
          onClose={() => setCorrecting(null)}
          periodMonth={correcting.period}
        />
      ) : null}
    </>
  );
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-dashed py-1.5">
      <dt className="flex items-center gap-1 text-muted-foreground">
        {label}
        {hint ? <InfoHint label={label}>{hint}</InfoHint> : null}
      </dt>
      <dd className="text-right font-medium tabular-nums">{value}</dd>
    </div>
  );
}

function RegisterSection({ canEdit }: { canEdit: boolean }) {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>(ALL);
  const [categoryFilter, setCategoryFilter] = useState<string>(ALL);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const listQuery = useQuery({
    queryKey: [QUERY_ROOT, "list", statusFilter, categoryFilter],
    queryFn: () =>
      getFixedAssets({
        status: statusFilter === ALL ? undefined : (statusFilter as AssetStatus),
        category_id: categoryFilter === ALL ? undefined : categoryFilter,
      }),
  });
  const categoriesQuery = useQuery({
    queryKey: [QUERY_ROOT, "categories"],
    queryFn: getAssetCategories,
  });

  // Поиск клиентом по загруженному ответу: мгновенный и без мигания таблицы. Поля те же,
  // по которым ищет бэкенд, — чтобы результат не расходился при переходе на серверный поиск.
  const rows = useMemo(() => {
    const items = listQuery.data?.items ?? [];
    const needle = search.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) =>
      [item.name, item.brand_model, item.inventory_number, item.source_ref]
        .filter(Boolean)
        .some((field) => (field as string).toLowerCase().includes(needle)),
    );
  }, [listQuery.data, search]);

  const columns: Array<DataTableColumn<FixedAsset>> = [
    {
      key: "inventory_number",
      header: "Инв. №",
      className: "whitespace-nowrap font-medium",
      cell: (row) => row.inventory_number ?? "—",
    },
    {
      key: "name",
      header: "Наименование",
      // Ширина задана явно: без неё длинные названия («Плита индукционная-вок (в упаковке +
      // распакованный образец)») растягивают колонку и выталкивают деньги за край экрана.
      cell: (row) => (
        <div className="w-[360px] max-w-full">
          <div className="truncate" title={row.name}>
            {row.name}
          </div>
          {row.brand_model ? (
            <div className="truncate text-xs text-muted-foreground">{row.brand_model}</div>
          ) : null}
        </div>
      ),
    },
    {
      key: "category",
      header: "Категория",
      className: "text-muted-foreground",
      cell: (row) => <div className="w-[150px] max-w-full text-xs">{row.category_name ?? "—"}</div>,
    },
    {
      key: "status",
      header: "Статус",
      cell: (row) => (
        <div className="flex flex-col items-start gap-1">
          <Badge className={cn("font-normal", STATUS_CLASSES[row.status])} variant="outline">
            {row.status_title}
          </Badge>
          {/* Причин четыре — выбыл, не введён, без срока, самортизирован. Какая именно,
              видно в карточке; в списке важен сам факт. */}
          {!row.depreciating ? (
            <span className="text-xs text-muted-foreground">не амортизируется</span>
          ) : null}
        </div>
      ),
    },
    {
      key: "initial_cost",
      header: "Первоначальная",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (row) => money(row.initial_cost),
    },
    {
      key: "residual",
      header: "Остаточная",
      className: "text-right tabular-nums text-sky-700",
      headerClassName: "text-right",
      cell: (row) => money(row.residual),
    },
  ];

  const filtersOn = search.trim() !== "" || statusFilter !== ALL || categoryFilter !== ALL;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 rounded-lg border bg-card p-3 lg:flex-row lg:items-end">
        <div className="flex flex-1 flex-col gap-2">
          <Label htmlFor="assets-search">Поиск</Label>
          <Input
            id="assets-search"
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Название, модель, инвентарный номер, опись"
            value={search}
          />
        </div>
        <div className="flex w-full flex-col gap-2 lg:w-48">
          <Label>Статус</Label>
          <Select onValueChange={setStatusFilter} value={statusFilter}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Все статусы</SelectItem>
              {STATUS_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex w-full flex-col gap-2 lg:w-64">
          <Label>Категория</Label>
          <Select onValueChange={setCategoryFilter} value={categoryFilter}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Все категории</SelectItem>
              {(categoriesQuery.data ?? []).map((category) => (
                <SelectItem key={category.id} value={category.id}>
                  {category.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-3">
          {filtersOn ? (
            <Button
              onClick={() => {
                setSearch("");
                setStatusFilter(ALL);
                setCategoryFilter(ALL);
              }}
              variant="ghost"
            >
              Сбросить
            </Button>
          ) : null}
          <span className="whitespace-nowrap text-sm text-muted-foreground">
            Показано: {rows.length}
          </span>
        </div>
      </div>

      {listQuery.isError ? (
        <ErrorBlock
          error={listQuery.error}
          fallback="Не удалось загрузить реестр"
          onRetry={() => void listQuery.refetch()}
        />
      ) : (
        <DataTable
          columns={columns}
          emptyMessage={
            filtersOn ? "Ничего не найдено — попробуйте изменить фильтры" : "Реестр пока пуст"
          }
          getRowKey={(row) => row.id}
          isLoading={listQuery.isLoading}
          onRowClick={(row) => setOpenId(row.id)}
          rowClassName={(row) =>
            row.review_status === "requires_owner_review" ? "bg-amber-50/60" : undefined
          }
          rows={rows}
        />
      )}

      {canEdit ? (
        <Button onClick={() => setCreating(true)} variant="outline">
          <Plus aria-hidden="true" />
          Завести карточку
        </Button>
      ) : null}

      <AssetSheet assetId={openId} canEdit={canEdit} onClose={() => setOpenId(null)} />
      {creating ? <CreateAssetDialog onClose={() => setCreating(false)} /> : null}
    </div>
  );
}

export function FixedAssetsRoute() {
  const permissions = usePermissions();
  const canRead = permissions.hasPermission("accounting.fixed_assets.read");
  const canEdit = permissions.hasPermission("accounting.fixed_assets.edit");
  // Закрытие месяца — это закрытие учётного периода, а не правка карточки: право отдельное.
  // Гейт по edit показал бы кнопку тому, кто получит от бэкенда 403.
  const canCloseMonth = permissions.hasPermission("accounting.periods.close");
  const [tab, setTab] = useState("register");

  return (
    <div className="space-y-5">
      <PageHeader
        action={canCloseMonth ? <CloseMonthButton /> : undefined}
        description="Реестр основных средств и линейная помесячная амортизация. Одна карточка — одна физическая единица."
        title="Учёт ОС"
      />

      {!canRead ? (
        <EmptyState
          description="Раздел основных средств доступен по праву «Смотреть основные средства». Обратитесь к владельцу."
          icon={<Boxes aria-hidden="true" className="h-5 w-5" />}
          title="Недостаточно прав"
        />
      ) : (
        <>
          <Tabs onValueChange={setTab} value={tab}>
            <TabsList>
              <TabsTrigger value="register">Реестр</TabsTrigger>
              <TabsTrigger value="summary">Свод</TabsTrigger>
            </TabsList>
          </Tabs>
          {tab === "register" ? <RegisterSection canEdit={canEdit} /> : null}
          {tab === "summary" ? <SummarySection /> : null}
        </>
      )}
    </div>
  );
}
