import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import { apiErrorMessage, createOwner, getOwners } from "@/lib/api";

const money = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });

function formatMoney(value: string): string {
  const number = Number(value);
  return Number.isFinite(number) ? `${money.format(number)} ₽` : value;
}

function formatShare(value: string): string {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("ru-RU")} %` : `${value} %`;
}

/**
 * Реестр собственников: кто владеет бизнесом и какой долей.
 *
 * Собственник — контрагент с записью в реестре. Личность и расчёты живут в карточке (механика
 * долга у него ровно та же, что у любого контрагента), доля — здесь: у поставщика её не бывает.
 *
 * Сумма долей ПОКАЗЫВАЕТСЯ, а не навязывается. Пока реестр заполняется, промежуточные 50 % —
 * нормальное состояние, и отказ в сохранении мешал бы вводу; а вот 130 % человек должен увидеть
 * сразу, не дожидаясь первых дивидендов.
 */
export function OwnersPanel({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [name, setName] = useState("");
  const [inn, setInn] = useState("");
  const [share, setShare] = useState("50");
  // Учредительный вклад: сумма и дата. Дата обязательна не для порядка — вклад без неё нельзя
  // отнести ни к одному срезу баланса, и он просто не попадёт ни в один месяц.
  const [contribution, setContribution] = useState("");
  const [contributedOn, setContributedOn] = useState("");

  const ownersQuery = useQuery({ queryKey: ["owners"], queryFn: () => getOwners() });

  const saveMutation = useMutation({
    mutationFn: () =>
      createOwner({
        name: name.trim(),
        inn: inn.trim() || null,
        share_percent: share.trim(),
        contribution_amount: contribution.trim() || "0",
        contributed_on: contributedOn.trim() || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["owners"] });
      toast.success("Собственник заведён");
      setDialogOpen(false);
      setName("");
      setInn("");
      setShare("50");
      setContribution("");
      setContributedOn("");
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      toast.error("Укажите имя собственника");
      return;
    }
    const value = Number(share.replace(",", "."));
    if (!Number.isFinite(value) || value <= 0 || value > 100) {
      toast.error("Доля — число от 0 до 100");
      return;
    }
    saveMutation.mutate();
  }

  const owners = ownersQuery.data?.items ?? [];
  const total = Number(ownersQuery.data?.shares_total ?? 0);
  const totalIsWhole = Math.abs(total - 100) < 0.005;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div>
          <CardTitle className="text-base">Собственники</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            Кто владеет бизнесом и какой долей. По доле делятся дивиденды; взнос, возврат и
            дивиденды учитываются по каждому отдельно — поэтому в этих движениях система
            спрашивает имя.
          </p>
        </div>
        {canEdit ? (
          <Button size="sm" onClick={() => setDialogOpen(true)}>
            <Plus className="mr-1 h-4 w-4" />
            Добавить
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3">
        {ownersQuery.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="h-4 w-4 animate-spin" />
            Загружаем реестр…
          </div>
        ) : null}

        {!ownersQuery.isLoading && owners.length === 0 ? (
          <div className="rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-sm text-amber-900">
            Собственники не заведены — взнос, возврат и дивиденды провести не получится: система
            не сможет спросить, чьи это деньги.
          </div>
        ) : null}

        {owners.map((owner) => (
          <div
            key={owner.id}
            className="flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3 py-2"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{owner.name}</span>
              <Badge variant="secondary">{formatShare(owner.share_percent)}</Badge>
              {Number(owner.contribution_amount) > 0 ? (
                <Badge variant="outline">
                  вклад {formatMoney(owner.contribution_amount)}
                  {owner.contributed_on ? ` · ${owner.contributed_on}` : ""}
                </Badge>
              ) : null}
              {owner.inn ? <Badge variant="outline">ИНН {owner.inn}</Badge> : null}
              {owner.ended_on ? (
                <Badge variant="outline" className="text-muted-foreground">
                  вышел {owner.ended_on}
                </Badge>
              ) : null}
            </div>
            <span className="text-xs text-muted-foreground">
              {Number(owner.contribution_amount) > 0
                ? "расчёты — в карточке контрагента"
                : "вклад не указан — в балансе он молча уйдёт в капитал"}
            </span>
          </div>
        ))}

        {owners.length > 0 ? (
          <div
            className={
              totalIsWhole
                ? "text-sm text-muted-foreground"
                : "rounded-md border border-amber-200 bg-amber-50/60 px-3 py-2 text-sm text-amber-900"
            }
          >
            Сумма долей: {formatShare(String(total))}
            {totalIsWhole
              ? ""
              : " — бизнес поделён не целиком. Дивиденды по таким долям разойдутся с фактом."}
          </div>
        ) : null}
      </CardContent>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Новый собственник</DialogTitle>
            <DialogDescription>
              Заводится карточкой контрагента — так у каждого появляются собственные расчёты с
              бизнесом: что внёс, что вернули, что начислено дивидендами.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-3" onSubmit={handleSubmit}>
            <div className="space-y-1">
              <Label htmlFor="owner-name">Имя</Label>
              <Input
                id="owner-name"
                value={name}
                placeholder="Павел"
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="owner-share">Доля, %</Label>
                <Input
                  id="owner-share"
                  value={share}
                  onChange={(event) => setShare(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="owner-inn">ИНН</Label>
                <Input
                  id="owner-inn"
                  value={inn}
                  placeholder="необязательно"
                  onChange={(event) => setInn(event.target.value)}
                />
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="owner-contribution">Вклад в капитал, ₽</Label>
                <Input
                  id="owner-contribution"
                  value={contribution}
                  placeholder="0"
                  onChange={(event) => setContribution(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="owner-contributed-on">Дата вклада</Label>
                <Input
                  id="owner-contributed-on"
                  type="date"
                  value={contributedOn}
                  onChange={(event) => setContributedOn(event.target.value)}
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              Вклад, внесённый ДО начала учёта: деньгами он в системе не проходил, и без этой
              строки в балансе он молча уйдёт в капитал безымянной величиной. Вклад после начала
              учёта заводить здесь не нужно — он придёт проводкой в ДДС, и запись тут посчитала
              бы его вторично.
            </p>
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
    </Card>
  );
}
