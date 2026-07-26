import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, ScrollText } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  apiErrorMessage,
  fetchOfficialProfile,
  putOfficialProfile,
  type OfficialProfile,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

/** Официальный зарплатный контур в карточке сотрудника (решение владельца 26.07.2026).
 *
 * Самодостаточная секция: свой GET/PUT на субресурс `/employees/{id}/official` под
 * правами staff.official.read/manage — в diff-механику общей карточки не входит.
 * По этим данным налоговый модуль прогнозирует НДФЛ, взносы МСП и травматизм за
 * отработанные месяцы до прихода оборотки бухгалтера.
 */
export function OfficialContourSection({ employeeId }: { employeeId: string }) {
  const permissions = usePermissions();
  const canManage = permissions.hasPermission("staff.official.manage");
  const canRead = canManage || permissions.hasPermission("staff.official.read");
  const queryClient = useQueryClient();

  const query = useQuery({
    enabled: canRead,
    queryFn: () => fetchOfficialProfile(employeeId),
    queryKey: ["official-profile", employeeId],
  });

  const [draft, setDraft] = useState<OfficialProfile | null>(null);
  useEffect(() => {
    if (query.data) {
      setDraft(query.data);
    }
  }, [query.data]);

  const mutation = useMutation({
    mutationFn: (payload: OfficialProfile) => putOfficialProfile(employeeId, payload),
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось сохранить официальный контур")),
    onSuccess: (data) => {
      queryClient.setQueryData(["official-profile", employeeId], data);
      // Прогнозные начисления пересчитываются от карточки — обновляем налоговые витрины.
      void queryClient.invalidateQueries({ queryKey: ["taxes"] });
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      toast.success("Официальный контур сохранён");
    },
  });

  if (!canRead) {
    return null;
  }

  if (query.isLoading || draft === null) {
    return (
      <div className="grid gap-3 rounded-lg border bg-card p-4">
        <div className="flex items-center gap-2 text-sm font-medium">
          <ScrollText aria-hidden="true" size={16} />
          Официальный контур
          {query.isLoading ? (
            <LoaderCircle aria-hidden="true" className="animate-spin" size={14} />
          ) : null}
        </div>
        {query.isError ? (
          <div className="text-xs text-destructive">
            {apiErrorMessage(query.error, "Не удалось загрузить официальный контур")}
          </div>
        ) : null}
      </div>
    );
  }

  const busy = !canManage || mutation.isPending;
  const nameValid =
    !draft.is_official ||
    (draft.official_full_name ?? "").trim().split(/\s+/).filter(Boolean).length >= 2;
  const salaryValid =
    !draft.is_official || (!!draft.official_salary && Number(draft.official_salary) > 0);
  const dirty = JSON.stringify(draft) !== JSON.stringify(query.data);

  return (
    <div className="grid gap-4 rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <ScrollText aria-hidden="true" size={16} />
          Официальный контур
        </div>
        <label className="flex items-center gap-2 text-sm">
          <span>Оформлен официально</span>
          <input
            checked={draft.is_official}
            disabled={busy}
            onChange={(event) => setDraft({ ...draft, is_official: event.target.checked })}
            type="checkbox"
          />
        </label>
      </div>

      {draft.is_official ? (
        <>
          <p className="text-xs leading-5 text-muted-foreground">
            По этим данным система прогнозирует НДФЛ, страховые взносы и травматизм за
            отработанные месяцы — до прихода оборотки бухгалтера. Официальные ФИО и
            табельный номер нужны для сверки с документами налоговой.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Label className="grid gap-1.5">
              <span>Официальные ФИО (как в трудовом договоре)</span>
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
                  Укажите минимум фамилию и имя
                </span>
              ) : null}
            </Label>
            <Label className="grid gap-1.5">
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
            <Label className="grid gap-1.5">
              <span>Официальный оклад, ₽/мес</span>
              <Input
                disabled={busy}
                inputMode="decimal"
                onChange={(event) =>
                  setDraft({ ...draft, official_salary: event.target.value })
                }
                placeholder="50000"
                value={draft.official_salary ?? ""}
              />
              {!salaryValid ? (
                <span className="text-xs text-destructive">Оклад обязателен</span>
              ) : null}
            </Label>
            <Label className="grid gap-1.5">
              <span>Вычет НДФЛ, ₽/мес</span>
              <Input
                disabled={busy}
                inputMode="decimal"
                onChange={(event) =>
                  setDraft({ ...draft, official_ndfl_deduction: event.target.value })
                }
                placeholder="1400"
                value={draft.official_ndfl_deduction}
              />
            </Label>
            <Label className="grid gap-1.5">
              <span>Статус</span>
              <select
                className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                disabled={busy}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    official_status: event.target.value as OfficialProfile["official_status"],
                  })
                }
                value={draft.official_status}
              >
                <option value="working">Работает — начисления идут</option>
                <option value="maternity_leave">Декрет — начислений нет</option>
              </select>
            </Label>
          </div>
        </>
      ) : (
        <p className="text-xs leading-5 text-muted-foreground">
          Сотрудник не оформлен официально — налоговый модуль начислений по нему не ждёт.
        </p>
      )}

      {canManage ? (
        <div className="flex justify-end">
          <Button
            disabled={busy || !dirty || !nameValid || !salaryValid}
            onClick={() =>
              mutation.mutate({
                ...draft,
                official_full_name: draft.official_full_name?.trim() || null,
                official_tab_number: draft.official_tab_number?.trim() || null,
                official_salary: draft.official_salary || null,
                official_ndfl_deduction: draft.official_ndfl_deduction || "0",
              })
            }
            size="sm"
          >
            {mutation.isPending ? (
              <LoaderCircle aria-hidden="true" className="animate-spin" size={16} />
            ) : null}
            Сохранить официальный контур
          </Button>
        </div>
      ) : null}
    </div>
  );
}
