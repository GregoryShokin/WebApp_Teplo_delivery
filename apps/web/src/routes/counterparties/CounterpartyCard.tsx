import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, LoaderCircle, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { apiErrorMessage } from "@/lib/api";

import { BarterSection } from "./BarterSection";
import {
  addCollectionSource,
  addRoutingRule,
  archiveCounterparty,
  deleteCollectionSource,
  deleteRoutingRule,
  getCounterpartyCard,
  getLedgerCategories,
  getRegistry,
  getRequisitesSuggestion,
  setKassaEnabled,
  setRequisites,
  unarchiveCounterparty,
  updateProfile,
  type CounterpartyCard as CardData,
} from "./api";
import {
  COLLECTION_KIND_LABELS,
  COUNTERPARTY_TYPE_LABELS,
  INVOICE_DIRECTION_LABELS,
  InvoiceStatusBadge,
  RELATIONSHIP_HINTS,
  RELATIONSHIP_LABELS,
  RelationshipBadge,
  SOURCE_LABELS,
  formatRub,
} from "./shared";

const REQUISITE_FIELDS: Array<{ key: string; label: string }> = [
  { key: "recipientName", label: "Получатель" },
  { key: "inn", label: "ИНН" },
  { key: "kpp", label: "КПП" },
  { key: "bankAcnt", label: "Расчётный счёт" },
  { key: "bankBik", label: "БИК" },
  { key: "recipientCorrAccountNumber", label: "Корр. счёт" },
];

