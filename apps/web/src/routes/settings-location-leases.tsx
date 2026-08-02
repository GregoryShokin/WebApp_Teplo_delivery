import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { LoaderCircle, LogOut, Pencil, Plus, Repeat } from "lucide-react";
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
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import {
  apiErrorMessage,
  closeLocationLease,
  createLocationLease,
  getDdsArticles,
  getLeaseLedger,
  getLocationLeases,
  rebuildLeaseAccrual,
  replaceLeaseLandlord,
  updateLocationLease,
  type LandlordInput,
  type LeaseRecord,
  type LeaseTermsPayload,
} from "@/lib/api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

type LeaseForm = {
  counterpartyId: string;
  landlordName: string;
  landlordInn: string;
  bankBik: string;
  bankAccount: string;
  corrAccount: string;
  monthlyAmount: string;
  paymentDay: string;
  paymentMode: "prepaid" | "postpaid";
  documentsMode: "official" | "informal";
  depositAmount: string;
  startedOn: string;
  note: string;
  ddsArticleId: string;
};

const EMPTY_LEASE: LeaseForm = {
  counterpartyId: "",
  landlordName: "",
  landlordInn: "",
  bankBik: "",
  bankAccount: "",
  corrAccount: "",
  monthlyAmount: "",
  paymentDay: "",
  paymentMode: "prepaid",
  documentsMode: "informal",
  depositAmount: "",
  startedOn: "",
  note: "",
  ddsArticleId: "",
};

function toForm(lease: LeaseRecord): LeaseForm {
  return {
    counterpartyId: lease.counterparty_id,
    landlordName: lease.counterparty_name,
    landlordInn: "",
    bankBik: "",
    bankAccount: "",
    corrAccount: "",
    monthlyAmount: String(lease.monthly_amount),
    paymentDay: lease.payment_day ? String(lease.payment_day) : "",
    paymentMode: lease.payment_mode,
    documentsMode: lease.documents_mode,
    depositAmount: lease.deposit_amount ? String(lease.deposit_amount) : "",
    startedOn: lease.started_on,
    note: lease.note ?? "",
    ddsArticleId: lease.dds_article_id ?? "",
  };
}

function toTerms(form: LeaseForm): LeaseTermsPayload {
  return {
    monthly_amount: Number(form.monthlyAmount.replace(",", ".")) || 0,
    payment_day: form.paymentDay ? Number(form.paymentDay) : null,
    payment_mode: form.paymentMode,
    documents_mode: form.documentsMode,
    deposit_amount: Number(form.depositAmount.replace(",", ".")) || 0,
    started_on: form.startedOn,
    note: form.note.trim() || null,
    dds_article_id: form.ddsArticleId || null,
  };
}

function toLandlord(form: LeaseForm): LandlordInput {
  return {
    name: form.landlordName.trim(),
    inn: form.landlordInn.trim() || null,
    bank_bik: form.bankBik.trim() || null,
    bank_account: form.bankAccount.trim() || null,
    corr_account: form.corrAccount.trim() || null,
  };
}


