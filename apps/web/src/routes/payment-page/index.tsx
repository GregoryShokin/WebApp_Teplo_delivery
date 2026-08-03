import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, FileText, RefreshCw, RotateCcw, Send, Trash2, Upload, X } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiErrorMessage } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { formatDate, formatRub, MetricCard } from "@/routes/counterparties/shared";

import {
  deleteIntake,
  excludeIntake,
  fetchIntakeFileUrl,
  ignoreIntake,
  listCounterpartyOptions,
  listIntakes,
  restoreIntake,
  sendManyToBank,
  uploadIntake,
  type PaymentIntake,
} from "./api";
import { ReviewDialog } from "./ReviewDialog";
import { SendDialog } from "./SendDialog";
import { UtilityDialog } from "./UtilityDialog";

const STATUS_LABELS: Record<string, string> = {
  new: "Новый",
  recognized: "Распознан",
  needs_review: "Требует проверки",
  linked: "Готов к оплате",
  // Закрывающий документ (УПД/акт): не платится напрямую, живёт в учёте ДЗ/КЗ.
  closing: "Закрывающий (ДЗ/КЗ)",
  duplicate: "Дубль",
  failed: "Ошибка",
  ignored: "Не счёт",
  excluded: "Исключён",
};

const STATUS_BADGE: Record<string, string> = {
  linked: "border-emerald-200 bg-emerald-50 text-emerald-700",
  closing: "border-violet-200 bg-violet-50 text-violet-700",
  needs_review: "border-amber-200 bg-amber-50 text-amber-700",
  duplicate: "border-sky-200 bg-sky-50 text-sky-700",
  ignored: "border-muted bg-muted text-muted-foreground",
  excluded: "border-rose-200 bg-rose-50 text-rose-700",
  failed: "border-red-200 bg-red-50 text-red-700",
  new: "border-slate-200 bg-slate-50 text-slate-700",
  recognized: "border-slate-200 bg-slate-50 text-slate-700",
};

const ENGINE_LABELS: Record<string, string> = {
  deterministic: "регекс",
  llm: "LLM",
  "deterministic+llm": "регекс+LLM",
};

const FILTER_OPTIONS: Array<{ value: string; label: string }> = [
  // Очередь оплат отвечает на вопрос «что ещё предстоит заплатить». Оплаченное на этот
  // вопрос не отвечает: у статуса linked внутри три разных состояния (готов / в банке /
  // оплачен), и, пока они лежали вперемешку, список рос, а работы в нём не прибавлялось.
  // Оплаченные теперь живут за своим фильтром — как история, а не как очередь.
  { value: "active", label: "Актуальные" },
  { value: "needs_review", label: "Требуют проверки" },
  { value: "linked", label: "Готовы к оплате" },
  { value: "in_bank", label: "Отправлены в банк" },
  { value: "paid", label: "Оплаченные" },
  { value: "closing", label: "Закрывающие (ДЗ/КЗ)" },
  { value: "duplicate", label: "Дубли" },
  { value: "ignored", label: "Не счета" },
  { value: "excluded", label: "Исключённые" },
  { value: "failed", label: "Ошибки" },
  { value: "all", label: "Все" },
];

/** Оплачен ли счёт строки. Статус intake остаётся `linked` и после оплаты — платёж живёт
 *  на связанной накладной, поэтому «оплачено» читается только отсюда. */
function isPaid(item: PaymentIntake): boolean {
  return item.invoice_payment_status === "paid";
}

function contractorOf(item: PaymentIntake): string {
  return (
    item.counterparty_name ||
    item.recipient_name ||
    (item.inn ? `ИНН ${item.inn}` : null) ||
    item.from_addr ||
    "—"
  );
}

