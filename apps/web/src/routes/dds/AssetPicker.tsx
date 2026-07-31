import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { LoaderCircle, Plus } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { InlineOptionList, type ComboboxOption } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  apiErrorMessage,
  assetIntakeStep,
  createAssetFromPayment,
  getAssetCategories,
  type AssetIntakeStep,
  type AssetIntakeTurn,
  type AssetOption,
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
  // Ручная форма остаётся ЗАПАСНЫМ путём, а не основным: без ключа или при лежащем релее
  // диалог невозможен, но покупку записать надо всё равно. Контур, который защищает баланс,
  // не должен сам мешать провести платёж.
  const [manual, setManual] = useState(false);

  // --- диалог ---
  const [purchase, setPurchase] = useState("");
  const [history, setHistory] = useState<AssetIntakeTurn[]>([]);
  const [step, setStep] = useState<AssetIntakeStep | null>(null);
  const [answer, setAnswer] = useState("");

  // --- ручная форма (и правка того, что предложила модель) ---
  const [newName, setNewName] = useState("");
  // Марка и модель — ДВА поля, а не одно (замечание владельца 31.07.2026). В карточке они
  // хранятся одной строкой ``brand_model``, как в реестре инвентаризации: дробить колонку ради
  // формы значило бы мигрировать 149 существующих карточек, разрезая их текст догадками.
  const [newBrand, setNewBrand] = useState("");
  const [newModel, setNewModel] = useState("");
  const [newCategoryId, setNewCategoryId] = useState("");
  const [specs, setSpecs] = useState("");

  const categoriesQuery = useQuery({
    queryKey: ["asset-categories"],
    queryFn: getAssetCategories,
    enabled: creating,
  });

  function resetDraft() {
    setCreating(false);
    setManual(false);
    setPurchase("");
    setHistory([]);
    setStep(null);
    setAnswer("");
    setNewName("");
    setNewBrand("");
    setNewModel("");
    setNewCategoryId("");
    setSpecs("");
  }

  const intakeMutation = useMutation({
    mutationFn: (payload: { purchase: string; history: AssetIntakeTurn[] }) =>
      assetIntakeStep(payload),
    onSuccess: (result) => {
      setStep(result);
      setAnswer("");
      if (result.status === "ready") {
        // Предложение модели кладём в те же поля, что и ручной ввод: человек видит карточку
        // целиком и правит любое поле перед записью. Ошибка модели остаётся видимой ДО
        // создания, а не всплывает потом в балансе.
        setNewName(result.name ?? purchase.trim());
        setNewBrand(result.brand ?? "");
        setNewModel(result.model ?? "");
        setNewCategoryId(result.category_id ?? "");
        setSpecs(result.specs ?? "");
      }
    },
    onError: (error) => {
      // Модель недоступна — не тупик: переводим на ручную форму и говорим почему.
      setManual(true);
      setNewName(purchase.trim());
      toast.error(apiErrorMessage(error, "Модель недоступна — заполните карточку вручную"));
    },
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createAssetFromPayment({
        name: newName.trim(),
        initial_cost: amount,
        category_id: newCategoryId || null,
        brand_model: [newBrand.trim(), newModel.trim()].filter(Boolean).join(" ") || null,
        // Характеристики (материал, размеры) кладём в заметку карточки: по ним объект узнают
        // при следующей инвентаризации, а отдельного поля под них в карточке нет.
        note: specs.trim() || null,
        // Купили в день платежа — с этого месяца и амортизируем. Без даты объект молча
        // выпал бы из начисления: ошибки нет, амортизации нет.
        commissioned_on: commissionedOn || null,
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

  if (forbidden) {
    return <p className="text-sm text-destructive">{ASSETS_FORBIDDEN_HINT}</p>;
  }

  if (creating) {
    const asking = step?.status === "need_more" && !manual;
    const ready = manual || step?.status === "ready";
    return (
      <div className="space-y-3 rounded-md border p-3">
        {!step && !manual ? (
          <>
            <div className="space-y-1">
              <Label className="text-sm">Что купили</Label>
              {/* ОДНО поле вместо формы (решение владельца 31.07.2026). Категорию, от которой
                  зависит срок амортизации, определяет модель уточняющими вопросами: покупку
                  заводит тот, кто платил, а не бухгалтер по ОС. */}
              <Input
                autoFocus
                onChange={(event) => setPurchase(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && purchase.trim()) {
                    intakeMutation.mutate({ purchase, history: [] });
                  }
                }}
                placeholder="Например: купили рисоварку"
                value={purchase}
              />
              <p className="text-xs text-muted-foreground">
                Напишите своими словами. Если чего-то не хватит — спросим.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={!purchase.trim() || intakeMutation.isPending}
                onClick={() => intakeMutation.mutate({ purchase, history: [] })}
                size="sm"
                type="button"
              >
                {intakeMutation.isPending ? (
                  <LoaderCircle className="mr-2 animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Дальше
              </Button>
              <Button onClick={resetDraft} size="sm" type="button" variant="ghost">
                Отмена
              </Button>
            </div>
          </>
        ) : null}

        {asking && step?.question ? (
          <div className="space-y-2">
            <div className="rounded-md bg-muted/40 p-2">
              <p className="text-sm font-medium">{step.question}</p>
              {step.why ? (
                <p className="mt-0.5 text-xs text-muted-foreground">{step.why}</p>
              ) : null}
            </div>
            {step.suggestions.length ? (
              <div className="flex flex-wrap gap-1.5">
                {step.suggestions.map((option) => (
                  <Button
                    key={option}
                    onClick={() => {
                      const turn = { question: step.question!, answer: option };
                      setHistory((prev) => [...prev, turn]);
                      intakeMutation.mutate({ purchase, history: [...history, turn] });
                    }}
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
                  const turn = { question: step.question!, answer: answer.trim() };
                  setHistory((prev) => [...prev, turn]);
                  intakeMutation.mutate({ purchase, history: [...history, turn] });
                }
              }}
              placeholder="Свой ответ…"
              value={answer}
            />
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={!answer.trim() || intakeMutation.isPending}
                onClick={() => {
                  const turn = { question: step.question!, answer: answer.trim() };
                  setHistory((prev) => [...prev, turn]);
                  intakeMutation.mutate({ purchase, history: [...history, turn] });
                }}
                size="sm"
                type="button"
              >
                {intakeMutation.isPending ? (
                  <LoaderCircle className="mr-2 animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Ответить
              </Button>
              <Button onClick={() => setManual(true)} size="sm" type="button" variant="ghost">
                Заполнить самому
              </Button>
            </div>
          </div>
        ) : null}

        {ready ? (
          <>
            {step?.reason && !manual ? (
              <p className="rounded-md bg-muted/40 p-2 text-xs text-muted-foreground">
                {step.reason}
              </p>
            ) : null}
            <div className="space-y-1">
              <Label className="text-sm">Наименование</Label>
              <Input
                onChange={(event) => setNewName(event.target.value)}
                placeholder="Например: Рисоварка промышленная"
                value={newName}
              />
            </div>
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
            {specs ? (
              <div className="space-y-1">
                <Label className="text-sm">Характеристики</Label>
                <Input
                  onChange={(event) => setSpecs(event.target.value)}
                  placeholder="Материал, размеры"
                  value={specs}
                />
              </div>
            ) : null}
            <div className="space-y-1">
              <Label className="text-sm">Категория</Label>
              {/* Категория задаёт срок службы. Без неё амортизация по объекту молча не пойдёт —
                  поэтому поле обязательно, хотя в модели допускает пустоту. */}
              <InlineOptionList
                options={(categoriesQuery.data ?? []).map((category) => ({
                  value: category.id,
                  label: `${category.name} · ${Math.round(category.useful_life_months / 12)} лет`,
                  keywords: category.name,
                }))}
                value={newCategoryId}
                onChange={setNewCategoryId}
                searchPlaceholder="Поиск категории…"
                emptyMessage={categoriesQuery.isLoading ? "Загружаем…" : "Категорий нет"}
                listClassName="max-h-40"
                autoFocus={false}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              Стоимость карточки — {formatDdsMoney(Number(amount) || 0)}, сумма этой строки
              платежа. Амортизация пойдёт с{" "}
              {commissionedOn ? formatDate(commissionedOn) : "дня оплаты"}. Инвентарный номер
              присвоится сам.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={!newName.trim() || !newCategoryId || createMutation.isPending}
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
          </>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {/* Кнопка ПЕРЕД списком: для покупки заведение нового — обычный случай, а привязка к
          существующему объекту (ремонт, вторая часть оплаты) — более редкий. */}
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
      <InlineOptionList
        options={options}
        value={value}
        onChange={onChange}
        searchPlaceholder="Поиск по номеру, названию или модели…"
        emptyMessage={
          isLoading ? "Загружаем объекты…" : "Объектов пока нет — заведите первый кнопкой выше."
        }
        listClassName="max-h-48"
        autoFocus={false}
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}
