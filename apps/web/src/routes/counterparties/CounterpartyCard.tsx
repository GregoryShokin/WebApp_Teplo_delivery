import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, LoaderCircle, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
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
  createMerchantRule,
  getCounterpartyCard,
  getMerchantRules,
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
  OFFICIAL_SUPPLIER_REQUIRED_REQUISITE_KEYS,
  RELATIONSHIP_HINTS,
  RELATIONSHIP_LABELS,
  RelationshipBadge,
  formatRub,
} from "./shared";

/** Реестр форм с несохранёнными правками. Формы живут в разных секциях и сохраняются
 *  каждая своей кнопкой, поэтому карточка узнаёт о «грязном» состоянии только так —
 *  иначе Esc или клик мимо выбрасывают набранное без единого вопроса. */
const DirtyFormsContext = createContext<((formId: string, dirty: boolean) => void) | null>(null);

/** Сообщает карточке, что в этой форме есть несохранённые правки. */
function useReportDirty(formId: string, dirty: boolean) {
  const report = useContext(DirtyFormsContext);
  useEffect(() => {
    report?.(formId, dirty);
    return () => report?.(formId, false);
  }, [report, formId, dirty]);
}

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
  const dirtyForms = useRef(new Set<string>());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const reportDirty = useCallback((formId: string, dirty: boolean) => {
    if (dirty) dirtyForms.current.add(formId);
    else dirtyForms.current.delete(formId);
  }, []);

  function close() {
    dirtyForms.current.clear();
    setConfirmOpen(false);
    onClose();
  }

  return (
    <Sheet
      open={Boolean(counterpartyId)}
      onOpenChange={(open) => {
        if (open) return;
        // Не закрываем молча поверх набранного: Esc, клик мимо и крестик идут сюда же.
        if (dirtyForms.current.size > 0) {
          setConfirmOpen(true);
          return;
        }
        close();
      }}
    >
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
          <DirtyFormsContext.Provider value={reportDirty}>
            <CardBody card={cardQuery.data} canOperate={canOperate} canAdmin={canAdmin} />
          </DirtyFormsContext.Provider>
        ) : null}
      </SheetContent>
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Закрыть без сохранения?</AlertDialogTitle>
            <AlertDialogDescription>
              В карточке есть несохранённые изменения — они пропадут.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setConfirmOpen(false)}>
              Вернуться к правке
            </AlertDialogCancel>
            <AlertDialogAction onClick={close}>Закрыть без сохранения</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
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
        {canOperate ? <KassaToggle card={card} /> : null}
      </div>
      {card.relationship === "barter" ? <BarterBalanceBanner card={card} /> : null}
      <Tabs defaultValue="general">
        <TabsList className="grid w-full grid-cols-3">
          <TabsTrigger value="general">Общая информация</TabsTrigger>
          <TabsTrigger value="requisites">Реквизиты</TabsTrigger>
          <TabsTrigger value="manager">Данные менеджера</TabsTrigger>
        </TabsList>
        {/* «Общая информация» — всё, что описывает самого контрагента: профиль, откуда
            приходят его документы (источники + маршрутизация iiko) и бартерное сальдо.
            Накладные тут не показываем: карточка отвечает на «кто это», а не «сколько ему
            должны» — для этого есть «Накладные». */}
        {/* forceMount + hidden: без него Radix РАЗМОНТИРУЕТ неактивную вкладку — набранные
            реквизиты пропадали при переключении, а cleanup useReportDirty снимал dirty-флаг,
            и гард «Закрыть без сохранения?» молча пропускал потерю правок. */}
        <TabsContent
          value="general"
          forceMount
          className="mt-5 space-y-8 data-[state=inactive]:hidden"
        >
          <ProfileSection card={card} canAdmin={canAdmin} />
          <CollectionSourcesSection card={card} canAdmin={canAdmin} />
          {card.aliases.some((alias) => alias.source === "iiko") ? (
            <RoutingSection card={card} canAdmin={canAdmin} />
          ) : null}
          {card.relationship === "barter" ? (
            <BarterSection counterpartyId={card.counterparty_id} canOperate={canOperate} />
          ) : null}
        </TabsContent>
        <TabsContent
          value="requisites"
          forceMount
          className="mt-5 data-[state=inactive]:hidden"
        >
          <RequisitesSection card={card} canAdmin={canAdmin} />
        </TabsContent>
        <TabsContent value="manager" forceMount className="mt-5 data-[state=inactive]:hidden">
          <ManagerSection card={card} canAdmin={canAdmin} />
        </TabsContent>
      </Tabs>
      {/* Архив — действие над всей карточкой, а не свойство вкладки: оставляем внизу,
          но отделяем, чтобы не читался как часть последней открытой вкладки. */}
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
  const [bankPrepayment, setBankPrepayment] = useState(false);
  const [periodOffset, setPeriodOffset] = useState("0");

  useEffect(() => {
    setRelationship(profile?.relationship ?? "official");
    setName(card.name);
    setType(card.type);
    setDdsArticleId(profile?.default_dds_article_id ?? "");
    setConfirmNoDdsArticle(profile?.confirm_no_dds_article ?? false);
    setServicePeriodRequired(profile?.service_period_required ?? false);
    setBankPrepayment(profile?.bank_payments_create_prepayment ?? false);
    setPeriodOffset(
      profile?.default_service_period_offset_months != null
        ? String(profile.default_service_period_offset_months)
        : "0",
    );
    // Заливаем серверные значения ТОЛЬКО при смене контрагента. Если зависеть от profile,
    // любой фоновый рефетч (например, от соседнего мгновенного тумблера «Активен в Кассе»,
    // который инвалидирует весь ключ ["cp"]) молча затирает несохранённые правки формы.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.counterparty_id]);

  const saveMutation = useMutation({
    mutationFn: () =>
      updateProfile(card.counterparty_id, {
        name: relationship === "official" ? undefined : name.trim(),
        // ИНН эта форма НЕ трогает (не слать даже null: backend понял бы это как «стереть»).
        // Он живёт в «Реквизитах» у официальных, а у неофициалов хранится как пришёл из
        // iiko/ЭДО — по нему синки узнают контрагента, затирание плодит дубли.
        type,
        relationship,
        default_dds_article_id: ddsArticleId || null,
        confirm_no_dds_article: confirmNoDdsArticle,
        service_period_required: servicePeriodRequired,
        bank_payments_create_prepayment: bankPrepayment,
        default_service_period_offset_months: servicePeriodRequired ? Number(periodOffset) : null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Общая информация сохранена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить")),
  });

  const disabled = !canAdmin;
  // Почему «Сохранить» не нажимается — говорим вслух: поле-виновник часто за сгибом,
  // а погасшая кнопка без причины читается как «сломалось».
  const saveBlockedReason =
    relationship !== "official" && !name.trim()
      ? "Укажите название контрагента"
      : !ddsArticleId && !confirmNoDdsArticle
        ? "Выберите статью ДДС или отметьте, что её нет"
        : null;
  const canSave = !saveBlockedReason;
  // Уход из «официального» стирает банковские реквизиты (правило реестра: они относятся
  // только к банковскому каналу; ИНН сохраняется — по нему синки узнают контрагента).
  // Предупреждаем ДО сохранения — иначе данные исчезают молча.
  const leavingOfficialWithRequisites =
    profile?.relationship === "official" &&
    relationship !== "official" &&
    Object.keys(profile?.requisites ?? {}).length > 0;
  const periodOffsetSaved =
    profile?.default_service_period_offset_months != null
      ? String(profile.default_service_period_offset_months)
      : "0";
  // Сравниваем ТО, что уйдёт при сохранении (trim) — иначе хвостовой пробел оставляет
  // форму «вечно несохранённой» после успешного сохранения и даёт ложный гард закрытия.
  const dirty =
    relationship !== (profile?.relationship ?? "official") ||
    (relationship !== "official" && name.trim() !== card.name) ||
    type !== card.type ||
    ddsArticleId !== (profile?.default_dds_article_id ?? "") ||
    confirmNoDdsArticle !== (profile?.confirm_no_dds_article ?? false) ||
    servicePeriodRequired !== (profile?.service_period_required ?? false) ||
    bankPrepayment !== (profile?.bank_payments_create_prepayment ?? false) ||
    (servicePeriodRequired && periodOffset !== periodOffsetSaved);
  useReportDirty("profile", dirty);

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
          {leavingOfficialWithRequisites ? (
            <p className="text-xs font-medium text-amber-700">
              При сохранении банковские реквизиты будут удалены: они относятся только к
              официальному каналу оплаты. Вернуть их можно будет только вводом заново. ИНН
              сохранится — по нему счета из iiko и ЭДО находят эту карточку.
            </p>
          ) : null}
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
            // «Не выбрана» на выключенном поле читалось как «забыли заполнить», хотя
            // решение принято. Формулировки те же, что в окне создания.
            placeholder={confirmNoDdsArticle ? "Статья не применяется" : "Выберите статью"}
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
              checked={bankPrepayment}
              disabled={disabled}
              onCheckedChange={setBankPrepayment}
            />
            <span>
              <span className="block font-medium">Списания из банка пополняют баланс у поставщика</span>
              <span className="block text-xs text-muted-foreground">
                Предоплатная модель (рекламный кабинет, авто-списания с карты): каждое списание
                в пользу контрагента из выписки становится предоплатой, закрывающий УПД из СБИС
                гасит её.
              </span>
            </span>
          </label>
          {bankPrepayment ? (
            <MerchantRulesBlock counterpartyId={card.counterparty_id} disabled={disabled} />
          ) : null}
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
              <Field label="Подставлять в ручной платёж">
                <Select disabled={disabled} value={periodOffset} onValueChange={setPeriodOffset}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="-1">Предыдущий месяц</SelectItem>
                    <SelectItem value="0">Месяц платежа</SelectItem>
                    <SelectItem value="1">Следующий месяц</SelectItem>
                  </SelectContent>
                </Select>
                <span className="text-xs text-muted-foreground">
                  Только для платежа без счёта. Из счёта период распознаётся сам.
                </span>
              </Field>
            </div>
          ) : null}
        </div>
      </div>
      {canAdmin ? (
        // Причина блокировки и признак несохранённого — рядом с кнопкой: поле-виновник
        // может быть выше по форме, а погасшая кнопка без объяснения читается как поломка.
        <div className="flex flex-wrap items-center gap-3 border-t pt-4">
          <Button
            disabled={!canSave || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            {saveMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
          {saveBlockedReason ? (
            <span className="text-xs text-muted-foreground">{saveBlockedReason}</span>
          ) : dirty ? (
            <span className="text-xs font-medium text-amber-700">
              Есть несохранённые изменения
            </span>
          ) : null}
        </div>
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
    // Только при смене контрагента — иначе фоновый рефетч затрёт набранное. См. ProfileSection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.counterparty_id]);

  // trim — как в payload сохранения, иначе пробел в конце «навечно» подсвечивает dirty.
  useReportDirty(
    "manager",
    managerName.trim() !== (card.profile?.manager_name ?? "") ||
      managerPhone.trim() !== (card.profile?.manager_phone ?? ""),
  );

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
    // Только при смене контрагента — иначе фоновый рефетч затрёт набранное. См. ProfileSection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card.counterparty_id]);

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
  useReportDirty(
    "requisites",
    verified !== Boolean(profile?.requisites_verified) ||
      COUNTERPARTY_REQUISITE_FIELDS.some(({ key }) => {
        const saved = (profile?.requisites as Record<string, unknown> | undefined)?.[key];
        // trim — как в payload сохранения, иначе пробел держит форму «несохранённой».
        return (values[key] ?? "").trim() !== (saved != null ? String(saved) : "");
      }),
  );
  // Отметку «проверены» нельзя ставить на неполном наборе — это же правило стоит на
  // сервере (registry._require_official_supplier_requisites). Показываем причину здесь,
  // чтобы человек увидел её до отправки, а не поймал 409.
  const missingRequired = OFFICIAL_SUPPLIER_REQUIRED_REQUISITE_KEYS.filter(
    (key) => !(values[key] ?? "").trim(),
  );
  const verifyBlockedReason =
    verified && missingRequired.length > 0
      ? "Для отметки «проверены» заполните: " +
        missingRequired
          .map((key) => COUNTERPARTY_REQUISITE_FIELDS.find((f) => f.key === key)?.label ?? key)
          .join(", ")
      : null;

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
          <Button
            disabled={saveMutation.isPending || Boolean(verifyBlockedReason)}
            onClick={() => saveMutation.mutate()}
          >
            {saveMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
          {verifyBlockedReason ? (
            <span className="text-xs text-muted-foreground">{verifyBlockedReason}</span>
          ) : null}
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
                <SelectItem value="sbis">СБИС (ЭДО)</SelectItem>
                <SelectItem value="manual">Ручной ввод</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Input
            className="flex-1"
            // Канал СБИС ключуется по типу источника, значение не нужно: заставлять
            // выдумывать текст = мусор в глобальной проверке уникальности (409 между
            // карточками на одинаковых придуманных значениях).
            placeholder={kind === "sbis" ? "не требуется — канал по ИНН" : "email / @handle / id"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <Button
            variant="outline"
            disabled={
              addMutation.isPending ||
              (kind !== "manual" && kind !== "sbis" && !value.trim())
            }
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
    queryKey: ["cp", "registry", "suppliers"],
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

/** Видимость в Кассе — не поле профиля, а статус: применяется сразу и живёт на праве
 *  canOperate (у кассира кнопки «Сохранить» вообще нет). Поэтому стоит в шапке рядом с
 *  бейджами, а не в форме: рядом с отложенными тумблерами он читался бы как такой же,
 *  хотя пишет в БД по клику. Подпись называет СОСТОЯНИЕ, а не действие. */
function KassaToggle({ card }: { card: CardData }) {
  const queryClient = useQueryClient();
  const enabled = Boolean(card.profile?.kassa_enabled);
  const mutation = useMutation({
    mutationFn: (next: boolean) => setKassaEnabled(card.counterparty_id, next),
    onSuccess: async (_data, next) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success(next ? "Контрагент доступен в Кассе" : "Контрагент скрыт из Кассы");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось переключить")),
  });

  return (
    <label
      className="flex cursor-pointer items-center gap-2 rounded-md border bg-background px-2.5 py-1 text-xs"
      title="Доступен в списке поставщиков при создании накладной через Кассу. Применяется сразу, без «Сохранить»."
    >
      <Switch
        checked={enabled}
        disabled={mutation.isPending}
        onCheckedChange={(value) => mutation.mutate(value)}
      />
      <span className={enabled ? "font-medium" : "text-muted-foreground"}>
        {enabled ? "Доступен в Кассе" : "Скрыт из Кассы"}
      </span>
    </label>
  );
}

/** Поле формы: подпись + контрол, прижатые к верху ячейки.
 *
 *  Именно flex, а не grid: в сетке `sm:grid-cols-2` ячейка растягивается по высоте соседа,
 *  а у grid `align-content: normal` = stretch — внутренние строки растут вместе с ячейкой и
 *  «роняют» контрол вниз на разную величину. Из-за этого поля с подсказкой и без неё
 *  вставали на разной высоте в одной строке (замер: контрол на 292 против 280).
 */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <Label>{label}</Label>
      {children}
    </div>
  );
}


/** Merchant-правила «назначение содержит X → этот контрагент»: без них карт-списания
 *  (реквизитов не несут) не привязываются, и предоплатная модель не срабатывает.
 *  Создание сразу переклассифицирует висящие списания с этим паттерном. */
function MerchantRulesBlock({
  counterpartyId,
  disabled,
}: {
  counterpartyId: string;
  disabled: boolean;
}) {
  const queryClient = useQueryClient();
  const [pattern, setPattern] = useState("");
  const rulesQuery = useQuery({
    queryKey: ["cp", "merchant-rules", counterpartyId],
    queryFn: () => getMerchantRules(counterpartyId),
  });
  const rules = rulesQuery.data ?? [];

  const createMutation = useMutation({
    mutationFn: () => createMerchantRule(counterpartyId, { purpose_pattern: pattern }),
    onSuccess: async (r) => {
      setPattern("");
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      await queryClient.invalidateQueries({ queryKey: ["dds"] });
      const tail = r.backfilled
        ? ` — привязано существующих списаний: ${r.backfilled}`
        : "";
      toast.success(
        (r.updated_existing ? "Правило дополнено контрагентом" : "Правило создано") + tail,
      );
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось создать правило")),
  });

  return (
    <div className="ml-12 grid gap-2">
      {rules.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {rules.map((rule) => (
            <Badge key={rule.id} variant="outline" className="font-normal">
              назначение содержит «{rule.purpose_pattern}»
              {rule.is_active ? "" : " · выключено"}
            </Badge>
          ))}
        </div>
      ) : (
        <span className="text-xs text-muted-foreground">
          Правила распознавания нет — списания не будут привязываться к контрагенту.
        </span>
      )}
      {!disabled ? (
        <div className="flex items-center gap-2">
          <Input
            className="max-w-64"
            placeholder="Текст в назначении (напр. mango)"
            value={pattern}
            onChange={(event) => setPattern(event.target.value)}
          />
          <Button
            variant="outline"
            size="sm"
            disabled={createMutation.isPending || pattern.trim().length < 3}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : null}
            Создать правило
          </Button>
        </div>
      ) : null}
    </div>
  );
}
