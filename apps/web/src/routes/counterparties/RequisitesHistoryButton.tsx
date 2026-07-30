import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { History, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { apiErrorMessage } from "@/lib/api";

import { searchRequisitesInHistory, type RequisitesCandidate } from "./api";
import { COUNTERPARTY_REQUISITE_FIELDS, formatDate, formatRub } from "./shared";

/** Кнопка «Найти реквизиты в истории» + выбор кандидата.
 *
 *  Общая для окна создания и карточки: искать реквизиты по ИНН и названию нужно ровно
 *  одинаково, а расходились бы две копии быстро. Найденное НЕ применяется молча даже
 *  когда кандидат один: имя и ИНН в платёжке пишет банк со слов плательщика, поэтому
 *  «нашли в истории» — это черновик для проверки, а не подтверждённые реквизиты.
 */
export function RequisitesHistoryButton({
  query,
  onPick,
  disabled,
  ignoreCounterpartyId,
  label = "Найти реквизиты в истории",
}: {
  query: string;
  onPick: (requisites: Record<string, string>) => void;
  disabled?: boolean;
  /** Своя же карточка не должна показываться как «уже заведён такой контрагент». */
  ignoreCounterpartyId?: string;
  label?: string;
}) {
  const [candidates, setCandidates] = useState<RequisitesCandidate[] | null>(null);

  const searchMutation = useMutation({
    mutationFn: () => searchRequisitesInHistory(query.trim()),
    onSuccess: (found) => {
      if (found.length === 0) {
        toast.info("В истории платежей и счетов ничего не нашлось");
        return;
      }
      setCandidates(found);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось найти реквизиты")),
  });

  const tooShort = query.trim().length < 3;

  return (
    <>
      <Button
        type="button"
        variant="outline"
        disabled={disabled || tooShort || searchMutation.isPending}
        title={tooShort ? "Введите ИНН или название — минимум 3 символа" : undefined}
        onClick={() => searchMutation.mutate()}
      >
        {searchMutation.isPending ? (
          <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
        ) : (
          <History size={16} aria-hidden="true" />
        )}
        {label}
      </Button>

      <Dialog open={candidates !== null} onOpenChange={(open) => !open && setCandidates(null)}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Реквизиты из истории</DialogTitle>
            <DialogDescription>
              Найдено по запросу «{query.trim()}». Это данные из наших платёжек и счетов —
              подставятся в форму, проверить и подтвердить их нужно вручную.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3">
            {(candidates ?? []).map((candidate) => (
              <CandidateRow
                key={candidate.key}
                candidate={candidate}
                ignoreCounterpartyId={ignoreCounterpartyId}
                onPick={() => {
                  onPick(candidate.requisites);
                  setCandidates(null);
                  toast.success("Реквизиты подставлены — проверьте и подтвердите");
                }}
              />
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function CandidateRow({
  candidate,
  onPick,
  ignoreCounterpartyId,
}: {
  candidate: RequisitesCandidate;
  onPick: () => void;
  ignoreCounterpartyId?: string;
}) {
  const duplicate =
    candidate.existing_counterparty_id && candidate.existing_counterparty_id !== ignoreCounterpartyId
      ? candidate.existing_counterparty_name
      : null;

  return (
    <div className="grid gap-2 rounded-md border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{candidate.requisites.recipientName ?? "Без названия"}</span>
        <Badge variant="outline">{candidate.source_label}</Badge>
        {candidate.hits > 1 ? (
          <span className="text-xs text-muted-foreground">операций: {candidate.hits}</span>
        ) : null}
      </div>

      <div className="grid gap-1 text-sm text-muted-foreground sm:grid-cols-2">
        {COUNTERPARTY_REQUISITE_FIELDS.filter(({ key }) => key !== "recipientName").map(
          ({ key, label }) => (
            <span key={key}>
              {label}: {candidate.requisites[key] ?? "—"}
            </span>
          ),
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span>Последняя операция: {formatDate(candidate.last_seen_on)}</span>
        {candidate.last_amount ? <span>{formatRub(candidate.last_amount)}</span> : null}
        {candidate.bank_name ? <span>{candidate.bank_name}</span> : null}
      </div>

      {candidate.missing.length > 0 ? (
        <p className="text-xs text-amber-600">
          Не хватает для платёжки: {candidate.missing.join(", ")} — остальное подставится, эти
          поля придётся заполнить руками.
        </p>
      ) : null}
      {duplicate ? (
        <p className="text-xs text-amber-600">
          Контрагент с таким ИНН уже заведён: «{duplicate}». Создавать второго нельзя — ИНН
          в реестре уникален.
        </p>
      ) : null}
      {candidate.own_account ? (
        <p className="text-xs text-amber-600">
          Это наш собственный счёт — похоже, перевод между своими, а не платёж контрагенту.
        </p>
      ) : null}

      <div>
        <Button type="button" size="sm" onClick={onPick}>
          Подставить
        </Button>
      </div>
    </div>
  );
}
