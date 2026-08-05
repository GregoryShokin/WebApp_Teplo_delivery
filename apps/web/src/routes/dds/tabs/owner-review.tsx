import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  apiErrorMessage,
  applyCardRefundCase,
  classifyOwnerReviewCase,
  confirmIikoManualCase,
  dismissOwnerReviewCase,
  getDdsArticles,
  getDdsOwnerReview,
  retryIikoPaymentCase,
  type ClassifyPayload,
  type DdsArticleRead,
  type OwnerReviewKind,
  type ReconciliationCaseRead,
} from "@/lib/api";
import {
  createCounterparty,
  getCounterpartyDirectory,
  type CounterpartyDirectoryItem,
} from "@/routes/counterparties/api";
import {
  DirectionBadge,
  PaginationControls,
  ProviderBadge,
  compactText,
  formatDate,
  formatDdsMoney,
} from "@/routes/dds/shared";

const LIMIT = 20;

export function OwnerReviewTab({ canClassify }: { canClassify: boolean }) {
  const [kind, setKind] = useState<OwnerReviewKind | "all">("all");
  const [offset, setOffset] = useState(0);
  const ownerReviewQuery = useQuery({
    queryKey: ["dds", "owner-review", kind, offset],
    queryFn: () => getDdsOwnerReview({ kind, limit: LIMIT, offset }),
  });
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["cp", "directory"],
    queryFn: getCounterpartyDirectory,
  });

  return (
    <div className="space-y-5">
      <div className="max-w-xs">
        <Label>Тип кейса</Label>
        <Select
          value={kind}
          onValueChange={(value) => {
            setKind(value as OwnerReviewKind | "all");
            setOffset(0);
          }}
        >
          <SelectTrigger className="mt-2">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Все</SelectItem>
            <SelectItem value="unclassified_operation">Неразмеченная операция</SelectItem>
            <SelectItem value="invalid_credentials">Проблема с доступом</SelectItem>
            <SelectItem value="unmatched_transfer">Найти перевод</SelectItem>
            <SelectItem value="unconfirmed_cheque">Чек без подтверждения банком</SelectItem>
            <SelectItem value="payer_wallet_unresolved">
              Не определён банк-счёт плательщика
            </SelectItem>
            <SelectItem value="iiko_payment_unsettled">Оплата в iiko не проведена</SelectItem>
            <SelectItem value="iiko_cash_payout_unsettled">
              Выдача не отражена в кассе iiko
            </SelectItem>
            <SelectItem value="card_refund_after_cheque">Возврат по проведённому чеку</SelectItem>
            <SelectItem value="cheque_refund_missing">Возврат не пришёл от банка</SelectItem>
            <SelectItem value="deposit_bank_draft_failed">
              Депозит выдан, а платёж в банк не ушёл
            </SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-4">
        {ownerReviewQuery.isLoading
          ? Array.from({ length: 3 }).map((_, index) => (
              <Card key={index}>
                <CardContent className="grid gap-3 p-5">
                  <div className="h-5 w-40 rounded bg-muted" />
                  <div className="h-20 rounded bg-muted" />
                  <div className="h-10 rounded bg-muted" />
                </CardContent>
              </Card>
            ))
          : (ownerReviewQuery.data?.items ?? []).map((item) => (
              <OwnerReviewCard
                articles={articlesQuery.data ?? []}
                canClassify={canClassify}
                counterparties={counterpartiesQuery.data ?? []}
                item={item}
                key={item.id}
              />
            ))}
      </div>

      {!ownerReviewQuery.isLoading && (ownerReviewQuery.data?.items.length ?? 0) === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          Очередь owner review пуста.
        </div>
      ) : null}

      <PaginationControls
        limit={LIMIT}
        offset={offset}
        total={ownerReviewQuery.data?.total ?? 0}
        onOffsetChange={setOffset}
      />
    </div>
  );
}

