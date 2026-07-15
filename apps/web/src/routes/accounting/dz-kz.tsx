import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Pencil } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { api, apiErrorMessage } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

type View = "open" | "all" | "needs_review" | "recognized";

type AccountingItem = {
  id: string;
  source_kind: "service_period" | "legacy_prepayment";
  counterparty_id: string;
  counterparty_name: string;
  article_id: string | null;
  article_name: string | null;
  invoice_id: string | null;
  invoice_number: string | null;
  amount: number;
  paid_amount: number;
  balance_amount: number;
  balance_type: "receivable" | "payable" | "scheduled" | "closed" | "needs_review";
  service_period_start: string | null;
  service_period_end: string | null;
  period_status: string;
  recognition_month: string | null;
  recognized: boolean;
};

type AccountingList = {
  items: AccountingItem[];
  receivable_total: number;
  payable_total: number;
  scheduled_total: number;
  needs_review_total: number;
};

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

const date = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });

async function getAccounting(view: View): Promise<AccountingList> {
  const response = await api.get<AccountingList>("/accounting/suppliers", { params: { view } });
  return response.data;
}

async function updatePeriod(
  id: string,
  payload: { service_period_start: string; service_period_end: string; reason?: string | null },
): Promise<AccountingItem> {
  const response = await api.patch<AccountingItem>(`/accounting/suppliers/service-periods/${id}`, payload);
  return response.data;
}

