import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileImage, FileSearch, LoaderCircle, Pencil, Plus, Upload } from "lucide-react";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { todayIso } from "@/lib/date";
import {
  apiErrorMessage,
  createUtilityAccount,
  createUtilityIntake,
  getDdsArticles,
  getLocations,
  getUtilityAccounts,
  getUtilityCalendar,
  getUtilityIntakes,
  revokeUtilityIntake,
  updateUtilityAccount,
  type UtilityAccountRecord,
  type UtilityCalendarRow,
  type UtilityIntakeRecord,
  type UtilityKind,
} from "@/lib/api";
import { getCounterpartyDirectory } from "@/routes/counterparties/api";
import { UtilityReviewDialog } from "@/routes/accounting/utility-review-dialog";

const KIND_LABELS: Record<UtilityKind, string> = {
  water: "Вода",
  gas: "Газ",
  electricity: "Электричество",
};

const STATE_BADGE: Record<
  UtilityCalendarRow["state"],
  { label: string; variant: "default" | "secondary" | "destructive" | "outline" }
> = {
  provided: { label: "Проведено", variant: "default" },
  waiting: { label: "Ждём документ", variant: "secondary" },
  overdue: { label: "Просрочено", variant: "destructive" },
  missing: { label: "Документа нет", variant: "destructive" },
};

const INTAKE_STATUS_LABELS: Record<UtilityIntakeRecord["status"], string> = {
  new: "Новая",
  needs_review: "Нужна проверка",
  ready: "Готова к проведению",
  promoted: "Проведена",
  rejected: "Отклонена",
};

function money(value: string | null): string {
  if (value === null) return "—";
  return Number(value).toLocaleString("ru-RU", { minimumFractionDigits: 2 });
}

