import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, LogOut, Pencil, Plus } from "lucide-react";
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
  getLocationLeases,
  updateLocationLease,
  type LeasePayload,
  type LeaseRecord,
} from "@/lib/api";
import { getCounterpartyDirectory } from "@/routes/counterparties/api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

type LeaseForm = {
  counterpartyId: string;
  monthlyAmount: string;
  paymentDay: string;
  paymentMode: "prepaid" | "postpaid";
  documentsMode: "official" | "informal";
  depositAmount: string;
  startedOn: string;
  note: string;
};

const EMPTY_LEASE: LeaseForm = {
  counterpartyId: "",
  monthlyAmount: "",
  paymentDay: "",
  paymentMode: "prepaid",
  documentsMode: "informal",
  depositAmount: "",
  startedOn: "",
  note: "",
};

function toForm(lease: LeaseRecord): LeaseForm {
  return {
    counterpartyId: lease.counterparty_id,
    monthlyAmount: String(lease.monthly_amount),
    paymentDay: lease.payment_day ? String(lease.payment_day) : "",
    paymentMode: lease.payment_mode,
    documentsMode: lease.documents_mode,
    depositAmount: lease.deposit_amount ? String(lease.deposit_amount) : "",
    startedOn: lease.started_on,
    note: lease.note ?? "",
  };
}

function toPayload(form: LeaseForm): LeasePayload {
  return {
    counterparty_id: form.counterpartyId,
    monthly_amount: Number(form.monthlyAmount.replace(",", ".")) || 0,
    payment_day: form.paymentDay ? Number(form.paymentDay) : null,
    payment_mode: form.paymentMode,
    documents_mode: form.documentsMode,
    deposit_amount: Number(form.depositAmount.replace(",", ".")) || 0,
    started_on: form.startedOn,
    note: form.note.trim() || null,
  };
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
  const directoryQuery = useQuery({
    queryKey: ["counterparty-directory"],
    queryFn: getCounterpartyDirectory,
    enabled: canEdit,
  });

  const counterparties = useMemo(
    () => (directoryQuery.data ?? []).map((item) => ({ id: item.id, name: item.name })),
    [directoryQuery.data],
  );

  useEffect(() => {
    if (dialogOpen) {
      setForm(editing ? toForm(editing) : EMPTY_LEASE);
    }
  }, [dialogOpen, editing]);

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["location-leases", locationId] });
  }

  const saveMutation = useMutation({
    mutationFn: async (payload: LeasePayload) =>
      editing
        ? updateLocationLease(locationId, editing.id, payload)
        : createLocationLease(locationId, payload),
    onSuccess: async () => {
      await refresh();
      toast.success(editing ? "Аренда обновлена" : "Аренда добавлена");
      setDialogOpen(false);
      setEditing(null);
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
    if (!form.counterpartyId) {
      toast.error("Выберите арендодателя");
      return;
    }
    if (!form.startedOn) {
      toast.error("Укажите дату начала аренды");
      return;
    }
    saveMutation.mutate(toPayload(form));
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
              Арендодатель — обычный контрагент: роль «арендодатель» проставится сама. Долг за
              аренду считается по этой сумме, даже если арендодатель не выставляет документов.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-3" onSubmit={handleSubmit}>
            <div className="space-y-1">
              <Label>Арендодатель</Label>
              <ArticleCombobox
                articles={counterparties}
                value={form.counterpartyId}
                onChange={(id) => setForm({ ...form, counterpartyId: id })}
                placeholder="Выберите контрагента"
              />
            </div>

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
