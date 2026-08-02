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

/**
 * Реестр собственников.
 *
 * Собственник — обычный контрагент с ролью «Собственник», своей таблицы у него нет: ему нужны
 * ровно те же расчёты, что и любому контрагенту, а второй реестр означал бы вторую, несовместимую
 * историю долга. Экран отдельный по другой причине: собственников двое, в списке поставщиков они
 * не ищутся, а три статьи ДДС — взнос, возврат и дивиденды — требуют назвать, чьё это движение.
 *
 * Пока реестр пуст, эти статьи провести нельзя вовсе. Это правильный отказ, а не помеха: деньги
 * каждого собственника учитываются отдельно, и «поступление от собственников» без имени —
 * общий котёл, из которого не вынуть, кто сколько внёс.
 */
export function OwnersPanel({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [name, setName] = useState("");
  const [inn, setInn] = useState("");

  const ownersQuery = useQuery({ queryKey: ["owners"], queryFn: getOwners });

  const saveMutation = useMutation({
    mutationFn: () => createOwner({ name: name.trim(), inn: inn.trim() || null }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["owners"] });
      toast.success("Собственник заведён");
      setDialogOpen(false);
      setName("");
      setInn("");
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      toast.error("Укажите имя собственника");
      return;
    }
    saveMutation.mutate();
  }

  const owners = ownersQuery.data ?? [];

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div>
          <CardTitle className="text-base">Собственники</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            Кто вкладывает деньги в бизнес и кому он должен. Взнос, возврат и дивиденды
            учитываются по каждому отдельно — поэтому в этих движениях система спрашивает имя.
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
              {owner.inn ? <Badge variant="outline">ИНН {owner.inn}</Badge> : null}
              {owner.status !== "active" ? (
                <Badge variant="outline" className="text-muted-foreground">
                  {owner.status}
                </Badge>
              ) : null}
            </div>
            {/* Карточка собственника — обычная карточка контрагента: там его расчёты. */}
            <span className="text-xs text-muted-foreground">
              расчёты — в карточке контрагента
            </span>
          </div>
        ))}
      </CardContent>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Новый собственник</DialogTitle>
            <DialogDescription>
              Заводится карточкой контрагента с ролью «Собственник» — так у каждого появляются
              собственные расчёты с бизнесом: что внёс, что вернули, что начислено дивидендами.
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
            <div className="space-y-1">
              <Label htmlFor="owner-inn">ИНН</Label>
              <Input
                id="owner-inn"
                value={inn}
                placeholder="необязательно"
                onChange={(event) => setInn(event.target.value)}
              />
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
    </Card>
  );
}
