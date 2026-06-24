import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiErrorMessage } from "@/lib/api";

import {
  createCounterparty,
  getIikoSuppliers,
  getLedgerCategories,
  type CounterpartyCard,
} from "./api";
import { COUNTERPARTY_TYPE_LABELS, RELATIONSHIP_HINTS, RELATIONSHIP_LABELS } from "./shared";

// Сентинел «не связывать с iiko» — заводим контрагента вручную, без alias.
const NO_IIKO = "none";

export function CreateCounterpartyDialog({
  open,
  onOpenChange,
  defaultRelationship = "official",
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Предустановленный канал оплаты (в бартер-контуре открываем сразу на «Бартер»).
  defaultRelationship?: string;
  // Колбэк после создания — родитель может сразу выбрать нового контрагента.
  onCreated?: (counterparty: CounterpartyCard) => void;
}) {
  const queryClient = useQueryClient();
  const categoriesQuery = useQuery({
    queryKey: ["cp", "categories"],
    queryFn: getLedgerCategories,
    enabled: open,
  });
  // Список поставщиков iiko без привязки — живой запрос к iiko, тянем только при открытии.
  const iikoSuppliersQuery = useQuery({
    queryKey: ["cp", "iiko-suppliers"],
    queryFn: getIikoSuppliers,
    enabled: open,
    staleTime: 60_000,
  });
  const [name, setName] = useState("");
  const [inn, setInn] = useState("");
  const [type, setType] = useState("legal_entity");
  const [relationship, setRelationship] = useState(defaultRelationship);
  const [categoryId, setCategoryId] = useState<string>("");
  const [managerName, setManagerName] = useState("");
  const [managerPhone, setManagerPhone] = useState("");
  const [iikoGuid, setIikoGuid] = useState<string>(NO_IIKO);

  // При каждом открытии возвращаем канал к требуемому по умолчанию и сбрасываем iiko-привязку.
  useEffect(() => {
    if (open) {
      setRelationship(defaultRelationship);
      setIikoGuid(NO_IIKO);
    }
  }, [open, defaultRelationship]);

  // Выбор поставщика iiko: подставляем имя/ИНН и угадываем канал (без ИНН — карта/нал → informal).
  function selectIikoSupplier(guid: string) {
    setIikoGuid(guid);
    if (guid === NO_IIKO) return;
    const supplier = iikoSuppliersQuery.data?.find((item) => item.guid === guid);
    if (!supplier) return;
    setName(supplier.name);
    setInn(supplier.inn ?? "");
    setType(supplier.inn && supplier.inn.length === 12 ? "individual" : "legal_entity");
    setRelationship(supplier.inn ? "official" : "informal");
  }

  // Опции пикера iiko: «не связывать» + незалинкованные поставщики (поиск по имени и ИНН).
  const iikoOptions: ComboboxOption[] = [
    { value: NO_IIKO, label: "Не связывать (ввести вручную)" },
    ...(iikoSuppliersQuery.data ?? []).map((supplier) => ({
      value: supplier.guid,
      label: supplier.inn ? `${supplier.name} · ИНН ${supplier.inn}` : supplier.name,
      keywords: supplier.inn ?? undefined,
    })),
  ];

  const createMutation = useMutation({
    mutationFn: () =>
      createCounterparty({
        name,
        inn: inn || null,
        type,
        relationship,
        ledger_category_id: categoryId || null,
        manager_name: managerName || null,
        manager_phone: managerPhone || null,
        iiko_supplier_guid: iikoGuid === NO_IIKO ? null : iikoGuid,
      }),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      setName("");
      setInn("");
      setManagerName("");
      setManagerPhone("");
      setIikoGuid(NO_IIKO);
      onOpenChange(false);
      toast.success("Контрагент создан");
      onCreated?.(created);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать контрагента")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Новый контрагент</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Поставщик из iiko (необязательно)</Label>
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
              {iikoGuid === NO_IIKO
                ? "Свяжите с поставщиком iiko, чтобы накладные подтягивались на этого контрагента (а не плодили дубль). Нужно для предоплат поставщику, который ещё не присылал накладных."
                : "Накладные этого поставщика iiko будут привязываться к контрагенту автоматически."}
            </p>
          </div>
          <div className="grid gap-2">
            <Label>Официальное название</Label>
            <Input value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>ИНН</Label>
              <Input value={inn} onChange={(event) => setInn(event.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label>Тип</Label>
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
            </div>
          </div>
          <div className="grid gap-2">
            <Label>Тип отношений</Label>
            <Select value={relationship} onValueChange={setRelationship}>
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
          </div>
          <div className="grid gap-2">
            <Label>Категория (леджер)</Label>
            <Select value={categoryId} onValueChange={setCategoryId}>
              <SelectTrigger>
                <SelectValue placeholder="Не выбрана" />
              </SelectTrigger>
              <SelectContent>
                {(categoriesQuery.data ?? []).map((category) => (
                  <SelectItem key={category.id} value={category.id}>
                    {category.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>Менеджер</Label>
              <Input value={managerName} onChange={(event) => setManagerName(event.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label>Телефон менеджера</Label>
              <Input
                value={managerPhone}
                onChange={(event) => setManagerPhone(event.target.value)}
              />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button
            disabled={!name.trim() || createMutation.isPending}
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
