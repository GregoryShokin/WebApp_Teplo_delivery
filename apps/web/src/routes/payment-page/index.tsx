import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, RefreshCw, Send, X } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { formatDate, formatRub, MetricCard } from "@/routes/counterparties/shared";

import {
  fetchIntakePdfUrl,
  ignoreIntake,
  listCounterpartyOptions,
  listIntakes,
  sendToBank,
  type PaymentIntake,
} from "./api";
import { ReviewDialog } from "./ReviewDialog";

const STATUS_LABELS: Record<string, string> = {
  new: "Новый",
  recognized: "Распознан",
  needs_review: "Требует проверки",
  linked: "Готов к оплате",
  duplicate: "Дубль",
  failed: "Ошибка",
  ignored: "Не счёт",
};

const STATUS_BADGE: Record<string, string> = {
  linked: "border-emerald-200 bg-emerald-50 text-emerald-700",
  needs_review: "border-amber-200 bg-amber-50 text-amber-700",
  duplicate: "border-sky-200 bg-sky-50 text-sky-700",
  ignored: "border-muted bg-muted text-muted-foreground",
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
  // По умолчанию показываем только актуальное; повторы и «не счета» — шум, прячем за фильтром.
  { value: "active", label: "Актуальные" },
  { value: "needs_review", label: "Требуют проверки" },
  { value: "linked", label: "Готовы к оплате" },
  { value: "duplicate", label: "Дубли" },
  { value: "ignored", label: "Не счета" },
  { value: "failed", label: "Ошибки" },
  { value: "all", label: "Все" },
];

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

// onNavigate придёт в дело в Фазе 3 (оплата со страницы); сейчас не используется.
export function PaymentPageRoute(_props: { onNavigate: (path: string) => void }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("active");
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [reviewItem, setReviewItem] = useState<PaymentIntake | null>(null);

  const intakesQuery = useQuery({
    queryKey: ["payment-page", "intakes"],
    queryFn: () => listIntakes(),
  });
  const counterpartiesQuery = useQuery({
    queryKey: ["payment-page", "counterparties"],
    queryFn: listCounterpartyOptions,
  });

  const all = useMemo(() => intakesQuery.data ?? [], [intakesQuery.data]);
  const rows = useMemo(() => {
    if (filter === "all") return all;
    // «Актуальные» = то, что реально требует внимания или ждёт оплаты; повторы/«не счета» скрыты.
    if (filter === "active") {
      return all.filter((item) => item.status === "needs_review" || item.status === "linked");
    }
    return all.filter((item) => item.status === filter);
  }, [all, filter]);

  const metrics = useMemo(() => {
    const by = (status: string) => all.filter((item) => item.status === status).length;
    return {
      total: all.length,
      review: by("needs_review"),
      linked: by("linked"),
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

  const sendMutation = useMutation({
    mutationFn: (id: string) => sendToBank(id),
    onMutate: (id: string) => setPendingId(id),
    onSuccess: () => {
      void invalidate();
      toast.success("Отправлено в банк — ожидает подтверждения");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отправить в банк")),
    onSettled: () => setPendingId(null),
  });

  async function handlePdf(item: PaymentIntake) {
    // Окно открываем СИНХРОННО (внутри жеста клика) — иначе после await браузер блокирует попап.
    const win = window.open("", "_blank");
    try {
      const url = await fetchIntakePdfUrl(item.id);
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
        <div>
          <h1 className="text-2xl font-medium">Страница на оплату</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Счета на оплату от непроизводственных контрагентов (услуги, маркетинг, подписки):
            распознанные контрагент и сумма. Склад не затрагивают. Подтвердите счёт — он будет
            готов к оплате в банк прямо отсюда.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => void intakesQuery.refetch()}
          disabled={intakesQuery.isFetching}
        >
          <RefreshCw className="h-4 w-4" aria-hidden="true" />
          Обновить
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricCard label="Всего из почты" value={String(metrics.total)} />
        <MetricCard label="Требуют проверки" value={String(metrics.review)} accent="info" />
        <MetricCard label="Готовы к оплате" value={String(metrics.linked)} />
        <MetricCard label="Дубли / не счета" value={String(metrics.noise)} />
      </div>

      <div className="flex items-center gap-3">
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
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Статус</TableHead>
              <TableHead>Дата</TableHead>
              <TableHead>Контрагент</TableHead>
              <TableHead className="text-right">Сумма</TableHead>
              <TableHead>№ счёта</TableHead>
              <TableHead>Распознавание</TableHead>
              <TableHead className="text-right">Действия</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {intakesQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                  Загрузка…
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                  Пусто. Новые счета появятся после опроса почты.
                </TableCell>
              </TableRow>
            ) : (
              rows.map((item) => {
                const busy = pendingId === item.id;
                return (
                  <TableRow key={item.id}>
                    <TableCell>
                      {(() => {
                        const b = statusBadge(item);
                        return <Badge className={b.cls}>{b.label}</Badge>;
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
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {item.amount ? formatRub(item.amount) : "—"}
                    </TableCell>
                    <TableCell className="text-sm">{item.invoice_number ?? "—"}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {item.engine ? ENGINE_LABELS[item.engine] ?? item.engine : "—"}
                      {item.confidence != null ? ` · ${Math.round(item.confidence * 100)}%` : ""}
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
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
                        {/* Разобрать: для неразобранных, а также для готового, пока не ушёл в банк
                            (поправить/подтвердить реквизиты). */}
                        {["needs_review", "new", "failed"].includes(item.status) ||
                        (item.status === "linked" && !item.invoice_in_draft) ? (
                          <Button size="sm" variant="outline" onClick={() => setReviewItem(item)}>
                            Разобрать
                          </Button>
                        ) : null}
                        {item.status === "linked" &&
                        !item.invoice_in_draft &&
                        item.requisites_verified ? (
                          <Button
                            size="sm"
                            disabled={busy}
                            onClick={() => sendMutation.mutate(item.id)}
                          >
                            <Send className="h-4 w-4" aria-hidden="true" />
                            Отправить в банк
                          </Button>
                        ) : null}
                        {["needs_review", "new", "failed"].includes(item.status) ? (
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
    </div>
  );
}
