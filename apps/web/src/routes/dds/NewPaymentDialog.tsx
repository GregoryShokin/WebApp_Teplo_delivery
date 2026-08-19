import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeftRight,
  Banknote,
  Building2,
  Clock,
  FileText,
  HandCoins,
  Landmark,
  LoaderCircle,
  MousePointerClick,
  Plus,
  Receipt,
  Search,
  Trash2,
  User,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { InlineOptionList, type ComboboxOption } from "@/components/ui/combobox";
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
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import { CounterpartyCombobox } from "@/components/ui-app/CounterpartyCombobox";
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
  createEmployeePayout,
  createExpenseCashReserves,
  createInternalTransfer,
  createNewPaymentExpenseDraft,
  createNewPaymentIncome,
  createNewPaymentInternalTransfer,
  createPayrollAdvance,
  getNewPaymentContext,
  getOnDemandEmployees,
  getPayrollAdvanceAvailability,
  type NewPaymentArticle,
  type NewPaymentCounterparty,
  type NewPaymentEmployee,
  type LocationOption,
  type LocationLeaseOption,
  type NewPaymentExpenseLine,
  type NewPaymentWallet,
  type AssetOption,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";
import {
  createBankPrepaymentDraft,
  createPrepayment,
  getRegistry,
} from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";
import { AssetPicker, assetTitle } from "@/routes/dds/AssetPicker";
import {
  ASSETS_FORBIDDEN_HINT,
  LOCATIONS_FORBIDDEN_HINT,
  assetOptionsQuery,
  formatDdsMoney,
  locationOptionsQuery,
} from "@/routes/dds/shared";
import { VatRateField } from "@/routes/dds/VatRateField";

/**
 * Окно «Новый платёж» — единая точка создания всех исходящих денег («статья решает всё»):
 * слева палитра операций с поиском (статьи + операции сотрудникам + перевод), справа
 * форма выбранной операции.
 *
 * UX-паттерн форм — «живое резюме»: счёт выбирается чипами (SourcePicker, кластеры
 * банк/наличные, собираются из контекста — новые кошельки подхватываются сами),
 * действие наличных — сегмент-контролом, а единственное место объяснения режима —
 * цветная панель «Что произойдёт» (SummaryPanel) с итогом, которая собирается живьём.
 * Статические описания, хвосты опций и хинт-боксы удалены сознательно — не возвращать.
 *
 * Маршрутизация — по flow статьи из контекста (services/new_payment.py). Внутренний
 * перевод фиксирован: банк → Сейф (черновиком), Сейф → Касса, Касса → Сейф.
 */

type OperationKind =
  | "expense"
  | "income"
  | "supplier_prepayment"
  | "employee_advance"
  | "employee_loan"
  | "employee_payout"
  | "transfer_plain";

/** Ключи учёта «в форме есть неотправленный ввод» (см. handleDone). */
type DirtyKind = "expense" | "income" | "prepayment" | "advance" | "payout" | "transfer";

const DIRTY_LABELS: Record<DirtyKind, string> = {
  expense: "строки расхода",
  income: "поступление",
  prepayment: "предоплата поставщику",
  advance: "аванс/заём",
  payout: "выплата долга по ЗП",
  transfer: "перевод",
};

const DIRTY_TO_MODE: Record<DirtyKind, OperationKind> = {
  expense: "expense",
  income: "income",
  prepayment: "supplier_prepayment",
  advance: "employee_advance",
  payout: "employee_payout",
  transfer: "transfer_plain",
};

/** Леджеры палитры: вид деятельности статьи (activity_type каталога ДДС). */
type LedgerKey = "operating" | "financing" | "investing";

const LEDGERS: Array<{ key: LedgerKey; label: string; title: string }> = [
  { key: "operating", label: "Опер.", title: "Операционная деятельность" },
  { key: "financing", label: "Фин.", title: "Финансовая деятельность" },
  { key: "investing", label: "Инвест.", title: "Инвестиционная деятельность" },
];

/** Сколько расходных статей видно в схлопнутой палитре — подобрано так, чтобы все
 *  группы влезали в окно без скролла. */
const EXPENSE_COLLAPSED_COUNT = 5;

// Снимок арендодателя выбранного договора: несёт реквизитный контур, чтобы окно выбрало канал
// платежа (банк по реквизитам / карта ИП → Сейф / наличные) той же логикой, что у контрагента,
// и месячную ставку — для мягкого предупреждения о переплате (не блокирует отправку).
type LeaseRecipient = {
  name: string;
  relationship: string;
  has_requisites: boolean;
  requisites_verified: boolean;
  monthlyAmount: number;
};

type ExpenseRow = {
  key: string;
  articleId: string;
  amount: string;
  purpose: string;
  counterpartyId: string; // «кому платим» — статьи с закреплёнными контрагентами
  servicePeriodStart: string;
  /** Сколько месяцев покрывает платёж: «1» — обычный, «3» — оплата за квартал вперёд. */
  servicePeriodMonths: string;
  /** Признавать расход помесячно самим — для контрагентов, которые закрывающих не присылают. */
  autoRecognizeMonthly: boolean;
  servicePeriodEnd: string;
  locationId: string; // помещение — для статей с location_required (аренда, коммуналка)
  leaseId: string; // договор аренды — подставляет арендодателя в counterpartyId
  // Основное средство — для статей с asset_link_kind («Покупка ОС», «Ремонт ОС»). Через это
  // окно покупку оборудования и заводят, поэтому объект спрашиваем здесь, пока рядом человек,
  // который знает, что купили: разбирать выписку будет другой и через неделю.
  assetId: string;
  leaseRecipient: LeaseRecipient | null; // арендодатель договора для реквизитной маршрутизации
};