const STATUS: Record<AccountingItem["balance_type"], { label: string; className: string }> = {
  receivable: { label: "Дебиторка", className: "border-sky-200 bg-sky-50 text-sky-700" },
  payable: { label: "Кредиторка", className: "border-rose-200 bg-rose-50 text-rose-700" },
  scheduled: { label: "Будущий расход", className: "border-violet-200 bg-violet-50 text-violet-700" },
  closed: { label: "Закрыто", className: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  needs_review: { label: "Нужно распределить", className: "border-amber-200 bg-amber-50 text-amber-800" },
};

function formatPeriod(item: AccountingItem) {
  if (!item.service_period_start || !item.service_period_end) return "Период не указан";
  return `${date.format(new Date(`${item.service_period_start}T00:00:00`))} — ${date.format(
    new Date(`${item.service_period_end}T00:00:00`),
  )}`;
}

export function DzKzRoute() {
  const permissions = usePermissions();
  const [view, setView] = useState<View>("open");
  const [editing, setEditing] = useState<AccountingItem | null>(null);
  const query = useQuery({
    queryKey: ["accounting", "suppliers", view],
    queryFn: () => getAccounting(view),
  });
  const canEdit = permissions.hasPermission("accounting.suppliers.edit");
  const canCorrectRecognized = permissions.hasPermission(
    "accounting.service_periods.correct_recognized",
  );

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Учёт ДЗ/КЗ</h1>
        <p className="text-sm text-muted-foreground">
          Предоплаты, обязательства и признание расходов по периоду оказания услуг.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Summary title="Дебиторская задолженность" value={query.data?.receivable_total} tone="sky" />
        <Summary title="Кредиторская задолженность" value={query.data?.payable_total} tone="rose" />
        <Summary title="Будущие расходы" value={query.data?.scheduled_total} tone="violet" />
        <Summary title="Нужно распределить" value={query.data?.needs_review_total} tone="amber" />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={view} onValueChange={(value) => setView(value as View)}>
          <TabsList>
            <TabsTrigger value="open">Актуальные</TabsTrigger>
            <TabsTrigger value="needs_review">На ручной разбор</TabsTrigger>
            <TabsTrigger value="recognized">Признанные</TabsTrigger>
            <TabsTrigger value="all">Все</TabsTrigger>
          </TabsList>
        </Tabs>
        <p className="text-xs text-muted-foreground">
          Расход признаётся после окончания последнего дня периода.
        </p>
      </div>

      <div className="overflow-hidden rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Статус</TableHead>
              <TableHead>Контрагент / документ</TableHead>
              <TableHead>Период услуги</TableHead>
              <TableHead>Статья</TableHead>
              <TableHead className="text-right">Оплачено</TableHead>
              <TableHead className="text-right">Остаток</TableHead>
              <TableHead className="w-14" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="py-12 text-center text-muted-foreground">
                  <Loader2 className="mr-2 inline animate-spin" size={16} /> Загрузка…
                </TableCell>
              </TableRow>
            ) : query.isError ? (
              <TableRow>
                <TableCell colSpan={7} className="py-12 text-center text-red-600">
                  {apiErrorMessage(query.error, "Не удалось загрузить учёт ДЗ/КЗ")}
                </TableCell>
              </TableRow>
            ) : (query.data?.items.length ?? 0) === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="py-12 text-center text-muted-foreground">
                  Записей в этом разделе нет.
                </TableCell>
              </TableRow>
            ) : (
              query.data?.items.map((item) => {
                const status = STATUS[item.balance_type];
                const correctionAllowed = !item.recognized || canCorrectRecognized;
                return (
                  <TableRow key={`${item.source_kind}:${item.id}`}>
                    <TableCell>
                      <Badge variant="outline" className={status.className}>{status.label}</Badge>
                    </TableCell>
                    <TableCell>
                      <div className="font-medium">{item.counterparty_name}</div>
                      <div className="text-xs text-muted-foreground">
                        {item.invoice_number ? `Счёт № ${item.invoice_number}` : "Предоплата"}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className={item.balance_type === "needs_review" ? "text-amber-700" : ""}>
                        {formatPeriod(item)}
                      </div>
                      {item.recognition_month ? (
                        <div className="text-xs text-muted-foreground">
                          P&L: {item.recognition_month.slice(0, 7)}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-muted-foreground">{item.article_name ?? "—"}</TableCell>
                    <TableCell className="text-right tabular-nums">{money.format(item.paid_amount)}</TableCell>
                    <TableCell className="text-right font-semibold tabular-nums">
                      {money.format(item.balance_amount)}
                    </TableCell>
                    <TableCell>
                      {canEdit && item.source_kind === "service_period" ? (
                        <Button
                          size="icon"
                          variant="ghost"
                          title={
                            correctionAllowed
                              ? "Изменить период"
                              : "Нужно отдельное право на корректировку признанного расхода"
                          }
                          disabled={!correctionAllowed}
                          onClick={() => setEditing(item)}
                        >
                          <Pencil size={15} />
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {editing ? <PeriodDialog item={editing} onClose={() => setEditing(null)} /> : null}
    </div>
  );
}

function Summary({
  title,
  value,
  tone,
}: {
  title: string;
  value?: number;
  tone: "sky" | "rose" | "violet" | "amber";
}) {
  const tones = {
    sky: "text-sky-700",
    rose: "text-rose-700",
    violet: "text-violet-700",
    amber: "text-amber-700",
  };
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className={`text-2xl font-semibold tabular-nums ${tones[tone]}`}>
          {value == null ? "—" : money.format(value)}
        </div>
      </CardContent>
    </Card>
  );
}

function PeriodDialog({ item, onClose }: { item: AccountingItem; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [start, setStart] = useState(item.service_period_start ?? "");
  const [end, setEnd] = useState(item.service_period_end ?? "");
  const [reason, setReason] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      updatePeriod(item.id, {
        service_period_start: start,
        service_period_end: end,
        reason: reason.trim() || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["accounting", "suppliers"] });
      toast.success("Период услуги изменён");
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось изменить период")),
  });
  const ready = Boolean(start && end && end >= start && (!item.recognized || reason.trim()));

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Период оказания услуги</DialogTitle>
          <DialogDescription>
            {item.counterparty_name}{item.invoice_number ? ` · счёт № ${item.invoice_number}` : ""}
          </DialogDescription>
        </DialogHeader>
        {item.recognized ? (
          <div className="flex gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            <AlertTriangle className="mt-0.5 shrink-0" size={17} />
            Период уже попал в P&L. Изменение перенесёт расход в другой месяц и останется в журнале аудита.
          </div>
        ) : null}
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label>С</Label>
            <Input type="date" value={start} onChange={(event) => setStart(event.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label>По</Label>
            <Input type="date" min={start || undefined} value={end} onChange={(event) => setEnd(event.target.value)} />
          </div>
        </div>
        <div className="grid gap-1.5">
          <Label>{item.recognized ? "Причина корректировки *" : "Комментарий"}</Label>
          <Textarea value={reason} onChange={(event) => setReason(event.target.value)} />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Отмена</Button>
          <Button disabled={!ready || mutation.isPending} onClick={() => mutation.mutate()}>
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
