import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Info, LoaderCircle, ScrollText } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import {
  apiErrorMessage,
  fetchOfficialProfile,
  putOfficialProfile,
  type OfficialProfile,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

/** Официальный зарплатный контур в карточке сотрудника (решение владельца 26.07.2026).
 *
 * В карточке — компактная сводка и кнопка «Настроить официальный контур»; сама форма
 * живёт в модалке (дизайн-критика 26.07: инлайн-секция читалась информационным шумом).
 * Самодостаточно: свой GET/PUT на субресурс `/employees/{id}/official` под правами
 * staff.official.read/manage — в diff-механику общей карточки не входит.
 */
export function OfficialContourSection({ employeeId }: { employeeId: string }) {
  const permissions = usePermissions();
  const canManage = permissions.hasPermission("staff.official.manage");
  const canRead = canManage || permissions.hasPermission("staff.official.read");

  const query = useQuery({
    enabled: canRead,
    queryFn: () => fetchOfficialProfile(employeeId),
    queryKey: ["official-profile", employeeId],
  });

  const [open, setOpen] = useState(false);

  if (!canRead) {
    return null;
  }

  const profile = query.data;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-xs font-medium uppercase text-muted-foreground">
          <ScrollText aria-hidden="true" size={14} />
          Официальный контур
        </div>
        <div className="mt-1 text-sm font-medium">
          {query.isLoading ? (
            "Загрузка…"
          ) : query.isError ? (
            <span className="text-destructive">
              {apiErrorMessage(query.error, "Не удалось загрузить")}
            </span>
          ) : profile?.is_official ? (
            <OfficialSummary profile={profile} />
          ) : (
            <span className="text-muted-foreground">
              Не оформлен — налоговый модуль начислений не ждёт
            </span>
          )}
        </div>
      </div>
      {profile ? (
        <Button onClick={() => setOpen(true)} type="button" variant="outline">
          {canManage ? "Настроить официальный контур" : "Посмотреть"}
        </Button>
      ) : null}
      {profile && open ? (
        <OfficialContourDialog
          canManage={canManage}
          employeeId={employeeId}
          initial={profile}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  );
}

const money = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });

function OfficialSummary({ profile }: { profile: OfficialProfile }) {
  const parts = [
    profile.official_salary ? `оклад ${money.format(Number(profile.official_salary))} ₽` : null,
    childrenLabel(profile.official_children_count),
    profile.ndfl_deduction_monthly && Number(profile.ndfl_deduction_monthly) > 0
      ? `вычет ${money.format(Number(profile.ndfl_deduction_monthly))} ₽/мес`
      : null,
    profile.official_status === "maternity_leave" ? "декрет" : null,
  ].filter(Boolean);
  return (
    <span>
      Оформлен{parts.length > 0 ? ` · ${parts.join(" · ")}` : ""}
    </span>
  );
}

function childrenLabel(count: number): string | null {
  if (count <= 0) {
    return null;
  }
  const mod10 = count % 10;
  const mod100 = count % 100;
  const word =
    mod10 === 1 && mod100 !== 11
      ? "ребёнок"
      : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)
        ? "ребёнка"
        : "детей";
  return `${count} ${word}`;
}

/** Иконка «i» со всплывающей подсказкой (паттерн InfoHint модуля «Налоги»). */
function InfoHint({ children }: { children: ReactNode }) {
  return (
    <span className="group relative inline-flex align-middle">
      <button
        aria-label="Пояснение"
        className="inline-flex size-4 items-center justify-center rounded-full text-muted-foreground hover:text-foreground"
        type="button"
      >
        <Info aria-hidden="true" className="size-3.5" />
      </button>
      <span
        className="pointer-events-none absolute left-0 top-5 z-30 hidden w-72 rounded-md border bg-card p-2.5 text-left text-xs font-normal normal-case leading-5 text-card-foreground shadow-md group-focus-within:block group-hover:block"
        role="tooltip"
      >
        {children}
      </span>
    </span>
  );
}

