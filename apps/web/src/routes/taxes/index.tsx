/** Страница «Налоги» — УСН «Доходы» 6% для ИП-общепита.
 *
 * Главное на странице — СВЕРКА, а не расчёт: три независимых слоя (наш расчёт из выручки
 * iiko, платёжка бухгалтера, факт из банка) сводятся в одну таблицу, и расхождение видно
 * на первом экране. Реальный случай, ради которого всё строилось: платёжка по УСН на 0 ₽
 * при расчёте 478 376 ₽ — такая строка обязана быть красной и первой.
 *
 * Расчёт и сверка берутся ОДНИМ запросом (`/taxes/overview`) намеренно: два отдельных
 * запроса могут разъехаться по дате среза и показать владельцу расхождение, которого нет.
 */
import { type ReactNode, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  FileText,
  Info,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { apiErrorMessage } from "@/lib/api";
import { todayIso } from "@/lib/date";
import { usePermissions } from "@/lib/permissions";
import {
  aiApplyTaxProposal,
  aiReviewAllTaxes,
  aiReviewTaxDocument,
  createTaxPaymentDraft,
  fetchTaxDocumentFileUrl,
  getTaxCalendar,
  getTaxDocuments,
  getTaxOverview,
  getTaxPayments,
  getTaxSources,
  getVatCriterion,
  promoteTaxDocuments,
  refreshTaxDocuments,
  reviewTaxDocument,
  setVatThreshold,
  type AiAuditReport,
  type Money,
  type Reconciliation,
  type ReconLine,
  type ReconSeverity,
  type ReconVerdict,
  type TaxCalendarItem,
  type TaxDocumentRow,
  type TaxPaymentRow,
  type TaxPromotionSummary,
  type TaxRecognition,
  type TaxSource,
  type TaxState,
  type VatWageCriterion,
} from "@/routes/taxes/api";

type TaxesTab = "summary" | "calendar" | "payments" | "documents" | "vat" | "sources";

// Источники, которые не показываем в реестре: это ручной ввод, живущий в других местах
// (порог НДС-льготы вводится на своей вкладке, сальдо ЕНС — из личного кабинета ФНС).
const HIDDEN_SOURCE_KEYS = new Set(["ens_balance", "regional_wage"]);

// ── форматирование ─────────────────────────────────────────────────────────────

const moneyFormatter = new Intl.NumberFormat("ru-RU", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

const dateTimeFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** «478 376,00 ₽» — неразрывные пробелы, запятая в дробной части. */
function formatMoney(value: Money | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "—";
  }
  // U+00A0 — неразрывный пробел перед знаком рубля (Intl ставит такие же внутри разрядов).
  return `${moneyFormatter.format(numeric)}\u00A0₽`;
}

/** Движок отдаёт ДОЛЮ (0,0512), а не проценты — приводим сами, точность не теряем.
 *
 * Доля бывает и больше единицы: в январе уплаченные взносы «за себя» легко перекрывают
 * выручку, и нагрузка выходит 268%. Поэтому умножаем всегда, без «похоже на проценты» —
 * такая догадка превратила бы 2,68 (268%) в «2,68%» и спрятала бы аномалию.
 */
function formatRate(value: Money | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return "—";
  }
  return `${(numeric * 100).toFixed(2).replace(".", ",")}%`;
}

/** Дата `YYYY-MM-DD`: собираем локальный полдень-полночь, чтобы не уехать на день назад. */
function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value.length === 10 ? `${value}T00:00:00` : value);
  return Number.isNaN(parsed.getTime()) ? value : dateFormatter.format(parsed);
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : dateTimeFormatter.format(parsed);
}

/** «01.07.2026 – 15.07.2026» из пары дат; если совпадают — одна дата; частичный период — как есть. */
function formatPeriodRange(
  start: string | null | undefined,
  end: string | null | undefined,
): string | null {
  if (!start && !end) {
    return null;
  }
  if (start && end) {
    return start === end ? formatDate(start) : `${formatDate(start)} – ${formatDate(end)}`;
  }
  return formatDate(start ?? end);
}

