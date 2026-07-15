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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import { apiErrorMessage, getDdsArticles } from "@/lib/api";

import { BarterSection } from "./BarterSection";
import {
  addCollectionSource,
  addRoutingRule,
  archiveCounterparty,
  deleteCollectionSource,
  deleteRoutingRule,
  getCounterpartyCard,
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
  COUNTERPARTY_REQUISITE_FIELDS,
  COUNTERPARTY_TYPE_LABELS,
  RELATIONSHIP_HINTS,
  RELATIONSHIP_LABELS,
  RelationshipBadge,
  formatRub,
} from "./shared";

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
      <Tabs defaultValue="general">
        <TabsList className="grid w-full grid-cols-3">
          <TabsTrigger value="general">Общая информация</TabsTrigger>
          <TabsTrigger value="requisites">Реквизиты</TabsTrigger>
          <TabsTrigger value="manager">Данные менеджера</TabsTrigger>
        </TabsList>
        {/* «Общая информация» — всё, что описывает самого контрагента: профиль, откуда
            приходят его счета и доступен ли он Кассе. Накладные тут не показываем: карточка
            отвечает на «кто это», а не «сколько ему должны» — для этого есть «Накладные». */}
        <TabsContent value="general" className="mt-5 space-y-8">
          <ProfileSection card={card} canAdmin={canAdmin} />
          <CollectionSourcesSection card={card} canAdmin={canAdmin} />
          {canOperate ? <KassaSection card={card} /> : null}
        </TabsContent>
        <TabsContent value="requisites" className="mt-5">
          <RequisitesSection card={card} canAdmin={canAdmin} />
        </TabsContent>
        <TabsContent value="manager" className="mt-5">
          <ManagerSection card={card} canAdmin={canAdmin} />
        </TabsContent>
      </Tabs>
      {card.aliases.some((alias) => alias.source === "iiko") ? (
        <RoutingSection card={card} canAdmin={canAdmin} />
      ) : null}
      {card.relationship === "barter" ? (
        <BarterSection counterpartyId={card.counterparty_id} canOperate={canOperate} />
      ) : null}
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
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const profile = card.profile;
  const [relationship, setRelationship] = useState("official");
  const [name, setName] = useState("");
  const [type, setType] = useState("legal_entity");
  const [ddsArticleId, setDdsArticleId] = useState("");
  const [confirmNoDdsArticle, setConfirmNoDdsArticle] = useState(false);
  const [servicePeriodRequired, setServicePeriodRequired] = useState(false);
  const [servicePeriodMode, setServicePeriodMode] = useState<"automatic" | "manual">("manual");
  const [periodOffset, setPeriodOffset] = useState("0");

  useEffect(() => {
    setRelationship(profile?.relationship ?? "official");
    setName(card.name);
    setType(card.type);
    setDdsArticleId(profile?.default_dds_article_id ?? "");
    setConfirmNoDdsArticle(profile?.confirm_no_dds_article ?? false);
    setServicePeriodRequired(profile?.service_period_required ?? false);
    setServicePeriodMode(profile?.service_period_mode ?? "manual");
    setPeriodOffset(
      profile?.default_service_period_offset_months != null
        ? String(profile.default_service_period_offset_months)
        : "0",
    );
  }, [profile, card.counterparty_id, card.name, card.type]);

  const saveMutation = useMutation({
    mutationFn: () =>
      updateProfile(card.counterparty_id, {
        name: relationship === "official" ? undefined : name.trim(),
        inn: relationship === "official" ? undefined : null,
        type,
        relationship,
        default_dds_article_id: ddsArticleId || null,
        confirm_no_dds_article: confirmNoDdsArticle,
        service_period_required: servicePeriodRequired,
        service_period_mode: servicePeriodMode,
        default_service_period_offset_months: servicePeriodRequired ? Number(periodOffset) : null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Исходные данные сохранены");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить")),
  });

  const disabled = !canAdmin;
  const canSave = Boolean(
    (relationship === "official" || name.trim()) && (ddsArticleId || confirmNoDdsArticle),
  );

  return (
    <Section title="Общая информация">
      <div className="grid gap-4 sm:grid-cols-2">
        {relationship !== "official" ? (
          <Field label="Название контрагента">
            <Input
              disabled={disabled}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
        ) : null}
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
          {profile?.relationship_manual ? (
            <p className="text-xs text-muted-foreground">
              Закреплено вручную — синхронизация из iiko не изменит тип.
            </p>
          ) : null}
        </Field>
        <Field label="Тип контрагента">
          <Select disabled={disabled} value={type} onValueChange={setType}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(COUNTERPARTY_TYPE_LABELS).map(([value, label]) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Статья ДДС по умолчанию">
          <ArticleCombobox
            articles={articlesQuery.data ?? []}
            value={ddsArticleId}
            onChange={setDdsArticleId}
            disabled={disabled || confirmNoDdsArticle}
            placeholder="Не выбрана"
          />
          <p className="text-xs text-muted-foreground">
            Подставляется в окно «В банк» при оплате счетов этого контрагента.
          </p>
          <label className="flex items-center gap-2 text-sm">
            <Switch
              checked={confirmNoDdsArticle}
              disabled={disabled}
              onCheckedChange={(checked) => {
                setConfirmNoDdsArticle(checked);
                if (checked) setDdsArticleId("");
              }}
            />
            У контрагента нет статьи ДДС
          </label>
        </Field>
        <div className="grid gap-3 rounded-md border p-3 sm:col-span-2">
          <label className="flex items-start gap-3 text-sm">
            <Switch
              checked={servicePeriodRequired}
              disabled={disabled}
              onCheckedChange={setServicePeriodRequired}
            />
            <span>
              <span className="block font-medium">Требовать период оказания услуг</span>
              <span className="block text-xs text-muted-foreground">
                Платёж без периода нельзя отправить в банк.
              </span>
            </span>
          </label>
          {servicePeriodRequired ? (
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Заполнение периода">
                <Select
                  disabled={disabled}
                  value={servicePeriodMode}
                  onValueChange={(value) =>
                    setServicePeriodMode(value as "automatic" | "manual")
                  }
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="automatic">Автоматически из счёта / ЭДО</SelectItem>
                    <SelectItem value="manual">Вручную</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Период по умолчанию">
                <Select disabled={disabled} value={periodOffset} onValueChange={setPeriodOffset}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="-1">Предыдущий месяц</SelectItem>
                    <SelectItem value="0">Месяц платежа</SelectItem>
                    <SelectItem value="1">Следующий месяц</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            </div>
          ) : null}
        </div>
      </div>
      {canAdmin ? (
        <Button
          disabled={!canSave || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : null}
          Сохранить
        </Button>
      ) : null}
    </Section>
  );
}

