import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Pencil, Plus } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
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
import {
  apiErrorMessage,
  createUtilityAccount,
  getDdsArticles,
  getUtilityAccounts,
  updateUtilityAccount,
  type UtilityAccountRecord,
  type UtilityKind,
} from "@/lib/api";
import { getRegistry } from "@/routes/counterparties/api";

const KIND_LABELS: Record<UtilityKind, string> = {
  water: "Вода",
  gas: "Газ",
  electricity: "Электричество",
};

type UtilityForm = {
  kind: UtilityKind;
  counterpartyId: string;
  articleId: string;
  expectedDay: string;
  startedOn: string;
  isActive: boolean;
};

const EMPTY_FORM: UtilityForm = {
  kind: "water",
  counterpartyId: "",
  articleId: "",
  expectedDay: "",
  startedOn: "",
  isActive: true,
};

function toForm(account: UtilityAccountRecord): UtilityForm {
  return {
    kind: account.kind,
    counterpartyId: account.counterparty_id,
    articleId: account.dds_article_id,
    expectedDay: account.expected_day ? String(account.expected_day) : "",
    startedOn: account.started_on,
    isActive: account.is_active,
  };
}

/**
 * Коммунальные потоки помещения: вода, газ, электричество.
 *
 * Поток — это ответы, которых нет в самой квитанции: КОМУ платим и на КАКУЮ статью относить
 * расход. Оба различаются в пределах одной точки — по решению владельца от 02.08.2026 вода и
 * газ возмещаются одному арендодателю, электричество другому. Помещение здесь же, и без него
 * расход осядет «без помещения», а прибыль точки посчитается без коммуналки.
 *
 * Настройка живёт рядом с арендой не для красоты: это одна и та же ось «где», и заводят их за
 * один заход. Отдельная страница коммунальных услуг была ошибкой — платёжка ушла в общую
 * очередь оплат, а здесь осталась только постоянная настройка.
 */