function monthLabel(iso: string): string {
  const [year, month] = iso.split("-");
  const names = [
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
  return `${names[Number(month) - 1]} ${year}`;
}

type IntakeForm = {
  accountId: string;
  month: string;
  amount: string;
  documentNumber: string;
  documentDate: string;
  note: string;
};

const EMPTY_INTAKE: IntakeForm = {
  accountId: "",
  month: todayIso().slice(0, 7),
  amount: "",
  documentNumber: "",
  documentDate: "",
  note: "",
};

export function UtilitiesRoute() {
  const queryClient = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [reviewing, setReviewing] = useState<UtilityIntakeRecord | null>(null);
  const [form, setForm] = useState<IntakeForm>(EMPTY_INTAKE);
  const [file, setFile] = useState<File | null>(null);
  const [accountDialog, setAccountDialog] = useState<UtilityAccountRecord | "new" | null>(null);

  const accountsQuery = useQuery({ queryKey: ["utility-accounts"], queryFn: () => getUtilityAccounts() });
  const intakesQuery = useQuery({ queryKey: ["utility-intakes"], queryFn: () => getUtilityIntakes() });
  const calendarQuery = useQuery({ queryKey: ["utility-calendar"], queryFn: () => getUtilityCalendar(6) });

  const accounts = useMemo(() => accountsQuery.data ?? [], [accountsQuery.data]);
  const activeAccounts = useMemo(() => accounts.filter((a) => a.is_active), [accounts]);

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["utility-intakes"] }),
      queryClient.invalidateQueries({ queryKey: ["utility-calendar"] }),
      queryClient.invalidateQueries({ queryKey: ["utility-accounts"] }),
    ]);
  }

  const saveIntake = useMutation({
    mutationFn: async () =>
      createUtilityIntake({
        file,
        accountId: form.accountId || null,
      }),
    onSuccess: async (created) => {
      setUploadOpen(false);
      setFile(null);
      setForm(EMPTY_INTAKE);
      await refresh();
      // Сразу открываем разбор: загрузка сама по себе ничего не значит, смысл появляется,
      // когда человек сверил сумму с документом.
      setReviewing(created);
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => revokeUtilityIntake(id),
    onSuccess: async () => {
      toast.success("Долг отозван — месяц свободен");
      await refresh();
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  const pending = (intakesQuery.data ?? []).filter((i) => i.status !== "promoted");
  const done = (intakesQuery.data ?? []).filter((i) => i.status === "promoted");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Коммунальные услуги</h1>
          <p className="text-muted-foreground text-sm">
            Вода, газ и электричество: возмещаем арендодателю по его расчёту. Долг открывается
            документом, расход признаётся в месяце потребления.
          </p>
        </div>
        <Button
          onClick={() => {
            setFile(null);
            setForm({ ...EMPTY_INTAKE, accountId: activeAccounts[0]?.id ?? "" });
            setUploadOpen(true);
          }}
        >
          <Upload className="mr-2 size-4" />
          Загрузить платёжку
        </Button>
      </div>

      <Tabs defaultValue="queue">
        <TabsList>
          <TabsTrigger value="queue">
            Платёжки
            {pending.length > 0 ? (
              <Badge variant="secondary" className="ml-2">
                {pending.length}
              </Badge>
            ) : null}
          </TabsTrigger>
          <TabsTrigger value="calendar">Что ждём</TabsTrigger>
          <TabsTrigger value="accounts">Потоки</TabsTrigger>
        </TabsList>

        <TabsContent value="queue" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>В работе</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {intakesQuery.isLoading ? (
                <LoaderCircle className="size-4 animate-spin" />
              ) : intakesQuery.isError ? (
                <p className="text-destructive text-sm">
                  Список платёжек не загрузился: {apiErrorMessage(intakesQuery.error)}
                </p>
              ) : pending.length === 0 ? (
                <p className="text-muted-foreground text-sm">
                  Непроведённых платёжек нет. Как принесут — загрузите фото, укажите сумму и месяц.
                </p>
              ) : (
                pending.map((intake) => (
                  <div
                    key={intake.id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
                  >
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{intake.account_title ?? "Поток не выбран"}</span>
                        <Badge variant="secondary">{INTAKE_STATUS_LABELS[intake.status]}</Badge>
                        {!intake.has_document ? <Badge variant="outline">без документа</Badge> : null}
                      </div>
                      <p className="text-muted-foreground text-sm">
                        {intake.period_end ? monthLabel(intake.period_end.slice(0, 7)) : "период не указан"}
                        {" · "}
                        {money(intake.amount)} ₽
                      </p>
                    </div>
                    <Button size="sm" onClick={() => setReviewing(intake)}>
                      <FileSearch className="mr-2 size-4" />
                      Разобрать
                    </Button>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Проведённые</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {done.length === 0 ? (
                <p className="text-muted-foreground text-sm">Пока ничего не проведено.</p>
              ) : (
                done.map((intake) => (
                  <div
                    key={intake.id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
                  >
                    <div>
                      <span className="font-medium">{intake.account_title}</span>
                      <p className="text-muted-foreground text-sm">
                        {intake.period_end ? monthLabel(intake.period_end.slice(0, 7)) : ""} ·{" "}
                        {money(intake.amount)} ₽
                      </p>
                    </div>
                    <div className="flex gap-2">
                      {intake.has_document ? (
                        <Button variant="ghost" size="sm" onClick={() => setReviewing(intake)}>
                          <FileImage className="mr-2 size-4" />
                          Документ
                        </Button>
                      ) : null}
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={revoke.isPending}
                        onClick={() => revoke.mutate(intake.id)}
                      >
                        Отозвать
                      </Button>
                    </div>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="calendar">
          <Card>
            <CardHeader>
              <CardTitle>Месяц за месяцем</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="text-muted-foreground text-sm">
                Строка есть у каждого месяца — в том числе у того, за который документ не приносили.
                Иначе пропущенный месяц не оставляет следа нигде.
              </p>
              {calendarQuery.isError ? (
                // Молчать здесь опаснее всего: пустой календарь читается как «всё принесли».
                <p className="text-destructive text-sm">
                  Календарь не загрузился: {apiErrorMessage(calendarQuery.error)}
                </p>
              ) : null}
              {(calendarQuery.data ?? []).map((row) => (
                <div
                  key={`${row.account_id}-${row.month}`}
                  className="flex items-center justify-between gap-3 rounded-lg border p-3"
                >
                  <div>
                    <span className="font-medium">{row.account_title}</span>
                    <p className="text-muted-foreground text-sm">{monthLabel(row.month.slice(0, 7))}</p>
                  </div>
                  <div className="flex items-center gap-3">
                    {row.amount ? <span className="tabular-nums">{money(row.amount)} ₽</span> : null}
                    <Badge variant={STATE_BADGE[row.state].variant}>{STATE_BADGE[row.state].label}</Badge>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="accounts">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle>Потоки</CardTitle>
              <Button size="sm" variant="outline" onClick={() => setAccountDialog("new")}>
                <Plus className="mr-2 size-4" />
                Добавить
              </Button>
            </CardHeader>
            <CardContent className="space-y-2">
              {accountsQuery.isError ? (
                <p className="text-destructive text-sm">
                  Потоки не загрузились: {apiErrorMessage(accountsQuery.error)}
                </p>
              ) : accounts.length === 0 ? (
                <p className="text-muted-foreground text-sm">
                  Заведите поток: помещение, вид услуги, кому платим и по какой статье.
                </p>
              ) : (
                accounts.map((account) => (
                  <div
                    key={account.id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
                  >
                    <div>
                      <span className="font-medium">
                        {account.kind_label} · {account.location_name}
                      </span>
                      <p className="text-muted-foreground text-sm">
                        {account.counterparty_name} · {account.dds_article_name}
                        {account.expected_day ? ` · ждём к ${account.expected_day} числу` : ""}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {!account.is_active ? <Badge variant="outline">выключен</Badge> : null}
                      <Button variant="ghost" size="sm" onClick={() => setAccountDialog(account)}>
                        <Pencil className="size-4" />
                      </Button>
                    </div>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Новая платёжка</DialogTitle>
            <DialogDescription>
              Загрузите фото или PDF — сумму и месяц сверите с документом на следующем шаге. По
              газу платёжки не бывает: файл можно не прикладывать, достаточно суммы со слов
              арендодателя.
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-3"
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              saveIntake.mutate();
            }}
          >
            <div className="space-y-1">
              <Label htmlFor="utility-file">Фото или PDF</Label>
              <Input
                id="utility-file"
                type="file"
                accept="image/*,application/pdf"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </div>
            <div className="space-y-1">
              <Label>Поток</Label>
              <Select
                value={form.accountId}
                onValueChange={(value) => setForm((prev) => ({ ...prev, accountId: value }))}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Выберите помещение и вид услуги" />
                </SelectTrigger>
                <SelectContent>
                  {activeAccounts.map((account) => (
                    <SelectItem key={account.id} value={account.id}>
                      {account.kind_label} · {account.location_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => setUploadOpen(false)}>
                Отмена
              </Button>
              <Button type="submit" disabled={saveIntake.isPending}>
                {saveIntake.isPending ? <LoaderCircle className="mr-2 size-4 animate-spin" /> : null}
                Загрузить и разобрать
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <UtilityReviewDialog
        intake={reviewing}
        accounts={accounts}
        onClose={() => setReviewing(null)}
        onSaved={refresh}
      />

      <AccountDialog
        value={accountDialog}
        onClose={() => setAccountDialog(null)}
        onSaved={async () => {
          setAccountDialog(null);
          await refresh();
        }}
      />
    </div>
  );
}

function AccountDialog({
  value,
  onClose,
  onSaved,
}: {
  value: UtilityAccountRecord | "new" | null;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const isNew = value === "new";
  const record = isNew ? null : value;
  const [form, setForm] = useState({
    locationId: "",
    kind: "water" as UtilityKind,
    counterpartyId: "",
    articleId: "",
    expectedDay: "",
    startedOn: todayIso(),
    endedOn: "",
    isActive: true,
    note: "",
  });
  const [initialised, setInitialised] = useState<string | null>(null);

  const locationsQuery = useQuery({
    queryKey: ["locations"],
    queryFn: getLocations,
    enabled: value !== null,
  });
  const articlesQuery = useQuery({
    queryKey: ["dds-articles"],
    queryFn: getDdsArticles,
    enabled: value !== null,
  });
  const counterpartiesQuery = useQuery({
    queryKey: ["counterparties-for-utilities"],
    queryFn: getCounterpartyDirectory,
    enabled: value !== null,
  });

  // Статья коммуналки — с признаком помещения, но НЕ арендная: арендная зарезервирована за
  // договором аренды, и один поток не должен закрывать месяц другого.
  const articles = (articlesQuery.data ?? []).filter(
    (article) => article.is_active && article.location_required && !article.lease_bound,
  );

  const key = value === "new" ? "new" : (value?.id ?? null);
  if (value === null && initialised !== null) {
    // Диалог закрыт — забываем, что заполняли: следующее открытие того же потока обязано
    // показать сохранённые данные, а не брошенные правки.
    setInitialised(null);
  }
  if (value !== null && initialised !== key) {
    setInitialised(key);
    setForm({
      locationId: record?.location_id ?? "",
      kind: record?.kind ?? "water",
      counterpartyId: record?.counterparty_id ?? "",
      articleId: record?.dds_article_id ?? "",
      expectedDay: record?.expected_day ? String(record.expected_day) : "",
      startedOn: record?.started_on ?? todayIso(),
      endedOn: record?.ended_on ?? "",
      isActive: record?.is_active ?? true,
      note: record?.note ?? "",
    });
  }

  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        location_id: form.locationId,
        kind: form.kind,
        counterparty_id: form.counterpartyId,
        dds_article_id: form.articleId,
        expected_day: form.expectedDay ? Number(form.expectedDay) : null,
        started_on: form.startedOn,
        // PATCH — полная замена: не пошли мы ended_on, и дата закрытия потока молча обнулилась бы.
        ended_on: form.endedOn || null,
        is_active: form.isActive,
        note: form.note || null,
      };
      return record ? updateUtilityAccount(record.id, payload) : createUtilityAccount(payload);
    },
    onSuccess: async () => {
      toast.success(record ? "Поток обновлён" : "Поток заведён");
      await onSaved();
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  return (
    <Dialog open={value !== null} onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{record ? "Поток" : "Новый поток"}</DialogTitle>
          <DialogDescription>
            За какой ресурс и какому помещению, кому платим и по какой статье расход.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-3"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label>Помещение</Label>
              <Select
                value={form.locationId}
                onValueChange={(v) => setForm((prev) => ({ ...prev, locationId: v }))}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Выберите" />
                </SelectTrigger>
                <SelectContent>
                  {(locationsQuery.data ?? []).map((location) => (
                    <SelectItem key={location.id} value={location.id}>
                      {location.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label>Вид услуги</Label>
              <Select
                value={form.kind}
                onValueChange={(v) => setForm((prev) => ({ ...prev, kind: v as UtilityKind }))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(KIND_LABELS) as UtilityKind[]).map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {KIND_LABELS[kind]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1">
            <Label>Кому возмещаем</Label>
            <Select
              value={form.counterpartyId}
              onValueChange={(v) => setForm((prev) => ({ ...prev, counterpartyId: v }))}
            >
              <SelectTrigger>
                <SelectValue placeholder="Арендодатель" />
              </SelectTrigger>
              <SelectContent>
                {(counterpartiesQuery.data ?? []).map((counterparty) => (
                  <SelectItem key={counterparty.id} value={counterparty.id}>
                    {counterparty.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label>Статья расхода</Label>
            <Select
              value={form.articleId}
              onValueChange={(v) => setForm((prev) => ({ ...prev, articleId: v }))}
            >
              <SelectTrigger>
                <SelectValue placeholder="Коммунальные платежи" />
              </SelectTrigger>
              <SelectContent>
                {articles.map((article) => (
                  <SelectItem key={article.id} value={article.id}>
                    {article.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="utility-expected">Ждём документ к числу</Label>
              <Input
                id="utility-expected"
                inputMode="numeric"
                placeholder="например 10"
                value={form.expectedDay}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, expectedDay: event.target.value }))
                }
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="utility-started">Ведём с</Label>
              <Input
                id="utility-started"
                type="date"
                value={form.startedOn}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, startedOn: event.target.value }))
                }
                required
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="utility-ended">Закрыт с</Label>
              <Input
                id="utility-ended"
                type="date"
                value={form.endedOn}
                onChange={(event) => setForm((prev) => ({ ...prev, endedOn: event.target.value }))}
              />
              <p className="text-muted-foreground text-xs">
                Пусто — поток действует. Дата закрывает его: календарь перестанет ждать документы.
              </p>
            </div>
            <div className="space-y-1">
              <Label htmlFor="utility-active">Состояние</Label>
              <div className="flex items-center gap-2 pt-2">
                <Switch
                  id="utility-active"
                  checked={form.isActive}
                  onCheckedChange={(checked) =>
                    setForm((prev) => ({ ...prev, isActive: checked }))
                  }
                />
                <span className="text-sm">{form.isActive ? "Действует" : "Выключен"}</span>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Отмена
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? <LoaderCircle className="mr-2 size-4 animate-spin" /> : null}
              Сохранить
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