function dateInput(year: number, month: number, day: number): string {
  return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/** Собрать даты периода из «месяц + сколько месяцев»: 2026-04 × 3 → 01.04.2026–30.06.2026.
 *
 *  Окно спрашивает месяцами (99 % таких контрагентов работают по абонентской плате), а на
 *  бэкенд период уходит датами — считаем их здесь, чтобы человек не собирал их руками и не
 *  ошибался на день в конце месяца.
 */
function monthsToPeriod(monthValue: string, months: string) {
  if (!monthValue) {
    return { servicePeriodStart: "", servicePeriodEnd: "", servicePeriodMonths: months };
  }
  const [year, month] = monthValue.split("-").map(Number);
  const count = Math.max(1, Number(months) || 1);
  const start = new Date(year, month - 1, 1);
  // Нулевой день следующего месяца = последний день предыдущего, без таблицы длин месяцев.
  const end = new Date(year, month - 1 + count, 0);
  return {
    servicePeriodStart: dateInput(start.getFullYear(), start.getMonth(), 1),
    servicePeriodEnd: dateInput(end.getFullYear(), end.getMonth(), end.getDate()),
    servicePeriodMonths: String(count),
  };
}

function formatPeriod(start: string, end: string) {
  const fmt = (value: string) => {
    const [year, month, day] = value.split("-");
    return `${day}.${month}.${year}`;
  };
  return `${fmt(start)} — ${fmt(end)}`;
}

/** Период при смене получателя всегда сбрасывается в пустой.
 *
 *  Подставлять его «по умолчанию» нельзя (правило владельца 01.08.2026): предзаполненный
 *  период не перечитывают — он выглядит как уже принятое решение. Ошибиться месяцем тут
 *  дороже, чем забыть: признание разложит расход не по тем месяцам, а увидят это только
 *  на сверке. Пустое поле, наоборот, само себя требует — платёж не отправить. */
function emptyServicePeriod() {
  return { servicePeriodStart: "", servicePeriodEnd: "", servicePeriodMonths: "1" };
}

const MONTH_NAMES = [
  "январь",
  "февраль",
  "март",
  "апрель",
  "май",
  "июнь",
  "июль",
  "август",
  "сентябрь",
  "октябрь",
  "ноябрь",
  "декабрь",
];

/** «2026-07» → «июль 2026»: подсказка читается словами, а не датой в формате input. */
function monthLabel(value: string): string {
  const [year, month] = value.split("-");
  return `${MONTH_NAMES[Number(month) - 1] ?? value} ${year}`;
}

/** Месяцы для выбора: год назад — год вперёд, новые сверху.
 *
 *  Раньше здесь стоял ``<input type="month">``. В Chrome это выпадающий календарь, а Safari
 *  на macOS такой тип НЕ ПОДДЕРЖИВАЕТ и рисует обычное текстовое поле — человек печатает
 *  «08.2026», как привык, а парсер ждёт «2026-08» и молча выдаёт NaN. Так на проде не
 *  оказалось периода НИ У ОДНОЙ строки платежа: люди его вводили, а он не сохранялся.
 *  Список вместо ввода снимает вопрос формата вовсе — ошибиться нечем.
 *
 *  Диапазон ±12 месяцев: платят и за прошлый месяц (услуга оказана), и вперёд за год
 *  (годовая лицензия). Дальше этого не встречалось. */
function monthChoices(): string[] {
    const now = new Date();
    const out: string[] = [];
    for (let offset = 12; offset >= -12; offset -= 1) {
        const d = new Date(now.getFullYear(), now.getMonth() + offset, 1);
        out.push(dateInput(d.getFullYear(), d.getMonth(), 1).slice(0, 7));
    }
    return out;
}

/** Месяц из настройки карточки (``default_service_period_offset_months``): 0 — текущий,
 *  −1 — прошлый. Не подставляется сам, а предлагается кнопкой в окне периода. */
function offsetMonthValue(offsetMonths: number | null | undefined): string | null {
  if (offsetMonths == null) return null;
  const now = new Date();
  const first = new Date(now.getFullYear(), now.getMonth() + offsetMonths, 1);
  return dateInput(first.getFullYear(), first.getMonth(), 1).slice(0, 7);
}

function normalizeAmount(value: string): string {
  return value.trim().replace(",", ".");
}

/** Строка суммы для payload: trim, запятая→точка, все пробелы (включая NBSP) вырезаны.
 *  Валидация и payload обязаны использовать одну и ту же нормализацию. */
function amountStr(value: string): string {
  return normalizeAmount(value).replace(/\s/g, "");
}

function amountOf(value: string): number {
  return Number(amountStr(value));
}

/** Русская плюрализация: plural(2, ["строка", "строки", "строк"]) → «строки». */
function plural(n: number, forms: [string, string, string]): string {
  const abs = Math.abs(Math.trunc(n)) % 100;
  const d = abs % 10;
  if (abs > 10 && abs < 20) return forms[2];
  if (d === 1) return forms[0];
  if (d >= 2 && d <= 4) return forms[1];
  return forms[2];
}

/** Обрезка длинных имён (контрагентов/сотрудников) для фраз панели. */
function shortName(value: string, max = 24): string {
  const trimmed = value.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

function todayInput(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

export function NewPaymentDialog({
  open,
  onOpenChange,
  presetArticleCode = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Пресет вызывающей стороны: код статьи, с которой открыть окно. */
  presetArticleCode?: string | null;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  // «Создать платёж» (сразу) и «Передать …» двигают живые деньги — уровень права
  // подтверждения оплат, как у выдачи резерва Сейфа.
  const canConfirmPaid = permissions.hasPermission("finance.safe.confirm_paid");
  // Резервы (плановые платежи) — право ручного резерва Сейфа.
  const canReserveCash = permissions.hasPermission("finance.safe.allocate");

  const [mode, setMode] = useState<OperationKind | null>(null);
  // Чем выбираем платёж: статьёй («расход по SEO») или получателем («платёж Наумченко»).
  // Это два разных вопроса в голове человека, а не два фильтра одного списка: во втором
  // случае статью не выбирают вовсе — она приходит из карточки контрагента.
  const [pickBy, setPickBy] = useState<"article" | "counterparty">("article");
  const [search, setSearch] = useState("");
  const [ledger, setLedger] = useState<LedgerKey>("operating");
  // Ключ сессии окна: на каждое открытие формы пересоздаются с чистым состоянием.
  const [sessionKey, setSessionKey] = useState(0);
  // Группа «Расходы» схлопнута — раскрывается кнопкой «Ещё…» или поиском.
  const [expenseExpanded, setExpenseExpanded] = useState(false);
  // Эпоха формы: бамп пересоздаёт отправленную форму, когда окно остаётся открытым.
  const [formEpoch, setFormEpoch] = useState<Partial<Record<DirtyKind, number>>>({});
  // Реестр «в форме есть неотправленный ввод» — гард от молчаливой потери при закрытии
  // окна после успешной отправки другой операции.
  const dirtyRef = useRef<Partial<Record<DirtyKind, boolean>>>({});

  const contextQuery = useQuery({
    queryKey: ["new-payment", "context"],
    queryFn: getNewPaymentContext,
    enabled: open,
  });
  const articles = useMemo(() => contextQuery.data?.articles ?? [], [contextQuery.data]);
  const counterparties = useMemo(
    () => contextQuery.data?.counterparties ?? [],
    [contextQuery.data],
  );
  const wallets = useMemo(() => contextQuery.data?.wallets ?? [], [contextQuery.data]);
  const employees = useMemo(() => contextQuery.data?.employees ?? [], [contextQuery.data]);

  const expenseArticles = useMemo(
    () => articles.filter((item) => item.flow === "expense"),
    [articles],
  );
  const incomeArticles = useMemo(
    () => articles.filter((item) => item.flow === "income"),
    [articles],
  );
  const prepaymentArticle = articles.find((item) => item.flow === "supplier_prepayment") ?? null;
  const advanceArticle = articles.find((item) => item.flow === "employee_advance") ?? null;
  const loanArticle = articles.find((item) => item.flow === "employee_loan") ?? null;
  const payoutArticles = useMemo(
    () => articles.filter((item) => item.flow === "employee_payout"),
    [articles],
  );
  const transferArticle = articles.find((item) => item.flow === "internal_transfer") ?? null;
  const kassaWallet =
    wallets.find((wallet) => wallet.kind === "cash" && wallet.location === "kassa") ?? null;

  // Статья поступления живёт в родителе — палитра выбирает её напрямую.
  const [incomeArticleId, setIncomeArticleId] = useState("");

  // --- Строки расхода живут в родителе: палитра добавляет статьи прямо в форму ---
  const rowSeq = useRef(0);
  const nextKey = () => {
    rowSeq.current += 1;
    return `row-${rowSeq.current}`;
  };
  const [expenseRows, setExpenseRows] = useState<ExpenseRow[]>([]);

  function emptyExpenseRow(articleId = "", counterpartyId = ""): ExpenseRow {
    return {
      key: nextKey(),
      articleId,
      amount: "",
      purpose: "",
      counterpartyId,
      servicePeriodStart: "",
      servicePeriodMonths: "1",
      autoRecognizeMonthly: false,
      servicePeriodEnd: "",
      locationId: "",
      leaseId: "",
      assetId: "",
      leaseRecipient: null,
    };
  }
  function updateExpenseRow(key: string, patch: Partial<ExpenseRow>) {
    setExpenseRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }
  function presetCounterparty(article: NewPaymentArticle | null): string {
    // На арендной статье получатель — только арендодатель из договора, свободного/закреплённого
    // контрагента тут нет: подставлять нечего.
    if (article?.lease_bound) return "";
    return article && (article.counterparties?.length ?? 0) === 1
      ? article.counterparties![0].counterparty_id
      : "";
  }
  function changeExpenseArticle(key: string, articleId: string) {
    const article = expenseArticles.find((item) => item.id === articleId) ?? null;
    // Смена статьи сбрасывает доп-данные строки — они относились к прежней статье.
    updateExpenseRow(key, {
      articleId,
      counterpartyId: presetCounterparty(article),
      purpose: "",
      locationId: "",
      leaseId: "",
      assetId: "",
      leaseRecipient: null,
      ...emptyServicePeriod(),
    });
  }

  /** Клик по контрагенту в палитре: платёж «от получателя», а не от статьи.
   *
   *  Так платёж и заводят на практике — «надо заплатить Наумченко», а не «надо провести
   *  расход по статье ФД». Статья подставляется из карточки контрагента; если её там нет,
   *  строка остаётся без статьи и форма прямо просит её выбрать. */
  function selectCounterparty(counterparty: NewPaymentCounterparty) {
    setMode("expense");
    const articleId =
      counterparty.default_dds_article_id &&
      expenseArticles.some((item) => item.id === counterparty.default_dds_article_id)
        ? counterparty.default_dds_article_id
        : "";
    setExpenseRows((prev) => {
      if (prev.some((row) => row.counterpartyId === counterparty.counterparty_id)) {
        return prev;
      }
      const patch = {
        articleId,
        counterpartyId: counterparty.counterparty_id,
        ...emptyServicePeriod(),
      };
      const emptyIndex = prev.findIndex((row) => !row.articleId && !row.counterpartyId);
      if (emptyIndex >= 0) {
        return prev.map((row, index) => (index === emptyIndex ? { ...row, ...patch } : row));
      }
      return [...prev, { ...emptyExpenseRow(), ...patch }];
    });
  }

  /** Клик по статье в палитре: расходная — заполняет пустую строку или добавляет новую
   *  (уже выбранная статья не дублируется); статья-маршрут — переключает операцию. */
  function selectArticle(article: NewPaymentArticle) {
    if (article.flow === "expense") {
      setMode("expense");
      setExpenseRows((prev) => {
        if (prev.some((row) => row.articleId === article.id)) {
          return prev;
        }
        const emptyIndex = prev.findIndex((row) => !row.articleId);
        if (emptyIndex >= 0) {
          return prev.map((row, index) =>
            index === emptyIndex
              ? {
                  ...row,
                  articleId: article.id,
                  counterpartyId: presetCounterparty(article),
                  ...emptyServicePeriod(),
                }
              : row,
          );
        }
        return [...prev, emptyExpenseRow(article.id, presetCounterparty(article))];
      });
      return;
    }
    if (article.flow === "income") {
      setMode("income");
      setIncomeArticleId(article.id);
      return;
    }
    const modeByFlow: Partial<Record<NewPaymentArticle["flow"], OperationKind>> = {
      supplier_prepayment: "supplier_prepayment",
      employee_advance: "employee_advance",
      employee_loan: "employee_loan",
      employee_payout: "employee_payout",
      internal_transfer: "transfer_plain",
    };
    const next = modeByFlow[article.flow];
    if (next) {
      setMode(next);
    }
  }

  // Сброс на каждое открытие — синхронно в рендере (React перерендерит до коммита):
  // без вспышки состояния прошлой сессии и без двойного mount форм.
  const [prevOpen, setPrevOpen] = useState(false);
  if (open !== prevOpen) {
    setPrevOpen(open);
    if (open) {
      setMode(null);
      setSearch("");
      setPickBy("article");
      setLedger("operating");
      setIncomeArticleId("");
      rowSeq.current = 0;
      setExpenseRows([emptyExpenseRow()]);
      setSessionKey((key) => key + 1);
      setExpenseExpanded(false);
      setFormEpoch({});
      dirtyRef.current = {};
    }
  }

  // Пресет статьи — пока пользователь ничего не выбрал сам.
  useEffect(() => {
    if (!open || !presetArticleCode || mode !== null || articles.length === 0) {
      return;
    }
    const preset = articles.find((item) => item.code === presetArticleCode);
    if (preset) {
      selectArticle(preset);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, presetArticleCode, articles, mode]);

  async function invalidateAll() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds"] }),
      queryClient.invalidateQueries({ queryKey: ["cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["cp"] }),
      queryClient.invalidateQueries({ queryKey: ["payroll"] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-advances"] }),
      queryClient.invalidateQueries({ queryKey: ["new-payment"] }),
      queryClient.invalidateQueries({ queryKey: ["finance-payments"] }),
    ]);
  }

  const setDirty = (kind: DirtyKind, value: boolean) => {
    dirtyRef.current[kind] = value;
  };
  const expenseDirty = () => expenseRows.some((row) => row.articleId && amountOf(row.amount) > 0);

  /** После успешной отправки: закрыть окно, если в других формах нет неотправленного
   *  ввода; иначе — остаться, пересоздать отправленную форму и показать, что осталось. */
  async function handleDone(kind: DirtyKind) {
    dirtyRef.current[kind] = false;
    await invalidateAll();
    const others = (Object.keys(DIRTY_LABELS) as DirtyKind[]).filter((key) =>
      key === kind ? false : key === "expense" ? expenseDirty() : Boolean(dirtyRef.current[key]),
    );
    if (others.length === 0) {
      onOpenChange(false);
      return;
    }
    if (kind === "expense") {
      rowSeq.current = 0;
      setExpenseRows([emptyExpenseRow()]);
    }
    // Пересоздаём отправленную форму (включая расход: его счёт и действие тоже
    // должны вернуться к безопасным дефолтам, а не залипать на «Оплатить сразу»).
    setFormEpoch((prev) => ({ ...prev, [kind]: (prev[kind] ?? 0) + 1 }));
    setMode(DIRTY_TO_MODE[others[0]]);
    toast.info(
      `Создано. В окне остался неотправленный ввод: ${others
        .map((key) => DIRTY_LABELS[key])
        .join(", ")} — отправьте или закройте окно.`,
    );
  }
  const close = () => onOpenChange(false);

  // --- Палитра: группы, леджер-фильтр, схлопывание «Расходов», поиск ---
  const q = search.trim().toLowerCase();
  const matches = (label: string) => !q || label.toLowerCase().includes(q);
  // Поиск ищет по всем леджерам; без поиска статьи фильтруются активным леджером.
  // Виды деятельности вне трёх чипов (technical/internal) показываем в «Опер.» —
  // иначе такая статья была бы недостижима ничем, кроме поиска.
  const articleLedger = (item: NewPaymentArticle): LedgerKey =>
    LEDGERS.some((entry) => entry.key === item.activity)
      ? (item.activity as LedgerKey)
      : "operating";
  const inLedger = (item: NewPaymentArticle) => Boolean(q) || articleLedger(item) === ledger;

  const usedArticleIds = new Set(expenseRows.map((row) => row.articleId).filter(Boolean));
  // Уже выбранные статьи видимы всегда — сквозь леджер и схлопывание.
  const matchedExpense = expenseArticles.filter(
    (item) => matches(item.name) && (inLedger(item) || usedArticleIds.has(item.id)),
  );
  const matchedIncome = incomeArticles.filter(
    (item) => matches(item.name) && (inLedger(item) || item.id === incomeArticleId),
  );
  // Без поиска и раскрытия — первые N статей + статьи, уже выбранные в строках.
  const visibleExpense =
    q || expenseExpanded
      ? matchedExpense
      : matchedExpense.filter(
          (item, index) => index < EXPENSE_COLLAPSED_COUNT || usedArticleIds.has(item.id),
        );
  const hiddenExpenseCount = matchedExpense.length - visibleExpense.length;

  const showPrepayment =
    prepaymentArticle !== null && matches(prepaymentArticle.name) && inLedger(prepaymentArticle);
  const advanceLabel = "Аванс сотруднику";
  const loanLabel = "Заём сотруднику";
  const payoutLabel = "Долг по ЗП";
  const transferLabel = transferArticle?.name ?? "Внутренний перевод";
  const showAdvance = advanceArticle !== null && matches(advanceLabel);
  const showLoan = loanArticle !== null && matches(loanLabel);
  const showPayout = payoutArticles.length > 0 && matches(payoutLabel);
  const showTransfer = transferArticle !== null && matches(transferLabel);

  // Список контрагентов в режиме «Контрагенты»: весь справочник, фильтр — по имени и ИНН
  // (у контрагентов бывают тёзки, «ИП Иванов» без ИНН не различить).
  const matchedCounterparties = counterparties.filter(
    (item) => !q || item.name.toLowerCase().includes(q) || (item.inn ?? "").includes(q),
  );

  const expenseGroupVisible = visibleExpense.length > 0 || showPrepayment;
  const incomeGroupVisible = matchedIncome.length > 0;
  const employeeGroupVisible = showAdvance || showLoan || showPayout;
  const nothingFound =
    pickBy === "counterparty"
      ? matchedCounterparties.length === 0
      : !expenseGroupVisible && !incomeGroupVisible && !employeeGroupVisible && !showTransfer;

  const context = contextQuery.data ?? null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[640px] max-h-[88vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="shrink-0 space-y-0 border-b py-4 pl-6 pr-14">
          <DialogTitle>Новый платёж</DialogTitle>
          {/* Обычная подпись «Выберите операцию…» дублирует пустое состояние справа —
              визуально скрыта, a11y-описание остаётся. */}
          <DialogDescription className="mt-0.5 sr-only">
            Выберите операцию — форма подстроится.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1">
          {/* Палитра операций */}
          <aside className="flex w-52 shrink-0 flex-col border-r sm:w-60">
            <div className="shrink-0 p-2.5 pb-1">
              {/* Чем выбираем платёж. Переключатель, а не общий поиск: «заплатить по статье»
                  и «заплатить контрагенту» — разные вопросы, и во втором статью не выбирают. */}
              <div className="flex items-center gap-0.5 rounded-md bg-muted p-0.5">
                {(
                  [
                    { key: "article", label: "По статье" },
                    { key: "counterparty", label: "Контрагенту" },
                  ] as const
                ).map((option) => (
                  <button
                    key={option.key}
                    type="button"
                    aria-pressed={pickBy === option.key}
                    onClick={() => {
                      setPickBy(option.key);
                      setSearch("");
                    }}
                    className={cn(
                      "flex-1 rounded px-2 py-1 text-xs transition-colors",
                      pickBy === option.key
                        ? "border bg-background font-medium shadow-sm"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <div className="relative mt-1.5">
                <Search
                  className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
                  size={14}
                />
                <Input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder={
                    pickBy === "counterparty" ? "Название или ИНН…" : "Статья или операция…"
                  }
                  className="h-8 pl-8 text-sm"
                />
              </div>
              {pickBy === "article" ? (
                <div className="mt-1.5 flex gap-1">
                  {LEDGERS.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      title={item.title}
                      onClick={() => setLedger(item.key)}
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[11px] transition-colors",
                        ledger === item.key && !q
                          ? "border-primary/40 bg-primary/10 font-medium text-primary"
                          : "border-input text-muted-foreground hover:bg-muted",
                        q && "opacity-50",
                      )}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-3">
              {contextQuery.isLoading ? (
                <div className="flex items-center gap-2 px-2 py-4 text-sm text-muted-foreground">
                  <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
                  Загрузка…
                </div>
              ) : contextQuery.isError ? (
                <div className="space-y-2 px-2 py-4">
                  <div className="text-sm text-muted-foreground">
                    Не удалось загрузить операции.
                  </div>
                  <Button size="sm" variant="outline" onClick={() => contextQuery.refetch()}>
                    Повторить
                  </Button>
                </div>
              ) : nothingFound ? (
                <div className="px-2 py-4 text-sm text-muted-foreground">Ничего не найдено</div>
              ) : pickBy === "counterparty" ? (
                <PaletteGroup title="Кому платим">
                  {matchedCounterparties.map((counterparty) => (
                    <PaletteItem
                      key={counterparty.counterparty_id}
                      icon={Building2}
                      label={counterparty.name}
                      title={
                        counterparty.default_dds_article_id
                          ? `${counterparty.name} — статья подставится из карточки`
                          : `${counterparty.name} — статью нужно будет выбрать`
                      }
                      active={
                        mode === "expense" &&
                        expenseRows.some(
                          (row) => row.counterpartyId === counterparty.counterparty_id,
                        )
                      }
                      onClick={() => selectCounterparty(counterparty)}
                    />
                  ))}
                </PaletteGroup>
              ) : (
                <>
                  {expenseGroupVisible ? (
                    <PaletteGroup title="Расходы">
                      {visibleExpense.map((article) => (
                        <PaletteItem
                          key={article.id}
                          icon={Receipt}
                          label={article.name}
                          active={
                            mode === "expense" &&
                            expenseRows.some((row) => row.articleId === article.id)
                          }
                          onClick={() => selectArticle(article)}
                        />
                      ))}
                      {hiddenExpenseCount > 0 ? (
                        <button
                          type="button"
                          onClick={() => setExpenseExpanded(true)}
                          className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-muted"
                        >
                          <Plus size={15} className="shrink-0" aria-hidden="true" />
                          Ещё {hiddenExpenseCount} статей…
                        </button>
                      ) : null}
                      {showPrepayment && prepaymentArticle ? (
                        <PaletteItem
                          icon={Building2}
                          label={prepaymentArticle.name}
                          active={mode === "supplier_prepayment"}
                          onClick={() => selectArticle(prepaymentArticle)}
                        />
                      ) : null}
                    </PaletteGroup>
                  ) : null}
                  {incomeGroupVisible ? (
                    <PaletteGroup title="Поступления">
                      {matchedIncome.map((article) => (
                        <PaletteItem
                          key={article.id}
                          icon={Banknote}
                          label={article.name}
                          active={mode === "income" && incomeArticleId === article.id}
                          onClick={() => selectArticle(article)}
                        />
                      ))}
                    </PaletteGroup>
                  ) : null}
                  {employeeGroupVisible ? (
                    <PaletteGroup title="Сотрудникам">
                      {showAdvance && advanceArticle ? (
                        <PaletteItem
                          icon={HandCoins}
                          label={advanceLabel}
                          active={mode === "employee_advance"}
                          onClick={() => selectArticle(advanceArticle)}
                        />
                      ) : null}
                      {showLoan && loanArticle ? (
                        <PaletteItem
                          icon={HandCoins}
                          label={loanLabel}
                          active={mode === "employee_loan"}
                          onClick={() => selectArticle(loanArticle)}
                        />
                      ) : null}
                      {showPayout ? (
                        <PaletteItem
                          icon={User}
                          label={payoutLabel}
                          title="Выплата долга по ЗП (оклад «по требованию»)"
                          active={mode === "employee_payout"}
                          onClick={() => setMode("employee_payout")}
                        />
                      ) : null}
                    </PaletteGroup>
                  ) : null}
                  {showTransfer && transferArticle ? (
                    <PaletteGroup title="Переводы">
                      <PaletteItem
                        icon={ArrowLeftRight}
                        label={transferLabel}
                        active={mode === "transfer_plain"}
                        onClick={() => selectArticle(transferArticle)}
                      />
                    </PaletteGroup>
                  ) : null}
                </>
              )}
            </div>
          </aside>

          {/* Форма выбранной операции. Формы смонтированы постоянно (скрыты классом) —
              состояние переживает переключение операций внутри одной сессии окна. */}
          <section className="min-h-0 flex-1 overflow-y-auto p-5">
            {mode === null ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                  <MousePointerClick className="text-muted-foreground" size={22} />
                </div>
                <div className="max-w-64 text-sm text-muted-foreground">
                  {pickBy === "counterparty"
                    ? "Выберите получателя слева — статья подставится из его карточки."
                    : "Выберите операцию слева: расходную статью, выплату сотруднику или перевод. Либо переключитесь на «Контрагенту» и платите получателю напрямую."}
                </div>
              </div>
            ) : null}
            {context ? (
              <>
                <div className={cn(mode === "expense" ? "" : "hidden")}>
                  <ExpenseForm
                    key={`expense-${sessionKey}-${formEpoch.expense ?? 0}`}
                    articles={expenseArticles}
                    counterparties={counterparties}
                    wallets={wallets}
                    kassaWallet={kassaWallet}
                    canConfirmPaid={canConfirmPaid}
                    rows={expenseRows}
                    onChangeArticle={changeExpenseArticle}
                    onUpdateRow={updateExpenseRow}
                    onAddRow={() => setExpenseRows((prev) => [...prev, emptyExpenseRow()])}
                    onRemoveRow={(key) =>
                      setExpenseRows((prev) =>
                        prev.length <= 1 ? prev : prev.filter((row) => row.key !== key),
                      )
                    }
                    onDone={() => handleDone("expense")}
                    onCancel={close}
                  />
                </div>
                {incomeArticles.length > 0 ? (
                  <div className={cn(mode === "income" ? "" : "hidden")}>
                    <IncomeForm
                      active={mode === "income"}
                      key={`income-${sessionKey}-${formEpoch.income ?? 0}`}
                      articles={incomeArticles}
                      wallets={wallets}
                      articleId={incomeArticleId}
                      onArticleChange={setIncomeArticleId}
                      onDirty={(value) => setDirty("income", value)}
                      onDone={() => handleDone("income")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {prepaymentArticle ? (
                  <div className={cn(mode === "supplier_prepayment" ? "" : "hidden")}>
                    <PrepaymentForm
                      active={mode === "supplier_prepayment"}
                      key={`prepayment-${sessionKey}-${formEpoch.prepayment ?? 0}`}
                      article={prepaymentArticle}
                      wallets={wallets}
                      canConfirmPaid={canConfirmPaid}
                      canReserveCash={canReserveCash}
                      onDirty={(value) => setDirty("prepayment", value)}
                      onDone={() => handleDone("prepayment")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {advanceArticle || loanArticle ? (
                  <div
                    className={cn(
                      mode === "employee_advance" || mode === "employee_loan" ? "" : "hidden",
                    )}
                  >
                    <AdvanceForm
                      active={mode === "employee_advance" || mode === "employee_loan"}
                      key={`advance-${sessionKey}-${formEpoch.advance ?? 0}`}
                      kind={mode === "employee_loan" ? "loan" : "advance"}
                      canLoan={loanArticle !== null}
                      onKindChange={(kind) =>
                        setMode(kind === "loan" ? "employee_loan" : "employee_advance")
                      }
                      wallets={wallets}
                      employees={employees}
                      onDirty={(value) => setDirty("advance", value)}
                      onDone={() => handleDone("advance")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {payoutArticles.length > 0 ? (
                  <div className={cn(mode === "employee_payout" ? "" : "hidden")}>
                    <PayoutDebtForm
                      active={mode === "employee_payout"}
                      key={`payout-${sessionKey}-${formEpoch.payout ?? 0}`}
                      articles={payoutArticles}
                      wallets={wallets}
                      employees={employees}
                      invalidate={invalidateAll}
                      onDirty={(value) => setDirty("payout", value)}
                      onClose={close}
                    />
                  </div>
                ) : null}
                {transferArticle ? (
                  <div className={cn(mode === "transfer_plain" ? "" : "hidden")}>
                    <TransferPlainForm
                      key={`transfer-${sessionKey}-${formEpoch.transfer ?? 0}`}
                      wallets={wallets}
                      onDirty={(value) => setDirty("transfer", value)}
                      onDone={() => handleDone("transfer")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
              </>
            ) : null}
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// --------------------------------------------------------------------------- //
// Палитра

function PaletteGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-1">
      <div className="px-2.5 pb-1 pt-2.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function PaletteItem({
  icon: Icon,
  label,
  title,
  active,
  onClick,
}: {
  icon: LucideIcon;
  label: string;
  title?: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title ?? label}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors",
        active ? "bg-primary/10 font-medium text-primary" : "hover:bg-muted",
      )}
    >
      <Icon
        size={15}
        className={cn("shrink-0", active ? "text-primary" : "text-muted-foreground")}
        aria-hidden="true"
      />
      <span className="line-clamp-1">{label}</span>
    </button>
  );
}

// --------------------------------------------------------------------------- //
// Общие блоки форм («живое резюме»)

function FormHeader({ title, description }: { title: string; description?: string }) {
  return (
    <div className="mb-4">
      <h3 className="text-base font-semibold">{title}</h3>
      {description ? <p className="mt-0.5 text-sm text-muted-foreground">{description}</p> : null}
    </div>
  );
}

function FormFooter({
  cancel,
  cancelLabel = "Отмена",
  submit,
  submitLabel,
  disabled,
  pending,
}: {
  cancel: () => void;
  cancelLabel?: string;
  submit: () => void;
  submitLabel: string;
  disabled: boolean;
  pending: boolean;
}) {
  return (
    <div className="mt-4 flex justify-end gap-2 border-t pt-3.5">
      <Button onClick={cancel} type="button" variant="outline">
        {cancelLabel}
      </Button>
      <Button disabled={disabled || pending} onClick={submit} type="button">
        {pending ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : null}
        {submitLabel}
      </Button>
    </div>
  );
}

/** Короткое имя счёта для чипов и фраз панели; полное имя — в title. */
function shortWalletName(wallet: NewPaymentWallet): string {
  if (wallet.kind === "cash") {
    return wallet.location === "kassa" ? "Касса ТК" : "Сейф";
  }
  if (wallet.bank_code === "tbank") return "Т-Банк";
  if (wallet.bank_code === "sber") return "Сбер";
  return shortName(wallet.name, 14);
}

/**
 * Выбор счёта чипами: кластеры «банк» и «наличные» собираются из контекста окна —
 * новые кошельки подхватываются автоматически. Недоступные — disabled с причиной
 * в title. При разрастании списка (>5) чипы складываются в обычный селект.
 */
function SourcePicker({
  label,
  wallets,
  value,
  onChange,
  disabledReason,
}: {
  label: string;
  wallets: NewPaymentWallet[];
  value: string;
  onChange: (id: string) => void;
  disabledReason?: (wallet: NewPaymentWallet) => string | null;
}) {
  // Т-Банк первым (он дефолт черновиков), затем Сбер, затем прочие банки.
  const bankOrder = (wallet: NewPaymentWallet) =>
    wallet.bank_code === "tbank" ? 0 : wallet.bank_code === "sber" ? 1 : 2;
  const banks = wallets
    .filter((wallet) => wallet.kind === "bank")
    .sort((a, b) => bankOrder(a) - bankOrder(b));
  const cash = wallets.filter((wallet) => wallet.kind === "cash");

  if (wallets.length > 5) {
    return (
      <Label className="block space-y-1">
        <span className="text-sm">{label}</span>
        <Select onValueChange={onChange} value={value}>
          <SelectTrigger>
            <SelectValue placeholder="Выберите счёт" />
          </SelectTrigger>
          <SelectContent>
            {wallets.map((wallet) => {
              const reason = disabledReason?.(wallet) ?? null;
              return (
                <SelectItem disabled={reason !== null} key={wallet.id} value={wallet.id}>
                  {wallet.name}
                  {reason ? ` — ${reason}` : ""}
                </SelectItem>
              );
            })}
          </SelectContent>
        </Select>
      </Label>
    );
  }

  const chip = (wallet: NewPaymentWallet) => {
    const reason = disabledReason?.(wallet) ?? null;
    const active = wallet.id === value;
    const Icon = wallet.kind === "bank" ? Landmark : Banknote;
    return (
      <button
        key={wallet.id}
        type="button"
        disabled={reason !== null}
        aria-pressed={active}
        title={reason ?? wallet.name}
        onClick={() => onChange(wallet.id)}
        className={cn(
          "flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors",
          active
            ? "border-primary/40 bg-primary/10 font-medium text-primary"
            : "border-input hover:bg-muted",
          reason !== null && "cursor-not-allowed opacity-50 hover:bg-transparent",
        )}
      >
        <Icon size={13} aria-hidden="true" />
        {shortWalletName(wallet)}
      </button>
    );
  };

  return (
    <div>
      <span className="text-sm font-medium">{label}</span>
      <div className="mt-1.5 flex items-end gap-3">
        {banks.length > 0 ? (
          <div>
            <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">Банк</p>
            <div className="flex flex-wrap gap-1.5">{banks.map(chip)}</div>
          </div>
        ) : null}
        {banks.length > 0 && cash.length > 0 ? (
          <div className="mb-1 h-7 w-px shrink-0 bg-border" aria-hidden="true" />
        ) : null}
        {cash.length > 0 ? (
          <div>
            <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              Наличные
            </p>
            <div className="flex flex-wrap gap-1.5">{cash.map(chip)}</div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Сегмент выбора действия (для наличных источников с >1 доступным действием). */
function ActionSegment({
  options,
  value,
  onChange,
}: {
  options: Array<{ key: string; label: string }>;
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div>
      <span className="text-sm font-medium">Действие</span>
      <div className="mt-1.5 inline-flex items-center gap-0.5 rounded-md bg-muted p-0.5">
        {options.map((option) => (
          <button
            key={option.key}
            type="button"
            aria-pressed={value === option.key}
            onClick={() => onChange(option.key)}
            className={cn(
              "rounded px-3 py-1 text-sm transition-colors",
              value === option.key
                ? "border bg-background font-medium shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

type SummaryTone = "draft" | "reserve" | "instant" | "move" | "warning";

const SUMMARY_TONES: Record<SummaryTone, { box: string; icon: LucideIcon }> = {
  draft: { box: "border-sky-200 bg-sky-50 text-sky-800", icon: FileText },
  reserve: { box: "border-amber-200 bg-amber-50 text-amber-800", icon: Clock },
  instant: { box: "border-emerald-200 bg-emerald-50 text-emerald-800", icon: Zap },
  move: { box: "border-violet-200 bg-violet-50 text-violet-800", icon: ArrowLeftRight },
  warning: { box: "border-amber-300 bg-amber-50 text-amber-800", icon: AlertTriangle },
};

/**
 * «Что произойдёт» — единственное место объяснения режима операции: фраза собирается
 * из выбранного счёта/действия, цвет и иконка кодируют режим, справа живой итог.
 * Warning-состояния занимают эту же панель — отдельных жёлтых боксов в формах нет.
 */
function SummaryPanel({
  tone,
  total,
  children,
}: {
  tone: SummaryTone;
  total?: number | null;
  children: React.ReactNode;
}) {
  const style = SUMMARY_TONES[tone];
  const Icon = style.icon;
  return (
    <div
      className={cn("flex items-center gap-2.5 rounded-md border px-3 py-2.5 text-sm", style.box)}
    >
      <Icon size={16} className="shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1">{children}</span>
      {total != null ? (
        <span className={cn("shrink-0 font-semibold tabular-nums", total === 0 && "opacity-60")}>
          Итого {formatRub(total)}
        </span>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Расход: построчный конструктор (банк → один черновик-транш; наличные → резерв /
// платёж сразу / передача на другой наличный счёт)

function ExpenseForm({
  articles,
  counterparties,
  wallets,
  kassaWallet,
  canConfirmPaid,
  rows,
  onChangeArticle,
  onUpdateRow,
  onAddRow,
  onRemoveRow,
  onDone,
  onCancel,
}: {
  articles: NewPaymentArticle[];
  counterparties: NewPaymentCounterparty[];
  wallets: NewPaymentWallet[];
  kassaWallet: NewPaymentWallet | null;
  canConfirmPaid: boolean;
  rows: ExpenseRow[];
  onChangeArticle: (key: string, articleId: string) => void;
  onUpdateRow: (key: string, patch: Partial<ExpenseRow>) => void;
  onAddRow: () => void;
  onRemoveRow: (key: string) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [walletId, setWalletId] = useState("");
  const [act, setAct] = useState<"reserve" | "now" | "move">("reserve");
  // Ставка НДС платежа («22», «10», …) или «» — без НДС. По умолчанию без НДС: утверждение
  // о налоге делается осознанно, а не потому что галку забыли снять.
  const [vatRate, setVatRate] = useState("");
  // Какая строка сейчас правит период услуги (ключ строки, а не индекс — строки удаляют).
  const [periodRowKey, setPeriodRowKey] = useState<string | null>(null);
  const [officialViaSafeConsent, setOfficialViaSafeConsent] = useState<string | null>(null);
  const recipientSignature = rows
    .map((row) => `${row.key}:${row.articleId}:${row.counterpartyId}`)
    .join("|");
  // Подтверждение fallback относится только к текущему набору получателей.
  const allowOfficialViaSafe = officialViaSafeConsent === recipientSignature;
  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);

  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  const isCashSource = selectedWallet?.kind === "cash";
  const isSafeSource = isCashSource && selectedWallet?.location === "safe";
  const channel: "bank_draft" | "bank_draft_sber" =
    selectedWallet?.bank_code === "sber" ? "bank_draft_sber" : "bank_draft";

  const safeWallet = wallets.find((w) => w.kind === "cash" && w.location === "safe") ?? null;
  const transferDest = isSafeSource ? kassaWallet : safeWallet;
  const moveLabel = isSafeSource ? "Передать в кассу" : "Передать на Сейф";
  const actOptions = [
    { key: "reserve", label: "Резерв" },
    ...(canConfirmPaid ? [{ key: "now", label: "Оплатить сразу" }] : []),
    ...(canConfirmPaid && transferDest ? [{ key: "move", label: moveLabel }] : []),
  ];
  // Смена счёта/прав может сделать выбранное действие недоступным — откат на резерв.
  useEffect(() => {
    if (!actOptions.some((option) => option.key === act)) {
      setAct("reserve");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [walletId, canConfirmPaid]);

  const articleById = useMemo(() => {
    const map = new Map<string, NewPaymentArticle>();
    articles.forEach((item) => map.set(item.id, item));
    return map;
  }, [articles]);

  // Справочник получателей — общий, а не «закреплённые за статьёй». Маршрут платежа
  // (по реквизитам / на карту ИП → Сейф) считается по свойствам контрагента, и пока эти
  // свойства брались из списка статьи, свободно выбранный официальный контрагент выглядел
  // для формы как «получателя нет» — платёж ушёл бы мимо реквизитов.
  const counterpartyById = useMemo(() => {
    const map = new Map<string, NewPaymentCounterparty>();
    counterparties.forEach((item) => map.set(item.counterparty_id, item));
    return map;
  }, [counterparties]);

  // Реестр помещений закрыт правом source.locations.read: без него поле «Помещение» не заполнить
  // в принципе, и сводка «Укажите помещение» вводит в заблуждение — причина блокировки другая.
  // Ключ тот же, что у ExpenseLocationPicker, — react-query отдаёт общий кэш, второго запроса нет.
  const locationArticleIds = rows
    .filter((row) => articleById.get(row.articleId)?.location_required)
    .map((row) => row.articleId);
  const locationQueries = useQueries({
    queries: Array.from(new Set(locationArticleIds)).map((id) => locationOptionsQuery(id)),
  });
  const locationsForbidden = locationQueries.some((query) => apiErrorStatus(query.error) === 403);
  // Объекты грузим, только если в форме есть «объектная» строка: список один на всё окно, и
  // тянуть его на каждое открытие ради ничего незачем.
  const usesAssetArticle = rows.some((row) => articleById.get(row.articleId)?.asset_link_kind);
  const assetsQuery = useQuery(assetOptionsQuery(usesAssetArticle));
  const assetsForbidden = apiErrorStatus(assetsQuery.error) === 403;
  // Какая строка сейчас выбирает объект. Ключ строки, а не индекс: строки добавляют и удаляют,
  // и по индексу модалка после удаления соседа открылась бы на чужой строке.
  const [assetRowKey, setAssetRowKey] = useState<string | null>(null);
  const assetRow = assetRowKey ? (rows.find((item) => item.key === assetRowKey) ?? null) : null;
  const assetRowArticle = assetRow ? articleById.get(assetRow.articleId) : undefined;
  const periodRow = periodRowKey ? (rows.find((item) => item.key === periodRowKey) ?? null) : null;
  const periodCounterparty = periodRow ? counterpartyById.get(periodRow.counterpartyId) : undefined;

  /** Подпись строки-ссылки периода: что уже задано либо чего не хватает. */
  const periodSummary = (row: ExpenseRow, counterparty: NewPaymentCounterparty): string => {
    if (!row.servicePeriodStart || !row.servicePeriodEnd) {
      return counterparty.service_period_required
        ? "нужен период оказания услуги"
        : "Период услуги — если платим за месяц вперёд или за несколько";
    }
    const months = Number(row.servicePeriodMonths || "1");
    const perMonth =
      months > 1 && amountOf(row.amount) > 0
        ? ` · по ${formatRub(amountOf(row.amount) / months)} в месяц`
        : "";
    // Признание без первички называем прямо: расход попадёт в P&L, но не в налоговую базу.
    const selfBilled = row.autoRecognizeMonthly ? " · признаём сами" : "";
    return `Период: ${formatPeriod(row.servicePeriodStart, row.servicePeriodEnd)}${perMonth}${selfBilled}`;
  };

  /** Подпись строки-ссылки: что уже выбрано либо чего не хватает. */
  const assetSummary = (row: ExpenseRow): string => {
    const asset = (assetsQuery.data ?? []).find((item) => item.asset_id === row.assetId);
    if (asset) return `Объект: ${assetTitle(asset)}`;
    const kind = articleById.get(row.articleId)?.asset_link_kind;
    return kind === "purchase" ? "нужен объект основных средств" : "нужен объект — что ремонтируем";
  };

  const total = rows.reduce(
    (sum, row) => sum + (amountOf(row.amount) > 0 ? amountOf(row.amount) : 0),
    0,
  );
  // Описательная часть назначения — ровно как её соберёт бэк: одиночный платёж берёт
  // назначение строки (пустое → имя статьи), транш — сводку по строкам. Нужна только для
  // предпросмотра под полем НДС: человеку надо видеть строку, которую прочитает банк.
  const bankPurposeBase = (() => {
    const parts = rows.map(
      (row) => row.purpose.trim() || articleById.get(row.articleId)?.name || "",
    );
    if (parts.length === 0) return "";
    // Формулировка транша дословно повторяет бэк (`create_expense_payment_draft`) — иначе
    // предпросмотр обещал бы одно, а в банк ушло бы другое.
    return parts.length === 1 ? parts[0] : `Транш ${parts.length} платежей: ${parts.join("; ")}`;
  })();
  const selectedCounterparties = rows.flatMap((row) => {
    const article = articleById.get(row.articleId);
    // Арендная статья: получатель — арендодатель договора. Его реквизитный контур ведём через
    // тот же список, что и закреплённых контрагентов, чтобы маршрут (банк по реквизитам / карта
    // ИП → Сейф / наличные) выбрался уже готовой логикой ниже.
    if (article?.lease_bound) {
      const recipient = row.leaseRecipient;
      return recipient && row.counterpartyId
        ? [
            {
              counterparty_id: row.counterpartyId,
              name: recipient.name,
              inn: null,
              relationship: recipient.relationship as "official" | "informal" | "barter",
              has_requisites: recipient.has_requisites,
              requisites_verified: recipient.requisites_verified,
              service_period_required: false,
              default_service_period_offset_months: null,
              // Статья у арендного платежа своя (сама арендная), из карточки её не берут.
              default_dds_article_id: null,
              confirm_no_dds_article: false,
            },
          ]
        : [];
    }
    const counterparty = counterpartyById.get(row.counterpartyId);
    return counterparty ? [counterparty] : [];
  });
  const officialRecipient = selectedCounterparties.find((item) => item.relationship !== "informal");
  const requisitesRecipient = selectedCounterparties.find(
    (item) => item.relationship !== "informal" && item.has_requisites,
  );
  const missingRequisitesRecipient = selectedCounterparties.find(
    (item) => item.relationship !== "informal" && !item.has_requisites,
  );
  const missingServicePeriodRecipient = rows
    .map((row) => {
      const counterparty = counterpartyById.get(row.counterpartyId);
      return counterparty?.service_period_required &&
        (!row.servicePeriodStart || !row.servicePeriodEnd)
        ? counterparty
        : null;
    })
    .find(Boolean);
  // Вход «от контрагента»: получателя выбрали, статью из карточки подставить не удалось.
  // Молча отправить нельзя — расход попал бы в ДДС без статьи и выпал из всей аналитики.
  const missingArticleRow = rows.find((row) => row.counterpartyId && !row.articleId);
  const missingAssetRow = rows.find((row) => {
    const article = articleById.get(row.articleId);
    return Boolean(article?.asset_link_kind) && !row.assetId;
  });
  const missingLocationRow = rows.find((row) => {
    const article = articleById.get(row.articleId);
    if (!article?.location_required) return false;
    if (!row.locationId) return true;
    // Арендная статья: без выбранного арендодателя (договора) платёж некому направить.
    return Boolean(article.lease_bound) && !row.leaseId;
  });
  // Мягкая проверка: сумма арендной строки больше месячной ставки договора. Предупреждаем, но
  // НЕ блокируем отправку — платёж может закрывать несколько месяцев или включать доплату.
  const rentOverpayRows = rows.filter((row) => {
    const article = articleById.get(row.articleId);
    const cap = row.leaseRecipient?.monthlyAmount ?? 0;
    return Boolean(article?.lease_bound) && cap > 0 && amountOf(row.amount) > cap;
  });
  const requiresRequisites = Boolean(requisitesRecipient);
  const directRouteBlocked =
    requiresRequisites &&
    (rows.length !== 1 || isCashSource || !requisitesRecipient?.requisites_verified);
  const fallbackRouteBlocked =
    Boolean(missingRequisitesRecipient) && (!allowOfficialViaSafe || isCashSource);
  const canSubmit =
    Boolean(walletId) &&
    rows.length > 0 &&
    rows.every((row) => {
      const article = articleById.get(row.articleId);
      const counterparty = counterpartyById.get(row.counterpartyId);
      const periodReady =
        !counterparty?.service_period_required ||
        Boolean(row.servicePeriodStart && row.servicePeriodEnd);
      const locationReady =
        !article?.location_required ||
        (Boolean(row.locationId) && (!article.lease_bound || Boolean(row.leaseId)));
      const assetReady = !article?.asset_link_kind || Boolean(row.assetId);
      return (
        row.articleId && amountOf(row.amount) > 0 && periodReady && locationReady && assetReady
      );
    }) &&
    !directRouteBlocked &&
    !fallbackRouteBlocked;

  const buildLines = (): NewPaymentExpenseLine[] =>
    rows.map((row) => ({
      article_id: row.articleId,
      amount: amountOf(row.amount),
      purpose: row.purpose.trim(),
      counterparty_id: row.counterpartyId || null,
      service_period_start: row.servicePeriodStart || null,
      service_period_months: row.servicePeriodStart ? Number(row.servicePeriodMonths || "1") : null,
      auto_recognize_monthly: row.autoRecognizeMonthly,
      service_period_end: row.servicePeriodEnd || null,
      location_id: row.locationId || null,
      lease_id: row.leaseId || null,
      asset_id: row.assetId || null,
    }));

  const mutation = useMutation({
    mutationFn: async ({ payNow }: { payNow: boolean }) => {
      const lines = buildLines();
      return isCashSource
        ? createExpenseCashReserves({ wallet_id: walletId, lines, pay_now: payNow })
        : createNewPaymentExpenseDraft({
            lines,
            vat_rate: vatRate || null,
            channel,
            allow_official_via_safe: allowOfficialViaSafe,
          });
    },
    onSuccess: async (_result, { payNow }) => {
      toast.success(
        !isCashSource
          ? "Черновик отправлен в банк"
          : payNow
            ? `Платёж проведён — списано с ${isSafeSource ? "Сейфа" : "Кассы"}`
            : rows.length > 1
              ? "Резервы созданы"
              : "Резерв создан",
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать платёж")),
  });

  const transferMutation = useMutation({
    mutationFn: () =>
      createInternalTransfer({
        source_wallet_id: walletId,
        dest_wallet_id: transferDest?.id ?? "",
        mode: "targeted",
        lines: buildLines(),
      }),
    onSuccess: async (result) => {
      toast.success(
        `${isSafeSource ? "Передано в кассу" : "Передано на Сейф"}: ${formatRub(result.amount)}, резервов: ${result.reserves}`,
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось передать резерв")),
  });

  const busy = mutation.isPending || transferMutation.isPending;
  const n = rows.length;

  // Панель «Что произойдёт» — единственное объяснение режима.
  let tone: SummaryTone;
  let summary: string;
  if (!selectedWallet) {
    tone = "warning";
    summary = "Выберите счёт списания.";
  } else if (missingArticleRow) {
    tone = "warning";
    summary = `Выберите статью ДДС для платежа ${shortName(
      counterpartyById.get(missingArticleRow.counterpartyId)?.name ?? "контрагенту",
    )} — без статьи расход выпадет из аналитики.`;
  } else if (missingAssetRow) {
    tone = "warning";
    // Причина у покупки и у ремонта разная, и общая формулировка врала бы в одну из сторон:
    // покупка без карточки уходит мимо баланса, а ремонт без объекта — мимо истории объекта,
    // по которой потом решают, чинить его дальше или менять.
    summary = assetsForbidden
      ? ASSETS_FORBIDDEN_HINT
      : articleById.get(missingAssetRow.articleId)?.asset_link_kind === "purchase"
        ? "Укажите основное средство — без карточки покупка уйдёт в расход мимо баланса."
        : "Укажите, что ремонтируем — иначе расход не попадёт в историю объекта.";
  } else if (missingLocationRow) {
    tone = "warning";
    summary = locationsForbidden
      ? LOCATIONS_FORBIDDEN_HINT
      : articleById.get(missingLocationRow.articleId)?.lease_bound && missingLocationRow.locationId
        ? "Выберите арендодателя для арендного платежа."
        : "Укажите помещение для арендного платежа.";
  } else if (missingServicePeriodRecipient) {
    tone = "warning";
    summary = `Укажите период оказания услуги для ${shortName(missingServicePeriodRecipient.name)}.`;
  } else if (requiresRequisites && rows.length !== 1) {
    tone = "warning";
    summary = "Платёж по реквизитам оформляется отдельной строкой на одного контрагента.";
  } else if (requiresRequisites && !requisitesRecipient?.requisites_verified) {
    tone = "warning";
    summary = `Реквизиты ${shortName(requisitesRecipient?.name ?? "контрагента")} не подтверждены — отправка недоступна.`;
  } else if (officialRecipient && isCashSource) {
    tone = "warning";
    summary = requiresRequisites
      ? `Для ${shortName(requisitesRecipient?.name ?? "контрагента")} выберите банковский счёт: оплата обязательна по реквизитам.`
      : `Для ${shortName(missingRequisitesRecipient?.name ?? "контрагента")} выберите банковский счёт. После этого можно вывести деньги на карту ИП.`;
  } else if (missingRequisitesRecipient && !allowOfficialViaSafe) {
    tone = "warning";
    summary = `У ${shortName(missingRequisitesRecipient.name)} не указаны реквизиты. Можно вывести деньги на карту ИП → Сейф.`;
  } else if (!isCashSource) {
    tone = "draft";
    if (requiresRequisites) {
      const bankName = selectedWallet?.bank_code === "sber" ? "Сбер" : "Т-Банк";
      summary = `Черновик в ${bankName} → ${shortName(requisitesRecipient?.name ?? "контрагент")} по реквизитам. Спишется после оплаты в банке.`;
    } else if (missingRequisitesRecipient) {
      summary = `Подтверждён вывод для ${shortName(missingRequisitesRecipient.name)}: карта ИП → Сейф. Спишется после оплаты в банке.`;
    } else {
      const route =
        selectedWallet?.bank_code === "sber"
          ? "Черновик через Сбер → Сейф."
          : n > 1
            ? `${n} ${plural(n, ["строка", "строки", "строк"])} — одним черновиком в Т-Банк → карта ИП → Сейф.`
            : "Черновик в Т-Банк → карта ИП → Сейф.";
      summary = `${route} ${n > 1 && selectedWallet?.bank_code !== "sber" ? "Разнос по статьям при оплате." : "Спишется после оплаты в банке."}`;
    }
  } else if (act === "now") {
    tone = "instant";
    summary = `Спишется с ${isSafeSource ? "Сейфа" : "Кассы"} сразу.`;
  } else if (act === "move") {
    tone = "move";
    summary = isSafeSource
      ? "Наличные уедут из Сейфа в Кассу и встанут резервом под выдачу."
      : "Наличные уедут из Кассы на Сейф и встанут резервом под выдачу.";
  } else {
    tone = "reserve";
    summary =
      n > 1
        ? `${n} ${plural(n, ["резерв", "резерва", "резервов"])} на ${isSafeSource ? "Сейфе" : "Кассе"} — по одному на строку, выдача позже.`
        : `Резерв на ${isSafeSource ? "Сейфе" : "Кассе"} — деньги остаются на счёте до выдачи.`;
  }

  const submitLabel = !isCashSource
    ? "Отправить в банк"
    : act === "now"
      ? "Создать платёж"
      : act === "move"
        ? moveLabel
        : "Создать резерв";

  function submit() {
    if (isCashSource && act === "move") {
      transferMutation.mutate();
      return;
    }
    mutation.mutate({ payNow: isCashSource && act === "now" });
  }

  return (
    <div>
      <FormHeader title="Свободный расход" />
      <div className="space-y-3">
        <SourcePicker
          label="Счёт списания"
          wallets={wallets}
          value={walletId}
          onChange={setWalletId}
          disabledReason={(wallet) =>
            officialRecipient && wallet.kind !== "bank"
              ? "Для официального контрагента выберите банковский счёт"
              : wallet.kind === "bank" &&
                  wallet.bank_code !== "tbank" &&
                  wallet.bank_code !== "sber"
                ? "Черновики — только из Т-Банка и Сбера"
                : null
          }
        />

        {isCashSource && actOptions.length > 1 ? (
          <ActionSegment
            options={actOptions}
            value={act}
            onChange={(key) => setAct(key as typeof act)}
          />
        ) : null}

        <div className="space-y-2">
          {rows.map((row) => {
            const article = articleById.get(row.articleId) ?? null;
            const pinnedIds = new Set(
              (article?.counterparties ?? []).map((item) => item.counterparty_id),
            );
            const selectedCounterparty = counterpartyById.get(row.counterpartyId);
            // Свободный «кому платим» — у всех неарендных статей, даже если за статьёй никто не
            // закреплён: получателя выбирают из общего справочника. У аренды получатель приходит
            // из договора (блок «Помещение» ниже), выбирать его руками нельзя.
            const showRecipient = !article?.lease_bound;
            // Период — переменная платежа, а не свойство карточки: обязателен там, где карточка
            // этого требует, но указать его можно для любого получателя (оплата за квартал
            // вперёд бывает и у тех, у кого галки нет).
            const periodMissing = Boolean(
              selectedCounterparty?.service_period_required && !row.servicePeriodStart,
            );
            return (
              <div className="space-y-1.5 rounded-md border p-2.5" key={row.key}>
                <div className="grid grid-cols-[minmax(0,1fr)_130px_auto] items-center gap-2">
                  <ArticleCombobox
                    articles={articles}
                    onChange={(value) => onChangeArticle(row.key, value)}
                    value={row.articleId}
                  />
                  <Input
                    aria-label="Сумма"
                    className="text-right tabular-nums"
                    inputMode="decimal"
                    onChange={(event) => onUpdateRow(row.key, { amount: event.target.value })}
                    placeholder="Сумма, ₽"
                    value={row.amount}
                  />
                  <Button
                    aria-label="Убрать строку"
                    disabled={rows.length <= 1}
                    onClick={() => onRemoveRow(row.key)}
                    size="icon"
                    title="Убрать строку"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </div>
                <div className={cn("gap-2", showRecipient ? "grid grid-cols-2" : "")}>
                  <Input
                    className="h-8 text-sm"
                    maxLength={210}
                    onChange={(event) => onUpdateRow(row.key, { purpose: event.target.value })}
                    placeholder="Назначение (необязательно)"
                    value={row.purpose}
                  />
                  {showRecipient ? (
                    <CounterpartyCombobox
                      counterparties={counterparties}
                      onChange={(counterpartyId) => {
                        const counterparty = counterpartyById.get(counterpartyId);
                        // Статью подставляем из карточки — но только в пустую строку: если
                        // человек уже выбрал статью сам, менять её под получателя нельзя.
                        // Патч один: через onChangeArticle получатель бы затёрся, эта ветка
                        // сбрасывает строку под новую статью.
                        const articleFromCard =
                          !row.articleId &&
                          counterparty?.default_dds_article_id &&
                          articleById.has(counterparty.default_dds_article_id)
                            ? { articleId: counterparty.default_dds_article_id }
                            : {};
                        onUpdateRow(row.key, {
                          counterpartyId,
                          ...articleFromCard,
                          ...emptyServicePeriod(),
                        });
                      }}
                      pinnedIds={pinnedIds}
                      value={row.counterpartyId}
                    />
                  ) : null}
                </div>
                {row.counterpartyId && !row.articleId ? (
                  <div className="text-xs text-amber-700">
                    {counterpartyById.get(row.counterpartyId)?.confirm_no_dds_article
                      ? "У контрагента статья ДДС не закреплена — выберите её для этого платежа."
                      : "В карточке контрагента нет статьи ДДС — выберите её здесь (и закрепите в карточке, чтобы дальше подставлялась сама)."}
                  </div>
                ) : null}
                {article?.asset_link_kind ? (
                  // Строкой-ссылкой, а не развёрнутым блоком (замечание владельца 31.07.2026):
                  // окно платежа и без того длинное, а объект нужен считанным статьям. Выбор
                  // уезжает в отдельную модалку — ровно так же, как в разборе ДДС, чтобы одно и
                  // то же действие в двух окнах не выглядело двумя разными.
                  <button
                    className={`text-left text-xs underline-offset-2 hover:underline ${
                      row.assetId ? "text-muted-foreground" : "font-medium text-amber-700"
                    }`}
                    onClick={() => setAssetRowKey(row.key)}
                    type="button"
                  >
                    {assetSummary(row)}
                  </button>
                ) : null}
                {article?.location_required ? (
                  <ExpenseLocationPicker
                    articleId={article.id}
                    leaseBound={Boolean(article.lease_bound)}
                    locationId={row.locationId}
                    leaseId={row.leaseId}
                    onChange={(patch) => onUpdateRow(row.key, patch)}
                  />
                ) : null}
                {selectedCounterparty ? (
                  // Строкой-ссылкой в отдельную модалку — тем же приёмом, что и объект ОС
                  // (правило владельца: окно платежа и без того длинное, развёрнутые блоки
                  // внутри строки в него не помещаются).
                  <button
                    className={cn(
                      "text-left text-xs underline-offset-2 hover:underline",
                      periodMissing ? "font-medium text-amber-700" : "text-muted-foreground",
                    )}
                    onClick={() => setPeriodRowKey(row.key)}
                    type="button"
                  >
                    {periodSummary(row, selectedCounterparty)}
                  </button>
                ) : null}
              </div>
            );
          })}
          <Button
            disabled={requiresRequisites}
            onClick={onAddRow}
            size="sm"
            title={
              requiresRequisites ? "Платёж по реквизитам создаётся отдельным документом" : undefined
            }
            type="button"
            variant="outline"
          >
            <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
            Добавить строку
          </Button>
        </div>

        {/* НДС — только у банковского платежа: наличный резерв в банк не уходит, и его
            назначение читает наш же журнал, а не платёжное поручение. */}
        {!isCashSource ? (
          <VatRateField
            total={total}
            value={vatRate}
            onChange={setVatRate}
            hint={bankPurposeBase ? `${bankPurposeBase.replace(/\s*\.*$/, "")}.` : undefined}
          />
        ) : null}

        <SummaryPanel tone={tone} total={total}>
          {summary}
        </SummaryPanel>
        {rentOverpayRows.length > 0 ? (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            {rentOverpayRows.length === 1 && rentOverpayRows[0].leaseRecipient
              ? `Сумма больше месячной аренды по договору с ${shortName(
                  rentOverpayRows[0].leaseRecipient.name,
                )} — ${formatRub(
                  rentOverpayRows[0].leaseRecipient.monthlyAmount,
                )}. Проверьте: отправить всё равно можно.`
              : `В ${rentOverpayRows.length} строках сумма больше месячной аренды по договору. Проверьте: отправить всё равно можно.`}
          </div>
        ) : null}
        {missingRequisitesRecipient && !requiresRequisites ? (
          <Button
            onClick={() =>
              setOfficialViaSafeConsent(allowOfficialViaSafe ? null : recipientSignature)
            }
            size="sm"
            type="button"
            variant="outline"
          >
            {allowOfficialViaSafe ? "Отменить вывод на карту ИП" : "Вывести на карту ИП"}
          </Button>
        ) : null}
      </div>
      <FormFooter
        cancel={onCancel}
        submit={submit}
        submitLabel={submitLabel}
        disabled={!canSubmit}
        pending={busy}
      />

      {assetRow && assetRowArticle?.asset_link_kind ? (
        <Dialog open onOpenChange={(open) => !open && setAssetRowKey(null)}>
          <DialogContent className="sm:max-w-lg">
            <DialogHeader>
              <DialogTitle>Основное средство</DialogTitle>
              <DialogDescription>
                {assetRowArticle.name} · {formatDdsMoney(amountOf(assetRow.amount))}
              </DialogDescription>
            </DialogHeader>
            <AssetPicker
              amount={String(amountOf(assetRow.amount))}
              assets={assetsQuery.data ?? []}
              forbidden={assetsForbidden}
              isLoading={assetsQuery.isLoading}
              kind={assetRowArticle.asset_link_kind}
              onChange={(assetId) => onUpdateRow(assetRow.key, { assetId })}
              onCreated={(asset) => {
                // Карточку кладём в кэш общего списка сразу: иначе она появится лишь
                // после повторного запроса, а выбрать её надо прямо сейчас.
                queryClient.setQueryData<AssetOption[]>(["asset-options"], (current) =>
                  current ? [...current, asset] : [asset],
                );
                onUpdateRow(assetRow.key, { assetId: asset.asset_id });
              }}
              value={assetRow.assetId}
            />
            <DialogFooter>
              <Button onClick={() => setAssetRowKey(null)} type="button">
                Готово
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}

      {periodRow && periodCounterparty ? (
        <ServicePeriodDialog
          amount={amountOf(periodRow.amount)}
          counterparty={periodCounterparty}
          onChange={(patch) => onUpdateRow(periodRow.key, patch)}
          onClose={() => setPeriodRowKey(null)}
          row={periodRow}
        />
      ) : null}
    </div>
  );
}

/** Период оказания услуги — отдельным окном, а не блоком внутри строки платежа.
 *
 *  Спрашиваем месяцами, а не двумя датами: почти вся абонентка помесячная, а «оплата за
 *  3 месяца» руками — это две даты, в которых легко ошибиться на день. Даты на бэкенд уходят
 *  по-прежнему, их считает выбор «месяц + сколько месяцев».
 */
function ServicePeriodDialog({
  amount,
  counterparty,
  onChange,
  onClose,
  row,
}: {
  amount: number;
  counterparty: NewPaymentCounterparty;
  onChange: (patch: Partial<ExpenseRow>) => void;
  onClose: () => void;
  row: ExpenseRow;
}) {
  const months = Number(row.servicePeriodMonths || "1");
  const filled = Boolean(row.servicePeriodStart && row.servicePeriodEnd);
  const suggestedMonth = offsetMonthValue(counterparty.default_service_period_offset_months);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Период оказания услуги</DialogTitle>
          <DialogDescription>
            {shortName(counterparty.name, 40)}
            {amount > 0 ? ` · ${formatDdsMoney(amount)}` : ""}
            {counterparty.service_period_required ? " · период обязателен" : ""}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <Label className="text-xs">Первый месяц услуги</Label>
              <Select
                value={row.servicePeriodStart.slice(0, 7) || undefined}
                onValueChange={(value) => onChange(monthsToPeriod(value, row.servicePeriodMonths))}
              >
                <SelectTrigger aria-label="Первый месяц услуги" className="h-9">
                  <SelectValue placeholder="Выберите месяц" />
                </SelectTrigger>
                <SelectContent>
                  {monthChoices().map((value) => (
                    <SelectItem key={value} value={value}>
                      {monthLabel(value)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Сколько месяцев</Label>
              <Select
                value={row.servicePeriodMonths}
                onValueChange={(value) =>
                  onChange(monthsToPeriod(row.servicePeriodStart.slice(0, 7), value))
                }
              >
                <SelectTrigger aria-label="Сколько месяцев" className="h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {["1", "2", "3", "6", "12"].map((count) => (
                    <SelectItem key={count} value={count}>
                      {count === "1" ? "1 месяц" : `${count} мес.`}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {filled ? (
            <div className="rounded-md bg-muted/50 px-3 py-2 text-xs">
              {formatPeriod(row.servicePeriodStart, row.servicePeriodEnd)}
              {months > 1 && amount > 0 ? ` · по ${formatRub(amount / months)} в месяц` : ""}
            </div>
          ) : suggestedMonth ? (
            // Настройка карточки («обычно платим за прошлый месяц») больше не подставляется
            // молча — она здесь кнопкой. Один клик вместо ввода, но клик человека.
            <Button
              className="h-8"
              onClick={() => onChange(monthsToPeriod(suggestedMonth, row.servicePeriodMonths))}
              size="sm"
              type="button"
              variant="outline"
            >
              Обычно у этого контрагента: {monthLabel(suggestedMonth)}
            </Button>
          ) : null}

          <label className="flex items-start gap-2 text-sm">
            <input
              checked={row.autoRecognizeMonthly}
              className="mt-1 h-4 w-4"
              type="checkbox"
              onChange={(event) => onChange({ autoRecognizeMonthly: event.target.checked })}
            />
            <span>
              <span className="block font-medium">Закрывающих документов не будет</span>
              <span className="block text-xs text-muted-foreground">
                Расход признаём сами, помесячно. Если УПД всё-таки придёт — он заменит наше
                признание, расход не задвоится.
              </span>
            </span>
          </label>
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          {/* Очистка нужна там, где период необязателен: иначе случайно выбранный месяц
              уехал бы в платёж, и признание разложило бы расход не туда. */}
          {filled && !counterparty.service_period_required ? (
            <Button
              onClick={() => {
                onChange({
                  servicePeriodStart: "",
                  servicePeriodEnd: "",
                  servicePeriodMonths: "1",
                  autoRecognizeMonthly: false,
                });
                onClose();
              }}
              type="button"
              variant="ghost"
            >
              Убрать период
            </Button>
          ) : (
            <span />
          )}
          <Button onClick={onClose} type="button">
            Готово
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// --------------------------------------------------------------------------- //
// Предоплата поставщику: банк — черновик; наличные — выплатить сразу / резерв /
// передать (дебиторка возникает при выплате)

function PrepaymentForm({
  active,
  article,
  wallets,
  canConfirmPaid,
  canReserveCash,
  onDirty,
  onDone,
  onCancel,
}: {
  active: boolean;
  article: NewPaymentArticle;
  wallets: NewPaymentWallet[];
  canConfirmPaid: boolean;
  canReserveCash: boolean;
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [counterpartyId, setCounterpartyId] = useState("");
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [act, setAct] = useState<"pay" | "reserve" | "move">("reserve");
  // Ставка НДС аванса («22», «10», …) или «» — без НДС. Уходит в назначение платёжки.
  const [vatRate, setVatRate] = useState("");

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  const isCashSource = selectedWallet?.kind === "cash";
  const isSafeSource = isCashSource && selectedWallet?.location === "safe";

  const transferDest = isSafeSource
    ? (wallets.find((w) => w.kind === "cash" && w.location === "kassa") ?? null)
    : (wallets.find((w) => w.kind === "cash" && w.location === "safe") ?? null);
  const moveLabel = isSafeSource ? "Передать в кассу" : "Передать на Сейф";
  const actOptions = [
    ...(canReserveCash ? [{ key: "reserve", label: "Резерв" }] : []),
    { key: "pay", label: "Выплатить сразу" },
    ...(canConfirmPaid && transferDest ? [{ key: "move", label: moveLabel }] : []),
  ];
  useEffect(() => {
    if (!actOptions.some((option) => option.key === act)) {
      setAct(canReserveCash ? "reserve" : "pay");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [walletId, canReserveCash, canConfirmPaid]);

  const dirty = amountOf(amount) > 0 || Boolean(counterpartyId);
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const registryQuery = useQuery({
    queryKey: ["cp", "registry"],
    queryFn: () => getRegistry(),
    enabled: active,
  });
  const counterparties = useMemo(
    () =>
      (registryQuery.data ?? [])
        .filter((item) => item.relationship !== "barter")
        .sort((a, b) => a.name.localeCompare(b.name, "ru")),
    [registryQuery.data],
  );
  const options: ComboboxOption[] = counterparties.map((item) => ({
    value: item.counterparty_id,
    label: item.name,
    keywords: item.inn ?? undefined,
  }));
  const selected = counterparties.find((item) => item.counterparty_id === counterpartyId) ?? null;
  const cpName = selected ? shortName(selected.name) : null;
  const isInformal = selected?.relationship === "informal";
  // Неофициальный поставщик: банк-черновик запрещён, наличными — можно.
  const informalBlocked = isInformal && !isCashSource;
  const canSubmit =
    Boolean(counterpartyId) && Boolean(walletId) && !informalBlocked && amountOf(amount) > 0;

  const payMutation = useMutation({
    mutationFn: async () => {
      if (isCashSource) {
        await createPrepayment({
          counterparty_id: counterpartyId,
          wallet_id: walletId,
          amount: amountOf(amount),
          article_id: article.id,
        });
        return;
      }
      await createBankPrepaymentDraft({
        counterparty_id: counterpartyId,
        amount: amountOf(amount),
        article_id: article.id,
        vat_rate: vatRate || null,
      });
    },
    onSuccess: async () => {
      toast.success(
        isCashSource
          ? "Предоплата выплачена — дебиторка создана"
          : "Черновик предоплаты отправлен в банк",
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать предоплату")),
  });

  const prepaymentLine = () => [
    {
      article_id: article.id,
      amount: amountOf(amount),
      purpose: "",
      counterparty_id: counterpartyId,
    },
  ];
  const reserveMutation = useMutation({
    mutationFn: () => createExpenseCashReserves({ wallet_id: walletId, lines: prepaymentLine() }),
    onSuccess: async () => {
      toast.success("Резерв предоплаты создан — дебиторка возникнет при выплате");
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать резерв")),
  });
  const transferMutation = useMutation({
    mutationFn: () =>
      createInternalTransfer({
        source_wallet_id: walletId,
        dest_wallet_id: transferDest?.id ?? "",
        mode: "targeted",
        lines: prepaymentLine(),
      }),
    onSuccess: async (result) => {
      toast.success(
        `${isSafeSource ? "Передано в кассу" : "Передано на Сейф"}: ${formatRub(result.amount)} — дебиторка возникнет при выдаче`,
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось передать резерв")),
  });
  const busy = payMutation.isPending || reserveMutation.isPending || transferMutation.isPending;

  // Панель «Что произойдёт».
  let tone: SummaryTone;
  let summary: string;
  if (!selectedWallet) {
    tone = "warning";
    summary = "Выберите счёт списания.";
  } else if (informalBlocked) {
    tone = "warning";
    summary = `«${cpName}» — неофициальный поставщик: банк недоступен. Выберите Сейф или Кассу.`;
  } else if (!isCashSource) {
    tone = "draft";
    summary = `Черновик в Т-Банк на счёт ${cpName ? `«${cpName}»` : "поставщика"}. Дебиторка — после оплаты.`;
  } else if (act === "pay") {
    tone = "instant";
    summary = `Спишется с ${isSafeSource ? "Сейфа" : "Кассы"} сразу. Дебиторка ${cpName ? `«${cpName}» ` : ""}появится сегодня.`;
  } else if (act === "move") {
    tone = "move";
    summary = `Наличные уедут ${isSafeSource ? "из Сейфа в Кассу" : "из Кассы на Сейф"} резервом. Дебиторка — при выдаче.`;
  } else {
    tone = "reserve";
    summary = `Резерв на ${isSafeSource ? "Сейфе" : "Кассе"}. Дебиторка возникнет при выплате.`;
  }

  const submitLabel = !isCashSource
    ? "Отправить в банк"
    : act === "pay"
      ? "Выплатить"
      : act === "move"
        ? moveLabel
        : "Создать резерв";

  function submit() {
    if (!isCashSource || act === "pay") {
      payMutation.mutate();
      return;
    }
    if (act === "move") {
      transferMutation.mutate();
      return;
    }
    reserveMutation.mutate();
  }

  return (
    <div>
      <FormHeader title={article.name} />
      <div className="space-y-3">
        <SourcePicker
          label="Счёт списания"
          wallets={wallets}
          value={walletId}
          onChange={setWalletId}
          disabledReason={(wallet) =>
            wallet.kind === "bank" && wallet.bank_code !== "tbank"
              ? "Банковская предоплата — только из Т-Банка"
              : null
          }
        />

        {isCashSource && actOptions.length > 1 ? (
          <ActionSegment
            options={actOptions}
            value={act}
            onChange={(key) => setAct(key as typeof act)}
          />
        ) : null}

        <div className="space-y-1">
          <Label className="text-sm">Контрагент</Label>
          <InlineOptionList
            emptyMessage="Контрагенты не найдены"
            listClassName="max-h-56"
            onChange={setCounterpartyId}
            options={options}
            searchPlaceholder="Название или ИНН…"
            value={counterpartyId}
          />
        </div>

        <Label className="block space-y-1">
          <span className="text-sm">Сумма, ₽</span>
          <Input
            className="tabular-nums"
            inputMode="decimal"
            onChange={(event) => setAmount(event.target.value)}
            placeholder="0"
            value={amount}
          />
        </Label>

        {/* НДС — только у банковского черновика: наличная предоплата в банк не уходит. */}
        {!isCashSource ? (
          <VatRateField
            total={amountOf(amount) > 0 ? amountOf(amount) : 0}
            value={vatRate}
            onChange={setVatRate}
            hint={`Предоплата поставщику ${selected?.name ?? "…"}.`}
          />
        ) : null}

        <SummaryPanel tone={tone} total={amountOf(amount) > 0 ? amountOf(amount) : 0}>
          {summary}
        </SummaryPanel>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={submit}
        submitLabel={submitLabel}
        disabled={!canSubmit}
        pending={busy}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Поступление: наличный приход на Сейф/в Кассу (проводка сразу; банк — из выписки)

function IncomeForm({
  active,
  articles,
  wallets,
  articleId,
  onArticleChange,
  onDirty,
  onDone,
  onCancel,
}: {
  active: boolean;
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
  articleId: string;
  onArticleChange: (id: string) => void;
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [walletId, setWalletId] = useState("");
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");
  const [counterpartyId, setCounterpartyId] = useState("");

  const dirty = amountOf(amount) > 0 || Boolean(counterpartyId) || purpose.trim().length > 0;
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const selectedArticle = articles.find((item) => item.id === articleId) ?? null;
  // Возврат от поставщика гасит его открытые предоплаты — без контрагента не провести.
  const counterpartyRequired = selectedArticle?.code === "vozvrat_pereplaty_ot_postavschikov";

  const cashWallets = wallets.filter((wallet) => wallet.kind === "cash");
  const safeWallet = cashWallets.find((wallet) => wallet.location === "safe") ?? null;
  const selectedWallet = cashWallets.find((wallet) => wallet.id === walletId) ?? null;
  const destName = selectedWallet?.location === "kassa" ? "в Кассу" : "на Сейф";
  useEffect(() => {
    if (!walletId && safeWallet) {
      setWalletId(safeWallet.id);
    }
  }, [walletId, safeWallet]);

  const registryQuery = useQuery({
    queryKey: ["cp", "registry"],
    queryFn: () => getRegistry(),
    enabled: active,
  });
  const registryById = useMemo(() => {
    const map = new Map<string, string>();
    (registryQuery.data ?? []).forEach((item) => map.set(item.counterparty_id, item.name));
    return map;
  }, [registryQuery.data]);
  const counterpartyOptions: ComboboxOption[] = useMemo(
    () => [
      { value: "", label: "Не указан" },
      ...(registryQuery.data ?? [])
        .filter((item) => item.relationship !== "barter")
        .sort((a, b) => a.name.localeCompare(b.name, "ru"))
        .map((item) => ({
          value: item.counterparty_id,
          label: item.name,
          keywords: item.inn ?? undefined,
        })),
    ],
    [registryQuery.data],
  );

  const canSubmit =
    Boolean(articleId) &&
    Boolean(walletId) &&
    amountOf(amount) > 0 &&
    (!counterpartyRequired || Boolean(counterpartyId));

  const mutation = useMutation({
    mutationFn: () =>
      createNewPaymentIncome({
        wallet_id: walletId,
        lines: [
          {
            article_id: articleId,
            amount: amountOf(amount),
            purpose: purpose.trim(),
            counterparty_id: counterpartyId || null,
          },
        ],
      }),
    onSuccess: async () => {
      toast.success("Поступление проведено");
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести поступление")),
  });

  // Панель «Что произойдёт».
  const cpName = counterpartyId ? shortName(registryById.get(counterpartyId) ?? "") : null;
  let tone: SummaryTone = "instant";
  let summary: string;
  if (!selectedWallet) {
    tone = "warning";
    summary = "Выберите счёт зачисления.";
  } else if (counterpartyRequired && !counterpartyId) {
    tone = "warning";
    summary =
      "Выберите поставщика — возврат гасит его открытые предоплаты (излишек останется обычным приходом).";
  } else if (counterpartyRequired && cpName) {
    summary = `Придёт ${destName} и зачтётся в предоплаты «${cpName}»; излишек — обычный приход.`;
  } else {
    summary = `Придёт ${destName} сразу. Банковские поступления приходят из выписки сами.`;
  }

  return (
    <div>
      <FormHeader title="Поступление" />
      <div className="space-y-3">
        <SourcePicker
          label="Счёт зачисления"
          wallets={cashWallets}
          value={walletId}
          onChange={setWalletId}
        />

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Статья</span>
            <ArticleCombobox
              articles={articles}
              onChange={onArticleChange}
              placeholder="Статья поступления"
              value={articleId}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
        </div>

        <Label className="block space-y-1">
          <span className="text-sm">Назначение</span>
          <Input
            maxLength={210}
            onChange={(event) => setPurpose(event.target.value)}
            placeholder="Необязательно"
            value={purpose}
          />
        </Label>

        <div className="space-y-1">
          <Label className="text-sm">
            {counterpartyRequired ? "Контрагент" : "Контрагент (необязательно)"}
          </Label>
          <InlineOptionList
            emptyMessage="Контрагенты не найдены"
            listClassName="max-h-40"
            onChange={setCounterpartyId}
            options={counterpartyOptions}
            searchPlaceholder="Название или ИНН…"
            value={counterpartyId}
          />
        </div>

        <SummaryPanel tone={tone} total={amountOf(amount) > 0 ? amountOf(amount) : 0}>
          {summary}
        </SummaryPanel>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel="Провести поступление"
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Аванс / заём сотруднику: одно действие на счёт (механика — контур авансов)

function AdvanceForm({
  active,
  kind,
  canLoan,
  onKindChange,
  wallets,
  employees,
  onDirty,
  onDone,
  onCancel,
}: {
  active: boolean;
  kind: "advance" | "loan";
  canLoan: boolean;
  onKindChange: (kind: "advance" | "loan") => void;
  wallets: NewPaymentWallet[];
  employees: NewPaymentEmployee[];
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [employeeId, setEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [installmentAmount, setInstallmentAmount] = useState("");
  const [recoveryStartDate, setRecoveryStartDate] = useState("");
  const [overrideCeiling, setOverrideCeiling] = useState(false);
  const [comment, setComment] = useState("");

  const dirty = amountOf(amount) > 0 || Boolean(employeeId);
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);

  const employeeOptions: ComboboxOption[] = employees.map((employee) => ({
    value: employee.id,
    label: employee.full_name,
  }));

  const availabilityQuery = useQuery({
    queryKey: ["payroll-advance-availability", employeeId],
    queryFn: () => getPayrollAdvanceAvailability(employeeId),
    enabled: active && Boolean(employeeId),
  });
  const available = availabilityQuery.data?.available ?? 0;
  // День выплаты: available уже 0 и причина — не «больше заработанного», а наступивший
  // день выплаты (заработанное уходит с ведомостью).
  const payoutReached = availabilityQuery.data?.payout_reached ?? false;
  const availabilityNote = availabilityQuery.data?.note ?? null;
  const earnedToDate = availabilityQuery.data?.earned_to_date ?? 0;
  const numericAmount = amountOf(amount);
  const overAvailable =
    kind === "advance" &&
    Boolean(employeeId) &&
    availabilityQuery.data != null &&
    numericAmount > available;

  const isLoan = kind === "loan";
  const canSubmit = Boolean(employeeId) && Boolean(walletId) && numericAmount > 0 && !overAvailable;
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;

  const mutation = useMutation({
    mutationFn: () =>
      createPayrollAdvance({
        employee_id: employeeId,
        // Та же нормализация, что в валидации: «5 000,50» → «5000.50».
        amount: amountStr(amount),
        kind,
        wallet_id: walletId,
        installment_amount:
          isLoan && installmentAmount.trim() ? amountStr(installmentAmount) : undefined,
        recovery_start_date: isLoan && recoveryStartDate ? recoveryStartDate : undefined,
        override_ceiling: isLoan ? overrideCeiling : false,
        comment: comment.trim() ? comment.trim() : null,
      }),
    onSuccess: async () => {
      toast.success(isLoan ? "Заём оформлен" : "Аванс оформлен");
      await onDone();
    },
    onError: (error) =>
      toast.error(
        apiErrorMessage(error, isLoan ? "Не удалось оформить заём" : "Не удалось оформить аванс"),
      ),
  });

  // Панель «Что произойдёт»: маршрут выдачи по счёту (фразы сверены с механикой
  // контура авансов); предупреждение — та же панель.
  const noun = isLoan ? "заём" : "аванс";
  let tone: SummaryTone;
  let summary: string;
  if (!selectedWallet) {
    tone = "warning";
    summary = "Выберите счёт списания.";
  } else if (overAvailable) {
    tone = "warning";
    const loanHint = canLoan
      ? " Оформите «Заём»."
      : " Такую сумму выдаёт только заём (нужно право займов).";
    summary = payoutReached
      ? `Наступил день выплаты — аванс за этот период недоступен.${loanHint}`
      : `Больше заработанного (${formatRub(available)}) — аванс не пройдёт.${loanHint}`;
  } else if (selectedWallet.kind !== "cash") {
    // Черновик идёт на реквизиты ИП → Сейф-резерв; сотруднику выдают наличными.
    tone = "draft";
    summary = `Черновик в Т-Банк на счёт ИП → Сейф; ${noun} выдадите наличными по «Выплачено».`;
  } else if (selectedWallet.location === "kassa") {
    // Денег не двигает: создаётся разрешение кассиру (kassa_pending).
    tone = "reserve";
    summary = `Кассир получит разрешение и выдаст наличные — ${noun} активируется при выдаче.`;
  } else {
    // Сейф: немедленная out-проводка, аванс активен сразу (issue_advance → issued).
    tone = "instant";
    summary = `Спишется с Сейфа сразу — выдадите наличными, ${noun} активен с момента оформления.`;
  }

  const submitLabel =
    selectedWallet?.kind !== "cash"
      ? "Отправить в банк"
      : selectedWallet.location === "kassa"
        ? "Передать в кассу"
        : isLoan
          ? "Оформить заём"
          : "Оформить аванс";

  return (
    <div>
      <FormHeader title={isLoan ? "Заём сотруднику" : "Аванс сотруднику"} />
      <div className="space-y-3">
        {canLoan ? (
          <div className="inline-flex w-fit overflow-hidden rounded-md border">
            <button
              className={cn(
                "px-4 py-1.5 text-sm",
                !isLoan && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => onKindChange("advance")}
              type="button"
            >
              Аванс
            </button>
            <button
              className={cn(
                "px-4 py-1.5 text-sm",
                isLoan && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => onKindChange("loan")}
              type="button"
            >
              Заём
            </button>
          </div>
        ) : null}

        <div className="space-y-1">
          <Label className="text-sm">Сотрудник</Label>
          <InlineOptionList
            emptyMessage="Сотрудники не найдены"
            listClassName="max-h-48"
            onChange={setEmployeeId}
            options={employeeOptions}
            searchPlaceholder="Поиск по имени…"
            value={employeeId}
          />
        </div>

        {employeeId ? (
          <div className="rounded-md border bg-muted/40 p-2.5 text-sm">
            {availabilityQuery.isLoading ? (
              "Считаем доступное…"
            ) : payoutReached ? (
              isLoan ? (
                <>
                  В пределах заработанного: <b>{formatRub(earnedToDate)}</b>
                </>
              ) : (
                <span className="text-amber-700">{availabilityNote}</span>
              )
            ) : availabilityQuery.data ? (
              <>
                Доступно к авансу сегодня: <b>{formatRub(available)}</b>
              </>
            ) : (
              "—"
            )}
          </div>
        ) : null}

        <SourcePicker
          label="Счёт списания"
          wallets={wallets}
          value={walletId}
          onChange={setWalletId}
          disabledReason={(wallet) =>
            wallet.kind === "bank" && wallet.bank_code !== "tbank"
              ? "Выдача — только из Т-Банка или наличными"
              : null
          }
        />

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Комментарий</span>
            <Input
              maxLength={210}
              onChange={(event) => setComment(event.target.value)}
              placeholder="Необязательно"
              value={comment}
            />
          </Label>
        </div>

        {isLoan ? (
          <>
            <div className="grid grid-cols-2 gap-3">
              <Label className="block space-y-1">
                <span className="text-sm">Удержание за период, ₽</span>
                <Input
                  inputMode="decimal"
                  onChange={(event) => setInstallmentAmount(event.target.value)}
                  placeholder="Пусто — весь заём разом"
                  value={installmentAmount}
                />
              </Label>
              <Label className="block space-y-1">
                <span className="text-sm">Удерживать с выплаты</span>
                <Input
                  onChange={(event) => setRecoveryStartDate(event.target.value)}
                  type="date"
                  value={recoveryStartDate}
                />
              </Label>
            </div>
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              <input
                checked={overrideCeiling}
                onChange={(event) => setOverrideCeiling(event.target.checked)}
                type="checkbox"
              />
              Превысить потолок займа (подтверждаю)
            </label>
          </>
        ) : null}

        <SummaryPanel tone={tone} total={numericAmount > 0 ? numericAmount : 0}>
          {summary}
        </SummaryPanel>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel={submitLabel}
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Долг по ЗП («по востребованию»): выплата накопленного долга, двухшаговая при банке

function PayoutDebtForm({
  active,
  articles,
  wallets,
  employees,
  invalidate,
  onDirty,
  onClose,
}: {
  active: boolean;
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
  employees: NewPaymentEmployee[];
  invalidate: () => Promise<void>;
  onDirty: (value: boolean) => void;
  onClose: () => void;
}) {
  const [articleId, setArticleId] = useState("");
  const [walletId, setWalletId] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [payoutDate, setPayoutDate] = useState(todayInput());
  const [note, setNote] = useState("");

  const dirty = amountOf(amount) > 0 || Boolean(employeeId);
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);
  useEffect(() => {
    if (!articleId && articles.length > 0) {
      const preset = articles.find((a) => a.code === "zarplata_administrativnogo_personala");
      setArticleId((preset ?? articles[0]).id);
    }
  }, [articleId, articles]);

  // Остаток долга по выбранному сотруднику — из ручки on-demand (начислено − выплачено).
  const debtQuery = useQuery({
    queryKey: ["payroll", "on-demand-debt"],
    queryFn: getOnDemandEmployees,
    enabled: active,
  });
  const debtInfo = debtQuery.data?.find((item) => item.id === employeeId) ?? null;

  const numericAmount = amountOf(amount);
  const canSubmit =
    Boolean(articleId) &&
    Boolean(walletId) &&
    Boolean(employeeId) &&
    Boolean(payoutDate) &&
    numericAmount > 0;

  const createMutation = useMutation({
    mutationFn: () =>
      createEmployeePayout({
        employee_id: employeeId,
        amount: numericAmount,
        wallet_id: walletId,
        payout_date: payoutDate,
        kind: "owner_salary",
        article_id: articleId,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: async (payout) => {
      await invalidate();
      if (payout.status === "failed") {
        toast.error("Банк отклонил черновик платежа");
        onClose();
        return;
      }
      // Ожидающая выплата дальше живёт строкой в «Активных платежах»: оплату доводит статус
      // платёжного документа (вебхук/поллинг), который сам заводит перевод на Сейф с резервом.
      // Привязка операции выписки осталась ручным запасным путём — кнопкой на той карточке,
      // поэтому гнать владельца через отдельный экран сразу после создания больше не нужно.
      toast.success(
        payout.status === "pending"
          ? "Черновик отправлен в банк — платёж в «Активных платежах»"
          : "Выплата проведена",
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать выплату")),
  });

  // Панель «Что произойдёт»: маршрут по счёту; переплата — предупреждением в той же панели.
  let tone: SummaryTone;
  let summary: string;
  if (!selectedWallet) {
    tone = "warning";
    summary = "Выберите счёт списания.";
  } else if (employeeId && debtInfo && numericAmount > debtInfo.debt) {
    tone = "warning";
    summary = `Больше остатка долга (${formatRub(debtInfo.debt)}) — долг уйдёт в минус (переплата).`;
  } else if (selectedWallet.kind === "cash") {
    tone = "instant";
    summary = `Спишется с ${selectedWallet.location === "kassa" ? "Кассы" : "Сейфа"} сразу — долг уменьшится.`;
  } else if (selectedWallet.bank_code === "tbank") {
    // Черновик идёт на реквизиты ИП; после оплаты в банке деньги встанут резервом на Сейфе —
    // перевод заводит статус платёжного документа, руками подтверждать не нужно.
    tone = "draft";
    summary = "Черновик в Т-Банк на счёт ИП → Сейф. Платёж — в «Активных платежах».";
  } else {
    // У Сбера черновиков нет: платёж ждёт в «Активных платежах», где его закрывает
    // поллинг статуса или кнопка «Привязать» (операция из выписки).
    tone = "draft";
    summary = "Этот банк не создаёт черновики — платёж будет ждать в «Активных платежах».";
  }

  return (
    <div>
      <FormHeader title="Долг по ЗП (по требованию)" />
      <div className="space-y-3">
        <Label className="block space-y-1">
          <span className="text-sm">Сотрудник</span>
          <Select onValueChange={setEmployeeId} value={employeeId}>
            <SelectTrigger>
              <SelectValue placeholder="Выберите сотрудника" />
            </SelectTrigger>
            <SelectContent>
              {employees.map((employee) => (
                <SelectItem disabled={!employee.on_demand} key={employee.id} value={employee.id}>
                  {employee.full_name}
                  {!employee.on_demand ? " — доступны аванс или займ" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground">
            Оклад «по требованию» включается в «Исходных данных».
          </span>
        </Label>

        {employeeId && debtInfo ? (
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-md bg-muted/50 px-3 py-2">
              <div className="text-xs text-muted-foreground">Начислено</div>
              <div className="text-sm font-medium tabular-nums">{formatRub(debtInfo.accrued)}</div>
            </div>
            <div className="rounded-md bg-muted/50 px-3 py-2">
              <div className="text-xs text-muted-foreground">Выплачено</div>
              <div className="text-sm font-medium tabular-nums">{formatRub(debtInfo.paid)}</div>
            </div>
            <div className="rounded-md bg-emerald-50 px-3 py-2">
              <div className="flex items-center justify-between text-xs text-emerald-700">
                Остаток
                {debtInfo.debt > 0 ? (
                  <button
                    className="font-medium hover:underline"
                    onClick={() => setAmount(String(debtInfo.debt))}
                    type="button"
                  >
                    взять всё
                  </button>
                ) : null}
              </div>
              <div className="text-sm font-semibold tabular-nums text-emerald-800">
                {formatRub(debtInfo.debt)}
              </div>
            </div>
          </div>
        ) : null}

        <SourcePicker
          label="Счёт списания"
          wallets={wallets}
          value={walletId}
          onChange={setWalletId}
        />

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Статья ДДС</span>
            <Select onValueChange={setArticleId} value={articleId}>
              <SelectTrigger>
                <SelectValue placeholder="Выберите статью" />
              </SelectTrigger>
              <SelectContent>
                {articles.map((article) => (
                  <SelectItem key={article.id} value={article.id}>
                    {article.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Дата выплаты</span>
            <Input
              onChange={(event) => setPayoutDate(event.target.value)}
              type="date"
              value={payoutDate}
            />
          </Label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Комментарий</span>
            <Input
              maxLength={210}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Необязательно"
              value={note}
            />
          </Label>
        </div>

        <SummaryPanel tone={tone} total={numericAmount > 0 ? numericAmount : 0}>
          {summary}
        </SummaryPanel>
      </div>
      <FormFooter
        cancel={onClose}
        submit={() => createMutation.mutate()}
        submitLabel="Создать выплату"
        disabled={!canSubmit}
        pending={createMutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Внутренний перевод: направление фиксировано источником — банк → Сейф (черновик
// пополнения), Сейф → Касса, Касса → Сейф. Направление показывает панель.

function TransferPlainForm({
  wallets,
  onDirty,
  onDone,
  onCancel,
}: {
  wallets: NewPaymentWallet[];
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [sourceId, setSourceId] = useState("");
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");

  const dirty = amountOf(amount) > 0;
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!sourceId && tbankWallet) {
      setSourceId(tbankWallet.id);
    }
  }, [sourceId, tbankWallet]);

  const safeWallet = wallets.find((w) => w.kind === "cash" && w.location === "safe") ?? null;
  const kassaWallet = wallets.find((w) => w.kind === "cash" && w.location === "kassa") ?? null;
  const sourceWallet = wallets.find((wallet) => wallet.id === sourceId) ?? null;
  const isBankSource = sourceWallet?.kind === "bank";
  const isSafeSource = sourceWallet?.kind === "cash" && sourceWallet.location === "safe";
  // Направление фиксировано: банк → Сейф, Сейф → Касса, Касса → Сейф. Внесение
  // Сейф→банк из окна не проводим — банковская нога приходит выпиской, разметка
  // перевода создала бы вторую ногу Сейфа (задвоение).
  const destWallet =
    sourceWallet == null
      ? null
      : sourceWallet.kind === "bank"
        ? safeWallet
        : isSafeSource
          ? kassaWallet
          : safeWallet;

  const canSubmit = Boolean(sourceId) && destWallet !== null && amountOf(amount) > 0;
  const submitLabel = isBankSource
    ? "Отправить в банк"
    : isSafeSource
      ? "Перевести в кассу"
      : "Перевести на Сейф";

  const mutation = useMutation({
    mutationFn: () =>
      createNewPaymentInternalTransfer({
        source_wallet_id: sourceId,
        dest_wallet_id: destWallet?.id ?? "",
        amount: amountOf(amount),
        purpose: purpose.trim() || null,
      }),
    onSuccess: async (result) => {
      toast.success(
        result.kind === "draft" ? "Черновик пополнения Сейфа отправлен в банк" : "Перевод проведён",
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выполнить перевод")),
  });

  // Панель «Что произойдёт»: маршрут — жирным первым токеном, вместо поля «Куда».
  const route = `${sourceWallet ? shortWalletName(sourceWallet) : "—"} → ${destWallet ? shortWalletName(destWallet) : "—"}`;
  const tone: SummaryTone = sourceWallet == null ? "warning" : isBankSource ? "draft" : "move";
  const rest = isBankSource
    ? "черновиком; деньги придут на Сейф после оплаты в банке."
    : isSafeSource
      ? "проведётся сразу. Внесение на банковский счёт — разметкой операции из выписки."
      : "проведётся сразу.";

  return (
    <div>
      <FormHeader title="Внутренний перевод" />
      <div className="space-y-3">
        <SourcePicker
          label="Откуда"
          wallets={wallets}
          value={sourceId}
          onChange={setSourceId}
          disabledReason={(wallet) =>
            wallet.kind === "bank" && wallet.bank_code !== "tbank" && wallet.bank_code !== "sber"
              ? "Черновики — только из Т-Банка и Сбера"
              : null
          }
        />

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Назначение</span>
            <Input
              maxLength={210}
              onChange={(event) => setPurpose(event.target.value)}
              placeholder="Необязательно"
              value={purpose}
            />
          </Label>
        </div>

        <SummaryPanel tone={tone} total={amountOf(amount) > 0 ? amountOf(amount) : 0}>
          {sourceWallet == null ? (
            "Выберите счёт-источник."
          ) : (
            <>
              <b>{route}</b> · {rest}
            </>
          )}
        </SummaryPanel>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel={submitLabel}
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}

/**
 * Выбор помещения и арендодателя для строки объектного платежа. Свой запрос, потому что
 * список зависит от статьи строки, а строки рендерятся в цикле.
 *
 * Арендная статья (``leaseBound``): арендодатель обязателен, свободного контрагента нет —
 * выбор договора подставляет арендодателя и его реквизитный контур. Прочие объектные статьи
 * (коммуналка, охрана): аренда опциональна, «Без договора» оставляет свободного контрагента.
 */
function ExpenseLocationPicker({
  articleId,
  leaseBound,
  locationId,
  leaseId,
  onChange,
}: {
  articleId: string;
  leaseBound: boolean;
  locationId: string;
  leaseId: string;
  onChange: (patch: {
    locationId: string;
    leaseId: string;
    counterpartyId?: string;
    leaseRecipient?: LeaseRecipient | null;
  }) => void;
}) {
  const optionsQuery = useQuery(locationOptionsQuery(articleId));
  // Запрос упал — список пуст не потому, что помещений нет. 403 (нет права
  // source.locations.read) отделяем от прочих ошибок: лечится он выдачей доступа, а не повтором.
  const forbidden = apiErrorStatus(optionsQuery.error) === 403;
  const options: LocationOption[] = optionsQuery.data ?? [];
  // Новый платёж создаётся только по действующей точке: закрытые (по ним разбирают исторические
  // выписки в окне разбора операции) в выборе не показываем.
  const activeOptions = options.filter((item) => item.status === "active");
  const selected = activeOptions.find((item) => item.location_id === locationId) ?? null;
  const leases = selected?.leases ?? [];

  const recipientOf = (lease: LocationLeaseOption): LeaseRecipient => ({
    name: lease.counterparty_name,
    relationship: lease.relationship,
    has_requisites: lease.has_requisites,
    requisites_verified: lease.requisites_verified,
    monthlyAmount: lease.monthly_amount,
  });

  const pickLocation = (value: string) =>
    leaseBound
      ? onChange({ locationId: value, leaseId: "", counterpartyId: "", leaseRecipient: null })
      : onChange({ locationId: value, leaseId: "" });

  const pickLandlord = (value: string) => {
    const lease = leases.find((item) => item.lease_id === value);
    onChange({
      locationId,
      leaseId: value,
      counterpartyId: lease?.counterparty_id ?? "",
      leaseRecipient: lease ? recipientOf(lease) : null,
    });
  };

  // Не заставляем выбирать там, где вариант один: единственную действующую точку и (для аренды)
  // её единственного арендодателя подставляем сами — селект остаётся только когда есть из чего
  // выбирать. Guard на пустое поле делает эффект идемпотентным.
  useEffect(() => {
    if (!optionsQuery.isSuccess) return;
    if (!locationId && activeOptions.length === 1) {
      const only = activeOptions[0];
      const singleLease = leaseBound && only.leases.length === 1 ? only.leases[0] : null;
      if (leaseBound) {
        onChange({
          locationId: only.location_id,
          leaseId: singleLease?.lease_id ?? "",
          counterpartyId: singleLease?.counterparty_id ?? "",
          leaseRecipient: singleLease ? recipientOf(singleLease) : null,
        });
      } else {
        onChange({ locationId: only.location_id, leaseId: "" });
      }
      return;
    }
    if (leaseBound && locationId && !leaseId && leases.length === 1) {
      const only = leases[0];
      onChange({
        locationId,
        leaseId: only.lease_id,
        counterpartyId: only.counterparty_id,
        leaseRecipient: recipientOf(only),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [optionsQuery.isSuccess, locationId, leaseId, leaseBound]);

  return (
    <div className="rounded-md bg-muted/40 p-2">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <Label className="text-xs font-medium">
          {leaseBound ? "Помещение и арендодатель" : "Помещение"}
        </Label>
        <span className="text-[11px] text-muted-foreground">обязательное поле</span>
      </div>
      <div className="grid gap-2">
        {optionsQuery.isError ? (
          <p className="text-[11px] text-destructive">
            {forbidden ? (
              LOCATIONS_FORBIDDEN_HINT
            ) : (
              <>
                Не удалось загрузить помещения:{" "}
                {apiErrorMessage(optionsQuery.error, "ошибка запроса")}.{" "}
                <button
                  className="underline"
                  onClick={() => void optionsQuery.refetch()}
                  type="button"
                >
                  Повторить
                </button>
              </>
            )}
          </p>
        ) : optionsQuery.isLoading ? (
          <p className="text-[11px] text-muted-foreground">Загружаем помещения…</p>
        ) : activeOptions.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            Нет действующих помещений. Заведите его в Настройках → Помещения.
          </p>
        ) : activeOptions.length > 1 ? (
          <Select
            value={locationId || "none"}
            onValueChange={(value) => pickLocation(value === "none" ? "" : value)}
          >
            <SelectTrigger className="h-8 text-sm">
              <SelectValue placeholder="Выберите помещение" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">Помещение: не выбрано</SelectItem>
              {activeOptions.map((item) => (
                <SelectItem key={item.location_id} value={item.location_id}>
                  {item.location_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <p className="text-xs text-muted-foreground">
            Помещение:{" "}
            <span className="font-medium text-foreground">{activeOptions[0]?.location_name}</span>
          </p>
        )}

        {leaseBound && selected ? (
          leases.length > 1 ? (
            <Select value={leaseId || undefined} onValueChange={pickLandlord}>
              <SelectTrigger className="h-8 text-sm">
                <SelectValue placeholder="Выберите арендодателя" />
              </SelectTrigger>
              <SelectContent>
                {leases.map((lease) => (
                  <SelectItem key={lease.lease_id} value={lease.lease_id}>
                    {lease.counterparty_name} ·{" "}
                    {Math.round(lease.monthly_amount).toLocaleString("ru-RU")} ₽/мес
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : leases.length === 1 ? (
            <p className="text-xs text-muted-foreground">
              Арендодатель:{" "}
              <span className="font-medium text-foreground">{leases[0].counterparty_name}</span> ·{" "}
              {Math.round(leases[0].monthly_amount).toLocaleString("ru-RU")} ₽/мес
            </p>
          ) : (
            <p className="text-[11px] text-muted-foreground">
              У помещения нет арендодателей. Заведите аренду в карточке помещения (Настройки →
              Помещения).
            </p>
          )
        ) : null}

        {!leaseBound && selected && leases.length > 0 ? (
          <Select
            value={leaseId || "none"}
            onValueChange={(value) =>
              value === "none" ? onChange({ locationId, leaseId: "" }) : pickLandlord(value)
            }
          >
            <SelectTrigger className="h-8 text-sm">
              <SelectValue placeholder="Договор аренды" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">Без договора (прочий расход по помещению)</SelectItem>
              {leases.map((lease) => (
                <SelectItem key={lease.lease_id} value={lease.lease_id}>
                  {lease.counterparty_name} ·{" "}
                  {Math.round(lease.monthly_amount).toLocaleString("ru-RU")} ₽/мес
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
      </div>
    </div>
  );
}