function OwnerReviewCard({
  articles,
  canClassify,
  counterparties,
  item,
}: {
  articles: DdsArticleRead[];
  canClassify: boolean;
  counterparties: CounterpartyDirectoryItem[];
  item: ReconciliationCaseRead;
}) {
  const queryClient = useQueryClient();
  const operation = item.operation;
  const hasOperation = operation != null;
  const [articleId, setArticleId] = useState("none");
  const [counterpartyId, setCounterpartyId] = useState("none");
  const [rememberAsRule, setRememberAsRule] = useState(false);
  const [isCreateCounterpartyOpen, setIsCreateCounterpartyOpen] = useState(false);
  const [newCounterpartyName, setNewCounterpartyName] = useState(
    operation?.counterparty_name_raw ?? "",
  );
  const [newCounterpartyInn, setNewCounterpartyInn] = useState(
    operation?.counterparty_inn_raw ?? "",
  );
  // Выбор чека для неоднозначного возврата (несколько чеков ждут ту же сумму).
  const refundCandidates = Array.isArray(item.payload?.candidates)
    ? (item.payload.candidates as Array<{ invoice_id: string; cheque_number: string | null }>)
    : [];
  const [chosenChequeId, setChosenChequeId] = useState(
    refundCandidates.length === 1 ? refundCandidates[0].invoice_id : "none",
  );

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds", "owner-review"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "operations"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "classification-rules"] }),
    ]);
  };

  const classifyMutation = useMutation({
    mutationFn: (payload: ClassifyPayload) => classifyOwnerReviewCase(item.id, payload),
    onSuccess: async (result, payload) => {
      await invalidate();
      const remembered = payload.remember_as_rule && !result?.rule_warning;
      toast.success(remembered ? "Классифицировано. Правило сохранено" : "Классифицировано");
      if (result?.rule_warning) toast.warning(result.rule_warning, { duration: 10000 });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось классифицировать")),
  });

  const dismissMutation = useMutation({
    mutationFn: () => dismissOwnerReviewCase(item.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Кейс отложен");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отложить кейс")),
  });

  const applyRefundMutation = useMutation({
    mutationFn: () =>
      applyCardRefundCase(item.id, chosenChequeId !== "none" ? chosenChequeId : undefined),
    onSuccess: async () => {
      await invalidate();
      toast.success("Возврат учтён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось учесть возврат")),
  });

  // «Повторить отправку» по iiko-кейсу: снять кап и переотправить. resolved → успех; иначе кейс
  // остаётся (поставлено в очередь сверочному джобу / iiko отклонил — текст в тосте).
  const retryIikoMutation = useMutation({
    mutationFn: () => retryIikoPaymentCase(item.id),
    onSuccess: async (result) => {
      await invalidate();
      if (result.status === "resolved") {
        toast.success("Оплата отправлена в iiko");
      } else if (result.iiko_payment_push_error) {
        toast.error(`iiko не принял оплату: ${result.iiko_payment_push_error}`);
      } else {
        toast.success("Повтор запланирован — зеркало попробует в ближайшие минуты");
      }
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось повторить отправку")),
  });

  // «Оплата проведена вручную»: ok-маркер (джоб больше не шлёт) + закрытие кейса.
  const confirmIikoManualMutation = useMutation({
    mutationFn: () => confirmIikoManualCase(item.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Отмечено: оплата проведена в iiko");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отметить оплату")),
  });

  const createCounterpartyMutation = useMutation({
    mutationFn: () =>
      createCounterparty({
        name: newCounterpartyName,
        inn: newCounterpartyInn || null,
        type: "legal_entity",
        // Реестровый create требует полные банковские реквизиты у official-поставщика и
        // решение по статье ДДС. Из разбора владельца реквизитов нет — заводим неофициальным
        // (канал оплаты уточнят в карточке), а статьёй по умолчанию берём выбранную в форме.
        relationship: "informal",
        default_dds_article_id: articleId !== "none" ? articleId : null,
      }),
    onSuccess: async (counterparty) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["cp"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "counterparties"] }),
      ]);
      setCounterpartyId(counterparty.counterparty_id);
      setIsCreateCounterpartyOpen(false);
      toast.success("Контрагент создан");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать контрагента")),
  });

  function classify(action: ClassifyPayload["action"]) {
    if (action === "set_article" && articleId === "none") {
      toast.error("Выберите статью ДДС");
      return;
    }
    classifyMutation.mutate({
      action,
      article_id: action === "set_article" ? articleId : null,
      counterparty_id: counterpartyId === "none" ? null : counterpartyId,
      remember_as_rule: rememberAsRule,
    });
  }

  const isBusy =
    !canClassify ||
    classifyMutation.isPending ||
    dismissMutation.isPending ||
    createCounterpartyMutation.isPending ||
    applyRefundMutation.isPending ||
    retryIikoMutation.isPending ||
    confirmIikoManualMutation.isPending;

  // Кейсы возвратов карт-покупок: своя механика вместо стандартной классификации.
  const isCardRefund = item.kind === "card_refund_after_cheque";
  const isRefundMissing = item.kind === "cheque_refund_missing";
  // Кейс «оплата в iiko не проведена»: своя карточка (поставщик/сумма/дата/причина) и действия.
  const isIikoUnsettled = item.kind === "iiko_payment_unsettled";
  // Депозит списан, а черновик в банк не ушёл: банковской операции нет вообще, поэтому
  // общая карточка показала бы пустые поля и сырой payload — рисуем своё.
  const isDepositDraftFailed = item.kind === "deposit_bank_draft_failed";
  const isCashPayoutUnsettled = item.kind === "iiko_cash_payout_unsettled";
  const iikoRetriable = isIikoUnsettled && item.payload?.retriable === true;
  const isMultiInvoiceResidual =
    isIikoUnsettled && item.payload?.reason_code === "multi_invoice_residual";
  const payloadText = (key: string) => {
    const value = item.payload?.[key];
    return value == null ? "—" : String(value);
  };

  return (
    <Card>
      <CardHeader className="space-y-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex flex-wrap items-center gap-2">
            <ProviderBadge provider={item.provider} />
            <Badge variant="outline">{caseKindLabel(item.kind)}</Badge>
            {isIikoUnsettled && item.payload?.reason_code ? (
              <Badge variant="secondary">{iikoReasonLabel(String(item.payload.reason_code))}</Badge>
            ) : null}
            {operation ? <DirectionBadge direction={operation.direction} /> : null}
          </div>
          <div className="text-left lg:text-right">
            <div className="text-2xl font-semibold tabular-nums">
              {operation
                ? formatDdsMoney(operation.amount)
                : item.payload?.amount != null
                  ? formatDdsMoney(Number(item.payload.amount))
                  : "—"}
            </div>
            <div className="text-sm text-muted-foreground">
              {operation ? formatDate(operation.operation_date) : formatDate(item.created_at)}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        {isDepositDraftFailed ? (
          <>
            <div className="grid gap-3 md:grid-cols-2">
              <ReviewField label="Назначение" value={payloadText("purpose")} />
              <ReviewField label="Документ" value={payloadText("document_id")} />
            </div>
            <div className="grid gap-2">
              <Label>Почему не ушло</Label>
              <div className="rounded-md border bg-muted/20 px-3 py-2 text-sm">
                {payloadText("reason")}
              </div>
            </div>
          </>
        ) : isIikoUnsettled ? (
          <>
            <div className="grid gap-3 md:grid-cols-3">
              <ReviewField label="Поставщик" value={payloadText("supplier_name")} />
              <ReviewField label="Накладная" value={payloadText("invoice_number")} />
              <ReviewField
                label="Дата"
                value={item.payload?.issued_at ? formatDate(String(item.payload.issued_at)) : "—"}
              />
            </div>
            <div className="grid gap-2">
              <Label>Причина</Label>
              <div className="rounded-md border bg-muted/20 px-3 py-2 text-sm">
                {payloadText("reason")}
              </div>
            </div>
          </>
        ) : (
          <>
            <div className="grid gap-3 md:grid-cols-3">
              <ReviewField
                label="Контрагент"
                value={compactText(operation?.counterparty_name_raw)}
              />
              <ReviewField label="ИНН" value={compactText(operation?.counterparty_inn_raw)} />
              <ReviewField label="Документ" value={compactText(operation?.document_number)} />
            </div>
            <div className="grid gap-2">
              <Label>Назначение платежа</Label>
              <Textarea
                className="min-h-[96px]"
                readOnly
                value={operation?.payment_purpose ?? JSON.stringify(item.payload, null, 2)}
              />
            </div>
          </>
        )}
      </CardContent>
      <CardFooter className="grid gap-4 border-t pt-4">
        {isCardRefund ? (
          <div className="grid gap-3">
            <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900 dark:border-red-900/40 dark:bg-red-950/30 dark:text-red-200">
              Банк прислал возврат {payloadText("refund_amount")} ₽ по карте {payloadText("card")}
              {item.payload?.merchant ? ` (${payloadText("merchant")})` : ""}.{" "}
              {item.payload?.reason === "ambiguous" ? (
                <>Ту же сумму ждут несколько чеков — выберите, к какому он относится:</>
              ) : (
                <>
                  Ни один чек не ждёт эту сумму — вероятно, кассир не отметил возврат в чеке.
                  «Учесть возврат» заведёт входящую проводку «Возврат расходов» и закроет кейс (чеки
                  и iiko не меняются; изъятия в iiko необратимы).
                </>
              )}
            </div>
            {refundCandidates.length > 0 ? (
              <Select
                disabled={!canClassify}
                value={chosenChequeId}
                onValueChange={setChosenChequeId}
              >
                <SelectTrigger className="max-w-xs">
                  <SelectValue placeholder="Выберите чек" />
                </SelectTrigger>
                <SelectContent>
                  {refundCandidates.length > 1 ? (
                    <SelectItem value="none">Не выбран</SelectItem>
                  ) : null}
                  {refundCandidates.map((candidate) => (
                    <SelectItem key={candidate.invoice_id} value={candidate.invoice_id}>
                      Чек {candidate.cheque_number ?? candidate.invoice_id.slice(0, 8)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
          </div>
        ) : isRefundMissing ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-950/30 dark:text-amber-200">
            Чек <span className="font-medium">{payloadText("cheque_number")}</span> ждёт возврат{" "}
            {payloadText("expected_refund")} ₽ (позиции исключены при вводе), но банк его не прислал
            дольше порога. Проверьте у кассира/в банке, действительно ли возврат был; если пометка
            ошибочна — разберите чек, иначе отложите кейс до прихода денег.
          </div>
        ) : hasOperation ? (
          <>
            <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] lg:items-end">
              <div className="grid gap-2">
                <Label>Статья ДДС</Label>
                <Select disabled={!canClassify} value={articleId} onValueChange={setArticleId}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">Не выбрана</SelectItem>
                    {articles.map((article) => (
                      <SelectItem key={article.id} value={article.id}>
                        {article.code} · {article.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-2">
                <Label>Контрагент</Label>
                <Select
                  disabled={!canClassify}
                  value={counterpartyId}
                  onValueChange={setCounterpartyId}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">Не выбран</SelectItem>
                    {counterparties.map((counterparty) => (
                      <SelectItem key={counterparty.id} value={counterparty.id}>
                        {counterparty.name}
                        {counterparty.inn ? ` · ${counterparty.inn}` : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {canClassify ? (
                <Button
                  onClick={() => setIsCreateCounterpartyOpen((value) => !value)}
                  type="button"
                  variant="outline"
                >
                  <Plus size={16} aria-hidden="true" />
                  Создать нового
                </Button>
              ) : null}
            </div>

            {canClassify && isCreateCounterpartyOpen ? (
              <div className="grid gap-3 rounded-lg border bg-muted/30 p-3 md:grid-cols-[minmax(0,1fr)_180px_auto] md:items-end">
                <div className="grid gap-2">
                  <Label htmlFor={`counterparty-name-${item.id}`}>Название</Label>
                  <Input
                    id={`counterparty-name-${item.id}`}
                    value={newCounterpartyName}
                    onChange={(event) => setNewCounterpartyName(event.target.value)}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor={`counterparty-inn-${item.id}`}>ИНН</Label>
                  <Input
                    id={`counterparty-inn-${item.id}`}
                    value={newCounterpartyInn}
                    onChange={(event) => setNewCounterpartyInn(event.target.value)}
                  />
                </div>
                <Button
                  disabled={
                    !newCounterpartyName.trim() ||
                    articleId === "none" ||
                    createCounterpartyMutation.isPending
                  }
                  onClick={() => createCounterpartyMutation.mutate()}
                >
                  {createCounterpartyMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : null}
                  Сохранить
                </Button>
                {articleId === "none" ? (
                  <p className="text-xs text-muted-foreground md:col-span-3">
                    Сначала выберите статью выше — она станет статьёй ДДС по умолчанию нового
                    контрагента.
                  </p>
                ) : null}
              </div>
            ) : null}

            <label className="flex items-start gap-3 text-sm">
              <input
                checked={rememberAsRule}
                className="mt-1 h-4 w-4"
                disabled={!canClassify}
                onChange={(event) => setRememberAsRule(event.target.checked)}
                type="checkbox"
              />
              <span>
                <span className="block font-medium">Запомнить как правило</span>
                <span className="block text-muted-foreground">
                  Если включить, будущие операции этого же отправителя разберутся сами: обычная
                  платёжка — по ИНН, оплата картой — по имени продавца в назначении.
                </span>
              </span>
            </label>
          </>
        ) : isDepositDraftFailed ? (
          <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900 dark:border-red-900/40 dark:bg-red-950/30 dark:text-red-200">
            Депозит уже списан со счёта сотрудника, но платёж в банк не ушёл — значит денег он
            не получил, и в банке подписывать нечего. Переведите сумму вручную (или выдайте
            наличными) и отметьте кейс разобранным.
          </div>
        ) : isCashPayoutUnsettled ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-950/30 dark:text-amber-200">
            {(item.payload?.reason as string) ??
              "Выдача прошла у нас, но изъятие в iiko не проведено — остаток «Главной кассы» разошёлся с ДДС."}{" "}
            {item.payload?.retriable
              ? "Причина известна и повтор безопасен: настройте тип проводки в iiko и проведите выдачу заново."
              : "Прошла проводка или нет — по ответу iiko не видно, поэтому автоматически её не повторяем: сверьтесь в бэк-офисе iiko и проведите вручную, если изъятия там нет."}
          </div>
        ) : item.kind === "iiko_payment_unsettled" ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-950/30 dark:text-amber-200">
            {isMultiInvoiceResidual
              ? "Подтверждённые банковские доли уже отправлены в iiko. Не распределяйте расхождение вслепую: сверьте банк, авансы, наличные и взаимозачёт, затем отметьте кейс разобранным."
              : `У нас накладная оплачена, а в iiko оплата не отражена. ${
                  iikoRetriable
                    ? "Повторите отправку — или, если оплата уже проведена в бэк-офисе iiko вручную, отметьте это."
                    : "Проведите оплату в iiko вручную после сверки и отметьте, что она проведена."
                }`}
          </div>
        ) : (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-950/30 dark:text-amber-200">
            Информационный кейс: не определён банк-счёт плательщика, поэтому проводка ДДС по этой
            оплате не заведена автоматически. Проверьте привязку счёта к кошельку, при необходимости
            заведите расход вручную и отложите кейс.
          </div>
        )}

        {canClassify ? (
          <div className="flex flex-wrap gap-2">
            {isCardRefund ? (
              <Button
                disabled={
                  isBusy || (item.payload?.reason === "ambiguous" && chosenChequeId === "none")
                }
                onClick={() => applyRefundMutation.mutate()}
              >
                {applyRefundMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Учесть возврат
              </Button>
            ) : isRefundMissing ? null : hasOperation ? (
              <>
                <Button disabled={isBusy} onClick={() => classify("set_article")}>
                  {classifyMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : null}
                  Классифицировать
                </Button>
                <Button
                  disabled={isBusy}
                  onClick={() => classify("mark_internal_transfer")}
                  variant="outline"
                >
                  Внутренний перевод
                </Button>
                <Button disabled={isBusy} onClick={() => classify("exclude")} variant="outline">
                  Исключить
                </Button>
              </>
            ) : isIikoUnsettled ? (
              <>
                {iikoRetriable ? (
                  <Button disabled={isBusy} onClick={() => retryIikoMutation.mutate()}>
                    {retryIikoMutation.isPending ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : null}
                    Повторить отправку
                  </Button>
                ) : null}
                <Button
                  disabled={isBusy}
                  variant="outline"
                  onClick={() => confirmIikoManualMutation.mutate()}
                >
                  {confirmIikoManualMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : null}
                  {isMultiInvoiceResidual ? "Расхождение разобрано" : "Оплата проведена вручную"}
                </Button>
              </>
            ) : null}
            {/* Для iiko-кейсов «Отложить» бесполезна: свип пере-создаст реально недошедшую оплату,
                а неразобранное расхождение нельзя скрывать. Валидны действия выше по причине. */}
            {isIikoUnsettled ? null : (
              <Button disabled={isBusy} onClick={() => dismissMutation.mutate()} variant="ghost">
                Отложить
              </Button>
            )}
          </div>
        ) : (
          <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
            Режим просмотра. Классификация и создание контрагентов скрыты.
          </div>
        )}
      </CardFooter>
    </Card>
  );
}

function ReviewField({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 min-w-0 break-words text-sm">{value}</div>
    </div>
  );
}

function caseKindLabel(kind: string) {
  if (kind === "unclassified_operation") {
    return "Неразмеченная операция";
  }
  if (kind === "invalid_credentials") {
    return "Проблема с доступом";
  }
  if (kind === "unmatched_transfer") {
    return "Найти перевод";
  }
  if (kind === "unconfirmed_cheque") {
    return "Чек без подтверждения банком";
  }
  if (kind === "payer_wallet_unresolved") {
    return "Не определён банк-счёт плательщика";
  }
  if (kind === "iiko_payment_unsettled") {
    return "Оплата в iiko не проведена";
  }
  if (kind === "iiko_cash_payout_unsettled") {
    return "Выдача не отражена в кассе iiko";
  }
  if (kind === "card_refund_after_cheque") {
    return "Возврат по проведённому чеку";
  }
  if (kind === "cheque_refund_missing") {
    return "Возврат не пришёл от банка";
  }
  if (kind === "deposit_bank_draft_failed") {
    return "Депозит выдан, а платёж в банк не ушёл";
  }
  return kind;
}

// Короткий ярлык машиночитаемой причины «оплата не дошла до iiko» (payload.reason_code) для бейджа.
function iikoReasonLabel(code: string) {
  switch (code) {
    case "multi_invoice":
      return "Мультиплатёж";
    case "multi_invoice_residual":
      return "Расхождение мультиплатежа";
    case "barter_counterparty":
      return "Взаимозачёт";
    case "paid_outside_draft":
      return "Мимо банк-черновика";
    case "correction_unsettled":
      return "Коррекция без оплаты";
    case "retry_cap_exhausted":
      return "Исчерпан лимит повторов";
    case "not_in_cloud_grace":
      return "Ещё не в iiko Cloud";
    case "not_in_cloud_terminal":
      return "Не появилась в iiko Cloud";
    case "iiko_rejected":
      return "iiko отклонил";
    case "orphaned_pending":
      return "Осиротевший пуш";
    default:
      return code;
  }
}