// Для linked-счёта показываем стадию оплаты: готов / отправлен в банк / оплачен.
function statusBadge(item: PaymentIntake): { label: string; cls: string } {
  if (item.status === "linked") {
    if (item.invoice_payment_status === "paid") {
      return { label: "Оплачен", cls: "border-emerald-200 bg-emerald-50 text-emerald-700" };
    }
    if (item.invoice_in_draft) {
      return { label: "Отправлен в банк", cls: "border-sky-200 bg-sky-50 text-sky-700" };
    }
  }
  return { label: STATUS_LABELS[item.status] ?? item.status, cls: STATUS_BADGE[item.status] ?? "" };
}

/** Журнал входящих счетов на оплату — вкладка «Активные» страницы «Платежи».
 *
 *  Жил отдельной страницей «Страница на оплату» и переехал сюда целиком (решение
 *  владельца 03.08): это основной модуль обработки платёжек из ЭДО СБИС, почты и
 *  телеграм-бота, а прежняя вкладка «Активные» показывала те же счета витриной без
 *  единого действия — две правды об одной строке в соседних пунктах меню. */
export function PaymentIntakesTab() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  // Все действия журнала (разбор, «В банк», исключение, удаление, загрузка) на бэкенде под
  // counterparties.operate. Читать очередь может и финансовая роль — ей кнопки не показываем,
  // иначе каждая даёт 403 там, где человек ничего неправильного не сделал.
  const canOperate = permissions.canPerformAction("counterparties.operate");
  const [filter, setFilter] = useState("active");
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [reviewItem, setReviewItem] = useState<PaymentIntake | null>(null);
  const [utilityItem, setUtilityItem] = useState<PaymentIntake | null>(null);
  const [sendItem, setSendItem] = useState<PaymentIntake | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const intakesQuery = useQuery({
    queryKey: ["payment-page", "intakes"],
    queryFn: () => listIntakes(),
  });
  const counterpartiesQuery = useQuery({
    queryKey: ["payment-page", "counterparties"],
    queryFn: listCounterpartyOptions,
  });

  // react-query держит сетевой сбой в состоянии paused (isError=false), поэтому одного isError
  // мало: мёртвый сервер иначе притворяется пустой очередью.
  const loadFailed =
    (intakesQuery.isError || intakesQuery.fetchStatus === "paused") &&
    intakesQuery.data === undefined;

  const all = useMemo(() => intakesQuery.data ?? [], [intakesQuery.data]);
  const rows = useMemo(() => {
    if (filter === "all") return all;
    // «Актуальные» = то, что ещё требует рук или денег. Оплаченное сюда не входит: счёт,
    // за который заплатили, работы больше не ждёт, а очередь он удлиняет — и чем дольше
    // работает система, тем сильнее (оплаченные копятся, неоплаченные — нет).
    if (filter === "active") {
      return all.filter(
        (item) =>
          (item.status === "needs_review" || item.status === "linked") && !isPaid(item),
      );
    }
    // Три состояния одного статуса linked разведены по своим фильтрам: платить нечего,
    // если счёт уже в банке, и тем более если он оплачен.
    if (filter === "linked") {
      return all.filter(
        (item) => item.status === "linked" && !isPaid(item) && !item.invoice_in_draft,
      );
    }
    if (filter === "in_bank") {
      return all.filter(
        (item) => item.status === "linked" && !isPaid(item) && item.invoice_in_draft,
      );
    }
    if (filter === "paid") {
      return all.filter(isPaid);
    }
    return all.filter((item) => item.status === filter);
  }, [all, filter]);

  const metrics = useMemo(() => {
    const by = (status: string) => all.filter((item) => item.status === status).length;
    return {
      total: all.length,
      review: by("needs_review"),
      // Ровно то, что показывает одноимённый фильтр: счёт, по которому можно нажать «В банк».
      // Оплаченный денег уже не ждёт, ушедший в банк ждать нечего — он ждёт банк.
      linked: all.filter(
        (item) => item.status === "linked" && !isPaid(item) && !item.invoice_in_draft,
      ).length,
      noise: by("duplicate") + by("ignored"),
    };
  }, [all]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["payment-page", "intakes"] });

  const ignoreMutation = useMutation({
    mutationFn: (id: string) => ignoreIntake(id),
    onMutate: (id: string) => setPendingId(id),
    onSuccess: () => {
      void invalidate();
      toast.success("Помечено как не счёт");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось пометить")),
    onSettled: () => setPendingId(null),
  });

  const excludeMutation = useMutation({
    mutationFn: (id: string) => excludeIntake(id),
    onMutate: (id: string) => setPendingId(id),
    onSuccess: () => {
      void invalidate();
      toast.success("Перенесён в «Исключённые»");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось исключить")),
    onSettled: () => setPendingId(null),
  });

  const restoreMutation = useMutation({
    mutationFn: (id: string) => restoreIntake(id),
    onMutate: (id: string) => setPendingId(id),
    onSuccess: () => {
      void invalidate();
      toast.success("Возвращён в рабочий список");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось вернуть")),
    onSettled: () => setPendingId(null),
  });

  // Один платёж = один получатель, поэтому выбор ограничен одним контрагентом. Сбрасываем
  // прежний выбор молча: объяснять «нельзя» там, где человек явно передумал, — лишний диалог.
  function toggleSelected(item: PaymentIntake) {
    setSelected((current) => {
      if (current.includes(item.id)) return current.filter((id) => id !== item.id);
      const others = all.filter((row) => current.includes(row.id));
      const sameCounterparty = others.every(
        (row) => row.counterparty_id === item.counterparty_id,
      );
      return sameCounterparty ? [...current, item.id] : [item.id];
    });
  }

  const selectedTotal = useMemo(
    () =>
      all
        .filter((item) => selected.includes(item.id))
        .reduce((sum, item) => sum + Number(item.amount ?? 0), 0),
    [all, selected],
  );

  const sendManyMutation = useMutation({
    mutationFn: (ids: string[]) => sendManyToBank(ids),
    onSuccess: (items) => {
      void invalidate();
      setSelected([]);
      toast.success(`Один платёж на ${items.length} счёта отправлен в банк`);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отправить")),
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadIntake(file),
    onSuccess: (items) => {
      void invalidate();
      // Один файл может дать несколько строк: энергетик за визит выдаёт два акта. Говорим,
      // сколько именно вышло, — иначе человек ищет вторую строку и не понимает, откуда она.
      const needsHands = items.filter((item) => item.status === "needs_review").length;
      const duplicates = items.filter((item) => item.status === "duplicate").length;
      if (duplicates === items.length) {
        toast.info("За этот месяц документ уже заведён — новый долг не создан");
      } else if (needsHands > 0) {
        toast.warning(
          items.length > 1
            ? `Загружено документов: ${items.length}, из них требуют проверки: ${needsHands}`
            : "Разобрать до конца не вышло — откройте «Разобрать»",
        );
      } else {
        toast.success(
          items.length > 1
            ? `Готово: ${items.length} документа в очереди оплат`
            : "Готово: документ в очереди оплат",
        );
      }
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось загрузить файл")),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteIntake(id),
    onMutate: (id: string) => setPendingId(id),
    onSuccess: () => {
      void invalidate();
      toast.success("Удалён навсегда");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить")),
    onSettled: () => setPendingId(null),
  });

  async function handlePdf(item: PaymentIntake) {
    // Окно открываем СИНХРОННО (внутри жеста клика) — иначе после await браузер блокирует попап.
    const win = window.open("", "_blank");
    try {
      const url = await fetchIntakeFileUrl(item.id);
      if (win && !win.closed) {
        win.location.href = url;
      } else {
        // Попап всё же заблокирован — скачиваем файл.
        const link = document.createElement("a");
        link.href = url;
        link.download = item.attachment_filename || "invoice.pdf";
        document.body.appendChild(link);
        link.click();
        link.remove();
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      win?.close();
      toast.error(apiErrorMessage(error, "Не удалось открыть PDF"));
    }
  }

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="max-w-2xl text-sm text-muted-foreground">
          Счета от непроизводственных контрагентов (услуги, маркетинг, подписки): распознанные
          контрагент и сумма. Склад не затрагивают. Приходят почтой, по ЭДО и фотографией из рук.
          Подтвердите счёт — он будет готов к оплате в банк прямо отсюда.
        </p>
        <div className="flex items-center gap-2">
          {/* Третий источник рядом с почтой и ЭДО: коммунальную квитанцию приносят снимком. */}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,application/pdf"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              // Сбрасываем значение сразу: без этого повторный выбор ТОГО ЖЕ файла не вызовет
              // onChange, и человек решит, что кнопка сломалась.
              event.target.value = "";
              if (file) uploadMutation.mutate(file);
            }}
          />
          {canOperate ? (
            <Button
              variant="outline"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadMutation.isPending}
            >
              <Upload className="h-4 w-4" aria-hidden="true" />
              {uploadMutation.isPending ? "Распознаём…" : "Загрузить документ"}
            </Button>
          ) : null}
          <Button
            variant="outline"
            onClick={() => void intakesQuery.refetch()}
            disabled={intakesQuery.isFetching}
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            Обновить
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricCard label="Всего документов" value={String(metrics.total)} />
        <MetricCard label="Требуют проверки" value={String(metrics.review)} accent="info" />
        <MetricCard label="Готовы к оплате" value={String(metrics.linked)} />
        <MetricCard label="Дубли / не счета" value={String(metrics.noise)} />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm text-muted-foreground">Фильтр</span>
        <Select value={filter} onValueChange={setFilter}>
          <SelectTrigger className="w-[220px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {FILTER_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* Один перевод на несколько счетов: энергетик привозит два акта — доплату за прошлый
            месяц и аванс за текущий, — а платятся они одной суммой одному получателю. */}
        {selected.length > 1 ? (
          <div className="ml-auto flex items-center gap-2">
            <span className="text-sm text-muted-foreground">
              Выбрано {selected.length} на {formatRub(selectedTotal)}
            </span>
            <Button
              size="sm"
              disabled={sendManyMutation.isPending}
              onClick={() => sendManyMutation.mutate(selected)}
            >
              <Send className="h-4 w-4" aria-hidden="true" />
              Оплатить вместе
            </Button>
          </div>
        ) : null}
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            {/* Ширины заданы явно: без них восемь колонок делят место поровну, «Статус» и
                «№ счёта» переносятся на две строки, а вслед за ними тянется вся строка —
                на широком мониторе список из пяти счетов занимал целый экран. */}
            <TableRow>
              <TableHead className="w-8" />
              <TableHead className="w-[132px]">Статус</TableHead>
              <TableHead className="w-[104px]">Дата</TableHead>
              <TableHead>Контрагент</TableHead>
              <TableHead className="w-[128px] text-right">Сумма</TableHead>
              <TableHead className="w-[124px]">№ счёта</TableHead>
              <TableHead className="w-[116px]">Распознавание</TableHead>
              <TableHead className="w-[224px] text-right">Действия</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {intakesQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={8} className="py-8 text-center text-muted-foreground">
                  Загрузка…
                </TableCell>
              </TableRow>
            ) : loadFailed ? (
              // Пустая очередь и недоступная очередь — разные состояния. Раньше вкладка была
              // отдельной страницей, куда заходили осознанно; теперь она открывается по умолчанию,
              // и «Пусто» вместо 403/500 читается как «всё оплачено».
              <TableRow>
                <TableCell colSpan={8} className="py-8 text-center text-destructive">
                  Не удалось загрузить счета:{" "}
                  {apiErrorMessage(intakesQuery.error, "ошибка запроса")}.{" "}
                  <button
                    className="underline"
                    onClick={() => void intakesQuery.refetch()}
                    type="button"
                  >
                    Повторить
                  </button>
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="py-8 text-center text-muted-foreground">
                  Пусто. Счета приходят почтой и по ЭДО, а квитанцию можно принести кнопкой «Загрузить документ».
                </TableCell>
              </TableRow>
            ) : (
              rows.map((item) => {
                const busy = pendingId === item.id;
                // «В работе» = неразобранные + готовый-к-оплате, ещё не ушедший в банк.
                // canOperate во всех трёх признаках: у финансовой роли право на чтение очереди
                // есть, а на действия — нет, и показывать ей кнопки значит обещать 403.
                const inWork =
                  canOperate &&
                  (["needs_review", "new", "failed"].includes(item.status) ||
                    (item.status === "linked" && !item.invoice_in_draft));
                // Готов к отправке/планированию: статус linked, ещё не ушёл в банк И НЕ ОПЛАЧЕН.
                // Оплатить счёт можно мимо очереди — свободным платежом из выписки, наличными из
                // Сейфа, ручной сверкой операции. Бейдж такую строку уже показывает как
                // «Оплачен», а кнопка «В банк» на ней оставалась: бэкенд платёж отбивал
                // («есть оплаченные накладные»), но человеку предлагалось заплатить второй раз.
                const isReadyRow =
                  canOperate &&
                  item.status === "linked" &&
                  !item.invoice_in_draft &&
                  item.invoice_payment_status !== "paid";
                return (
                  <TableRow key={item.id}>
                    <TableCell>
                      {/* Отмечать можно только готовые к оплате: остальные платить нечем. */}
                      {isReadyRow ? (
                        <Checkbox
                          checked={selected.includes(item.id)}
                          onChange={() => toggleSelected(item)}
                          aria-label="Выбрать для общего платежа"
                        />
                      ) : null}
                    </TableCell>
                    <TableCell>
                      {(() => {
                        const b = statusBadge(item);
                        return (
                          <Badge className={`${b.cls} whitespace-nowrap`}>{b.label}</Badge>
                        );
                      })()}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-sm">
                      {formatDate(item.invoice_date ?? item.received_at ?? item.created_at)}
                    </TableCell>
                    <TableCell>
                      <div className="font-medium">{contractorOf(item)}</div>
                      {item.subject ? (
                        <div className="max-w-[280px] truncate text-xs text-muted-foreground">
                          {item.subject}
                        </div>
                      ) : null}
                      {item.companion_invoice_id ? (
                        // В файле пришёл пакет: счёт (в очереди оплат) + закрывающий УПД,
                        // который уже проведён в учёте ДЗ/КЗ отдельным документом.
                        <div className="text-xs text-muted-foreground">
                          + закрывающий документ
                          {item.companion_amount ? ` на ${formatRub(item.companion_amount)}` : ""}
                        </div>
                      ) : null}
                      {item.utility_kind_label ? (
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          <Badge className="border-cyan-200 bg-cyan-50 text-cyan-700">
                            {item.utility_kind_label}
                            {item.utility_act_kind === "advance" ? " · аванс" : ""}
                          </Badge>
                          {/* Расход месяца показываем ТОЛЬКО когда он расходится с платежом:
                              иначе строка врала бы, что чисел два, там где оно одно. */}
                          {item.utility_expense_amount &&
                          item.utility_expense_amount !== item.utility_payable_amount ? (
                            <span className="text-xs text-muted-foreground">
                              расход {formatRub(item.utility_expense_amount)}
                            </span>
                          ) : null}
                        </div>
                      ) : null}
                      {item.utility_hints?.length ? (
                        <div className="text-xs text-amber-700">{item.utility_hints.join(". ")}</div>
                      ) : null}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-right tabular-nums">
                      {item.amount ? formatRub(item.amount) : "—"}
                    </TableCell>
                    {/* Номер счёта не переносим: «0000-003174» ломался пополам и один тянул
                        строку на вторую высоту. Длинный — обрезаем, полный виден в подсказке. */}
                    <TableCell className="text-sm">
                      <div className="truncate" title={item.invoice_number ?? undefined}>
                        {item.invoice_number ?? "—"}
                      </div>
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {item.engine ? ENGINE_LABELS[item.engine] ?? item.engine : "—"}
                      {item.confidence != null ? ` · ${Math.round(item.confidence * 100)}%` : ""}
                    </TableCell>
                    <TableCell>
                      {/* nowrap: кнопки уезжали на вторую строку под «Разобрать» и удваивали
                          высоту каждой строки очереди. */}
                      <div className="flex flex-nowrap items-center justify-end gap-1">
                        {item.has_pdf ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            title="Открыть PDF"
                            onClick={() => void handlePdf(item)}
                          >
                            <FileText className="h-4 w-4" aria-hidden="true" />
                          </Button>
                        ) : null}

                        {item.status === "excluded" && canOperate ? (
                          <>
                            {/* Корзина «Исключённые»: вернуть в работу или удалить навсегда. */}
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              onClick={() => restoreMutation.mutate(item.id)}
                            >
                              <RotateCcw className="h-4 w-4" aria-hidden="true" />
                              Вернуть
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="text-red-600 hover:text-red-700"
                              title="Удалить навсегда"
                              disabled={busy}
                              onClick={() => {
                                if (window.confirm("Удалить счёт навсегда? Действие необратимо."))
                                  deleteMutation.mutate(item.id);
                              }}
                            >
                              <Trash2 className="h-4 w-4" aria-hidden="true" />
                            </Button>
                          </>
                        ) : (
                          <>
                            {/* Разобрать: для неразобранных и для готового, пока не ушёл в банк. */}
                            {inWork ? (
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() =>
                                  // У коммунальной платёжки свой разбор: там поток, период и две
                                  // суммы, а контрагента менять нельзя — он задан потоком.
                                  item.utility_kind
                                    ? setUtilityItem(item)
                                    : setReviewItem(item)
                                }
                              >
                                Разобрать
                              </Button>
                            ) : null}

                            {isReadyRow ? (
                              <>
                                {item.scheduled_send_date ? (
                                  <Badge className="border-violet-200 bg-violet-50 text-violet-700">
                                    → банк {formatDate(item.scheduled_send_date)}
                                  </Badge>
                                ) : null}
                                {/* Единая кнопка на всех готовых строках → окно отправки с выбором
                                    даты (реквизиты сверяются и подтверждаются прямо там). */}
                                <Button
                                  size="sm"
                                  title="Отправить в банк"
                                  onClick={() => setSendItem(item)}
                                >
                                  <Send className="h-4 w-4" aria-hidden="true" />В банк
                                </Button>
                              </>
                            ) : null}

                            {/* Исключить рабочий счёт в «Исключённые» (решение «не платим»). */}
                            {inWork ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                title="Исключить из оплаты"
                                disabled={busy}
                                onClick={() => excludeMutation.mutate(item.id)}
                              >
                                <Ban className="h-4 w-4" aria-hidden="true" />
                              </Button>
                            ) : null}

                            {canOperate &&
                            ["needs_review", "new", "failed"].includes(item.status) ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                title="Не счёт"
                                disabled={busy}
                                onClick={() => ignoreMutation.mutate(item.id)}
                              >
                                <X className="h-4 w-4" aria-hidden="true" />
                              </Button>
                            ) : null}
                          </>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {reviewItem ? (
        <ReviewDialog
          intake={reviewItem}
          counterpartyOptions={counterpartiesQuery.data ?? []}
          onClose={() => setReviewItem(null)}
        />
      ) : null}

      {utilityItem ? (
        <UtilityDialog intake={utilityItem} onClose={() => setUtilityItem(null)} />
      ) : null}

      {sendItem ? (
        <SendDialog intake={sendItem} onClose={() => setSendItem(null)} />
      ) : null}
    </div>
  );
}
