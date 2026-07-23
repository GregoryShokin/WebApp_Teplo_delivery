import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Pencil, Plus } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { LocationLeases } from "@/routes/settings-location-leases";
import {
  apiErrorMessage,
  createLocation,
  getLocations,
  updateLocation,
  type LocationKind,
  type LocationPayload,
  type LocationRecord,
} from "@/lib/api";

const KIND_LABELS: Record<LocationKind, string> = {
  point: "Торговая точка",
  warehouse: "Склад",
  office: "Офис",
};

type FormState = {
  name: string;
  kind: LocationKind;
  address: string;
  iikoOrganizationId: string;
  iikoDepartmentId: string;
  iikoStoreIds: string;
  openedOn: string;
  closedOn: string;
  note: string;
  status: "active" | "inactive";
};

const EMPTY_FORM: FormState = {
  name: "",
  kind: "point",
  address: "",
  iikoOrganizationId: "",
  iikoDepartmentId: "",
  iikoStoreIds: "",
  openedOn: "",
  closedOn: "",
  note: "",
  status: "active",
};

function toForm(location: LocationRecord): FormState {
  return {
    name: location.name,
    kind: location.kind,
    address: location.address ?? "",
    iikoOrganizationId: location.iiko_organization_id ?? "",
    iikoDepartmentId: location.iiko_department_id ?? "",
    iikoStoreIds: location.iiko_store_ids.join(", "),
    openedOn: location.opened_on ?? "",
    closedOn: location.closed_on ?? "",
    note: location.note ?? "",
    status: location.status,
  };
}

function toPayload(form: FormState): LocationPayload {
  return {
    name: form.name.trim(),
    kind: form.kind,
    address: form.address.trim() || null,
    iiko_organization_id: form.iikoOrganizationId.trim() || null,
    iiko_department_id: form.iikoDepartmentId.trim() || null,
    iiko_store_ids: form.iikoStoreIds
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    opened_on: form.openedOn || null,
    closed_on: form.closedOn || null,
    note: form.note.trim() || null,
    status: form.status,
  };
}

