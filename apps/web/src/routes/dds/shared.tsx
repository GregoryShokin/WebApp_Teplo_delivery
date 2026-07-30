import { useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { toIsoDate } from "@/lib/date";

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
import {
  apiErrorMessage,
  apiErrorStatus,
  getAssetOptions,
  getLocationOptionsForArticle,
  triggerBankSync,
  type DdsProvider,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

export type DdsActiveTab =
  | "today"
  | "accounts"
  | "operations"
  | "ledger"
  | "owner-review"
  | "counterparties"
  | "articles"
  | "rules"
  | "credentials";

export const DDS_ACTIVE_TAB_STORAGE_KEY = "dds.activeTab";

export const DDS_TABS: Array<{
  value: DdsActiveTab;
  label: string;
  path: string;
  // Вкладка-ссылка: её путь редиректит ВОН из ДДС (единый реестр контрагентов). Внутри ДДС
  // она не рендерится, поэтому запоминать её как «последнюю вкладку» нельзя — см. isDdsTabInternal.
  external?: true;
}> = [
  { value: "today", label: "Деньги сегодня", path: "/dds" },
  { value: "accounts", label: "Счета", path: "/dds/accounts" },
  { value: "ledger", label: "Журнал ДДС", path: "/dds/ledger" },
  // Единый Журнал (98916b2b) забрал разбор операций, но кейсы владельцу — не операции:
  // «оплата не в iiko», «найти перевод», непрошедший черновик. Их некуда было открыть —
  // вкладку убрали, а кнопку «Требует разбора» на «Деньгах сегодня» оставили, и она
  // редиректила обратно. Без этой строки кейсы копятся вслепую.
  { value: "owner-review", label: "Требуют разбора", path: "/dds/owner-review" },
  { value: "counterparties", label: "Контрагенты", path: "/dds/counterparties", external: true },
  { value: "articles", label: "Статьи ДДС", path: "/dds/articles" },
  { value: "credentials", label: "Доступы", path: "/dds/credentials" },
];

export const providerLabels: Record<string, string> = {
  sber: "Сбер",
  tbank: "T-Bank",
};

export const statusLabels: Record<string, string> = {
  pending: "Ожидает",
  classified: "Размечено",
  internal_transfer: "Внутренний перевод",
  needs_review: "Требует разбора",
  excluded: "Исключено",
  matched: "Сопоставлено",
  awaiting_confirmation: "Ожидает подтверждения",
};

export const directionLabels: Record<string, string> = {
  in: "Поступление",
  out: "Списание",
};

export const movementLabels: Record<string, string> = {
  inflow: "Поступление",
  outflow: "Списание",
  internal: "Внутреннее",
};

export const activityLabels: Record<string, string> = {
  operating: "Операционная",
  financing: "Финансовая",
  investing: "Инвестиционная",
  technical: "Техническая",
  internal: "Техническая",
};

export const qualityLabels: Record<string, string> = {
  auto: "Авто",
  owner_review: "Owner review",
  owner_reviewed: "Owner review",
  disputed: "Спорное",
};

export function ddsTabPath(tab: DdsActiveTab) {
  return DDS_TABS.find((item) => item.value === tab)?.path ?? "/dds";
}

export function isDdsTab(value: unknown): value is DdsActiveTab {
  return DDS_TABS.some((item) => item.value === value);
}

/** Вкладка, которая реально живёт ВНУТРИ ДДС (не ссылка наружу). Только такую можно
 * запомнить как последнюю: иначе заход на /dds каждый раз уводил бы в чужой раздел. */
export function isDdsTabInternal(value: unknown): value is DdsActiveTab {
  return DDS_TABS.some((item) => item.value === value && !item.external);
}

export function formatDdsMoney(value: string | number | null | undefined) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "—";
  }
  // ДДС всегда показывает копейки (2 знака); округление только с 3-го знака после запятой.
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "RUB",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(numeric);
}

export function formatDate(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short" }).format(new Date(value));
}

