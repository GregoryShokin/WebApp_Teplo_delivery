import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { LoaderCircle, Plus, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { InlineOptionList, type ComboboxOption } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  apiErrorMessage,
  assetIntakeStep,
  createAssetFromPayment,
  getAssetCategories,
  type AssetCondition,
  type AssetIntakeStep,
  type AssetIntakeTurn,
  type AssetOption,
  type AssetSpecProfile,
} from "@/lib/api";
import { ASSETS_FORBIDDEN_HINT, formatDate, formatDdsMoney } from "@/routes/dds/shared";

/** Подпись объекта: инвентарный номер + имя. Номер первым — по нему объект ищут на наклейке. */
export function assetTitle(asset: AssetOption): string {
  const number = asset.inventory_number ? `${asset.inventory_number} · ` : "";
  return `${number}${asset.name}`;
}

/**
 * Выбор основного средства: список уже заведённых + заведение новой карточки.
 *
 * КНОПКА «ЗАВЕСТИ НОВЫЙ» — НЕ УДОБСТВО, А УСЛОВИЕ РАБОТОСПОСОБНОСТИ (замечание владельца
 * 31.07.2026). Список показывает только существующие карточки, а покупка по определению
 * заводит НОВЫЙ объект: купили рисоварку — привязывать не к чему. Отправлять человека в
 * «Учёт ОС» и обратно значит гарантировать, что он выберет статью попроще, и покупка уйдёт в
 * расход мимо баланса — ровно та дыра, которую весь контур и закрывает.
 *
 * ФОРМА — ОСНОВНОЙ ПУТЬ, МОДЕЛЬ — КНОПКА (уточнение владельца 31.07.2026). Первый вопрос формы —
 * категория: она задаёт срок амортизации, и она же решает, что спрашивать дальше. У техники
 * опознавательный признак — марка и модель, у производственного стола — материал и размеры, а
 * марки у него обычно нет вовсе. Набор полей приходит с категории (``spec_profile``), а не
 * зашит здесь списком имён: категории владелец правит из интерфейса, и захардкоженный перечень
 * после одиннадцатой начал бы врать молча.
 *
 * Стоимость НЕ спрашиваем: для покупки первоначальная стоимость и есть сумма платежа
 * (``valuation_basis='payment'``). Спросить её отдельно значит позволить двум цифрам
 * разойтись, а потом искать, какая из них правда.
 */