export function LocationsPanel({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<LocationRecord | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  const locationsQuery = useQuery({ queryKey: ["locations"], queryFn: getLocations });

  useEffect(() => {
    if (!dialogOpen) {
      return;
    }
    setForm(editing ? toForm(editing) : EMPTY_FORM);
  }, [dialogOpen, editing]);

  const saveMutation = useMutation({
    mutationFn: async (payload: LocationPayload) =>
      editing ? updateLocation(editing.id, payload) : createLocation(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["locations"] });
      toast.success(editing ? "Помещение обновлено" : "Помещение добавлено");
      setDialogOpen(false);
      setEditing(null);
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  function openCreate() {
    setEditing(null);
    setDialogOpen(true);
  }

  function openEdit(location: LocationRecord) {
    setEditing(location);
    setDialogOpen(true);
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!form.name.trim()) {
      toast.error("Укажите название помещения");
      return;
    }
    saveMutation.mutate(toPayload(form));
  }

  const locations = locationsQuery.data ?? [];

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div>
          <CardTitle className="text-base">Помещения</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            Филиалы, склады и офисы. Помещение отвечает за аренду и расходы точки, а для
            подключённых к iiko хранит организацию, подразделение и склады — из них берутся
            выручка, остатки и накладные.
          </p>
        </div>
        {canEdit ? (
          <Button size="sm" onClick={openCreate}>
            <Plus className="mr-1 h-4 w-4" />
            Добавить
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3">
        {locationsQuery.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="h-4 w-4 animate-spin" />
            Загружаем помещения…
          </div>
        ) : null}

        {locationsQuery.isError ? (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            Не удалось загрузить помещения
          </div>
        ) : null}

        {!locationsQuery.isLoading && locations.length === 0 ? (
          <div className="text-sm text-muted-foreground">Помещения ещё не заведены</div>
        ) : null}

        {locations.map((location) => (
          <div
            key={location.id}
            className="flex flex-wrap items-start justify-between gap-3 rounded-lg border px-3 py-2"
          >
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{location.name}</span>
                <Badge variant="secondary">{KIND_LABELS[location.kind]}</Badge>
                {location.status === "inactive" ? (
                  <Badge variant="outline" className="text-muted-foreground">
                    закрыто{location.closed_on ? ` · ${location.closed_on}` : ""}
                  </Badge>
                ) : null}
                {location.iiko_linked ? (
                  <Badge className="bg-emerald-600 hover:bg-emerald-600">iiko подключён</Badge>
                ) : (
                  <Badge variant="outline">без iiko</Badge>
                )}
              </div>
              {location.address ? (
                <div className="mt-1 text-sm text-muted-foreground">{location.address}</div>
              ) : null}
              {location.iiko_linked ? (
                <div className="mt-1 text-xs text-muted-foreground">
                  подразделение {location.iiko_department_id} · складов{" "}
                  {location.iiko_store_ids.length}
                </div>
              ) : null}
            </div>
            {canEdit ? (
              <Button variant="ghost" size="sm" onClick={() => openEdit(location)}>
                <Pencil className="mr-1 h-4 w-4" />
                Изменить
              </Button>
            ) : null}

            <div className="w-full">
              <LocationLeases
                locationId={location.id}
                locationName={location.name}
                canEdit={canEdit}
              />
            </div>
          </div>
        ))}
      </CardContent>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{editing ? "Изменить помещение" : "Новое помещение"}</DialogTitle>
            <DialogDescription>
              Идентификаторы iiko можно оставить пустыми — арендованный склад или офис в iiko не
              заведены, но аренду и расходы они всё равно несут.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-3" onSubmit={handleSubmit}>
            <div className="space-y-1">
              <Label htmlFor="location-name">Название</Label>
              <Input
                id="location-name"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                placeholder="Черникова"
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label>Тип</Label>
                <Select
                  value={form.kind}
                  onValueChange={(value) => setForm({ ...form, kind: value as LocationKind })}
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
                <Label>Статус</Label>
                <Select
                  value={form.status}
                  onValueChange={(value) =>
                    setForm({ ...form, status: value as "active" | "inactive" })
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="active">Работает</SelectItem>
                    <SelectItem value="inactive">Закрыто</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-1">
              <Label htmlFor="location-address">Адрес</Label>
              <Input
                id="location-address"
                value={form.address}
                onChange={(event) => setForm({ ...form, address: event.target.value })}
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="location-opened">Открыто</Label>
                <Input
                  id="location-opened"
                  type="date"
                  value={form.openedOn}
                  onChange={(event) => setForm({ ...form, openedOn: event.target.value })}
                />
              </div>
              {form.status === "inactive" ? (
                <div className="space-y-1">
                  <Label htmlFor="location-closed">Закрыто</Label>
                  <Input
                    id="location-closed"
                    type="date"
                    value={form.closedOn}
                    onChange={(event) => setForm({ ...form, closedOn: event.target.value })}
                  />
                </div>
              ) : null}
            </div>

            <div className="rounded-md border p-3">
              <div className="text-sm font-medium">Привязка к iiko</div>
              <p className="mt-1 text-xs text-muted-foreground">
                Организация нужна для накладных и платежей поставщикам, подразделение — для
                выручки и кассовых смен, склады — для остатков и инвентаризаций.
              </p>
              <div className="mt-3 space-y-3">
                <div className="space-y-1">
                  <Label htmlFor="location-org">Организация (organizationId)</Label>
                  <Input
                    id="location-org"
                    value={form.iikoOrganizationId}
                    onChange={(event) =>
                      setForm({ ...form, iikoOrganizationId: event.target.value })
                    }
                    placeholder="5c7e51f9-…"
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="location-dep">Подразделение (departmentId)</Label>
                  <Input
                    id="location-dep"
                    value={form.iikoDepartmentId}
                    onChange={(event) => setForm({ ...form, iikoDepartmentId: event.target.value })}
                    placeholder="d8d4a22e-…"
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="location-stores">Склады (storeId через запятую)</Label>
                  <Input
                    id="location-stores"
                    value={form.iikoStoreIds}
                    onChange={(event) => setForm({ ...form, iikoStoreIds: event.target.value })}
                    placeholder="склад кухни, склад бара"
                  />
                </div>
              </div>
            </div>

            <div className="space-y-1">
              <Label htmlFor="location-note">Заметка</Label>
              <Textarea
                id="location-note"
                value={form.note}
                onChange={(event) => setForm({ ...form, note: event.target.value })}
                rows={2}
              />
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDialogOpen(false)}
                disabled={saveMutation.isPending}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={saveMutation.isPending}>
                {saveMutation.isPending ? (
                  <LoaderCircle className="mr-1 h-4 w-4 animate-spin" />
                ) : null}
                Сохранить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