export function formatDateTime(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

export function isoDateDaysAgo(days: number) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return toIsoDate(date);
}

// Реэкспорт ради обратной совместимости импортов: реализация — в @/lib/date (локальная зона).
export { toIsoDate };

export function toDateTimeLocalInput(date: Date) {
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60_000).toISOString().slice(0, 16);
}

export function DdsStatusBadge({ status }: { status: string }) {
  return <Badge className={statusClass(status)}>{statusLabels[status] ?? status}</Badge>;
}

export function ProviderBadge({ provider }: { provider: string | null | undefined }) {
  if (!provider) {
    return <Badge variant="outline">Любой</Badge>;
  }
  return <Badge variant="outline">{providerLabels[provider] ?? provider}</Badge>;
}

export function DirectionBadge({ direction }: { direction: string | null | undefined }) {
  if (!direction) {
    return <Badge variant="outline">Любое</Badge>;
  }
  return (
    <Badge
      className={
        direction === "in"
          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
          : "border-amber-200 bg-amber-50 text-amber-700"
      }
    >
      {direction === "in" ? "↓" : "↑"} {directionLabels[direction] ?? direction}
    </Badge>
  );
}

export function MovementBadge({ movement }: { movement: string }) {
  const className =
    movement === "inflow"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : movement === "outflow"
        ? "border-amber-200 bg-amber-50 text-amber-700"
        : "border-sky-200 bg-sky-50 text-sky-700";
  return <Badge className={className}>{movementLabels[movement] ?? movement}</Badge>;
}

export function QualityBadge({ quality }: { quality: string }) {
  const className =
    quality === "auto"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : quality === "disputed"
        ? "border-orange-200 bg-orange-50 text-orange-700"
        : "border-sky-200 bg-sky-50 text-sky-700";
  return <Badge className={className}>{qualityLabels[quality] ?? quality}</Badge>;
}

export function statusClass(status: string) {
  if (status === "classified" || status === "matched") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (status === "internal_transfer") {
    return "border-sky-200 bg-sky-50 text-sky-700";
  }
  if (status === "awaiting_confirmation") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  if (status === "pending" || status === "needs_review") {
    return "border-orange-200 bg-orange-50 text-orange-700";
  }
  if (status === "excluded") {
    return "border-muted bg-muted text-muted-foreground";
  }
  return "";
}

export function PaginationControls({
  limit,
  offset,
  total,
  onOffsetChange,
}: {
  limit: number;
  offset: number;
  total: number;
  onOffsetChange: (offset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));

  return (
    <div className="flex flex-col gap-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
      <div>
        Стр. {page} из {totalPages}, всего {total}
      </div>
      <div className="flex gap-2">
        <Button
          disabled={offset === 0}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
          size="sm"
          variant="outline"
        >
          Назад
        </Button>
        <Button
          disabled={offset + limit >= total}
          onClick={() => onOffsetChange(offset + limit)}
          size="sm"
          variant="outline"
        >
          Вперёд
        </Button>
      </div>
    </div>
  );
}