function OfficialContourDialog({
  canManage,
  employeeId,
  initial,
  onClose,
}: {
  canManage: boolean;
  employeeId: string;
  initial: OfficialProfile;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<OfficialProfile>(initial);

  const mutation = useMutation({
    mutationFn: (payload: OfficialProfile) => putOfficialProfile(employeeId, payload),
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось сохранить официальный контур")),
    onSuccess: (data) => {
      queryClient.setQueryData(["official-profile", employeeId], data);
      // Прогнозные начисления пересчитываются от карточки — обновляем налоговые витрины.
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
      toast.success("Официальный контур сохранён");
      onClose();
    },
  });

  const busy = !canManage || mutation.isPending;
  const nameValid =
    !draft.is_official ||
    (draft.official_full_name ?? "").trim().split(/\s+/).filter(Boolean).length >= 2;
  const salaryValid =
    !draft.is_official || (!!draft.official_salary && Number(draft.official_salary) > 0);

  // Вычет для живого предпросмотра — та же формула, что на сервере (ст. 218 НК).
  const deductionPreview = (() => {
    const n = draft.official_children_count;
    let total = 0;
    if (n >= 1) total += 1400;
    if (n >= 2) total += 2800;
    if (n >= 3) total += 6000 * (n - 2);
    return draft.official_single_parent ? total * 2 : total;
  })();

  return (
    <Dialog onOpenChange={(next) => (!next ? onClose() : undefined)} open>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Официальный контур</DialogTitle>
          <DialogDescription>
            Данные трудового договора: по ним система считает НДФЛ, взносы и травматизм
            до прихода оборотки бухгалтера.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-5">
          <label className="flex items-center justify-between gap-3 rounded-lg border px-4 py-3">
            <span className="text-sm font-medium">Оформлен официально</span>
            <Switch
              checked={draft.is_official}
              disabled={busy}
              onCheckedChange={(checked) => setDraft({ ...draft, is_official: checked })}
            />
          </label>

          {draft.is_official ? (
            <>
              <fieldset className="grid gap-3">
                <legend className="text-xs font-medium uppercase text-muted-foreground">
                  Трудовой договор
                </legend>
                <Label className="grid gap-1.5">
                  <span>Официальные ФИО</span>
                  <Input
                    autoComplete="off"
                    disabled={busy}
                    onChange={(event) =>
                      setDraft({ ...draft, official_full_name: event.target.value })
                    }
                    placeholder="Фамилия Имя Отчество"
                    value={draft.official_full_name ?? ""}
                  />
                  {!nameValid ? (
                    <span className="text-xs text-destructive">
                      Укажите минимум фамилию и имя — по ним сверяемся с документами ФНС
                    </span>
                  ) : null}
                </Label>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Label className="grid content-start gap-1.5">
                    <span>Табельный номер</span>
                    <Input
                      autoComplete="off"
                      disabled={busy}
                      onChange={(event) =>
                        setDraft({ ...draft, official_tab_number: event.target.value })
                      }
                      placeholder="206"
                      value={draft.official_tab_number ?? ""}
                    />
                  </Label>
                  <Label className="grid content-start gap-1.5">
                    <span>Оклад, ₽/мес</span>
                    <Input
                      disabled={busy}
                      inputMode="decimal"
                      onChange={(event) =>
                        setDraft({ ...draft, official_salary: event.target.value })
                      }
                      placeholder="50 000"
                      value={draft.official_salary ?? ""}
                    />
                    {!salaryValid ? (
                      <span className="text-xs text-destructive">Оклад обязателен</span>
                    ) : null}
                  </Label>
                </div>
              </fieldset>

              <fieldset className="grid gap-3">
                <legend className="flex items-center gap-1.5 text-xs font-medium uppercase text-muted-foreground">
                  Вычет НДФЛ
                  <InfoHint>
                    Стандартный детский вычет (ст. 218 НК): на первого ребёнка 1 400, на
                    второго 2 800, на третьего и каждого следующего 6 000 ₽/мес — суммы
                    складываются. Единственному родителю — вдвое. Вычет сам отключается с
                    месяца, когда доход с начала года превышает 450 000 ₽.
                  </InfoHint>
                </legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Label className="grid content-start gap-1.5">
                    <span>Несовершеннолетних детей</span>
                    <Select
                      disabled={busy}
                      onValueChange={(value) =>
                        setDraft({ ...draft, official_children_count: Number(value) })
                      }
                      value={String(draft.official_children_count)}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="0">Нет</SelectItem>
                        <SelectItem value="1">1</SelectItem>
                        <SelectItem value="2">2</SelectItem>
                        <SelectItem value="3">3</SelectItem>
                        <SelectItem value="4">4</SelectItem>
                        <SelectItem value="5">5</SelectItem>
                        {draft.official_children_count > 5 ? (
                          <SelectItem value={String(draft.official_children_count)}>
                            {draft.official_children_count}
                          </SelectItem>
                        ) : null}
                      </SelectContent>
                    </Select>
                  </Label>
                  <label className="flex items-center gap-2 self-end pb-2 text-sm">
                    <Checkbox
                      checked={draft.official_single_parent}
                      disabled={busy}
                      onChange={(event) =>
                        setDraft({ ...draft, official_single_parent: event.target.checked })
                      }
                    />
                    Единственный родитель
                  </label>
                </div>
                <div className="text-sm text-muted-foreground">
                  Вычет:{" "}
                  <span className="font-medium text-foreground">
                    {money.format(deductionPreview)} ₽/мес
                  </span>
                </div>
              </fieldset>

              <Label className="grid gap-1.5">
                <span className="text-xs font-medium uppercase text-muted-foreground">
                  Статус
                </span>
                <Select
                  disabled={busy}
                  onValueChange={(value) =>
                    setDraft({
                      ...draft,
                      official_status: value as OfficialProfile["official_status"],
                    })
                  }
                  value={draft.official_status}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="working">Работает — начисления идут</SelectItem>
                    <SelectItem value="maternity_leave">Декрет — начислений нет</SelectItem>
                  </SelectContent>
                </Select>
              </Label>
            </>
          ) : null}
        </div>

        <DialogFooter>
          <Button onClick={onClose} type="button" variant="outline">
            Отмена
          </Button>
          {canManage ? (
            <Button
              disabled={busy || !nameValid || !salaryValid}
              onClick={() =>
                mutation.mutate({
                  ...draft,
                  official_full_name: draft.official_full_name?.trim() || null,
                  official_tab_number: draft.official_tab_number?.trim() || null,
                  official_salary: draft.official_salary || null,
                })
              }
              type="button"
            >
              {mutation.isPending ? (
                <LoaderCircle aria-hidden="true" className="animate-spin" size={16} />
              ) : null}
              Сохранить
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