export function LocationUtilities({
  locationId,
  canEdit,
}: {
  locationId: string;
  canEdit: boolean;
}) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<UtilityAccountRecord | null>(null);
  const [form, setForm] = useState<UtilityForm>(EMPTY_FORM);

  const accountsQuery = useQuery({ queryKey: ["utility-accounts"], queryFn: () => getUtilityAccounts() });
  const articlesQuery = useQuery({
    queryKey: ["dds-articles"],
    queryFn: getDdsArticles,
    enabled: dialogOpen,
  });
  const counterpartiesQuery = useQuery({
    queryKey: ["counterparty-registry", "utilities"],
    queryFn: () => getRegistry(),
    enabled: dialogOpen,
  });

  useEffect(() => {
    if (dialogOpen) setForm(editing ? toForm(editing) : EMPTY_FORM);
  }, [dialogOpen, editing]);

  const accounts = useMemo(
    () => (accountsQuery.data ?? []).filter((account) => account.location_id === locationId),
    [accountsQuery.data, locationId],
  );

  // Арендные статьи исключены зеркально запрету коммунальной статьи в договоре аренды: два
  // потока одного арендодателя обязаны различаться статьёй, иначе гард «месяц уже закрыт»
  // принимает один расход за другой, и один из них пропадает.
  const articles = useMemo(
    () =>
      (articlesQuery.data ?? [])
        .filter(
          (article) => article.movement_type === "outflow" && article.is_active && !article.lease_bound,
        )
        .map((article) => ({ value: article.id, label: article.name })),
    [articlesQuery.data],
  );

  const counterparties = useMemo(
    () =>
      (counterpartiesQuery.data ?? [])
        .map((row) => ({
          value: row.counterparty_id,
          label: row.inn ? `${row.name} · ИНН ${row.inn}` : row.name,
        }))
        .sort((a, b) => a.label.localeCompare(b.label, "ru")),
    [counterpartiesQuery.data],
  );

  const saveMutation = useMutation({
    mutationFn: async (value: UtilityForm) => {
      const payload = {
        location_id: locationId,
        kind: value.kind,
        counterparty_id: value.counterpartyId,
        dds_article_id: value.articleId,
        expected_day: value.expectedDay ? Number(value.expectedDay) : null,
        started_on: value.startedOn,
        is_active: value.isActive,
      };
      return editing
        ? updateUtilityAccount(editing.id, payload)
        : createUtilityAccount(payload);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["utility-accounts"] });
      toast.success(editing ? "Поток обновлён" : "Поток заведён");
      setDialogOpen(false);
      setEditing(null);
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!form.counterpartyId) {
      toast.error("Выберите, кому платим");
      return;
    }
    if (!form.articleId) {
      toast.error("Выберите статью расхода");
      return;
    }
    if (!form.startedOn) {
      toast.error("Укажите, с какого месяца ведём поток");
      return;
    }
    saveMutation.mutate(form);
  }

  return (
    <div className="mt-2 rounded-md border p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium">Коммунальные услуги</div>
          <p className="mt-1 text-xs text-muted-foreground">
            {accounts.length > 0
              ? `Потоков: ${accounts.length}. Платёжки приходят в «Финансы → Платежи».`
              : "Потоки не заведены — принесённую квитанцию не к чему привязать"}
          </p>
        </div>
        {canEdit ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setEditing(null);
              setDialogOpen(true);
            }}
          >
            <Plus className="mr-1 h-4 w-4" />
            Поток
          </Button>
        ) : null}
      </div>

      {accountsQuery.isLoading ? (
        <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
          <LoaderCircle className="h-4 w-4 animate-spin" />
          Загружаем потоки…
        </div>
      ) : null}

      <div className="mt-3 space-y-2">
        {accounts.map((account) => (
          <div key={account.id} className="rounded-md bg-muted/40 px-3 py-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{account.kind_label}</span>
                  <Badge variant="secondary">{account.counterparty_name}</Badge>
                  {!account.is_active ? <Badge variant="outline">отключён</Badge> : null}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  статья «{account.dds_article_name}» · с {account.started_on}
                  {account.expected_day ? ` · ждём документ до ${account.expected_day}-го` : ""}
                </div>
              </div>
              {canEdit ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setEditing(account);
                    setDialogOpen(true);
                  }}
                >
                  <Pencil className="mr-1 h-4 w-4" />
                  Изменить
                </Button>
              ) : null}
            </div>
          </div>
        ))}
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? "Изменить поток" : "Новый коммунальный поток"}</DialogTitle>
            <DialogDescription>
              Кому платим — обычно арендодатель: договор с ресурсником заключал он, и квитанция
              выставлена на него. Реквизиты из самой квитанции в платёж не идут.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-3" onSubmit={handleSubmit}>
            <div className="space-y-1">
              <Label>Ресурс</Label>
              <Select
                value={form.kind}
                onValueChange={(value) => setForm({ ...form, kind: value as UtilityKind })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(KIND_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label>Кому платим</Label>
              <Combobox
                options={counterparties}
                value={form.counterpartyId}
                onChange={(value) => setForm({ ...form, counterpartyId: value })}
                placeholder="Выберите контрагента…"
              />
            </div>

            <div className="space-y-1">
              <Label>Статья расхода</Label>
              <Combobox
                options={articles}
                value={form.articleId}
                onChange={(value) => setForm({ ...form, articleId: value })}
                placeholder="Выберите статью…"
              />
              <p className="text-xs text-muted-foreground">
                Арендных статей в списке нет намеренно: аренда и коммуналка одного арендодателя
                должны различаться статьёй, иначе один расход съедает другой.
              </p>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label>Ведём с</Label>
                <Input
                  type="date"
                  value={form.startedOn}
                  onChange={(event) => setForm({ ...form, startedOn: event.target.value })}
                />
              </div>
              <div className="space-y-1">
                <Label>Ждём документ до, число</Label>
                <Input
                  type="number"
                  min={1}
                  max={28}
                  value={form.expectedDay}
                  placeholder="не задано"
                  onChange={(event) => setForm({ ...form, expectedDay: event.target.value })}
                />
              </div>
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setDialogOpen(false)}
                disabled={saveMutation.isPending}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={saveMutation.isPending}>
                {saveMutation.isPending ? "Сохраняем…" : "Сохранить"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