export function BankSyncButton({ variant = "outline" }: { variant?: "default" | "outline" }) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [isOpen, setIsOpen] = useState(false);
  const [provider, setProvider] = useState<DdsProvider>("sber");
  const [dateFrom, setDateFrom] = useState(isoDateDaysAgo(7));
  const [dateTo, setDateTo] = useState(toIsoDate(new Date()));

  const syncMutation = useMutation({
    mutationFn: () => triggerBankSync(provider, { date_from: dateFrom, date_to: dateTo }),
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["dds", "operations"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "owner-review"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      ]);
      toast.success(`Запущено: ${result.job_id}`);
      setIsOpen(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось запустить синхронизацию")),
  });

  if (!permissions.canPerformAction("finance.cashflow.sync")) {
    return null;
  }

  return (
    <>
      <Button onClick={() => setIsOpen(true)} variant={variant}>
        <RefreshCw size={16} aria-hidden="true" />
        Синхронизировать выписки
      </Button>
      <Dialog open={isOpen} onOpenChange={setIsOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Синхронизация выписок</DialogTitle>
            <DialogDescription>Выберите банк и период загрузки операций.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2 sm:col-span-2">
              <Label>Провайдер</Label>
              <Select value={provider} onValueChange={(value) => setProvider(value as DdsProvider)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="sber">Сбер</SelectItem>
                  <SelectItem value="tbank">T-Bank</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="dds-sync-from">Дата с</Label>
              <Input
                id="dds-sync-from"
                type="date"
                value={dateFrom}
                onChange={(event) => setDateFrom(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="dds-sync-to">Дата по</Label>
              <Input
                id="dds-sync-to"
                type="date"
                value={dateTo}
                onChange={(event) => setDateTo(event.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              disabled={syncMutation.isPending || !dateFrom || !dateTo}
              onClick={() => syncMutation.mutate()}
            >
              {syncMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <RefreshCw size={16} aria-hidden="true" />
              )}
              Запустить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function DetailRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="grid gap-1 border-b py-3 text-sm last:border-b-0">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className="min-w-0 break-words text-foreground">{value || "—"}</div>
    </div>
  );
}

export function JsonPreview({ value }: { value: unknown }) {
  return (
    <pre className="max-h-[360px] overflow-auto rounded-md border bg-muted/40 p-3 text-xs leading-5">
      {JSON.stringify(value ?? {}, null, 2)}
    </pre>
  );
}

export function compactText(value: string | null | undefined, fallback = "—") {
  const text = value?.trim();
  return text ? text : fallback;
}

export function badgeMutedClass(isActive: boolean) {
  return cn(
    isActive
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : "border-muted bg-muted text-muted-foreground",
  );
}

/**
 * Весь роутер /locations закрыт правом source.locations.read. Без права запрос падает 403,
 * список схлопывается в пустой — и раньше UI объявлял это «помещений нет»: 28.07.2026 у роли
 * office_manager права не было, и владелец полдня искал поломку в реестре помещений.
 * Текст один на оба пикера (новый платёж и разбор операции), чтобы диагноз не расходился.
 */
export const LOCATIONS_FORBIDDEN_HINT =
  "Нет доступа к реестру помещений — попросите Собственника выдать право «Смотреть реестр помещений» (Настройки → Доступы).";

/**
 * Помещения и их аренды по статье. Общий ключ: пикер строки и сводка блокировки над кнопкой
 * читают один кэш react-query, второго запроса на тот же articleId не уходит.
 */
export function locationOptionsQuery(articleId: string) {
  return {
    queryKey: ["location-options", articleId],
    queryFn: () => getLocationOptionsForArticle(articleId),
    enabled: Boolean(articleId),
    // 403 — не «моргание сети»: право не появится от повтора, а ретрай лишь тянет заглушку.
    retry: (failureCount: number, error: Error) =>
      apiErrorStatus(error) !== 403 && failureCount < 1,
  };
}

/**
 * Тот же урок, что и с помещениями, но 403 здесь маловероятен: маршрут пускает и по праву
 * «разбирать ДДС». Сообщение всё равно нужно — роль могли настроить так, что разбор есть, а
 * этого права нет, и «Объекты не найдены» соврало бы про пустой реестр.
 */
export const ASSETS_FORBIDDEN_HINT =
  "Нет доступа к списку основных средств — попросите Собственника выдать право «Смотреть основные средства» (Настройки → Доступы).";

/**
 * Объекты для строк «объектных» статей. Список ОДИН на весь диалог (не зависит от статьи, в
 * отличие от помещений), поэтому и ключ без параметров: сколько бы строк ни было, запрос уходит
 * один.
 */
export function assetOptionsQuery(enabled: boolean) {
  return {
    queryKey: ["asset-options"],
    queryFn: getAssetOptions,
    enabled,
    retry: (failureCount: number, error: Error) =>
      apiErrorStatus(error) !== 403 && failureCount < 1,
  };
}