function ManagerSection({ card, canAdmin }: { card: CardData; canAdmin: boolean }) {
  const queryClient = useQueryClient();
  const [managerName, setManagerName] = useState("");
  const [managerPhone, setManagerPhone] = useState("");

  useEffect(() => {
    setManagerName(card.profile?.manager_name ?? "");
    setManagerPhone(card.profile?.manager_phone ?? "");
  }, [card.profile, card.counterparty_id]);

  const mutation = useMutation({
    mutationFn: () =>
      updateProfile(card.counterparty_id, {
        manager_name: managerName.trim() || null,
        manager_phone: managerPhone.trim() || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Данные менеджера сохранены");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить")),
  });

  return (
    <Section title="Данные менеджера">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Менеджер поставщика">
          <Input
            disabled={!canAdmin}
            value={managerName}
            onChange={(event) => setManagerName(event.target.value)}
          />
        </Field>
        <Field label="Телефон менеджера">
          <Input
            disabled={!canAdmin}
            value={managerPhone}
            onChange={(event) => setManagerPhone(event.target.value)}
          />
        </Field>
      </div>
      {canAdmin ? (
        <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
          {mutation.isPending ? (
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
    COUNTERPARTY_REQUISITE_FIELDS.forEach(({ key }) => {
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
        COUNTERPARTY_REQUISITE_FIELDS.forEach(({ key }) => {
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

  if (card.relationship !== "official") {
    return (
      <Section title="Реквизиты">
        <div className="rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <p className="font-medium">Реквизиты недоступны</p>
          <p className="mt-1 text-amber-800">
            Неофициальные контрагенты оплачиваются переводом на карту или наличными.
          </p>
        </div>
      </Section>
    );
  }

  return (
    <Section title="Платёжные реквизиты">
      {!profile?.requisites_verified ? (
        <p className="text-sm text-amber-600">
          Без подтверждённых реквизитов отправка в банк недоступна.
        </p>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2">
        {COUNTERPARTY_REQUISITE_FIELDS.map(({ key, label }) => (
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
    mutationFn: () => addCollectionSource(card.counterparty_id, { kind, value: value || null }),
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
  const registryQuery = useQuery({
    queryKey: ["cp", "registry", "all"],
    queryFn: () => getRegistry(),
  });
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
          <p className="text-sm text-muted-foreground">
            Правил нет — накладные идут на этого контрагента.
          </p>
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


function ArchiveSection({ card }: { card: CardData }) {
  const queryClient = useQueryClient();
  const archived = card.status === "archived";
  const mutation = useMutation({
    mutationFn: () =>
      archived
        ? unarchiveCounterparty(card.counterparty_id)
        : archiveCounterparty(card.counterparty_id),
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