function toNumber(value: Money | null | undefined): number {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

// ── словари ────────────────────────────────────────────────────────────────────

// Владелец думает кварталами: «полугодие»/«9 месяцев» — язык деклараций, а платёж за
// полугодие — это доплата за II квартал. Показываем кварталы.
const PERIOD_TITLES: Record<string, string> = {
  q1: "I квартал",
  h1: "II квартал",
  "9m": "III квартал",
  year: "год",
};

const TAX_KIND_LABELS: Record<string, string> = {
  usn_advance: "УСН, авансовый платёж",
  usn_year: "УСН за год",
  contrib_fixed: "Фиксированные взносы ИП «за себя»",
  contrib_extra_1pct: "Допвзнос 1%",
  contrib_employees: "Взносы за работников",
  ndfl: "НДФЛ за работников",
  enp_payroll: "Зарплатный ЕНП (НДФЛ + взносы)",
  contrib_injury: "Взносы на травматизм (0,2%)",
  other: "Прочее",
};

const VERDICT_LABELS: Record<ReconVerdict, string> = {
  ok: "Сходится",
  doc_mismatch: "Документ ≠ расчёт",
  payment_mismatch: "Оплата ≠ документ",
  overdue: "Просрочено",
  due: "К уплате",
  no_data: "Нет данных",
};

const SEVERITY_ORDER: Record<ReconSeverity, number> = {
  alert: 0,
  warning: 1,
  info: 2,
  ok: 3,
};

const SEVERITY_BADGE: Record<ReconSeverity, string> = {
  alert: "border-rose-200 bg-rose-50 text-rose-700",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  info: "border-sky-200 bg-sky-50 text-sky-700",
  ok: "border-emerald-200 bg-emerald-50 text-emerald-700",
};

const SEVERITY_ROW: Record<ReconSeverity, string> = {
  alert: "bg-rose-50/70 hover:bg-rose-50",
  warning: "bg-amber-50/50 hover:bg-amber-50",
  info: "",
  ok: "",
};

/** Какая из трёх колонок «виновата» в вердикте — её и подсвечиваем.
 *
 * Подсвечивать всегда «Документ» нельзя: при `payment_mismatch` бумага как раз верна, а
 * разошёлся факт, а при `overdue` документа может не быть вовсе — жирный прочерк в чужой
 * колонке уводит взгляд не туда.
 */
const VERDICT_HIGHLIGHT: Record<ReconVerdict, "documented" | "paid" | null> = {
  ok: null,
  doc_mismatch: "documented",
  payment_mismatch: "paid",
  overdue: "paid",
  due: null,
  no_data: null,
};

/** Неизвестная severity с бэкенда не должна ломать сортировку (NaN) — уводим в конец. */
function severityRank(severity: string): number {
  return SEVERITY_ORDER[severity as ReconSeverity] ?? 9;
}

const NEUTRAL_BADGE = "border-border bg-muted text-muted-foreground";

const PAYMENT_STATUS: Record<string, { label: string; className: string }> = {
  planned: { label: "Запланировано", className: "border-sky-200 bg-sky-50 text-sky-700" },
  paid: { label: "Оплачено", className: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  cancelled: { label: "Отменено", className: NEUTRAL_BADGE },
};

const OVERDUE_BADGE = "border-rose-200 bg-rose-50 text-rose-700";

const DOCUMENT_STATUS: Record<string, { label: string; className: string }> = {
  parsed: { label: "Распознан", className: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  needs_review: {
    label: "Нужна проверка",
    className: "border-amber-200 bg-amber-50 text-amber-800",
  },
  promoted: { label: "Продвинут", className: "border-sky-200 bg-sky-50 text-sky-700" },
  unsupported: { label: "Не поддержан", className: NEUTRAL_BADGE },
  error: { label: "Ошибка разбора", className: OVERDUE_BADGE },
  ignored: { label: "Игнорируем", className: NEUTRAL_BADGE },
};

/** Что означает каждый статус — показываем по «i», чтобы не гадать. */
const DOCUMENT_STATUS_HINT: Record<string, string> = {
  parsed:
    "Распознан уверенно и готов к продвижению. Кнопка «Продвинуть готовые» превратит платёжку в плановое обязательство (попадёт в календарь и сверку), а оборотку — в помесячную раскладку зарплаты.",
  needs_review:
    "Распознан частично — автоматика не уверена (например, смешанный ЕНП или нечитаемая сумма). Проверьте вручную кнопками «Проверено» / «Игнорировать» в строке.",
  promoted:
    "Уже продвинут: из документа создано налоговое обязательство или строка расчёта — данные ушли в контур. Делать с ним больше ничего не нужно.",
  unsupported:
    "Документ не разобран автоматикой: либо он не из платёжного контура (приказ, договор, кадровый), либо формат файла не читается (например, старый Word .doc). Конкретная причина написана под статусом; в налоги документ не идёт.",
  error: "Файл не удалось разобрать. Причина — красным текстом в колонке полей.",
  ignored: "Вы отклонили документ вручную — в расчёт и сверку он не попадёт.",
};

const DOCUMENT_TYPE_LABELS: Record<string, string> = {
  payment_order: "Платёжное поручение",
  payroll_statement: "Зарплатная ведомость",
  turnover_statement: "Оборотно-сальдовая ведомость",
  unknown: "Тип не определён",
};

/** Тип выплаты в ведомости Т-53 — из имени файла бухгалтера. */
const PAYOUT_KIND_LABELS: Record<string, string> = {
  advance: "Аванс",
  salary: "Зарплата",
};

function payoutKindLabel(kind: string | null | undefined): string {
  if (!kind) return "Ведомость";
  return PAYOUT_KIND_LABELS[kind] ?? "Ведомость";
}

const MONTHS_RU_NOMINATIVE = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
];

/** Заголовок периода оборотки: «Июль 2026», иначе — как есть из period_hint. */
function turnoverPeriodLabel(recognition: TaxRecognition): string {
  const { year, month, period_hint } = recognition;
  if (year && month && month >= 1 && month <= 12) {
    return `${MONTHS_RU_NOMINATIVE[month - 1]} ${year}`;
  }
  return period_hint ?? "Оборотка";
}

const SOURCE_METHOD_LABELS: Record<string, string> = {
  api: "запрос к системе",
  file_cache: "выгрузка файлом",
  derived: "выводится расчётом",
  manual: "вводит человек",
  code_constant: "константа в коде",
};

const SOURCE_CONFIDENCE: Record<string, { label: string; className: string }> = {
  verified: {
    label: "подтверждён",
    className: "border-emerald-200 bg-emerald-50 text-emerald-700",
  },
  reconstructed: {
    label: "восстановлен расчётом",
    className: "border-amber-200 bg-amber-50 text-amber-800",
  },
  unverified: { label: "не сверялся", className: "border-sky-200 bg-sky-50 text-sky-700" },
  missing: { label: "источника нет", className: OVERDUE_BADGE },
};

function periodTitle(code: string | null | undefined): string {
  if (!code) return "—";
  // Месячный период приходит кодом «2026-06» — человеку показываем «июнь 2026».
  const monthly = /^(\d{4})-(\d{2})$/.exec(code);
  if (monthly) {
    const month = Number(monthly[2]);
    if (month >= 1 && month <= 12) {
      return `${MONTHS_RU_NOMINATIVE[month - 1].toLowerCase()} ${monthly[1]}`;
    }
  }
  return PERIOD_TITLES[code] ?? code;
}

/** Получатель бюджетного платежа — русской аббревиатурой, а не кодом из БД. */
const RECIPIENT_LABELS: Record<string, string> = {
  fns: "ФНС",
  sfr: "СФР",
};

function recipientLabel(code: string | null | undefined): string {
  if (!code) return "—";
  return RECIPIENT_LABELS[code] ?? code;
}

function taxKindLabel(kind: string | null | undefined): string {
  if (!kind) return "—";
  return TAX_KIND_LABELS[kind] ?? kind;
}

// ── мелкие блоки ───────────────────────────────────────────────────────────────

function LoadingBlock({ rows = 3 }: { rows?: number }) {
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
  return (
    <Card className="border-rose-200 bg-rose-50/60 shadow-none">
      <CardContent className="flex flex-col gap-3 p-5 text-sm text-rose-900 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{apiErrorMessage(error, fallback ?? "Не удалось загрузить данные")}</span>
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
  tone = "default",
}: {
  title: string;
  value: string;
  hint?: string;
  tone?: "default" | "danger" | "success";
}) {
  const valueTone =
    tone === "danger"
      ? "text-rose-700"
      : tone === "success"
        ? "text-emerald-700"
        : "text-foreground";
  return (
    <Card className="shadow-none">
      <CardContent className="flex flex-col gap-1 p-5">
        <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {title}
        </div>
        <div className={`text-2xl font-semibold tabular-nums ${valueTone}`}>{value}</div>
        {hint ? <div className="text-xs leading-5 text-muted-foreground">{hint}</div> : null}
      </CardContent>
    </Card>
  );
}

function NoticeList({
  items,
  tone,
  title,
}: {
  items: string[];
  tone: "warning" | "danger";
  title: string;
}) {
  if (items.length === 0) {
    return null;
  }
  const className =
    tone === "danger"
      ? "border-rose-200 bg-rose-50/70 text-rose-900"
      : "border-amber-200 bg-amber-50/70 text-amber-900";
  return (
    <Card className={`shadow-none ${className}`}>
      <CardContent className="flex gap-3 p-4 text-sm">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <div className="space-y-1">
          <div className="font-semibold">{title}</div>
          <ul className="list-disc space-y-1 pl-4 leading-6">
            {items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}

function DetailRow({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-dashed border-border py-1.5 last:border-0">
      <div className="min-w-0">
        <div className="text-muted-foreground">{label}</div>
        {hint ? <div className="text-xs leading-5 text-muted-foreground/80">{hint}</div> : null}
      </div>
      <div className="shrink-0 font-medium tabular-nums">{value}</div>
    </div>
  );
}

// ── вкладка «Сводка»: сверка ───────────────────────────────────────────────────

function ReconciliationBlock({
  reconciliation,
  isBlocked,
}: {
  reconciliation: Reconciliation;
  isBlocked: boolean;
}) {
  // Красные — наверх: владелец должен увидеть расхождение, не листая таблицу.
  const lines = useMemo(
    () =>
      [...reconciliation.lines].sort((a, b) => {
        const bySeverity = severityRank(a.severity) - severityRank(b.severity);
        if (bySeverity !== 0) return bySeverity;
        return (a.due_date ?? "9999-12-31").localeCompare(b.due_date ?? "9999-12-31");
      }),
    [reconciliation.lines],
  );

  const alertCount =
    reconciliation.alert_count ?? lines.filter((line) => line.severity === "alert").length;

  if (lines.length === 0) {
    return (
      <EmptyState
        description="На выбранную дату среза нет ни одного закрывшегося отчётного периода — сверять расчёт с платёжками бухгалтера пока не с чем. Первая строка появится после 31 марта, когда закроется I квартал (сам аванс по УСН за него платится до 28 апреля)."
        icon={<ShieldCheck size={18} aria-hidden="true" />}
        title="Сверять пока нечего"
      />
    );
  }

  return (
    <div className="space-y-3">
      {alertCount > 0 ? (
        <Card className="border-rose-200 bg-rose-50/70 shadow-none">
          <CardContent className="flex gap-3 p-4 text-sm text-rose-900">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <div>
              <div className="font-semibold">Расхождений, требующих решения: {alertCount}</div>
              <p className="mt-1 leading-6">
                Ниже красным — обязательства, где платёжка бухгалтера или факт из банка не
                совпали с нашим расчётом. Пока причина не выяснена, платить по такому
                документу нельзя.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : isBlocked ? (
        // Зелёное «всё сходится» на неполной выручке — прямой обман: расчёт, с которым
        // сравнивают платёжку, сам занижен. Пока база не догружена, честный ответ — жёлтый.
        <Card className="border-amber-200 bg-amber-50/70 shadow-none">
          <CardContent className="flex gap-3 p-4 text-sm text-amber-900">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <div>
              <div className="font-semibold">Расхождений не видно, но проверять рано</div>
              <p className="mt-1 leading-6">
                Выручка загружена не за весь период (причина указана выше), поэтому «сходится»
                здесь значит лишь «не расходится с неполной цифрой». Вернитесь к сверке после
                догрузки выручки.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card className="border-emerald-200 bg-emerald-50/70 shadow-none">
          <CardContent className="flex gap-3 p-4 text-sm text-emerald-900">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <div>
              <div className="font-semibold">Расхождений нет</div>
              <p className="mt-1 leading-6">
                Расчёт, платёжки бухгалтера и факт из банка сходятся по всем обязательствам
                на выбранную дату.
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      <Card className="shadow-none">
        <CardHeader className="pb-3">
          <CardTitle className="text-base font-semibold">
            Сверка: расчёт ↔ документ ↔ факт
          </CardTitle>
          <p className="text-sm text-muted-foreground">
            Прочерк значит «источника нет»: платёжку не присылали или платёж не проходил.
            Это не то же самое, что ноль в документе.
          </p>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="min-w-[220px]">Обязательство</TableHead>
                <TableHead className="text-right">Расчёт</TableHead>
                <TableHead className="text-right">Документ</TableHead>
                <TableHead className="text-right">Факт</TableHead>
                <TableHead className="min-w-[200px]">Вердикт</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {lines.map((line) => (
                <ReconRow key={`${line.tax_kind}-${line.period_code}`} line={line} />
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

/** Иконка «i» со всплывающей подсказкой по наведению/фокусу (без внешних зависимостей). */
function InfoHint({ children, tone = "muted" }: { children: ReactNode; tone?: "muted" | "alert" }) {
  return (
    <span className="group relative inline-flex align-middle">
      <button
        aria-label="Пояснение"
        className={`inline-flex size-4 items-center justify-center rounded-full ${
          tone === "alert"
            ? "text-rose-500 hover:text-rose-700"
            : "text-muted-foreground hover:text-foreground"
        }`}
        type="button"
      >
        <Info className="size-3.5" aria-hidden="true" />
      </button>
      <span
        className="pointer-events-none absolute right-0 top-5 z-30 hidden w-72 rounded-md border bg-card p-2.5 text-left text-xs font-normal leading-5 text-card-foreground shadow-md group-focus-within:block group-hover:block"
        role="tooltip"
      >
        {children}
      </span>
    </span>
  );
}

function ReconRow({ line }: { line: ReconLine }) {
  const isAlert = line.severity === "alert";
  const isCalm = line.severity === "ok" || line.severity === "info";
  const highlighted = isCalm ? null : VERDICT_HIGHLIGHT[line.verdict];
  const emphasis = isAlert ? "font-semibold text-rose-700" : "font-semibold text-amber-800";

  const queryClient = useQueryClient();
  const canManage = usePermissions().hasPermission("accounting.taxes.manage");
  const [payOpen, setPayOpen] = useState(false);
  const payable = line.payable_amount;
  const draftMutation = useMutation({
    mutationFn: () =>
      createTaxPaymentDraft({
        tax_kind: line.tax_kind,
        amount: toNumber(payable),
        purpose: "Единый налоговый платеж",
        for_period: line.period_code,
        for_year: line.due_date ? Number(line.due_date.slice(0, 4)) : undefined,
        due_date: line.due_date ?? undefined,
        title: line.label,
      }),
    onSuccess: () => {
      setPayOpen(false);
      toast.success("Платёж добавлен в «Активные платежи»", {
        description: "Проверьте реквизиты и отправьте в банк из окна активных платежей.",
      });
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
      void queryClient.invalidateQueries({ queryKey: ["finance-payments"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось подготовить платёж")),
  });

  return (
    <TableRow className={SEVERITY_ROW[line.severity] ?? ""}>
      <TableCell className="align-top">
        <div className={`font-medium ${isAlert ? "text-rose-900" : ""}`}>{line.label}</div>
        <div className="text-xs text-muted-foreground">
          {taxKindLabel(line.tax_kind)}
          {line.due_date ? ` · срок ${formatDate(line.due_date)}` : ""}
        </div>
      </TableCell>
      <TableCell className="text-right align-top tabular-nums">
        {formatMoney(line.calculated)}
      </TableCell>
      <TableCell
        className={`text-right align-top tabular-nums ${
          highlighted === "documented" ? emphasis : ""
        }`}
      >
        {formatMoney(line.documented)}
      </TableCell>
      <TableCell
        className={`text-right align-top tabular-nums ${highlighted === "paid" ? emphasis : ""}`}
      >
        {formatMoney(line.paid)}
      </TableCell>
      <TableCell className="align-top">
        <div className="flex items-center gap-1.5">
          <Badge className={SEVERITY_BADGE[line.severity] ?? NEUTRAL_BADGE} variant="outline">
            {VERDICT_LABELS[line.verdict] ?? line.verdict}
          </Badge>
          {line.messages.length > 0 ? (
            <InfoHint tone={isAlert ? "alert" : "muted"}>
              <ul className="space-y-1">
                {line.messages.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            </InfoHint>
          ) : null}
        </div>
        {line.action ? (
          <div
            className={`mt-1.5 text-xs leading-5 ${
              isAlert ? "text-rose-700" : "text-muted-foreground"
            }`}
          >
            <span className="font-medium">Что делать:</span> {line.action}
          </div>
        ) : null}
        {/* Платёж уже в работе — кнопку гасим и показываем состояние: повторная отправка
            плодила бы дубли платёжек. Синхронизировано с окном «Активные платежи». */}
        {line.draft_status === "in_bank" ? (
          <div className="mt-2 flex items-center gap-1.5 text-xs text-sky-700">
            <CheckCircle2 className="size-3.5 shrink-0" aria-hidden="true" />
            Отправлен в банк — подтвердите платёжку в банк-клиенте
          </div>
        ) : line.draft_status === "ready_to_send" ? (
          <div className="mt-2 text-xs text-muted-foreground">
            Платёж подготовлен — отправьте его из окна «Активные платежи»
          </div>
        ) : payable != null && canManage ? (
          <Button
            className="mt-2"
            onClick={() => setPayOpen(true)}
            size="sm"
            variant="outline"
          >
            Отправить в банк ({formatMoney(payable)})
          </Button>
        ) : null}
        <AlertDialog onOpenChange={setPayOpen} open={payOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Подготовить платёж к отправке в банк?</AlertDialogTitle>
              <AlertDialogDescription asChild>
                <div className="space-y-2 text-sm">
                  <p>
                    Платёж на <b>{formatMoney(payable)}</b> — «{line.label}» — появится в окне
                    «Активные платежи». Там вы сверите реквизиты и суммы и отправите его в банк
                    одной кнопкой. Это ещё не оплата: деньги уйдут только после подтверждения
                    платёжки в банк-клиенте.
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Получатель: Казначейство России (ФНС России), КБК 18201061201010000510,
                    назначение «Единый налоговый платеж».
                  </p>
                </div>
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Отмена</AlertDialogCancel>
              <AlertDialogAction
                disabled={draftMutation.isPending}
                onClick={(event) => {
                  event.preventDefault();
                  draftMutation.mutate();
                }}
              >
                {draftMutation.isPending ? (
                  <Loader2 className="animate-spin" aria-hidden="true" />
                ) : null}
                Подготовить платёж
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </TableCell>
    </TableRow>
  );
}

// ── вкладка «Сводка» ───────────────────────────────────────────────────────────

function SummaryTab({ asOf }: { asOf: string }) {
  const query = useQuery({
    queryKey: ["taxes", "overview", asOf],
    queryFn: () => getTaxOverview(asOf),
  });

  if (query.isLoading) {
    return <LoadingBlock rows={6} />;
  }
  if (query.isError) {
    return (
      <ErrorBlock
        error={query.error}
        fallback="Не удалось получить расчёт и сверку"
        onRetry={() => void query.refetch()}
      />
    );
  }
  if (!query.data) {
    return null;
  }

  const { state, reconciliation, is_blocked: isBlocked } = query.data;

  return (
    <div className="space-y-5">
      {/* Блокирующие причины — ВЫШЕ вердикта сверки: иначе владелец сначала читает
          «расхождений нет», а уже потом узнаёт, что сравнивали с неполной цифрой. */}
      <NoticeList
        items={state.blocking}
        title="Расчёт неполный — верить цифрам и вердикту сверки нельзя"
        tone="danger"
      />

      <ReconciliationBlock isBlocked={isBlocked} reconciliation={reconciliation} />

      <NoticeList items={state.warnings} title="На что обратить внимание" tone="warning" />

      <TaxMetrics isBlocked={isBlocked} state={state} />

      <Card className="shadow-none">
        <CardHeader className="pb-3">
          <CardTitle className="text-base font-semibold">Как сложился вычет</CardTitle>
          <p className="text-sm text-muted-foreground">
            Взносы уменьшают налог, но не более чем наполовину. Всё, что не влезло в
            половину, — сгорает и никуда не переносится.
          </p>
        </CardHeader>
        <CardContent className="grid gap-x-8 gap-y-2 p-6 pt-0 text-sm sm:grid-cols-2">
          <DetailRow
            hint="Только по факту уплаты (кассовый принцип)"
            label="Взносы за работников"
            value={formatMoney(state.employees_paid)}
          />
          <DetailRow
            hint={`Доступно ${formatMoney(state.fixed_available)}`}
            label="Фиксированные взносы ИП — заявлено"
            value={formatMoney(state.fixed_claimed)}
          />
          <DetailRow label="Допвзнос 1% — начислено" value={formatMoney(state.extra_accrued)} />
          <DetailRow
            hint={`Доступно ${formatMoney(state.extra_available)}`}
            label="Допвзнос 1% — заявлено"
            value={formatMoney(state.extra_claimed)}
          />
          <DetailRow
            hint="Срезано лимитом 50% — потеряно навсегда"
            label="Сгорело"
            value={formatMoney(state.deduction_burned)}
          />
          <DetailRow
            hint="Начислено, но не уплачено — уйдёт в вычет следующих периодов года"
            label="Отложено"
            value={formatMoney(state.deduction_deferred)}
          />
          <DetailRow
            hint="Внутри года ещё можно использовать"
            label="Не заявлено"
            value={formatMoney(state.deduction_unclaimed)}
          />
          <DetailRow
            hint="Налог плюс взносы к доходу нарастающим итогом"
            label="Итоговая нагрузка"
            value={`${formatMoney(state.total_burden)} · ${formatRate(state.effective_rate)}`}
          />
        </CardContent>
      </Card>
    </div>
  );
}

function TaxMetrics({ state, isBlocked }: { state: TaxState; isBlocked: boolean }) {
  const overpayment = toNumber(state.overpayment);
  const amountDue = toNumber(state.amount_due);
  // Зелёный ноль на неполной выручке читается как «платить нечего» — при блокировке
  // оставляем нейтральный цвет, чтобы цифра не выглядела закрытым вопросом.
  const dueTone = amountDue > 0 ? "danger" : isBlocked ? "default" : "success";
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        hint={`Нарастающим итогом с 1 января, ${periodTitle(state.period_code)} ${state.year}`}
        title="Доход"
        value={formatMoney(state.income_ytd)}
      />
      <MetricCard
        hint="6% от дохода, в полных рублях (п. 6 ст. 52 НК)"
        title="Налог исчисленный"
        value={formatMoney(state.tax_computed)}
      />
      <MetricCard
        hint={`Потолок 50% — ${formatMoney(state.deduction_limit)}. Всего взносов заявлено ${formatMoney(
          state.deduction_total,
        )}`}
        title="Вычет применён"
        value={formatMoney(state.deduction_applied)}
      />
      <MetricCard
        hint={
          overpayment > 0
            ? `Переплата ${formatMoney(state.overpayment)}: авансы больше начисленного`
            : `Уже уплачено авансами ${formatMoney(state.advances_paid)}`
        }
        title="К уплате"
        tone={dueTone}
        value={formatMoney(state.amount_due)}
      />
    </div>
  );
}

// ── вкладка «Календарь» ────────────────────────────────────────────────────────

function CalendarTab({ year }: { year: number }) {
  const query = useQuery({
    queryKey: ["taxes", "calendar", year],
    queryFn: () => getTaxCalendar(year),
  });

  if (query.isLoading) {
    return <LoadingBlock rows={5} />;
  }
  if (query.isError) {
    return (
      <ErrorBlock
        error={query.error}
        fallback="Не удалось загрузить календарь сроков"
        onRetry={() => void query.refetch()}
      />
    );
  }

  const data = query.data;
  if (!data || data.items.length === 0) {
    return (
      <EmptyState
        description={`За ${year} год обязательств не заведено. Они появляются двумя путями: платёжка бухгалтера продвигается во вкладке «Документы» либо платёж приходит фактом из банковской выписки.`}
        icon={<CalendarClock size={18} aria-hidden="true" />}
        title="Сроков пока нет"
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
        <span>
          Уплачено:{" "}
          <span className="font-semibold tabular-nums text-foreground">
            {formatMoney(data.paid_total)}
          </span>
        </span>
        <span>
          Запланировано:{" "}
          <span className="font-semibold tabular-nums text-foreground">
            {formatMoney(data.planned_total)}
          </span>
        </span>
        {data.overdue_count > 0 ? (
          <span className="text-rose-700">
            Просрочено: {data.overdue_count} на{" "}
            <span className="font-semibold tabular-nums">{formatMoney(data.overdue_total)}</span>
          </span>
        ) : (
          <span className="text-emerald-700">Просроченных сроков нет</span>
        )}
      </div>

      <Card className="shadow-none">
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[130px]">Дата</TableHead>
                <TableHead className="min-w-[240px]">Что платим</TableHead>
                <TableHead className="text-right">Сумма</TableHead>
                <TableHead className="w-[180px]">Статус</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((item) => (
                <CalendarRow item={item} key={item.id} />
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

function CalendarRow({ item }: { item: TaxCalendarItem }) {
  const status = PAYMENT_STATUS[item.status] ?? { label: item.status, className: NEUTRAL_BADGE };
  return (
    <TableRow className={item.is_overdue ? "bg-rose-50/70 hover:bg-rose-50" : undefined}>
      <TableCell
        className={`whitespace-nowrap align-top tabular-nums ${
          item.is_overdue ? "font-semibold text-rose-700" : ""
        }`}
      >
        {formatDate(item.due_date ?? item.paid_on)}
        <div className="text-xs font-normal text-muted-foreground">
          {item.status === "planned" ? "срок уплаты" : "дата списания"}
        </div>
      </TableCell>
      <TableCell className="align-top">
        <div className="font-medium">{taxKindLabel(item.kind)}</div>
        <div className="text-xs text-muted-foreground">
          {[
            item.for_period ? periodTitle(item.for_period) : null,
            item.recipient ? recipientLabel(item.recipient) : null,
            item.note ?? item.purpose,
          ]
            .filter(Boolean)
            .join(" · ") || "—"}
        </div>
      </TableCell>
      <TableCell className="text-right align-top tabular-nums">{formatMoney(item.amount)}</TableCell>
      <TableCell className="align-top">
        <Badge
          className={item.is_overdue ? OVERDUE_BADGE : status.className}
          variant="outline"
        >
          {item.is_overdue ? "Просрочено" : status.label}
        </Badge>
      </TableCell>
    </TableRow>
  );
}

// ── вкладка «Платежи» ──────────────────────────────────────────────────────────

function PaymentsTab({ year }: { year: number }) {
  const query = useQuery({
    queryKey: ["taxes", "payments", year],
    queryFn: () => getTaxPayments({ year }),
  });

  if (query.isLoading) {
    return <LoadingBlock rows={5} />;
  }
  if (query.isError) {
    return (
      <ErrorBlock
        error={query.error}
        fallback="Не удалось загрузить реестр платежей"
        onRetry={() => void query.refetch()}
      />
    );
  }

  const data = query.data;
  if (!data || data.items.length === 0) {
    return (
      <EmptyState
        description={`За ${year} год фактических уплат в бюджет не зарегистрировано. Здесь только реальные списания из банка; планы и сроки — во вкладке «Календарь».`}
        icon={<FileText size={18} aria-hidden="true" />}
        title="Уплат пока нет"
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
        <span>
          Уплачено за {year} год (факт из банка):{" "}
          <span className="font-semibold tabular-nums text-foreground">
            {formatMoney(data.paid_total)}
          </span>
        </span>
        <span>строк: {data.total}</span>
      </div>

      <Card className="shadow-none">
        <CardHeader className="pb-3">
          <p className="text-sm text-muted-foreground">
            Только реальные списания: что и когда фактически ушло в бюджет. Будущие сроки и
            планы — во вкладке «Календарь», помесячная разбивка ЕНП — в «Сводке». Одна
            строка — одно назначение внутри перевода: ЕНП уходит единой суммой, но внутри и
            НДФЛ (в вычет не идёт), и взносы за работников (идут).
          </p>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[120px]">Дата</TableHead>
                <TableHead className="min-w-[220px]">Вид платежа</TableHead>
                <TableHead className="text-right">Сумма</TableHead>
                <TableHead className="min-w-[200px]">Получатель и документ</TableHead>
                <TableHead className="w-[160px]">Статус</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((row) => (
                <PaymentRow key={row.id} row={row} />
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

function PaymentRow({ row }: { row: TaxPaymentRow }) {
  const status = PAYMENT_STATUS[row.status] ?? { label: row.status, className: NEUTRAL_BADGE };
  const reconstructed = row.quality_status === "reconstructed";
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap align-top tabular-nums">
        {formatDate(row.paid_on)}
      </TableCell>
      <TableCell className="align-top">
        <div className="font-medium">{taxKindLabel(row.kind)}</div>
        <div className="text-xs text-muted-foreground">
          {[row.for_period ? periodTitle(row.for_period) : null, row.purpose]
            .filter(Boolean)
            .join(" · ") || "—"}
        </div>
      </TableCell>
      <TableCell className="text-right align-top tabular-nums">{formatMoney(row.amount)}</TableCell>
      <TableCell className="align-top">
        <div className="truncate">{recipientLabel(row.recipient)}</div>
        <div className="text-xs text-muted-foreground">
          {row.document_number ? `№ ${row.document_number}` : "без номера документа"}
        </div>
        {reconstructed ? (
          <div className="mt-1 text-xs text-amber-800">
            Сумма назначения выведена расчётом: банк отдаёт бюджетные платежи одним КБК.
          </div>
        ) : null}
      </TableCell>
      <TableCell className="align-top">
        <Badge className={status.className} variant="outline">
          {status.label}
        </Badge>
      </TableCell>
    </TableRow>
  );
}

// ── вкладка «Документы» ────────────────────────────────────────────────────────

function DocumentsTab({ canManage }: { canManage: boolean }) {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [aiReport, setAiReport] = useState<AiAuditReport | null>(null);

  const query = useQuery({
    queryKey: ["taxes", "documents"],
    queryFn: () => getTaxDocuments(),
  });

  // Наверх — то, что ждёт человека: сначала needs_review, потом ошибки разбора.
  const rows = useMemo(() => {
    const source = query.data?.items ?? [];
    const rank: Record<string, number> = { needs_review: 0, error: 1, parsed: 2 };
    return [...source].sort((a, b) => {
      const byStatus = (rank[a.status] ?? 3) - (rank[b.status] ?? 3);
      if (byStatus !== 0) return byStatus;
      return (b.received_at ?? "").localeCompare(a.received_at ?? "");
    });
  }, [query.data]);

  const readyCount = rows.filter(
    (row) =>
      row.status === "parsed" &&
      (row.document_type === "payment_order" ||
        row.document_type === "turnover_statement") &&
      // ЕНП-платёжка не продвигается вручную: её разнос делает оборотка.
      row.recognition?.tax_kind !== "enp_payroll",
  ).length;

  const promoteMutation = useMutation({
    mutationFn: promoteTaxDocuments,
    onSuccess: (summary: TaxPromotionSummary) => {
      setConfirmOpen(false);
      const reasons = summary.results
        .filter((item) => item.action === "skipped")
        .map((item) => item.reason)
        .filter((reason): reason is string => Boolean(reason));
      const description = reasons.length > 0 ? reasons.join("; ") : undefined;

      if (summary.created === 0 && summary.updated === 0) {
        toast.info("Ни один документ не продвинут", {
          description: description ?? "Готовых к продвижению платёжек не нашлось.",
        });
      } else {
        toast.success(
          `Продвинуто: создано ${summary.created}, обновлено ${summary.updated}` +
            (summary.skipped > 0 ? `, пропущено ${summary.skipped}` : ""),
          { description },
        );
      }
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось продвинуть документы")),
  });

  const refreshMutation = useMutation({
    mutationFn: refreshTaxDocuments,
    onSuccess: (result) => {
      const added = Number(result.parsed ?? 0) + Number(result.needs_review ?? 0);
      if (result.status === "not_configured") {
        toast.info("Почта не настроена на этом стенде");
      } else if (added === 0) {
        toast.info("Новых документов в почте нет");
      } else {
        toast.success(`Забрано новых документов: ${added}`);
      }
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось проверить почту")),
  });

  const aiAuditMutation = useMutation({
    mutationFn: aiReviewAllTaxes,
    onSuccess: (report) => {
      setAiReport(report);
      const proposals = report.documents.filter((d) => d.proposal).length;
      toast.success(
        `ИИ-ревизия готова${
          proposals > 0
            ? `: предложений по документам — ${proposals} (смотрите «Разбор ИИ» в строках)`
            : ""
        }`,
      );
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "ИИ-ревизия не удалась")),
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-3xl text-sm text-muted-foreground">
          Что пришло от бухгалтера на почту. Продвижение превращает уверенно распознанную
          платёжку в плановое обязательство: оно попадает в календарь и в сверку ещё до того,
          как деньги ушли из банка. Документы со статусом «Нужна проверка» автоматика не
          трогает — их проверяете вы: кнопками в строке.
        </p>
        {canManage ? (
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={refreshMutation.isPending}
              onClick={() => refreshMutation.mutate()}
              variant="outline"
            >
              {refreshMutation.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : (
                <RefreshCw aria-hidden="true" />
              )}
              Проверить почту
            </Button>
            <Button
              disabled={aiAuditMutation.isPending}
              onClick={() => aiAuditMutation.mutate()}
              title="Claude разберёт документы, требующие внимания, и проверит сверку целиком"
              variant="outline"
            >
              {aiAuditMutation.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : (
                <Sparkles aria-hidden="true" />
              )}
              ИИ-ревизия
            </Button>
            <Button
              disabled={promoteMutation.isPending || readyCount === 0}
              onClick={() => setConfirmOpen(true)}
            >
              {promoteMutation.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : null}
              Продвинуть готовые{readyCount > 0 ? ` (${readyCount})` : ""}
            </Button>
          </div>
        ) : null}
      </div>

      {aiReport ? (
        <div className="space-y-2 rounded-lg border border-violet-200 bg-violet-50/60 p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-start gap-2">
              <Sparkles className="mt-0.5 size-4 shrink-0 text-violet-700" aria-hidden="true" />
              <div>
                <div className="text-sm font-medium text-violet-900">Вердикт ИИ-ревизии</div>
                <p className="text-sm text-violet-900/90">{aiReport.verdict}</p>
              </div>
            </div>
            <button
              className="text-xs text-violet-700 hover:underline"
              onClick={() => setAiReport(null)}
              type="button"
            >
              Скрыть
            </button>
          </div>
          {aiReport.findings.length > 0 ? (
            <ul className="space-y-1.5">
              {aiReport.findings.map((finding, index) => (
                <li className="text-sm" key={index}>
                  <Badge
                    className={
                      finding.severity === "alert"
                        ? "border-rose-200 bg-rose-50 text-rose-700"
                        : finding.severity === "warning"
                          ? "border-amber-200 bg-amber-50 text-amber-800"
                          : NEUTRAL_BADGE
                    }
                    variant="outline"
                  >
                    {finding.title}
                  </Badge>{" "}
                  <span className="text-violet-900/90">{finding.detail}</span>
                </li>
              ))}
            </ul>
          ) : null}
          <p className="text-xs text-violet-900/60">
            Проверено моделью {"Claude"} по снимку сверки, обязательств и ЕНС-кошелька.
            Объяснения по разобранным документам — в их строках ниже.
          </p>
        </div>
      ) : null}

      {query.isLoading ? <LoadingBlock rows={5} /> : null}
      {query.isError ? (
        <ErrorBlock
          error={query.error}
          fallback="Не удалось загрузить документы"
          onRetry={() => void query.refetch()}
        />
      ) : null}

      {!query.isLoading && !query.isError && rows.length === 0 ? (
        <EmptyState
          description="Почтовый разбор ещё ничего не положил в очередь: писем с платёжками от бухгалтера не приходило либо разбор почты выключен в настройках."
          icon={<FileText size={18} aria-hidden="true" />}
          title="Документов пока нет"
        />
      ) : null}

      {rows.length > 0 ? (
        <Card className="shadow-none">
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="min-w-[220px]">Файл</TableHead>
                  <TableHead className="w-[190px]">Тип</TableHead>
                  <TableHead className="min-w-[260px]">Распознано</TableHead>
                  <TableHead className="w-[200px]">Статус</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <DocumentRow key={row.id} row={row} />
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ) : null}

      <AlertDialog onOpenChange={setConfirmOpen} open={confirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Продвинуть готовые документы?</AlertDialogTitle>
            <AlertDialogDescription>
              Из уверенно распознанных документов ({readyCount} шт.) платёжки станут плановыми
              обязательствами, а оборотно-сальдовые ведомости — помесячной раскладкой зарплаты
              (эталон для разноса ЕНП). Нулевые платёжки-заглушки и зарплатный ЕНП без разноса
              система пропустит с причиной, документы со статусом «Нужна проверка» не тронет.
              Повторное продвижение обновит уже созданное, а не задвоит его.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={promoteMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                promoteMutation.mutate();
              }}
            >
              Продвинуть
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/** Проверка платёжки человеком: дозаполнить то, что автоматика не распознала.

    Раньше «Проверено» лишь снимало блокировку — срок так и оставался пустым, и
    обязательство создавалось без срока. Теперь поля правятся прямо при проверке. */
function ReviewPaymentDialog({
  row,
  onClose,
}: {
  row: TaxDocumentRow;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const recognition = row.recognition ?? {};
  const [taxKind, setTaxKind] = useState(recognition.tax_kind ?? "");
  const [amount, setAmount] = useState(
    recognition.amount !== undefined && recognition.amount !== null
      ? String(recognition.amount)
      : "",
  );
  const [due, setDue] = useState(recognition.due_date ?? "");
  const [period, setPeriod] = useState(recognition.period_hint ?? "");

  const mutation = useMutation({
    mutationFn: () =>
      reviewTaxDocument(row.id, "parsed", {
        tax_kind: taxKind || undefined,
        amount: amount ? Number(amount.replace(",", ".").replace(/\s/g, "")) : undefined,
        due_date: due || undefined,
        period_hint: period || undefined,
      }),
    onSuccess: () => {
      toast.success("Документ проверен", {
        description: "Поля сохранены — «Продвинуть готовые» создаст обязательство по ним.",
      });
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить проверку")),
  });

  const fieldLabel = "text-sm font-medium";
  const missing = (recognition.review_reasons ?? []).join("; ");

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Проверить платёжку</DialogTitle>
          <DialogDescription>{row.filename ?? "Документ"}</DialogDescription>
        </DialogHeader>
        <div className="min-w-0 space-y-3">
          {missing ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Автоматика не распознала: {missing}. Заполните недостающее — обязательство
              создастся с этими значениями.
            </div>
          ) : null}
          <div className="space-y-1.5">
            <label className={fieldLabel} htmlFor="rev-kind">
              Вид платежа
            </label>
            <select
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              id="rev-kind"
              onChange={(event) => setTaxKind(event.target.value)}
              value={taxKind}
            >
              <option value="">— не определён —</option>
              {Object.entries(TAX_KIND_LABELS)
                .filter(([kind]) => !["contrib_employees", "ndfl", "injury", "other"].includes(kind))
                .map(([kind, label]) => (
                  <option key={kind} value={kind}>
                    {label}
                  </option>
                ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label className={fieldLabel} htmlFor="rev-amount">
              Сумма, ₽
            </label>
            <Input
              className="tabular-nums"
              id="rev-amount"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              value={amount}
            />
          </div>
          <div className="space-y-1.5">
            <label className={fieldLabel} htmlFor="rev-due">
              Срок уплаты
            </label>
            <Input
              id="rev-due"
              onChange={(event) => setDue(event.target.value)}
              type="date"
              value={due}
            />
          </div>
          <div className="space-y-1.5">
            <label className={fieldLabel} htmlFor="rev-period">
              Период
            </label>
            {/* В данные уходит КОД периода (q1/h1/9m/year/ГГГГ-ММ) — по нему сверка
                матчит платёжку с обязательством; человеку показываем русские названия. */}
            <select
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              id="rev-period"
              onChange={(event) => setPeriod(event.target.value)}
              value={period}
            >
              <option value="">— не указан —</option>
              {Object.entries(PERIOD_TITLES).map(([code, label]) => (
                <option key={code} value={code}>
                  {label}
                </option>
              ))}
              {period && !(period in PERIOD_TITLES) ? (
                <option value={period}>{periodTitle(period)}</option>
              ) : null}
            </select>
          </div>
        </div>
        <DialogFooter>
          <Button onClick={onClose} variant="outline">
            Отмена
          </Button>
          <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <Loader2 className="mr-1.5 animate-spin" size={15} />
            ) : null}
            Подтвердить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Окно ИИ-разбора: объяснение и предложение полей. ИИ не применяет ничего сам —
    владелец смотрит и подтверждает кнопкой «Применить» («да, делай»). */
function AiReviewDialog({
  row,
  onClose,
  onRerun,
  rerunPending,
}: {
  row: TaxDocumentRow;
  onClose: () => void;
  onRerun: () => void;
  rerunPending: boolean;
}) {
  const queryClient = useQueryClient();
  const review = row.recognition?.ai_review;
  const proposal = review?.proposal ?? null;
  const canApply = Boolean(proposal) && row.status !== "promoted" && !review?.applied;

  const applyMutation = useMutation({
    mutationFn: () => aiApplyTaxProposal(row.id),
    onSuccess: () => {
      toast.success("Предложение ИИ применено", {
        description: "Документ распознан — «Продвинуть готовые» создаст обязательство.",
      });
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось применить")),
  });

  if (!review) return null;
  const confidencePct = Math.round((review.confidence ?? 0) * 100);

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="size-4 text-violet-700" aria-hidden="true" />
            Разбор ИИ
          </DialogTitle>
          <DialogDescription>{row.filename ?? "Документ"}</DialogDescription>
        </DialogHeader>
        <div className="min-w-0 space-y-3">
          <p className="text-sm leading-6">{review.summary}</p>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge className={NEUTRAL_BADGE} variant="outline">
              уверенность {confidencePct}%
            </Badge>
            {review.applied ? (
              <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700" variant="outline">
                применено
              </Badge>
            ) : null}
            {review.at ? <span>{formatDateTime(review.at)}</span> : null}
          </div>
          {review.needs_human && (review.reasons ?? []).length > 0 ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              <div className="font-medium">Нужен человек:</div>
              <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
                {(review.reasons ?? []).map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {proposal ? (
            <div className="rounded-md border border-violet-200 bg-violet-50/60 px-3 py-2">
              <div className="text-xs font-medium text-violet-900">
                Предложенные поля платёжки
              </div>
              <dl className="mt-1.5 space-y-1 text-sm">
                {proposal.tax_kind ? (
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Вид платежа</dt>
                    <dd className="font-medium">{taxKindLabel(proposal.tax_kind)}</dd>
                  </div>
                ) : null}
                {proposal.amount ? (
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Сумма</dt>
                    <dd className="font-medium tabular-nums">
                      {formatMoney(Number(proposal.amount))}
                    </dd>
                  </div>
                ) : null}
                {proposal.due_date ? (
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Срок уплаты</dt>
                    <dd className="font-medium">{formatDate(proposal.due_date)}</dd>
                  </div>
                ) : null}
                {proposal.period_hint ? (
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">Период</dt>
                    <dd className="font-medium">{periodTitle(proposal.period_hint)}</dd>
                  </div>
                ) : null}
              </dl>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Полей к применению нет: это не платёжное поручение либо данных в файле
              недостаточно.
            </p>
          )}
        </div>
        <DialogFooter className="gap-2">
          <Button disabled={rerunPending} onClick={onRerun} size="sm" variant="ghost">
            {rerunPending ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : (
              <RefreshCw aria-hidden="true" />
            )}
            Разобрать заново
          </Button>
          <Button onClick={onClose} size="sm" variant="outline">
            Закрыть
          </Button>
          {canApply ? (
            <Button
              disabled={applyMutation.isPending}
              onClick={() => applyMutation.mutate()}
              size="sm"
            >
              {applyMutation.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : null}
              Применить
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DocumentRow({ row }: { row: TaxDocumentRow }) {
  const queryClient = useQueryClient();
  const canManage = usePermissions().hasPermission("accounting.taxes.manage");
  const [reviewOpen, setReviewOpen] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const reviewMutation = useMutation({
    mutationFn: (status: "parsed" | "ignored") => reviewTaxDocument(row.id, status),
    onSuccess: (_result, status) => {
      toast.success(status === "parsed" ? "Отмечено проверенным" : "Документ отклонён");
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось изменить статус")),
  });
  // Разбор по клику: результат показываем в окне, а не вписываем в строку.
  const aiMutation = useMutation({
    mutationFn: () => aiReviewTaxDocument(row.id),
    onSuccess: () => {
      setAiOpen(true);
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "ИИ-разбор не удался")),
  });
  const status = DOCUMENT_STATUS[row.status] ?? { label: row.status, className: NEUTRAL_BADGE };
  const statusHint = DOCUMENT_STATUS_HINT[row.status];
  const recognition = row.recognition ?? {};

  // Открываем исходник синхронно в жесте клика (иначе браузер блокирует попап после await);
  // если попап всё же заблокирован — скачиваем файл.
  async function openFile() {
    const win = window.open("", "_blank");
    try {
      const url = await fetchTaxDocumentFileUrl(row.id);
      if (win && !win.closed) {
        win.location.href = url;
      } else {
        const link = document.createElement("a");
        link.href = url;
        link.download = row.filename ?? "document";
        document.body.appendChild(link);
        link.click();
        link.remove();
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      win?.close();
      toast.error(apiErrorMessage(error, "Не удалось открыть файл"));
    }
  }

  const reasons = recognition.review_reasons ?? [];
  const needsReview = row.status === "needs_review";
  // ИИ-разбор доступен и там, где автоматика сдалась совсем (ошибка/неподдержанный формат).
  const needsAttention =
    needsReview || row.status === "error" || row.status === "unsupported";
  const isTurnover = row.document_type === "turnover_statement";
  const isPayroll = row.document_type === "payroll_statement";
  const turnoverRows = recognition.rows ?? [];
  const payrollPeriod = formatPeriodRange(recognition.period_start, recognition.period_end);
  const hasFields =
    Boolean(recognition.tax_kind) ||
    recognition.amount !== undefined ||
    Boolean(recognition.due_date);
  const m = (value: Money | null | undefined) =>
    value === undefined || value === null ? "—" : formatMoney(value);

  return (
    <TableRow className={needsReview ? "bg-amber-50/50 hover:bg-amber-50" : undefined}>
      <TableCell className="align-top">
        {row.has_file ? (
          <button
            className="flex items-start gap-1.5 text-left font-medium text-sky-700 hover:underline"
            onClick={openFile}
            title="Открыть исходный файл"
            type="button"
          >
            <FileText className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {row.filename ?? "Без имени файла"}
          </button>
        ) : (
          <div className="font-medium">{row.filename ?? "Без имени файла"}</div>
        )}
        <div className="text-xs text-muted-foreground">
          {[formatDateTime(row.received_at ?? row.created_at), row.from_addr]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </TableCell>
      <TableCell className="align-top">
        {DOCUMENT_TYPE_LABELS[row.document_type] ?? row.document_type}
      </TableCell>
      <TableCell className="align-top">
        {isTurnover ? (
          <div className="space-y-1">
            <div className="font-medium">Зарплата · {turnoverPeriodLabel(recognition)}</div>
            {turnoverRows.length > 0 ? (
              <div className="space-y-1 text-xs text-muted-foreground">
                {turnoverRows.map((person, index) => (
                  <div key={person.tab_number ?? index}>
                    <span className="font-medium text-foreground">
                      {person.employee ?? "—"}
                    </span>
                    {" — "}
                    начислено {m(person.accrued)} · НДФЛ {m(person.ndfl)} · аванс{" "}
                    {m(person.advance)} · взносы {m(person.contributions)} · к выплате{" "}
                    {m(person.to_pay)}
                  </div>
                ))}
                <div className="pt-0.5 text-foreground">
                  Итого за месяц: НДФЛ {m(recognition.ndfl_total)} · взносы{" "}
                  {m(recognition.contributions_total)} · травматизм{" "}
                  {m(recognition.injury_total)}
                </div>
              </div>
            ) : (
              <span className="text-sm text-muted-foreground">Строки не распознаны</span>
            )}
          </div>
        ) : isPayroll ? (
          <div className="space-y-1">
            <div className="font-medium">
              {payoutKindLabel(recognition.payout_kind)}
              {payrollPeriod ? ` · ${payrollPeriod}` : ""}
            </div>
            {turnoverRows.length > 0 ? (
              <div className="space-y-0.5 text-xs text-muted-foreground">
                {turnoverRows.map((person, index) => (
                  <div key={person.tab_number ?? index}>
                    <span className="font-medium text-foreground">
                      {person.employee ?? "—"}
                    </span>
                    {" — "}
                    {m(person.amount)}
                  </div>
                ))}
                <div className="pt-0.5 text-foreground">Итого: {m(recognition.total)}</div>
              </div>
            ) : (
              <span className="text-sm text-muted-foreground">Строки не распознаны</span>
            )}
          </div>
        ) : hasFields ? (
          <div className="space-y-0.5">
            <div className="font-medium">{taxKindLabel(recognition.tax_kind)}</div>
            <div className="text-xs text-muted-foreground">
              {[
                recognition.amount !== undefined && recognition.amount !== null
                  ? formatMoney(recognition.amount)
                  : null,
                recognition.period_hint ? periodTitle(recognition.period_hint) : null,
                recognition.due_date ? `срок ${formatDate(recognition.due_date)}` : null,
                recognition.kbk ? `КБК ${recognition.kbk}` : null,
              ]
                .filter(Boolean)
                .join(" · ") || "—"}
            </div>
            {recognition.tax_kind === "enp_payroll" ? (
              <div className="text-xs leading-5 text-emerald-700">
                Разнос НДФЛ и взносов берётся из оборотки за месяц — отдельного подтверждения
                не нужно.
              </div>
            ) : null}
          </div>
        ) : (
          <span className="text-sm text-muted-foreground">Поля не распознаны</span>
        )}
        {row.error ? <div className="mt-1 text-xs text-rose-700">{row.error}</div> : null}
      </TableCell>
      <TableCell className="align-top">
        <div className="flex items-center gap-1.5">
          <Badge className={status.className} variant="outline">
            {status.label}
          </Badge>
          {statusHint ? <InfoHint>{statusHint}</InfoHint> : null}
        </div>
        {reasons.length > 0 ? (
          <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-xs leading-5 text-amber-800">
            {reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : null}
        {recognition.reason ? (
          <div className="mt-1.5 max-w-56 text-xs leading-5 text-muted-foreground">
            {recognition.reason}
          </div>
        ) : null}
        {(needsAttention || recognition.ai_review) && canManage ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {needsReview ? (
              <Button
                disabled={reviewMutation.isPending}
                onClick={() =>
                  // У платёжки открываем форму: можно дозаполнить срок/сумму/вид/период.
                  // У ведомостей/оборотк править нечего — подтверждаем как есть.
                  row.document_type === "payment_order"
                    ? setReviewOpen(true)
                    : reviewMutation.mutate("parsed")
                }
                size="sm"
                variant="outline"
              >
                {row.document_type === "payment_order" ? "Проверить…" : "Проверено"}
              </Button>
            ) : null}
            {/* Разбор уже есть — кнопка открывает окно; нет — запускает модель. */}
            <Button
              className={recognition.ai_review ? "text-violet-700" : undefined}
              disabled={aiMutation.isPending}
              onClick={() =>
                recognition.ai_review ? setAiOpen(true) : aiMutation.mutate()
              }
              size="sm"
              title={
                recognition.ai_review
                  ? "Открыть разбор: что это за документ и что предлагает ИИ"
                  : "Claude прочитает документ, объяснит его и предложит недостающие поля"
              }
              variant="outline"
            >
              {aiMutation.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : (
                <Sparkles aria-hidden="true" />
              )}
              {recognition.ai_review ? "Разбор ИИ" : "ИИ-разбор"}
            </Button>
            {needsReview ? (
              <Button
                disabled={reviewMutation.isPending}
                onClick={() => reviewMutation.mutate("ignored")}
                size="sm"
                variant="ghost"
              >
                Игнорировать
              </Button>
            ) : null}
          </div>
        ) : null}
        {aiOpen && recognition.ai_review ? (
          <AiReviewDialog
            onClose={() => setAiOpen(false)}
            onRerun={() => aiMutation.mutate()}
            rerunPending={aiMutation.isPending}
            row={row}
          />
        ) : null}
        {reviewOpen ? (
          <ReviewPaymentDialog onClose={() => setReviewOpen(false)} row={row} />
        ) : null}
      </TableCell>
    </TableRow>
  );
}

// ── вкладка «НДС-льгота»: зарплатный критерий ───────────────────────────────────

function VatTab({ year, canManage }: { year: number; canManage: boolean }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const query = useQuery({
    queryKey: ["taxes", "vat", year],
    queryFn: () => getVatCriterion(year),
  });
  const mutation = useMutation({
    mutationFn: (amount: number) => setVatThreshold(year, amount),
    onSuccess: () => {
      setDraft("");
      toast.success("Региональный порог сохранён");
      void queryClient.invalidateQueries({ queryKey: ["taxes", "vat"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить порог")),
  });

  if (query.isLoading) {
    return <LoadingBlock rows={4} />;
  }
  if (query.isError || !query.data) {
    return (
      <ErrorBlock
        error={query.error}
        fallback="Не удалось загрузить зарплатный критерий"
        onRetry={() => void query.refetch()}
      />
    );
  }

  const data: VatWageCriterion = query.data;
  const indicator = data.indicator_full ?? data.indicator_all;
  const verdict =
    data.passes === null
      ? { label: "порог не введён", className: NEUTRAL_BADGE }
      : data.passes
        ? {
            label: "критерий проходит",
            className: "border-emerald-200 bg-emerald-50 text-emerald-700",
          }
        : { label: "критерий НЕ проходит", className: OVERDUE_BADGE };

  const submit = () => {
    const amount = Number(draft.replace(/\s/g, "").replace(",", "."));
    if (Number.isFinite(amount) && amount > 0) {
      mutation.mutate(amount);
    }
  };

  return (
    <div className="space-y-3">
      <Card className="shadow-none">
        <CardHeader className="pb-3">
          <CardTitle className="text-base font-semibold">
            Зарплатный критерий льготы по НДС
          </CardTitle>
          <p className="text-sm text-muted-foreground">
            Одно из условий освобождения общепита от НДС (подп. 38 п. 3 ст. 149 НК) —
            среднемесячная зарплата не ниже среднеотраслевой по региону (ОКВЭД класс 56).
            Считаем по действующему штатному сотруднику из оборотк бухгалтера; ушедшие в декрет
            и уволенные в расчёт не берутся.
          </p>
        </CardHeader>
        <CardContent className="space-y-4 p-6 pt-0">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <div className="text-xs text-muted-foreground">Действующий сотрудник</div>
              <div className="font-medium">{data.active_employee ?? "—"}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">
                Показатель (по полным месяцам)
              </div>
              <div className="font-medium">
                {indicator != null ? formatMoney(indicator) : "—"}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Региональный порог</div>
              <div className="font-medium">
                {data.threshold != null ? formatMoney(data.threshold) : "не введён"}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">Вердикт</div>
              <Badge className={verdict.className} variant="outline">
                {verdict.label}
              </Badge>
              {data.margin != null ? (
                <div className="mt-1 text-xs text-muted-foreground">
                  {data.passes ? "запас" : "дефицит"} {formatMoney(data.margin)}
                </div>
              ) : null}
            </div>
          </div>

          {data.messages.length > 0 ? (
            <ul className="list-disc space-y-0.5 pl-4 text-sm text-muted-foreground">
              {data.messages.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          ) : null}

          {canManage ? (
            <div className="flex flex-wrap items-end gap-2 border-t pt-3">
              <div>
                <label className="text-xs text-muted-foreground" htmlFor="vat-threshold">
                  Региональный порог за {year} год, ₽ (Росстат / ЕМИСС)
                </label>
                <Input
                  className="mt-1 w-48"
                  id="vat-threshold"
                  inputMode="decimal"
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder={
                    data.threshold != null ? String(data.threshold) : "напр. 57000"
                  }
                  value={draft}
                />
              </div>
              <Button disabled={mutation.isPending || draft.trim() === ""} onClick={submit}>
                {mutation.isPending ? (
                  <Loader2 className="animate-spin" aria-hidden="true" />
                ) : null}
                Сохранить порог
              </Button>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card className="shadow-none">
        <CardHeader className="pb-3">
          <CardTitle className="text-base font-semibold">Начисления по месяцам</CardTitle>
          <p className="text-sm text-muted-foreground">
            Из оборотк. «Полный месяц» — где начислено равно окладу; месяц приёма или ухода
            неполный и занижает среднее, поэтому основной показатель считается по полным.
          </p>
        </CardHeader>
        <CardContent className="p-6 pt-0">
          {data.months.length === 0 ? (
            <EmptyState
              description="Оборотки за год ещё не загружены."
              title="Нет начислений"
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Месяц</TableHead>
                  <TableHead className="text-right">Начислено</TableHead>
                  <TableHead>Отработка</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.months.map((row) => (
                  <TableRow key={row.month}>
                    <TableCell>{MONTHS_RU_NOMINATIVE[row.month - 1] ?? row.month}</TableCell>
                    <TableCell className="text-right">{formatMoney(row.accrued)}</TableCell>
                    <TableCell>
                      {row.full_month ? (
                        <span className="text-muted-foreground">полный месяц</span>
                      ) : (
                        <span className="text-amber-700">неполный</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ── блок «Источники данных» ────────────────────────────────────────────────────

function SourcesBlock() {
  const query = useQuery({ queryKey: ["taxes", "sources"], queryFn: getTaxSources });

  if (query.isLoading) {
    return <LoadingBlock rows={2} />;
  }
  if (query.isError) {
    return (
      <ErrorBlock
        error={query.error}
        fallback="Не удалось загрузить реестр источников"
        onRetry={() => void query.refetch()}
      />
    );
  }

  const sources = (query.data?.sources ?? []).filter(
    (item) => !HIDDEN_SOURCE_KEYS.has(item.key),
  );
  if (sources.length === 0) {
    return null;
  }

  const weakKeys = new Set((query.data?.weakest_links ?? []).map((item) => item.key));
  const ordered = [...sources].sort((a, b) => {
    const weak = Number(weakKeys.has(b.key)) - Number(weakKeys.has(a.key));
    if (weak !== 0) return weak;
    return a.title.localeCompare(b.title, "ru");
  });

  return (
    <Card className="shadow-none">
      <CardHeader className="pb-3">
        <CardTitle className="text-base font-semibold">Источники данных</CardTitle>
        <p className="text-sm text-muted-foreground">
          Где цифра взята из документа, а где выведена расчётом. Слабые звенья — вверху: их
          нельзя считать первичными, и расхождение по ним объясняется методикой, а не ошибкой.
        </p>
      </CardHeader>
      <CardContent className="grid gap-2 p-6 pt-0 md:grid-cols-2">
        {ordered.map((source) => (
          <SourceItem isWeak={weakKeys.has(source.key)} key={source.key} source={source} />
        ))}
      </CardContent>
    </Card>
  );
}

function SourceItem({ source, isWeak }: { source: TaxSource; isWeak: boolean }) {
  const confidence = SOURCE_CONFIDENCE[source.confidence] ?? {
    label: source.confidence,
    className: NEUTRAL_BADGE,
  };
  return (
    <div
      className={`rounded-md border p-3 text-sm ${
        isWeak ? "border-amber-200 bg-amber-50/50" : "border-border bg-card"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">{source.title}</span>
        <Badge className={confidence.className} variant="outline">
          {confidence.label}
        </Badge>
      </div>
      <div className="mt-1 text-xs text-muted-foreground">
        {source.system} · {SOURCE_METHOD_LABELS[source.method] ?? source.method}
      </div>
      {isWeak ? <p className="mt-1.5 text-xs leading-5 text-amber-900">{source.note}</p> : null}
      {isWeak && source.target ? (
        <p className="mt-1 flex items-start gap-1.5 text-xs leading-5 text-muted-foreground">
          <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
          Куда движемся: {source.target}
        </p>
      ) : null}
    </div>
  );
}

// ── страница ───────────────────────────────────────────────────────────────────

/** Счётчик на корешке вкладки: сколько там ждёт человека. Ноль не рисуем. */
function TabCount({ value, tone }: { value: number; tone: "danger" | "warning" }) {
  if (value <= 0) {
    return null;
  }
  const className =
    tone === "danger" ? "bg-rose-100 text-rose-800" : "bg-amber-100 text-amber-800";
  return <span className={`ml-1.5 rounded-full px-1.5 text-xs ${className}`}>{value}</span>;
}

export function TaxesRoute() {
  const permissions = usePermissions();
  const queryClient = useQueryClient();
  const canManage = permissions.hasPermission("accounting.taxes.manage");

  const [tab, setTab] = useState<TaxesTab>("summary");
  // Срез расчёта. По умолчанию — сегодня по ЛОКАЛЬНОЙ зоне: `toISOString().slice(0, 10)`
  // ночью по МСК дал бы вчерашний день, а с ним и другой отчётный период.
  const [asOf, setAsOf] = useState(todayIso());
  const year = Number(asOf.slice(0, 4)) || new Date().getFullYear();

  // Ключи и функции здесь ДОСЛОВНО те же, что во вкладках, — react-query отдаёт один кэш,
  // так что счётчики не стоят лишних запросов. Нужны они потому, что просроченный срок и
  // документ «на проверку» — тоже сигналы к действию, а из «Сводки» их не видно.
  const overviewQuery = useQuery({
    queryKey: ["taxes", "overview", asOf],
    queryFn: () => getTaxOverview(asOf),
  });
  const calendarQuery = useQuery({
    queryKey: ["taxes", "calendar", year],
    queryFn: () => getTaxCalendar(year),
  });
  const documentsQuery = useQuery({
    queryKey: ["taxes", "documents"],
    queryFn: () => getTaxDocuments(),
  });

  const alertCount = overviewQuery.data?.alert_count ?? 0;
  const overdueCount = calendarQuery.data?.overdue_count ?? 0;
  const reviewCount = documentsQuery.data?.status_counts?.needs_review ?? 0;

  return (
    <div className="space-y-5">
      <PageHeader
        action={
          <div className="flex items-center gap-2">
            <label className="text-sm text-muted-foreground" htmlFor="taxes-as-of">
              Срез на
            </label>
            <Input
              className="w-[165px]"
              id="taxes-as-of"
              onChange={(event) => setAsOf(event.target.value || todayIso())}
              type="date"
              value={asOf}
            />
            <Button
              onClick={() => void queryClient.invalidateQueries({ queryKey: ["taxes"] })}
              variant="outline"
            >
              <RefreshCw aria-hidden="true" />
              Обновить
            </Button>
          </div>
        }
        description="УСН «Доходы» 6% нарастающим итогом: сверка расчёта с платёжками бухгалтера и фактом из банка, сроки уплаты, реестр платежей и документы."
        title="Налоги"
      />

      <Tabs onValueChange={(value) => setTab(value as TaxesTab)} value={tab}>
        <TabsList>
          <TabsTrigger value="summary">
            Сводка
            <TabCount tone="danger" value={alertCount} />
          </TabsTrigger>
          <TabsTrigger value="calendar">
            Календарь
            <TabCount tone="danger" value={overdueCount} />
          </TabsTrigger>
          <TabsTrigger value="payments">Платежи</TabsTrigger>
          <TabsTrigger value="documents">
            Документы
            <TabCount tone="warning" value={reviewCount} />
          </TabsTrigger>
          <TabsTrigger value="vat">НДС-льгота</TabsTrigger>
          <TabsTrigger value="sources">Источники</TabsTrigger>
        </TabsList>
      </Tabs>

      {tab === "summary" ? <SummaryTab asOf={asOf} /> : null}
      {tab === "calendar" ? <CalendarTab year={year} /> : null}
      {tab === "payments" ? <PaymentsTab year={year} /> : null}
      {tab === "documents" ? <DocumentsTab canManage={canManage} /> : null}
      {tab === "vat" ? <VatTab canManage={canManage} year={year} /> : null}
      {tab === "sources" ? <SourcesBlock /> : null}
    </div>
  );
}