/** Данные арендодателя. Реквизиты показываем только официальной аренде — по ним и платят. */
function LandlordFields({
  form,
  setForm,
  idPrefix,
}: {
  form: LeaseForm;
  setForm: (next: LeaseForm) => void;
  idPrefix: string;
}) {
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
        <div className="space-y-1">
          <Label htmlFor={`${idPrefix}-landlord`}>Арендодатель</Label>
          <Input
            id={`${idPrefix}-landlord`}
            value={form.landlordName}
            onChange={(event) => setForm({ ...form, landlordName: event.target.value })}
            placeholder="ИП Иванов И. И."
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${idPrefix}-inn`}>ИНН</Label>
          <Input
            id={`${idPrefix}-inn`}
            inputMode="numeric"
            value={form.landlordInn}
            onChange={(event) => setForm({ ...form, landlordInn: event.target.value })}
            placeholder={form.documentsMode === "official" ? "обязательно" : "необязательно"}
          />
        </div>
      </div>
      {form.documentsMode === "official" ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="space-y-1">
            <Label htmlFor={`${idPrefix}-bik`}>БИК банка</Label>
            <Input
              id={`${idPrefix}-bik`}
              inputMode="numeric"
              value={form.bankBik}
              onChange={(event) => setForm({ ...form, bankBik: event.target.value })}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor={`${idPrefix}-acc`}>Расчётный счёт</Label>
            <Input
              id={`${idPrefix}-acc`}
              inputMode="numeric"
              value={form.bankAccount}
              onChange={(event) => setForm({ ...form, bankAccount: event.target.value })}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor={`${idPrefix}-corr`}>Корр. счёт</Label>
            <Input
              id={`${idPrefix}-corr`}
              inputMode="numeric"
              value={form.corrAccount}
              onChange={(event) => setForm({ ...form, corrAccount: event.target.value })}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Начисления и долг по одному договору: раскрывающийся блок в карточке аренды. Данные тянем
 * лениво — только когда оператор раскрыл. Кнопка «Пересобрать» гонит обязательство текущего
 * месяца под текущие условия договора (открытое обновит, прошлое в силе не тронет).
 */
function LeaseLedgerPanel({
  locationId,
  lease,
  canEdit,
}: {
  locationId: string;
  lease: LeaseRecord;
  canEdit: boolean;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const ledgerQuery = useQuery({
    queryKey: ["lease-ledger", locationId, lease.id],
    queryFn: () => getLeaseLedger(locationId, lease.id),
    enabled: open,
  });
  const rebuildMutation = useMutation({
    mutationFn: () => rebuildLeaseAccrual(locationId, lease.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["lease-ledger", locationId, lease.id] });
      toast.success("Начисления пересобраны за текущий месяц");
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });
  const ledger = ledgerQuery.data;

  return (
    <div className="mt-2 border-t pt-2">
      <button
        type="button"
        className="text-xs text-muted-foreground hover:underline"
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "Скрыть начисления" : "Начисления и долг"}
      </button>
      {open ? (
        <div className="mt-2 space-y-2">
          {ledgerQuery.isLoading ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              Загружаем начисления…
            </div>
          ) : ledger ? (
            <>
              {ledger.accruals.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Начислений пока нет — они появятся ночной джобой или по кнопке «Пересобрать».
                </p>
              ) : (
                <div className="space-y-1">
                  {ledger.accruals.map((accrual) => (
                    <div
                      key={accrual.invoice_id}
                      className="flex flex-wrap items-center justify-between gap-2 text-xs"
                    >
                      <span className="text-muted-foreground">
                        {accrual.period_start && accrual.period_end
                          ? `${accrual.period_start} — ${accrual.period_end}`
                          : accrual.number}
                      </span>
                      <span className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium">{money.format(accrual.amount)}</span>
                        <Badge variant={accrual.activation_status === "active" ? "secondary" : "outline"}>
                          {accrual.activation_status === "active" ? "в силе" : "ждёт даты"}
                        </Badge>
                        <Badge variant={accrual.payment_status === "paid" ? "secondary" : "outline"}>
                          {accrual.payment_status === "paid"
                            ? "оплачено"
                            : accrual.paid_amount > 0
                              ? `частично ${money.format(accrual.paid_amount)}`
                              : "не оплачено"}
                        </Badge>
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>Начислено: {money.format(ledger.accrued_total)}</span>
                <span>Оплачено: {money.format(ledger.paid_total)}</span>
                <span className="font-medium text-foreground">
                  Долг: {money.format(ledger.outstanding_total)}
                </span>
                {ledger.deposit_outstanding > 0 ? (
                  <span>Залог: {money.format(ledger.deposit_outstanding)}</span>
                ) : null}
              </div>
              {canEdit ? (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={rebuildMutation.isPending}
                  onClick={() => rebuildMutation.mutate()}
                >
                  {rebuildMutation.isPending ? (
                    <LoaderCircle className="mr-1 h-4 w-4 animate-spin" />
                  ) : null}
                  Пересобрать текущий месяц
                </Button>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Аренда помещения. Арендодателей может быть несколько: площадь бывает поделена между
 * собственниками. Смена собственника — не правка строки, а «Съехали» + новая аренда,
 * иначе прошлые месяцы задним числом уедут на нового арендодателя.
 */
export function LocationLeases({
  locationId,
  locationName,
  canEdit,
}: {
  locationId: string;
  locationName: string;
  canEdit: boolean;
}) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<LeaseRecord | null>(null);
  const [form, setForm] = useState<LeaseForm>(EMPTY_LEASE);
  const [closingLease, setClosingLease] = useState<LeaseRecord | null>(null);
  const [closingDate, setClosingDate] = useState("");

  const leasesQuery = useQuery({
    queryKey: ["location-leases", locationId],
    queryFn: () => getLocationLeases(locationId),
  });
  const articlesQuery = useQuery({
    queryKey: ["dds-articles"],
    queryFn: getDdsArticles,
    enabled: canEdit,
  });
  // Только статьи-аренды (`lease_bound`), а не все «привязанные к помещению». Под
  // `location_required` попадают ещё коммуналка и охрана: выбрав их в договоре аренды, человек
  // получал бы арендное начисление под коммунальной статьёй — и месяц коммуналки считался бы
  // закрытым арендой.
  const rentArticles = useMemo(
    () =>
      (articlesQuery.data ?? [])
        .filter((article) => article.lease_bound && article.is_active)
        .map((article) => ({ id: article.id, name: article.name })),
    [articlesQuery.data],
  );
  useEffect(() => {
    if (dialogOpen) {
      setForm(editing ? toForm(editing) : EMPTY_LEASE);
    }
  }, [dialogOpen, editing]);

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["location-leases", locationId] });
  }

  const [replacing, setReplacing] = useState<LeaseRecord | null>(null);
  const [replaceForm, setReplaceForm] = useState<LeaseForm>(EMPTY_LEASE);
  const [previousEndedOn, setPreviousEndedOn] = useState("");

  const saveMutation = useMutation({
    mutationFn: async (form: LeaseForm) =>
      editing
        ? updateLocationLease(locationId, editing.id, toTerms(form))
        : createLocationLease(locationId, { ...toTerms(form), landlord: toLandlord(form) }),
    onSuccess: async () => {
      await refresh();
      toast.success(editing ? "Аренда обновлена" : "Аренда добавлена");
      setDialogOpen(false);
      setEditing(null);
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  const replaceMutation = useMutation({
    mutationFn: async ({ lease, form, endedOn }: { lease: LeaseRecord; form: LeaseForm; endedOn: string }) =>
      replaceLeaseLandlord(locationId, lease.id, {
        landlord: toLandlord(form),
        terms: toTerms(form),
        previous_ended_on: endedOn,
      }),
    onSuccess: async (result) => {
      await refresh();
      await queryClient.invalidateQueries({ queryKey: ["counterparty-leases"] });
      toast.success(
        result.previous_archived
          ? "Арендодатель сменён, прежний убран в архив"
          : "Арендодатель сменён — прежний остаётся, он сдаёт другое помещение",
      );
      setReplacing(null);
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  const closeMutation = useMutation({
    mutationFn: async ({ lease, endedOn }: { lease: LeaseRecord; endedOn: string }) =>
      closeLocationLease(locationId, lease.id, endedOn),
    onSuccess: async () => {
      await refresh();
      toast.success("Аренда закрыта — заведите нового арендодателя");
      setClosingLease(null);
      setClosingDate("");
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!editing && !form.landlordName.trim()) {
      toast.error("Укажите название арендодателя");
      return;
    }
    if (!form.startedOn) {
      toast.error("Укажите дату начала аренды");
      return;
    }
    saveMutation.mutate(form);
  }

  const leases = leasesQuery.data ?? [];
  const active = leases.filter((lease) => lease.is_active);
  const history = leases.filter((lease) => !lease.is_active);
  const monthlyTotal = active.reduce((sum, lease) => sum + lease.monthly_amount, 0);

  return (
    <div className="rounded-md border p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium">Аренда</div>
          <p className="mt-1 text-xs text-muted-foreground">
            {active.length > 0
              ? `${money.format(monthlyTotal)} в месяц · арендодателей: ${active.length}`
              : "Арендодатель не указан — платежи за аренду не с кем связать"}
          </p>
        </div>
        {canEdit ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setEditing(null);
              setDialogOpen(true);
            }}
          >
            <Plus className="mr-1 h-4 w-4" />
            Арендодатель
          </Button>
        ) : null}
      </div>

      {leasesQuery.isLoading ? (
        <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" />
          Загружаем аренду…
        </div>
      ) : null}

      <div className="mt-3 space-y-2">
        {active.map((lease) => (
          <div key={lease.id} className="rounded-md bg-muted/40 px-3 py-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{lease.counterparty_name}</span>
                  <Badge variant="secondary">{money.format(lease.monthly_amount)}/мес</Badge>
                  <Badge variant="outline">
                    {lease.documents_mode === "official" ? "с документами" : "без документов"}
                  </Badge>
                  <Badge variant="outline">
                    {lease.payment_mode === "prepaid" ? "предоплата" : "постоплата"}
                  </Badge>
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  с {lease.started_on}
                  {lease.payment_day ? ` · платим ${lease.payment_day}-го` : ""}
                  {lease.deposit_amount > 0
                    ? ` · залог ${money.format(lease.deposit_amount)}`
                    : ""}
                </div>
              </div>
              {canEdit ? (
                <div className="flex items-center gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setEditing(lease);
                      setDialogOpen(true);
                    }}
                  >
                    <Pencil className="mr-1 h-4 w-4" />
                    Изменить
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setReplacing(lease);
                      setReplaceForm({ ...EMPTY_LEASE, monthlyAmount: String(lease.monthly_amount) });
                      setPreviousEndedOn("");
                    }}
                  >
                    <Repeat className="mr-1 h-4 w-4" />
                    Сменить арендодателя
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setClosingLease(lease);
                      setClosingDate("");
                    }}
                  >
                    <LogOut className="mr-1 h-4 w-4" />
                    Съехали
                  </Button>
                </div>
              ) : null}
            </div>
            <LeaseLedgerPanel locationId={locationId} lease={lease} canEdit={canEdit} />
          </div>
        ))}

        {history.length > 0 ? (
          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer select-none">
              Прежние арендодатели ({history.length})
            </summary>
            <div className="mt-2 space-y-1">
              {history.map((lease) => (
                <div key={lease.id} className="flex flex-wrap items-center gap-2">
                  <span>{lease.counterparty_name}</span>
                  <span>{money.format(lease.monthly_amount)}/мес</span>
                  <span>
                    {lease.started_on} — {lease.ended_on}
                  </span>
                </div>
              ))}
            </div>
          </details>
        ) : null}
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {editing ? "Изменить аренду" : `Аренда помещения «${locationName}»`}
            </DialogTitle>
            <DialogDescription>
              Арендодателя заводим прямо здесь: достаточно названия, ИНН — по желанию.
              Карточка контрагента создастся сама, а если такой уже есть — возьмём его.
              Долг считается по этой сумме, даже если арендодатель не выставляет документов.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-3" onSubmit={handleSubmit}>
            {editing ? (
              <div className="rounded-md bg-muted/50 px-3 py-2 text-sm">
                Арендодатель: <span className="font-medium">{editing.counterparty_name}</span>
                <p className="mt-1 text-xs text-muted-foreground">
                  Здесь меняются только условия. Сменить собственника — кнопка «Сменить
                  арендодателя»; фамилию, ИНН и реквизиты правят в карточке контрагента.
                </p>
              </div>
            ) : (
              <LandlordFields form={form} setForm={setForm} idPrefix="lease" />
            )}

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="lease-amount">Стоимость в месяц, ₽</Label>
                <Input
                  id="lease-amount"
                  inputMode="decimal"
                  value={form.monthlyAmount}
                  onChange={(event) => setForm({ ...form, monthlyAmount: event.target.value })}
                  placeholder="100000"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lease-day">Платим числа</Label>
                <Input
                  id="lease-day"
                  inputMode="numeric"
                  value={form.paymentDay}
                  onChange={(event) => setForm({ ...form, paymentDay: event.target.value })}
                  placeholder="1"
                />
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label>Порядок оплаты</Label>
                <Select
                  value={form.paymentMode}
                  onValueChange={(value) =>
                    setForm({ ...form, paymentMode: value as "prepaid" | "postpaid" })
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="prepaid">Предоплата</SelectItem>
                    <SelectItem value="postpaid">Постоплата</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label>Документы</Label>
                <Select
                  value={form.documentsMode}
                  onValueChange={(value) =>
                    setForm({ ...form, documentsMode: value as "official" | "informal" })
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="official">Официально, с УПД</SelectItem>
                    <SelectItem value="informal">Без документов</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="lease-start">Аренда с</Label>
                <Input
                  id="lease-start"
                  type="date"
                  value={form.startedOn}
                  onChange={(event) => setForm({ ...form, startedOn: event.target.value })}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lease-deposit">Залог, ₽</Label>
                <Input
                  id="lease-deposit"
                  inputMode="decimal"
                  value={form.depositAmount}
                  onChange={(event) => setForm({ ...form, depositAmount: event.target.value })}
                  placeholder="0"
                />
              </div>
            </div>

            <div className="space-y-1">
              <Label>Статья ДДС аренды</Label>
              <ArticleCombobox
                articles={rentArticles}
                value={form.ddsArticleId}
                onChange={(id) => setForm({ ...form, ddsArticleId: id })}
                placeholder="Выберите статью аренды"
              />
              <p className="text-xs text-muted-foreground">
                По этой статье платят аренду. Платёж по ней предложит арендодателей именно этого
                помещения.
              </p>
            </div>

            <div className="space-y-1">
              <Label htmlFor="lease-note">Заметка</Label>
              <Input
                id="lease-note"
                value={form.note}
                onChange={(event) => setForm({ ...form, note: event.target.value })}
              />
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDialogOpen(false)}
                disabled={saveMutation.isPending}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={saveMutation.isPending}>
                {saveMutation.isPending ? (
                  <LoaderCircle className="mr-1 h-4 w-4 animate-spin" />
                ) : null}
                Сохранить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={replacing !== null}
        onOpenChange={(open) => {
          if (!open) {
            setReplacing(null);
          }
        }}
      >
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Сменить арендодателя</DialogTitle>
            <DialogDescription>
              Прежняя аренда с «{replacing?.counterparty_name}» закроется указанной датой, а
              новая заведётся отдельной строкой — платежи прошлых месяцев останутся за прежним
              собственником. Если он больше ничего не сдаёт, его карточка уйдёт в архив.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="replace-prev-end">Прежняя аренда до</Label>
                <Input
                  id="replace-prev-end"
                  type="date"
                  value={previousEndedOn}
                  onChange={(event) => setPreviousEndedOn(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="replace-start">Новая аренда с</Label>
                <Input
                  id="replace-start"
                  type="date"
                  value={replaceForm.startedOn}
                  onChange={(event) =>
                    setReplaceForm({ ...replaceForm, startedOn: event.target.value })
                  }
                />
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="replace-amount">Стоимость в месяц, ₽</Label>
                <Input
                  id="replace-amount"
                  inputMode="decimal"
                  value={replaceForm.monthlyAmount}
                  onChange={(event) =>
                    setReplaceForm({ ...replaceForm, monthlyAmount: event.target.value })
                  }
                />
              </div>
              <div className="space-y-1">
                <Label>Документы</Label>
                <Select
                  value={replaceForm.documentsMode}
                  onValueChange={(value) =>
                    setReplaceForm({
                      ...replaceForm,
                      documentsMode: value as "official" | "informal",
                    })
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="official">Официально, с УПД</SelectItem>
                    <SelectItem value="informal">Без документов</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <LandlordFields form={replaceForm} setForm={setReplaceForm} idPrefix="replace" />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setReplacing(null)}>
              Отмена
            </Button>
            <Button
              disabled={
                !previousEndedOn ||
                !replaceForm.startedOn ||
                !replaceForm.landlordName.trim() ||
                replaceMutation.isPending
              }
              onClick={() => {
                if (replacing) {
                  replaceMutation.mutate({
                    lease: replacing,
                    form: replaceForm,
                    endedOn: previousEndedOn,
                  });
                }
              }}
            >
              {replaceMutation.isPending ? (
                <LoaderCircle className="mr-1 h-4 w-4 animate-spin" />
              ) : null}
              Сменить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={closingLease !== null}
        onOpenChange={(open) => {
          if (!open) {
            setClosingLease(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Съехали от «{closingLease?.counterparty_name}»</DialogTitle>
            <DialogDescription>
              Аренда закроется этой датой. Прошлые месяцы останутся за прежним арендодателем —
              нового заведите отдельной строкой.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1">
            <Label htmlFor="lease-close-date">Последний день аренды</Label>
            <Input
              id="lease-close-date"
              type="date"
              value={closingDate}
              onChange={(event) => setClosingDate(event.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setClosingLease(null)}>
              Отмена
            </Button>
            <Button
              disabled={!closingDate || closeMutation.isPending}
              onClick={() => {
                if (closingLease && closingDate) {
                  closeMutation.mutate({ lease: closingLease, endedOn: closingDate });
                }
              }}
            >
              {closeMutation.isPending ? (
                <LoaderCircle className="mr-1 h-4 w-4 animate-spin" />
              ) : null}
              Закрыть аренду
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