export function CounterpartyCard({
  counterpartyId,
  canOperate,
  canAdmin,
  onClose,
}: {
  counterpartyId: string | null;
  canOperate: boolean;
  canAdmin: boolean;
  onClose: () => void;
}) {
  const cardQuery = useQuery({
    queryKey: ["cp", "card", counterpartyId],
    queryFn: () => getCounterpartyCard(counterpartyId as string),
    enabled: Boolean(counterpartyId),
  });

  return (
    <Sheet open={Boolean(counterpartyId)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>{cardQuery.data?.name ?? "Контрагент"}</SheetTitle>
          <SheetDescription>
            {cardQuery.data
              ? `${COUNTERPARTY_TYPE_LABELS[cardQuery.data.type] ?? cardQuery.data.type}${
                  cardQuery.data.inn ? ` · ИНН ${cardQuery.data.inn}` : ""
                }`
              : "Загрузка…"}
          </SheetDescription>
        </SheetHeader>
        {cardQuery.data ? (
          <CardBody card={cardQuery.data} canOperate={canOperate} canAdmin={canAdmin} />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function CardBody({
  card,
  canOperate,
  canAdmin,
}: {
  card: CardData;
  canOperate: boolean;
  canAdmin: boolean;
}) {
  return (
    <div className="mt-5 space-y-8">
      <div className="flex flex-wrap items-center gap-2">
        <RelationshipBadge relationship={card.relationship} />
        {card.status === "archived" ? (
          <Badge className="border-muted bg-muted text-muted-foreground">В архиве</Badge>
        ) : null}
      </div>
      {card.relationship === "barter" ? <BarterBalanceBanner card={card} /> : null}
      <ProfileSection card={card} canAdmin={canAdmin} />
      <RequisitesSection card={card} canAdmin={canAdmin} />
      <CollectionSourcesSection card={card} canAdmin={canAdmin} />
      {card.aliases.some((alias) => alias.source === "iiko") ? (
        <RoutingSection card={card} canAdmin={canAdmin} />
      ) : null}
      <InvoicesSection card={card} />
      {card.relationship === "barter" ? (
        <BarterSection counterpartyId={card.counterparty_id} canOperate={canOperate} />
      ) : null}
      {canOperate ? <KassaSection card={card} /> : null}
      {canAdmin ? <ArchiveSection card={card} /> : null}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3 border-t pt-5 first:border-t-0 first:pt-0">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function ProfileSection({ card, canAdmin }: { card: CardData; canAdmin: boolean }) {
  const queryClient = useQueryClient();
  const categoriesQuery = useQuery({ queryKey: ["cp", "categories"], queryFn: getLedgerCategories });
  const profile = card.profile;
  const [relationship, setRelationship] = useState("official");
  const [internalName, setInternalName] = useState("");
  const [brandGroup, setBrandGroup] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [delayDays, setDelayDays] = useState("");
  const [dueDay, setDueDay] = useState("");
  const [managerName, setManagerName] = useState("");
  const [managerPhone, setManagerPhone] = useState("");

  useEffect(() => {
    setRelationship(profile?.relationship ?? "official");
    setInternalName(profile?.internal_name ?? "");
    setBrandGroup(profile?.brand_group ?? "");
    setCategoryId(profile?.ledger_category_id ?? "");
    setDelayDays(profile?.payment_delay_days != null ? String(profile.payment_delay_days) : "");
    setDueDay(
      profile?.payment_due_day_of_month != null ? String(profile.payment_due_day_of_month) : "",
    );
    setManagerName(profile?.manager_name ?? "");
    setManagerPhone(profile?.manager_phone ?? "");
  }, [profile, card.counterparty_id]);

  const saveMutation = useMutation({
    mutationFn: () =>
      updateProfile(card.counterparty_id, {
        relationship,
        internal_name: internalName || null,
        brand_group: brandGroup || null,
        ledger_category_id: categoryId || null,
        payment_delay_days: delayDays ? Number(delayDays) : null,
        payment_due_day_of_month: dueDay ? Number(dueDay) : null,
        manager_name: managerName || null,
        manager_phone: managerPhone || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Исходные данные сохранены");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить")),
  });

  const disabled = !canAdmin;

  return (
    <Section title="Исходные данные">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Тип отношений">
          <Select disabled={disabled} value={relationship} onValueChange={setRelationship}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(RELATIONSHIP_LABELS).map(([value, label]) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">{RELATIONSHIP_HINTS[relationship]}</p>
        </Field>
        <Field label="Внутреннее имя (рудимент)">
          <Input
            disabled={disabled}
            value={internalName}
            onChange={(event) => setInternalName(event.target.value)}
          />
        </Field>
        <Field label="Группа-бренд">
          <Input
            disabled={disabled}
            value={brandGroup}
            onChange={(event) => setBrandGroup(event.target.value)}
          />
        </Field>
        <Field label="Категория (леджер)">
          <Select disabled={disabled} value={categoryId} onValueChange={setCategoryId}>
            <SelectTrigger>
              <SelectValue placeholder="Не выбрана" />
            </SelectTrigger>
            <SelectContent>
              {(categoriesQuery.data ?? []).map((category) => (
                <SelectItem key={category.id} value={category.id}>
                  {category.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Отсрочка, дней">
            <Input
              disabled={disabled}
              type="number"
              value={delayDays}
              onChange={(event) => setDelayDays(event.target.value)}
            />
          </Field>
          <Field label="Платить до числа">
            <Input
              disabled={disabled}
              type="number"
              min={1}
              max={31}
              value={dueDay}
              onChange={(event) => setDueDay(event.target.value)}
            />
          </Field>
        </div>
        <Field label="Менеджер поставщика">
          <Input
            disabled={disabled}
            value={managerName}
            onChange={(event) => setManagerName(event.target.value)}
          />
        </Field>
        <Field label="Телефон менеджера">
          <Input
            disabled={disabled}
            value={managerPhone}
            onChange={(event) => setManagerPhone(event.target.value)}
          />
        </Field>
      </div>
      {canAdmin ? (
        <Button disabled={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
          {saveMutation.isPending ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : null}
          Сохранить
        </Button>
      ) : null}
    </Section>
  );
}

function RequisitesSection({ card, canAdmin }: { card: CardData; canAdmin: boolean }) {
  const queryClient = useQueryClient();
  const profile = card.profile;
  const [values, setValues] = useState<Record<string, string>>({});
  const [verified, setVerified] = useState(false);

  useEffect(() => {
    const source = (profile?.requisites ?? {}) as Record<string, unknown>;
    const next: Record<string, string> = {};
    REQUISITE_FIELDS.forEach(({ key }) => {
      next[key] = source[key] != null ? String(source[key]) : "";
    });
    setValues(next);
    setVerified(Boolean(profile?.requisites_verified));
  }, [profile, card.counterparty_id]);

  const suggestionMutation = useMutation({
    mutationFn: () => getRequisitesSuggestion(card.counterparty_id),
    onSuccess: (suggestion) => {
      setValues((prev) => {
        const next = { ...prev };
        REQUISITE_FIELDS.forEach(({ key }) => {
          if (suggestion[key] != null) {
            next[key] = String(suggestion[key]);
          }
        });
        return next;
      });
      toast.success("Реквизиты подтянуты из истории платежей — проверьте и подтвердите");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Нет данных в истории платежей")),
  });

  const saveMutation = useMutation({
    mutationFn: () => {
      const requisites: Record<string, string> = {};
      Object.entries(values).forEach(([key, value]) => {
        if (value.trim()) {
          requisites[key] = value.trim();
        }
      });
      return setRequisites(card.counterparty_id, { requisites, verified });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Реквизиты сохранены");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить реквизиты")),
  });

  const disabled = !canAdmin;

  return (
    <Section title="Платёжные реквизиты">
      {!profile?.requisites_verified ? (
        <p className="text-sm text-amber-600">
          Без подтверждённых реквизитов отправка в банк недоступна.
        </p>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2">
        {REQUISITE_FIELDS.map(({ key, label }) => (
          <Field key={key} label={label}>
            <Input
              disabled={disabled}
              value={values[key] ?? ""}
              onChange={(event) => setValues((prev) => ({ ...prev, [key]: event.target.value }))}
            />
          </Field>
        ))}
      </div>
      {canAdmin ? (
        <div className="flex flex-wrap items-center gap-4">
          <Button
            variant="outline"
            disabled={suggestionMutation.isPending}
            onClick={() => suggestionMutation.mutate()}
          >
            <Download size={16} aria-hidden="true" />
            Подтянуть из истории
          </Button>
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={verified} onCheckedChange={setVerified} />
            Реквизиты проверены
          </label>
          <Button disabled={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            {saveMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </div>
      ) : null}
    </Section>
  );
}

function CollectionSourcesSection({ card, canAdmin }: { card: CardData; canAdmin: boolean }) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("email");
  const [value, setValue] = useState("");

  const addMutation = useMutation({
    mutationFn: () =>
      addCollectionSource(card.counterparty_id, { kind, value: value || null }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      setValue("");
      toast.success("Источник добавлен");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить источник")),
  });

  const removeMutation = useMutation({
    mutationFn: (sourceId: string) => deleteCollectionSource(card.counterparty_id, sourceId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Источник удалён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить источник")),
  });

  return (
    <Section title="Источники сбора информации">
      <div className="grid gap-2">
        {card.collection_sources.length === 0 ? (
          <p className="text-sm text-muted-foreground">Источники не настроены.</p>
        ) : null}
        {card.collection_sources.map((source) => (
          <div
            key={source.id}
            className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
          >
            <span className="min-w-0 truncate">
              <Badge variant="outline" className="mr-2">
                {COLLECTION_KIND_LABELS[source.kind] ?? source.kind}
              </Badge>
              {source.value ?? "—"}
            </span>
            {canAdmin ? (
              <Button
                onClick={() => removeMutation.mutate(source.id)}
                size="icon"
                variant="ghost"
                title="Удалить"
              >
                <Trash2 size={15} aria-hidden="true" />
              </Button>
            ) : null}
          </div>
        ))}
      </div>
      {canAdmin ? (
        <div className="flex flex-wrap items-end gap-2">
          <div className="grid gap-2">
            <Label>Канал</Label>
            <Select value={kind} onValueChange={setKind}>
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="email">Почта</SelectItem>
                <SelectItem value="telegram">Telegram</SelectItem>
                <SelectItem value="iiko">iiko</SelectItem>
                <SelectItem value="manual">Ручной ввод</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Input
            className="flex-1"
            placeholder="email / @handle / id"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <Button
            variant="outline"
            disabled={addMutation.isPending || (kind !== "manual" && !value.trim())}
            onClick={() => addMutation.mutate()}
          >
            Добавить
          </Button>
        </div>
      ) : null}
    </Section>
  );
}

function RoutingSection({ card, canAdmin }: { card: CardData; canAdmin: boolean }) {
  const queryClient = useQueryClient();
  const registryQuery = useQuery({ queryKey: ["cp", "registry", "all"], queryFn: () => getRegistry() });
  const [prefix, setPrefix] = useState("");
  const [target, setTarget] = useState("");

  const addMutation = useMutation({
    mutationFn: () =>
      addRoutingRule(card.counterparty_id, { prefix, target_counterparty_id: target }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      setPrefix("");
      setTarget("");
      toast.success("Правило маршрутизации добавлено");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить правило")),
  });

  const removeMutation = useMutation({
    mutationFn: (ruleId: string) => deleteRoutingRule(card.counterparty_id, ruleId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Правило удалено");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить правило")),
  });

  return (
    <Section title="Маршрутизация бренда (iiko → юрлицо)">
      <p className="text-xs text-muted-foreground">
        Один поставщик в iiko может платиться на несколько юрлиц. Накладная привязывается по
        префиксу номера документа: напр. ТРКА → ООО «ТОРА», 0ЭКА → ИП Скачкова.
      </p>
      <div className="grid gap-2">
        {card.routing_rules.length === 0 ? (
          <p className="text-sm text-muted-foreground">Правил нет — накладные идут на этого контрагента.</p>
        ) : null}
        {card.routing_rules.map((rule) => (
          <div
            key={rule.id}
            className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
          >
            <span>
              <Badge variant="outline" className="mr-2">
                {rule.prefix}*
              </Badge>
              → {rule.target_name}
            </span>
            {canAdmin ? (
              <Button
                size="icon"
                variant="ghost"
                title="Удалить"
                onClick={() => removeMutation.mutate(rule.id)}
              >
                <Trash2 size={15} aria-hidden="true" />
              </Button>
            ) : null}
          </div>
        ))}
      </div>
      {canAdmin ? (
        <div className="flex flex-wrap items-end gap-2">
          <div className="grid gap-2">
            <Label>Префикс</Label>
            <Input
              className="w-32"
              placeholder="ТРКА"
              value={prefix}
              onChange={(event) => setPrefix(event.target.value)}
            />
          </div>
          <div className="grid min-w-[180px] flex-1 gap-2">
            <Label>Юрлицо</Label>
            <Select value={target} onValueChange={setTarget}>
              <SelectTrigger>
                <SelectValue placeholder="Выберите юрлицо" />
              </SelectTrigger>
              <SelectContent>
                {(registryQuery.data ?? []).map((item) => (
                  <SelectItem key={item.counterparty_id} value={item.counterparty_id}>
                    {item.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            variant="outline"
            disabled={!prefix.trim() || !target || addMutation.isPending}
            onClick={() => addMutation.mutate()}
          >
            Добавить
          </Button>
        </div>
      ) : null}
    </Section>
  );
}

function BarterBalanceBanner({ card }: { card: CardData }) {
  const net = card.barter_balance;
  return (
    <div className="rounded-md border border-violet-200 bg-violet-50/50 p-3 text-sm">
      <span className="font-medium">Сальдо по бартеру: </span>
      {net === 0 ? (
        <span className="text-muted-foreground">расчёты закрыты</span>
      ) : net > 0 ? (
        <span>
          мы должны <span className="font-medium tabular-nums">{formatRub(net)}</span>
        </span>
      ) : (
        <span className="text-emerald-700">
          нам должны <span className="font-medium tabular-nums">{formatRub(Math.abs(net))}</span>
        </span>
      )}
    </div>
  );
}

function InvoicesSection({ card }: { card: CardData }) {
  const payables = card.invoices.filter((invoice) => invoice.direction === "payable");
  const receivables = card.invoices.filter((invoice) => invoice.direction === "receivable");
  return (
    <Section title="Накладные">
      <InvoiceList title={INVOICE_DIRECTION_LABELS.payable} invoices={payables} />
      {receivables.length > 0 ? (
        <InvoiceList title={INVOICE_DIRECTION_LABELS.receivable} invoices={receivables} />
      ) : null}
    </Section>
  );
}

function InvoiceList({ title, invoices }: { title: string; invoices: CardData["invoices"] }) {
  return (
    <div className="space-y-2">
      <h4 className="text-xs font-medium uppercase text-muted-foreground">{title}</h4>
      {invoices.length === 0 ? (
        <p className="text-sm text-muted-foreground">Нет накладных.</p>
      ) : (
        <div className="grid gap-2">
          {invoices.slice(0, 20).map((invoice) => (
            <div
              key={invoice.id}
              className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
            >
              <span className="min-w-0 truncate">
                <Badge variant="outline" className="mr-2">
                  {SOURCE_LABELS[invoice.source] ?? invoice.source}
                </Badge>
                {invoice.number ?? "—"}
              </span>
              <span className="flex items-center gap-3">
                <span className="tabular-nums">{formatRub(invoice.amount)}</span>
                <InvoiceStatusBadge
                  status={invoice.payment_status}
                  direction={invoice.direction}
                  barterSettled={!!invoice.barter_settlement_id}
                  barterRole={invoice.barter_role}
                />
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ArchiveSection({ card }: { card: CardData }) {
  const queryClient = useQueryClient();
  const archived = card.status === "archived";
  const mutation = useMutation({
    mutationFn: () =>
      archived ? unarchiveCounterparty(card.counterparty_id) : archiveCounterparty(card.counterparty_id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success(archived ? "Контрагент возвращён из архива" : "Контрагент в архиве");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось изменить статус")),
  });

  return (
    <Section title="Архив">
      <Button variant="outline" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
        {archived ? "Вернуть из архива" : "В архив"}
      </Button>
    </Section>
  );
}

function KassaSection({ card }: { card: CardData }) {
  const queryClient = useQueryClient();
  const enabled = Boolean(card.profile?.kassa_enabled);
  const mutation = useMutation({
    mutationFn: (next: boolean) => setKassaEnabled(card.counterparty_id, next),
    onSuccess: async (_data, next) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success(next ? "Контрагент активен в Кассе" : "Контрагент скрыт из Кассы");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось переключить")),
  });

  return (
    <Section title="Касса">
      <label className="flex items-center gap-2 text-sm">
        <Switch
          checked={enabled}
          disabled={mutation.isPending}
          onCheckedChange={(value) => mutation.mutate(value)}
        />
        Активен в Кассе
      </label>
      <p className="text-xs text-muted-foreground">
        Когда включено — поставщик доступен в дропдауне при создании накладной через Кассу.
      </p>
    </Section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      {children}
    </div>
  );
}