export function AssetPicker({
  amount,
  commissionedOn,
  assets,
  forbidden,
  isLoading,
  kind,
  onChange,
  onCreated,
  value,
}: {
  amount: string;
  /** Дата платежа = дата ввода в эксплуатацию: амортизация пойдёт с этого месяца.
   *
   * Пусто — платёж ещё не состоялся («Новый платёж» создаёт черновик, деньги уйдут позже), и
   * дату подставит бэкенд. Оставить объект вовсе без даты нельзя: он молча выпадет из
   * начисления амортизации. */
  commissionedOn?: string;
  assets: AssetOption[];
  forbidden: boolean;
  isLoading: boolean;
  kind: "purchase" | "repair" | "maintenance" | null;
  onChange: (assetId: string) => void;
  onCreated: (asset: AssetOption) => void;
  value: string;
}) {
  const [creating, setCreating] = useState(false);
  // Диалог с моделью — надстройка над формой, а не замена ей: он ЗАПОЛНЯЕТ те же поля, а
  // подтверждает карточку всё равно человек в форме. Поэтому состояние формы одно на оба пути.
  const [ai, setAi] = useState(false);

  // --- диалог с моделью ---
  const [purchase, setPurchase] = useState("");
  const [history, setHistory] = useState<AssetIntakeTurn[]>([]);
  const [step, setStep] = useState<AssetIntakeStep | null>(null);
  const [answer, setAnswer] = useState("");
  const [aiReason, setAiReason] = useState<string | null>(null);

  // --- форма карточки ---
  const [newName, setNewName] = useState("");
  const [newCategoryId, setNewCategoryId] = useState("");
  // Марка и модель — ДВА поля, а не одно (замечание владельца 31.07.2026). В карточке они
  // хранятся одной строкой ``brand_model``, как в реестре инвентаризации: дробить колонку ради
  // формы значило бы мигрировать 149 существующих карточек, разрезая их текст догадками.
  const [newBrand, setNewBrand] = useState("");
  const [newModel, setNewModel] = useState("");
  const [material, setMaterial] = useState("");
  const [dimensions, setDimensions] = useState("");
  const [specs, setSpecs] = useState("");
  const [condition, setCondition] = useState<AssetCondition | "">("");
  const [conditionNote, setConditionNote] = useState("");

  const categoriesQuery = useQuery({
    queryKey: ["asset-categories"],
    queryFn: getAssetCategories,
    enabled: creating,
  });
  const categories = categoriesQuery.data ?? [];
  const category = categories.find((item) => item.id === newCategoryId) ?? null;
  const profile: AssetSpecProfile | null = category?.spec_profile ?? null;

  function resetDraft() {
    setCreating(false);
    setAi(false);
    setPurchase("");
    setHistory([]);
    setStep(null);
    setAnswer("");
    setAiReason(null);
    setNewName("");
    setNewCategoryId("");
    setNewBrand("");
    setNewModel("");
    setMaterial("");
    setDimensions("");
    setSpecs("");
    setCondition("");
    setConditionNote("");
  }

  const intakeMutation = useMutation({
    mutationFn: (payload: { purchase: string; history: AssetIntakeTurn[] }) =>
      assetIntakeStep(payload),
    onSuccess: (result) => {
      setStep(result);
      setAnswer("");
      if (result.status !== "ready") return;
      // Предложение модели приземляется в ту же форму, что и ручной ввод: человек видит
      // карточку целиком и правит любое поле перед записью. Ошибка модели остаётся видимой ДО
      // создания, а не всплывает потом в балансе.
      setNewName(result.name ?? purchase.trim());
      setNewCategoryId(result.category_id ?? "");
      setNewBrand(result.brand ?? "");
      setNewModel(result.model ?? "");
      setMaterial(result.material ?? "");
      setDimensions(result.dimensions ?? "");
      setSpecs(result.specs ?? "");
      // Состояние модель ставит, только если сотрудник сам о нём сказал. Пустое не затираем:
      // переключатель всё равно обязателен, и пусть лучше человек выберет, чем модель угадает.
      if (result.condition) setCondition(result.condition);
      if (result.condition_note) setConditionNote(result.condition_note);
      setAiReason(result.reason);
      setAi(false);
    },
    onError: (error) => {
      // Модель недоступна — не тупик: возвращаемся в форму и говорим почему. Платёж должен
      // провестись в любом случае.
      setAi(false);
      setNewName((prev) => prev || purchase.trim());
      toast.error(apiErrorMessage(error, "Модель недоступна — заполните карточку сами"));
    },
  });

  // В карточку уходит только то, что человек ВИДИТ: набор полей задан профилем категории, и
  // скрытое поле не имеет права попасть в запись. Иначе стол, заведённый после переключения
  // категории с техники, унёс бы в заметку чужую марку.
  const brandModel =
    profile === "equipment" ? [newBrand.trim(), newModel.trim()].filter(Boolean).join(" ") : "";
  const noteParts =
    profile === "furniture"
      ? [
          material.trim() ? `Материал: ${material.trim()}` : "",
          dimensions.trim() ? `Размеры: ${dimensions.trim()}` : "",
          specs.trim(),
        ]
      : [specs.trim()];

  const createMutation = useMutation({
    mutationFn: () =>
      createAssetFromPayment({
        name: newName.trim(),
        initial_cost: amount,
        category_id: newCategoryId || null,
        brand_model: brandModel || null,
        // Характеристики (материал, размеры) кладём в заметку карточки: по ним объект узнают
        // при следующей инвентаризации, а отдельного поля под них в карточке нет.
        note: noteParts.filter(Boolean).join(". ") || null,
        // Купили в день платежа — с этого месяца и амортизируем. Без даты объект молча
        // выпал бы из начисления: ошибки нет, амортизации нет.
        commissioned_on: commissionedOn || null,
        condition: condition || null,
        condition_note: condition === "used" ? conditionNote.trim() : null,
      }),
    onSuccess: (asset) => {
      onCreated(asset);
      resetDraft();
      toast.success(`Заведена карточка ${asset.inventory_number ?? asset.name}`);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось завести карточку")),
  });

  const options: ComboboxOption[] = assets.map((asset) => ({
    value: asset.asset_id,
    // Где стоит и в каком состоянии — то, чем один «Стол производственный» отличается от
    // четырёх других в списке. Без этого выбрать нужный можно только угадав.
    label: [
      assetTitle(asset),
      asset.location_name ?? "без помещения",
      asset.status === "in_use" ? null : asset.status_title,
    ]
      .filter(Boolean)
      .join(" · "),
    // Ищут и по номеру, и по названию, и по модели с наклейки на корпусе.
    keywords: [asset.inventory_number, asset.name, asset.brand_model, asset.location_name]
      .filter(Boolean)
      .join(" "),
  }));

  const hint =
    kind === "purchase"
      ? "Платёж покупает этот объект — его сумма станет первоначальной стоимостью карточки."
      : kind === "repair"
        ? "Капитальный ремонт: если работы тянут больше 15% стоимости объекта, владелец подтвердит новую стоимость в карточке."
        : "Текущий ремонт: стоимость объекта не изменится, но расход попадёт в его историю.";

  // Профильные поля обязательны: без марки и модели технику не опознать при инвентаризации и
  // не оценить, если она б/у; у мебели ту же роль играют материал и размеры.
  const specsFilled =
    profile === "equipment"
      ? Boolean(newBrand.trim() && newModel.trim())
      : profile === "furniture"
        ? Boolean(material.trim() && dimensions.trim())
        : true;
  const conditionFilled =
    condition === "used" ? Boolean(conditionNote.trim()) : condition === "new";
  const canCreate = Boolean(newName.trim() && newCategoryId) && specsFilled && conditionFilled;

  function askModel(turn?: AssetIntakeTurn) {
    const next = turn ? [...history, turn] : [];
    if (turn) setHistory(next);
    intakeMutation.mutate({ purchase, history: next });
  }

  if (forbidden) {
    return <p className="text-sm text-destructive">{ASSETS_FORBIDDEN_HINT}</p>;
  }

  if (creating && ai) {
    return (
      <div className="space-y-3 rounded-md border p-3">
        {step?.status === "need_more" && step.question ? (
          <div className="space-y-2">
            <div className="rounded-md bg-muted/40 p-2">
              <p className="text-sm font-medium">{step.question}</p>
              {step.why ? <p className="mt-0.5 text-xs text-muted-foreground">{step.why}</p> : null}
            </div>
            {step.suggestions.length ? (
              <div className="flex flex-wrap gap-1.5">
                {step.suggestions.map((option) => (
                  <Button
                    key={option}
                    onClick={() => askModel({ question: step.question!, answer: option })}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    {option}
                  </Button>
                ))}
              </div>
            ) : null}
            <Input
              autoFocus
              onChange={(event) => setAnswer(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && answer.trim()) {
                  askModel({ question: step.question!, answer: answer.trim() });
                }
              }}
              placeholder="Свой ответ…"
              value={answer}
            />
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={!answer.trim() || intakeMutation.isPending}
                onClick={() => askModel({ question: step.question!, answer: answer.trim() })}
                size="sm"
                type="button"
              >
                {intakeMutation.isPending ? (
                  <LoaderCircle className="mr-2 animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Ответить
              </Button>
              <Button onClick={() => setAi(false)} size="sm" type="button" variant="ghost">
                Заполнить самому
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="space-y-1">
              <Label className="text-sm">Что купили</Label>
              {/* Одно поле вместо формы: категорию, от которой зависит срок амортизации,
                  модель определит уточняющими вопросами. Путь ОПЦИОНАЛЬНЫЙ — его включают,
                  когда непонятно, куда объект отнести. */}
              <Input
                autoFocus
                onChange={(event) => setPurchase(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && purchase.trim()) askModel();
                }}
                placeholder="Например: купили рисоварку"
                value={purchase}
              />
              <p className="text-xs text-muted-foreground">
                Напишите своими словами. Если чего-то не хватит — спросим и заполним форму сами.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={!purchase.trim() || intakeMutation.isPending}
                onClick={() => askModel()}
                size="sm"
                type="button"
              >
                {intakeMutation.isPending ? (
                  <LoaderCircle className="mr-2 animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Дальше
              </Button>
              <Button onClick={() => setAi(false)} size="sm" type="button" variant="ghost">
                Назад к форме
              </Button>
            </div>
          </>
        )}
      </div>
    );
  }

  if (creating) {
    return (
      <div className="space-y-3 rounded-md border p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium">Новый объект</p>
          <Button
            onClick={() => {
              // Диалог всегда начинается с чистого листа: незакрытая переписка прошлого захода
              // прислалась бы моделью как контекст к другому объекту.
              setStep(null);
              setHistory([]);
              setAnswer("");
              setAi(true);
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            <Sparkles size={16} aria-hidden="true" />
            Внести с помощью ИИ
          </Button>
        </div>

        <div className="space-y-1">
          <Label className="text-sm">Что за оборудование</Label>
          {/* ПЕРВЫЙ вопрос формы. Категория задаёт срок службы — без неё амортизация по объекту
              молча не пойдёт, — и она же решает, какие поля показывать ниже. */}
          <InlineOptionList
            options={categories.map((item) => ({
              value: item.id,
              label: `${item.name} · ${Math.round(item.useful_life_months / 12)} лет`,
              keywords: `${item.name} ${item.note ?? ""}`,
            }))}
            value={newCategoryId}
            onChange={setNewCategoryId}
            searchPlaceholder="Поиск категории…"
            emptyMessage={categoriesQuery.isLoading ? "Загружаем…" : "Категорий нет"}
            listClassName="max-h-40"
            autoFocus={false}
          />
          {category?.note ? <p className="text-xs text-muted-foreground">{category.note}</p> : null}
        </div>

        {profile ? (
          <>
            {aiReason ? (
              <p className="rounded-md bg-muted/40 p-2 text-xs text-muted-foreground">{aiReason}</p>
            ) : null}
            <div className="space-y-1">
              <Label className="text-sm">Наименование</Label>
              <Input
                onChange={(event) => setNewName(event.target.value)}
                placeholder="Например: Рисоварка промышленная"
                value={newName}
              />
            </div>

            {profile === "equipment" ? (
              <div className="grid grid-cols-2 gap-2">
                <div className="space-y-1">
                  <Label className="text-sm">Марка</Label>
                  <Input
                    onChange={(event) => setNewBrand(event.target.value)}
                    placeholder="Gastrorag"
                    value={newBrand}
                  />
                </div>
                <div className="space-y-1">
                  <Label className="text-sm">Модель</Label>
                  <Input
                    onChange={(event) => setNewModel(event.target.value)}
                    placeholder="DH-RC-2"
                    value={newModel}
                  />
                </div>
              </div>
            ) : null}

            {profile === "furniture" ? (
              <div className="grid grid-cols-2 gap-2">
                <div className="space-y-1">
                  <Label className="text-sm">Материал</Label>
                  <Input
                    onChange={(event) => setMaterial(event.target.value)}
                    placeholder="Нержавеющая сталь"
                    value={material}
                  />
                </div>
                <div className="space-y-1">
                  <Label className="text-sm">Размеры</Label>
                  <Input
                    onChange={(event) => setDimensions(event.target.value)}
                    placeholder="1200×600×850 мм"
                    value={dimensions}
                  />
                </div>
              </div>
            ) : null}

            {profile !== "equipment" ? (
              <div className="space-y-1">
                <Label className="text-sm">Характеристики (необязательно)</Label>
                <Input
                  onChange={(event) => setSpecs(event.target.value)}
                  placeholder="Объём, мощность, комплектация"
                  value={specs}
                />
              </div>
            ) : null}

            <div className="space-y-1">
              <Label className="text-sm">Состояние</Label>
              {/* Новое или б/у — не формальность: у купленного с рук объекта износ уже есть, а
                  ни сумма платежа, ни срок из категории его не видят. Карточка без этого
                  признака амортизировалась бы как новая несколько лет подряд. */}
              <div className="flex flex-wrap gap-2">
                <Button
                  onClick={() => setCondition("new")}
                  size="sm"
                  type="button"
                  variant={condition === "new" ? "default" : "outline"}
                >
                  Новое
                </Button>
                <Button
                  onClick={() => setCondition("used")}
                  size="sm"
                  type="button"
                  variant={condition === "used" ? "default" : "outline"}
                >
                  Б/У
                </Button>
              </div>
            </div>

            {condition === "used" ? (
              <div className="space-y-1">
                <Label className="text-sm">Что с ним</Label>
                <Textarea
                  onChange={(event) => setConditionNote(event.target.value)}
                  placeholder="Например: 2019 года, работает, дверь провисла, компрессор менялся"
                  rows={3}
                  value={conditionNote}
                />
                <p className="text-xs text-muted-foreground">
                  По описанию модель оценит износ и предложит владельцу стоимость. Само описание
                  останется в карточке.
                </p>
              </div>
            ) : null}

            <p className="text-xs text-muted-foreground">
              Стоимость карточки — {formatDdsMoney(Number(amount) || 0)}, сумма этой строки платежа.
              Амортизация пойдёт с {commissionedOn ? formatDate(commissionedOn) : "дня оплаты"}.
              Инвентарный номер присвоится сам.
            </p>
          </>
        ) : (
          <p className="text-xs text-muted-foreground">
            Выберите категорию — от неё зависит срок амортизации и что спросим дальше. Не знаете,
            куда отнести, — нажмите «Внести с помощью ИИ».
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <Button
            disabled={!canCreate || createMutation.isPending}
            onClick={() => createMutation.mutate()}
            size="sm"
            type="button"
          >
            {createMutation.isPending ? (
              <LoaderCircle className="mr-2 animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Завести и выбрать
          </Button>
          <Button onClick={resetDraft} size="sm" type="button" variant="ghost">
            Отмена
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {/* Заводить карточку можно ТОЛЬКО у покупки (замечание владельца 31.07.2026). Ремонт и
          обслуживание — это работы по объекту, который уже стоит на балансе; кнопка здесь
          завела бы второй такой же объект стоимостью в сумму ремонта, и в балансе появился бы
          несуществующий пароконвектомат за двенадцать тысяч. */}
      {kind === "purchase" ? (
        <Button
          className="w-full justify-start"
          onClick={() => setCreating(true)}
          size="sm"
          type="button"
          variant="outline"
        >
          <Plus size={16} aria-hidden="true" />
          Завести новый объект на эту сумму
        </Button>
      ) : null}
      <InlineOptionList
        options={options}
        value={value}
        onChange={onChange}
        searchPlaceholder="Поиск по номеру, названию или модели…"
        emptyMessage={
          isLoading
            ? "Загружаем объекты…"
            : kind === "purchase"
              ? "Объектов пока нет — заведите первый кнопкой выше."
              : "Объектов пока нет. Ремонтировать нечего: сначала должна появиться покупка."
        }
        listClassName="max-h-48"
        autoFocus={false}
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}
