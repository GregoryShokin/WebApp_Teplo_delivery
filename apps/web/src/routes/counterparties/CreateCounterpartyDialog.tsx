import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
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
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import { apiErrorMessage, getDdsArticles } from "@/lib/api";

import { createCounterparty, getIikoSuppliers, type CounterpartyCard } from "./api";
import { RequisitesHistoryButton } from "./RequisitesHistoryButton";
import {
  COUNTERPARTY_REQUISITE_FIELDS,
  COUNTERPARTY_TYPE_LABELS,
  OFFICIAL_SUPPLIER_REQUIRED_REQUISITE_KEYS,
  RELATIONSHIP_HINTS,
  RELATIONSHIP_LABELS,
} from "./shared";

const NO_IIKO = "none";

type CreateTab = "general" | "requisites" | "manager";

function emptyRequisites(): Record<string, string> {
  return Object.fromEntries(COUNTERPARTY_REQUISITE_FIELDS.map(({ key }) => [key, ""]));
}

export function CreateCounterpartyDialog({
  open,
  onOpenChange,
  defaultRelationship = "official",
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultRelationship?: string;
  onCreated?: (counterparty: CounterpartyCard) => void;
}) {
  const queryClient = useQueryClient();
  const articlesQuery = useQuery({
    queryKey: ["dds", "articles"],
    queryFn: getDdsArticles,
    enabled: open,
  });
  const iikoSuppliersQuery = useQuery({
    queryKey: ["cp", "iiko-suppliers"],
    queryFn: getIikoSuppliers,
    enabled: open,
    staleTime: 60_000,
  });

  const [activeTab, setActiveTab] = useState<CreateTab>("general");
  const [name, setName] = useState("");
  const [type, setType] = useState("legal_entity");
  const [relationship, setRelationship] = useState(defaultRelationship);
  const [ddsArticleId, setDdsArticleId] = useState("");
  const [confirmNoDdsArticle, setConfirmNoDdsArticle] = useState(false);
  const [servicePeriodRequired, setServicePeriodRequired] = useState(false);
  const [periodOffset, setPeriodOffset] = useState("0");
  const [requisites, setRequisites] = useState<Record<string, string>>(emptyRequisites);
  const [requisitesVerified, setRequisitesVerified] = useState(false);
  const [managerName, setManagerName] = useState("");
  const [managerPhone, setManagerPhone] = useState("");
  const [iikoGuid, setIikoGuid] = useState(NO_IIKO);

  useEffect(() => {
    if (!open) return;
    setActiveTab("general");
    setName("");
    setType("legal_entity");
    setRelationship(defaultRelationship);
    setDdsArticleId("");
    setConfirmNoDdsArticle(false);
    setServicePeriodRequired(false);
    setPeriodOffset("0");
    setRequisites(emptyRequisites());
    setRequisitesVerified(false);
    setManagerName("");
    setManagerPhone("");
    setIikoGuid(NO_IIKO);
  }, [open, defaultRelationship]);

  function changeRelationship(nextRelationship: string) {
    setRelationship(nextRelationship);
    if (nextRelationship !== "official") {
      // Набранные реквизиты НЕ стираем: человек мог переключить тип, только чтобы
      // прочитать подсказку, и вернуться. В payload для неофициала они всё равно не
      // уходят (фильтр на сабмите) — терять ввод до отправки формы незачем.
      setActiveTab("general");
    }
  }

  function applyFoundRequisites(found: Record<string, string>) {
    setRequisites((current) => ({ ...current, ...found }));
    // Название карточки берём из платёжки, только если человек его ещё не ввёл: в банке
    // контрагент подписан официально, а в форме может стоять привычное рабочее имя.
    if (!name.trim() && found.recipientName) {
      setName(found.recipientName);
    }
  }

  function selectIikoSupplier(guid: string) {
    setIikoGuid(guid);
    if (guid === NO_IIKO) return;
    const supplier = iikoSuppliersQuery.data?.find((item) => item.guid === guid);
    if (!supplier) return;
    setName(supplier.name);
    setType(supplier.inn && supplier.inn.length === 12 ? "individual" : "legal_entity");
    const nextRelationship = supplier.inn ? "official" : "informal";
    changeRelationship(nextRelationship);
    if (nextRelationship === "official") {
      setRequisites((current) => ({
        ...current,
        recipientName: supplier.name,
        inn: supplier.inn ?? "",
      }));
    }
  }

  const iikoOptions: ComboboxOption[] = [
    { value: NO_IIKO, label: "Не связывать (ввести вручную)" },
    ...(iikoSuppliersQuery.data ?? []).map((supplier) => ({
      value: supplier.guid,
      label: supplier.inn ? `${supplier.name} · ИНН ${supplier.inn}` : supplier.name,
      keywords: supplier.inn ?? undefined,
    })),
  ];

  const isOfficialSupplier =
    relationship === "official" && type !== "bank" && type !== "tax_authority";
  const hasRequiredRequisites = OFFICIAL_SUPPLIER_REQUIRED_REQUISITE_KEYS.every((key) =>
    Boolean(requisites[key]?.trim()),
  );
  const hasArticleDecision = Boolean(ddsArticleId) || confirmNoDdsArticle;
  const isFormComplete =
    Boolean(name.trim()) && hasArticleDecision && (!isOfficialSupplier || hasRequiredRequisites);

  const createMutation = useMutation({
    mutationFn: () => {
      const isOfficial = relationship === "official";
      const cleanRequisites = isOfficial
        ? Object.fromEntries(
            Object.entries({
              ...requisites,
              recipientName: requisites.recipientName || name,
            })
              .map(([key, value]) => [key, value.trim()])
              .filter(([, value]) => Boolean(value)),
          )
        : {};
      return createCounterparty({
        name: name.trim(),
        // ИНН уходит и у неофициала (если ввели): это ключ идентификации для синков
        // iiko/почты/ЭДО, а не банковский реквизит — backend хранит его для любого типа.
        inn: (requisites.inn ?? "").trim() || null,
        type,
        relationship,
        default_dds_article_id: ddsArticleId || null,
        confirm_no_dds_article: confirmNoDdsArticle,
        service_period_required: servicePeriodRequired,
        default_service_period_offset_months: servicePeriodRequired ? Number(periodOffset) : null,
        requisites: cleanRequisites,
        requisites_verified: isOfficial && requisitesVerified,
        manager_name: managerName.trim() || null,
        manager_phone: managerPhone.trim() || null,
        iiko_supplier_guid: iikoGuid === NO_IIKO ? null : iikoGuid,
      });
    },
    onSuccess: (created) => {
      // Закрываем ДО инвалидации: ключ ["cp"] задевает и справочник поставщиков iiko,
      // а он с недоступного iiko отвечает 502 через ~11 с и ретраится — ожидание держало
      // окно в спиннере ~23 с, хотя контрагент уже создан. Закрытие снимает enabled у
      // iiko-запроса, так что зря дёргать его никто не будет.
      onOpenChange(false);
      toast.success("Контрагент создан");
      onCreated?.(created);
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать контрагента")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Новый контрагент</DialogTitle>
        </DialogHeader>

        <Tabs value={activeTab} onValueChange={(value) => setActiveTab(value as CreateTab)}>
          <TabsList className="grid h-auto w-full grid-cols-3">
            <TabsTrigger value="general">Общая информация</TabsTrigger>
            <TabsTrigger value="requisites" disabled={relationship !== "official"}>
              Реквизиты{isOfficialSupplier ? " *" : ""}
            </TabsTrigger>
            <TabsTrigger value="manager">Данные менеджера</TabsTrigger>
          </TabsList>

          <TabsContent value="general" className="space-y-4 pt-2">
            <Field label="Поставщик из iiko (необязательно)">
              <Combobox
                options={iikoOptions}
                value={iikoGuid}
                onChange={selectIikoSupplier}
                placeholder={iikoSuppliersQuery.isLoading ? "Загрузка из iiko…" : "Не связывать"}
                searchPlaceholder="Поиск поставщика iiko…"
                emptyMessage={
                  iikoSuppliersQuery.isLoading ? "Загрузка из iiko…" : "Поставщики не найдены"
                }
              />
              <p className="text-xs text-muted-foreground">
                {iikoSuppliersQuery.isError
                  ? "iiko сейчас недоступен — контрагента можно создать вручную."
                  : "Связь с iiko не даст синхронизации накладных создать дубль."}
              </p>
            </Field>

            <Field label="Официальное название *">
              <Input value={name} onChange={(event) => setName(event.target.value)} />
              {relationship === "official" ? (
                <div className="flex flex-wrap items-center gap-3">
                  {/* Кнопка стоит и здесь, и на вкладке реквизитов: название вводят первым,
                      и находить контрагента логично сразу, не переключая вкладку. */}
                  <RequisitesHistoryButton query={name} onPick={applyFoundRequisites} />
                  <span className="text-xs text-muted-foreground">
                    Подставит ИНН и банковские реквизиты из прошлых платежей и счетов.
                  </span>
                </div>
              ) : null}
            </Field>

            <Field label="Тип контрагента">
              <Select value={type} onValueChange={setType}>
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

            <Field label="Тип отношений">
              <Select value={relationship} onValueChange={changeRelationship}>
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
              {relationship !== "official" ? (
                <p className="text-xs text-muted-foreground">
                  Банковские реквизиты не заполняются: для этого типа отношений используется
                  отдельный способ расчёта.
                </p>
              ) : null}
            </Field>

            <Field label="Статья ДДС *">
              <ArticleCombobox
                articles={articlesQuery.data ?? []}
                value={ddsArticleId}
                onChange={setDdsArticleId}
                placeholder={confirmNoDdsArticle ? "Статья не применяется" : "Выберите статью"}
                disabled={confirmNoDdsArticle}
              />
              <label className="flex items-center gap-2 text-sm">
                <Switch
                  checked={confirmNoDdsArticle}
                  onCheckedChange={(checked) => {
                    setConfirmNoDdsArticle(checked);
                    if (checked) setDdsArticleId("");
                  }}
                />
                У контрагента нет статьи ДДС
              </label>
              <p className="text-xs text-muted-foreground">
                Если статьи действительно нет, подтвердите это переключателем — оставить поле пустым
                случайно нельзя.
              </p>
            </Field>

            <div className="grid gap-3 rounded-md border p-4">
              <label className="flex items-start gap-3">
                <Switch
                  checked={servicePeriodRequired}
                  onCheckedChange={setServicePeriodRequired}
                />
                <span>
                  <span className="block text-sm font-medium">Требовать период оказания услуг</span>
                  <span className="block text-xs text-muted-foreground">
                    Платёж без периода нельзя отправить в банк.
                  </span>
                </span>
              </label>
              {servicePeriodRequired ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Подставлять в ручной платёж">
                    <Select value={periodOffset} onValueChange={setPeriodOffset}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
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
          </TabsContent>

          <TabsContent value="requisites" className="space-y-4 pt-2">
            <p className="text-sm text-muted-foreground">
              Реквизиты официального контрагента для создания платёжного поручения в банке.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <RequisitesHistoryButton
                query={(requisites.inn ?? "").trim() || name}
                onPick={applyFoundRequisites}
              />
              <span className="text-xs text-muted-foreground">
                Ищем по ИНН, названию или расчётному счёту в наших платежах и счетах из почты.
              </span>
            </div>
            {isOfficialSupplier ? (
              <p className="text-xs text-muted-foreground">
                Обязательны БИК банка, ИНН, расчётный и корреспондентский счета. КПП можно не
                заполнять.
              </p>
            ) : null}
            <div className="grid gap-4 sm:grid-cols-2">
              {COUNTERPARTY_REQUISITE_FIELDS.map(({ key, label }) => (
                <Field
                  key={key}
                  label={`${label}${
                    isOfficialSupplier &&
                    OFFICIAL_SUPPLIER_REQUIRED_REQUISITE_KEYS.includes(
                      key as (typeof OFFICIAL_SUPPLIER_REQUIRED_REQUISITE_KEYS)[number],
                    )
                      ? " *"
                      : ""
                  }`}
                >
                  <Input
                    value={requisites[key] ?? ""}
                    placeholder={
                      key === "recipientName" ? name || "Официальное название" : undefined
                    }
                    onChange={(event) =>
                      setRequisites((current) => ({ ...current, [key]: event.target.value }))
                    }
                  />
                </Field>
              ))}
            </div>
            <label className="flex items-center gap-2 text-sm">
              <Switch checked={requisitesVerified} onCheckedChange={setRequisitesVerified} />
              Реквизиты проверены
            </label>
            <p className="text-xs text-muted-foreground">
              Без подтверждения контрагента можно создать, но отправка платежа в банк будет
              заблокирована до проверки реквизитов.
            </p>
          </TabsContent>

          <TabsContent value="manager" className="space-y-4 pt-2">
            <p className="text-sm text-muted-foreground">
              Контакт сотрудника контрагента для вопросов по накладным и оплатам.
            </p>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Менеджер">
                <Input
                  value={managerName}
                  onChange={(event) => setManagerName(event.target.value)}
                />
              </Field>
              <Field label="Телефон менеджера">
                <Input
                  type="tel"
                  value={managerPhone}
                  onChange={(event) => setManagerPhone(event.target.value)}
                />
              </Field>
            </div>
          </TabsContent>
        </Tabs>

        <DialogFooter>
          <Button
            disabled={!isFormComplete || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Создать
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Поле формы: подпись + контрол, прижатые к верху ячейки. flex, а не grid — см. тот же
 *  Field в CounterpartyCard: в двухколоночной сетке grid растягивает внутренние строки по
 *  высоте соседней ячейки, и поля с подсказкой и без неё встают на разной высоте. */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <Label>{label}</Label>
      {children}
    </div>
  );
}
