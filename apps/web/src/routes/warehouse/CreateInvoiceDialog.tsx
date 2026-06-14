import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2, Warehouse } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
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
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

import { getRegistry, type RegistryItem } from "../counterparties/api";
import { formatRub } from "../counterparties/shared";
import {
  createBarterReturn,
  createWarehouseInvoice,
  getLoanReturnable,
  getNextInvoiceNumber,
  getOpenLoans,
  getProducts,
  type WarehouseProduct,
} from "./api";

type DraftLine = {
  key: string;
  product_id: string | null;
  name: string;
  unit: string | null;
  quantity: string;
  price: string;
  vat: string;
  is_staff: boolean;
};

function emptyLine(): DraftLine {
  return {
    key: Math.random().toString(36).slice(2),
    product_id: null,
    name: "",
    unit: null,
    quantity: "",
    price: "",
    vat: "",
    is_staff: false,
  };
}

const num = (v: string) => Math.max(0, Number(v) || 0);

export function CreateInvoiceDialog({
  open,
  onOpenChange,
  onCreated,
  barter = false,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
  barter?: boolean;
}) {
  const [kind, setKind] = useState<"normal" | "barter">(barter ? "barter" : "normal");
  const [weLend, setWeLend] = useState(true);
  const [barterAction, setBarterAction] = useState<"loan" | "return">("loan");
  const [counterpartyId, setCounterpartyId] = useState("");
  const [issuedAt, setIssuedAt] = useState("");
  const [number, setNumber] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);
  const queryClient = useQueryClient();
  const isBarter = kind === "barter";
  const isReturn = isBarter && barterAction === "return";

  const registryQuery = useQuery({
    queryKey: ["cp", "registry", "all"],
    queryFn: () => getRegistry(),
    enabled: open,
  });
  // Все GOODS загружаем разом и фильтруем на клиенте — как поиск контрагентов.
  const productsQuery = useQuery({
    queryKey: ["wh", "products", "goods-all"],
    queryFn: () => getProducts({ type: "GOODS", limit: 2000 }),
    enabled: open,
  });
  const nextNumberQuery = useQuery({
    queryKey: ["wh", "next-number"],
    queryFn: getNextInvoiceNumber,
    enabled: open,
  });

  useEffect(() => {
    if (open && !number && nextNumberQuery.data) {
      setNumber(nextNumberQuery.data);
    }
  }, [open, number, nextNumberQuery.data]);

  // Инбокс задаёт лишь значение по умолчанию; внутри окна можно переключить.
  useEffect(() => {
    if (open) setKind(barter ? "barter" : "normal");
  }, [open, barter]);

  const reset = () => {
    setKind(barter ? "barter" : "normal");
    setWeLend(true);
    setBarterAction("loan");
    setCounterpartyId("");
    setIssuedAt("");
    setNumber("");
    setLines([emptyLine()]);
  };

  const createMutation = useMutation({
    mutationFn: () =>
      createWarehouseInvoice({
        counterparty_id: counterpartyId,
        issued_at: issuedAt,
        mode: isBarter ? "loan" : "normal",
        we_lend: weLend,
        number: number || null,
        lines: lines
          .filter((l) => l.name && num(l.quantity) > 0)
          .map((l) => ({
            name: l.name,
            quantity: num(l.quantity),
            price: num(l.price),
            iiko_product_id: l.product_id,
            vat_percent: num(l.vat) > 0 ? num(l.vat) : null,
            is_staff: l.is_staff,
          })),
      }),
    onSuccess: () => {
      onCreated();
      void queryClient.invalidateQueries({ queryKey: ["wh", "next-number"] });
      reset();
      onOpenChange(false);
      toast.success(isBarter ? "Займ оформлен" : "Накладная создана");
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось создать накладную")),
  });

  const totals = useMemo(() => {
    let total = 0;
    let staff = 0;
    for (const l of lines) {
      const s = num(l.quantity) * num(l.price);
      total += s;
      if (l.is_staff) staff += s;
    }
    return { total, staff };
  }, [lines]);

  const updateLine = (key: string, patch: Partial<DraftLine>) =>
    setLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));

  const filledLines = lines.filter((l) => l.name && num(l.quantity) > 0).length;
  const canSave = !!counterpartyId && !!issuedAt && filledLines > 0 && !createMutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Warehouse size={18} aria-hidden="true" />
            {isBarter ? "Бартерная накладная" : "Создать накладную"}
          </DialogTitle>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="inline-flex w-fit overflow-hidden rounded-md border">
            <button
              type="button"
              className={cn(
                "px-4 py-1.5 text-sm",
                !isBarter && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => setKind("normal")}
            >
              Обычная
            </button>
            <button
              type="button"
              className={cn(
                "px-4 py-1.5 text-sm",
                isBarter && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => setKind("barter")}
            >
              Бартер
            </button>
          </div>

          {isBarter ? (
            <div className="flex flex-wrap items-center gap-2">
              <div className="inline-flex overflow-hidden rounded-md border">
                <button
                  type="button"
                  className={cn("px-3 py-1 text-xs", !isReturn && "bg-primary/10 font-medium text-primary")}
                  onClick={() => setBarterAction("loan")}
                >
                  Займ
                </button>
                <button
                  type="button"
                  className={cn("px-3 py-1 text-xs", isReturn && "bg-primary/10 font-medium text-primary")}
                  onClick={() => setBarterAction("return")}
                >
                  Возврат
                </button>
              </div>
              {!isReturn ? (
                <div className="inline-flex overflow-hidden rounded-md border">
                  <button
                    type="button"
                    className={cn("px-3 py-1 text-xs", weLend && "bg-muted font-medium")}
                    onClick={() => setWeLend(true)}
                  >
                    Мы выдаём
                  </button>
                  <button
                    type="button"
                    className={cn("px-3 py-1 text-xs", !weLend && "bg-muted font-medium")}
                    onClick={() => setWeLend(false)}
                  >
                    Нам выдают
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}

          {isReturn ? (
            <ReturnForm
              onDone={() => {
                onCreated();
                void queryClient.invalidateQueries({ queryKey: ["wh", "next-number"] });
                reset();
                onOpenChange(false);
              }}
            />
          ) : (
            <>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="grid gap-2">
              <Label>Контрагент</Label>
              <CounterpartySearch
                items={registryQuery.data ?? []}
                value={counterpartyId}
                onPick={setCounterpartyId}
              />
            </div>
            <div className="grid gap-2">
              <Label>Дата и время чека</Label>
              <Input
                type="datetime-local"
                value={issuedAt}
                onChange={(e) => setIssuedAt(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label>Номер</Label>
              <Input value={number} onChange={(e) => setNumber(e.target.value)} />
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>{isBarter ? "Товары займа" : "Строки накладной"}</Label>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setLines((prev) => [...prev, emptyLine()])}
              >
                <Plus size={14} aria-hidden="true" />
                Строка
              </Button>
            </div>

            <div
              className={cn(
                "grid items-center gap-2 px-2 text-xs text-muted-foreground",
                isBarter
                  ? "grid-cols-[1fr_56px_76px_80px_auto]"
                  : "grid-cols-[1fr_56px_76px_52px_80px_auto_auto]",
              )}
            >
              <span>Товар</span>
              <span>Кол-во</span>
              <span>Цена</span>
              {!isBarter ? <span>НДС%</span> : null}
              <span className="text-right">Сумма</span>
              {!isBarter ? <span className="text-center">Перс.</span> : null}
              <span aria-hidden="true" />
            </div>

            <div className="space-y-2">
              {lines.map((line) => (
                <LineRow
                  key={line.key}
                  line={line}
                  barter={isBarter}
                  products={productsQuery.data ?? []}
                  onChange={(patch) => updateLine(line.key, patch)}
                  onRemove={() =>
                    setLines((prev) =>
                      prev.length > 1 ? prev.filter((l) => l.key !== line.key) : prev,
                    )
                  }
                />
              ))}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-4 rounded-md bg-muted/50 p-2 text-sm tabular-nums">
            <span>
              Итого: <span className="font-medium">{formatRub(totals.total)}</span>
            </span>
            {totals.staff > 0 ? (
              <span className="text-amber-700">Персонал: {formatRub(totals.staff)}</span>
            ) : null}
          </div>
            </>
          )}
        </div>

        {!isReturn ? (
          <DialogFooter>
            <Button disabled={!canSave} onClick={() => createMutation.mutate()}>
              {createMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              {isBarter ? "Оформить займ" : "Создать"}
            </Button>
          </DialogFooter>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function ReturnForm({ onDone }: { onDone: () => void }) {
  const [loanId, setLoanId] = useState("");
  const [issuedAt, setIssuedAt] = useState("");
  const [qtyByLine, setQtyByLine] = useState<Record<string, string>>({});

  const loansQuery = useQuery({ queryKey: ["wh", "loans"], queryFn: () => getOpenLoans() });
  const returnableQuery = useQuery({
    queryKey: ["wh", "returnable", loanId],
    queryFn: () => getLoanReturnable(loanId),
    enabled: !!loanId,
  });
  const loan = returnableQuery.data;

  const returnLines = useMemo(() => {
    if (!loan) return [];
    return loan.lines.map((l) => {
      const qty = Math.min(num(qtyByLine[l.id] ?? ""), l.remaining_qty);
      return { ...l, raw: qtyByLine[l.id] ?? "", qty, amount: Math.round(qty * l.price * 100) / 100 };
    });
  }, [loan, qtyByLine]);
  const totalReturn = returnLines.reduce((s, l) => s + l.amount, 0);

  const returnMutation = useMutation({
    mutationFn: () =>
      createBarterReturn({
        loan_id: loanId,
        issued_at: issuedAt,
        returns: returnLines
          .filter((l) => l.qty > 0)
          .map((l) => ({ loan_line_item_id: l.id, quantity: l.qty, amount: l.amount })),
      }),
    onSuccess: () => {
      toast.success("Возврат проведён");
      onDone();
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось провести возврат")),
  });

  const canReturn = !!loanId && !!issuedAt && totalReturn > 0 && !returnMutation.isPending;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label>Заём для возврата</Label>
          <Select
            value={loanId}
            onValueChange={(v) => {
              setLoanId(v);
              setQtyByLine({});
            }}
          >
            <SelectTrigger>
              <SelectValue placeholder="Выберите открытый заём" />
            </SelectTrigger>
            <SelectContent>
              {(loansQuery.data ?? []).map((ln) => (
                <SelectItem key={ln.id} value={ln.id}>
                  {ln.counterparty_name} · №{ln.number ?? "—"} · остаток {formatRub(ln.remaining)} ·{" "}
                  {ln.we_lend ? "мы выдали" : "нам выдали"}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <Label>Дата и время возврата</Label>
          <Input
            type="datetime-local"
            value={issuedAt}
            onChange={(e) => setIssuedAt(e.target.value)}
          />
        </div>
      </div>

      {loan ? (
        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_96px_84px_90px] gap-2 px-2 text-xs text-muted-foreground">
            <span>Товар</span>
            <span className="text-right">Остаток</span>
            <span className="text-right">Вернуть</span>
            <span className="text-right">Сумма</span>
          </div>
          {returnLines.map((l) => (
            <div
              key={l.id}
              className="grid grid-cols-[1fr_96px_84px_90px] items-center gap-2 rounded-md border p-2 text-sm"
            >
              <span className="truncate">{l.name}</span>
              <span className="text-right tabular-nums text-muted-foreground">
                {l.remaining_qty} {l.unit ?? ""}
              </span>
              <Input
                type="number"
                min={0}
                max={l.remaining_qty}
                value={l.raw}
                onChange={(e) => setQtyByLine((p) => ({ ...p, [l.id]: e.target.value }))}
              />
              <span className="text-right tabular-nums">{formatRub(l.amount)}</span>
            </div>
          ))}
          <div className="flex items-center justify-between rounded-md bg-muted/50 p-2 text-sm">
            <span>
              К возврату: <span className="font-medium tabular-nums">{formatRub(totalReturn)}</span>
            </span>
            <span className="text-xs text-muted-foreground">
              остаток займа {formatRub(loan.remaining)}
            </span>
          </div>
        </div>
      ) : null}

      <DialogFooter>
        <Button disabled={!canReturn} onClick={() => returnMutation.mutate()}>
          {returnMutation.isPending ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : null}
          Провести возврат
        </Button>
      </DialogFooter>
    </div>
  );
}

function LineRow({
  line,
  barter,
  products,
  onChange,
  onRemove,
}: {
  line: DraftLine;
  barter: boolean;
  products: WarehouseProduct[];
  onChange: (patch: Partial<DraftLine>) => void;
  onRemove: () => void;
}) {
  const sum = num(line.quantity) * num(line.price);
  return (
    <div
      className={cn(
        "grid items-center gap-2 rounded-md border p-2",
        barter
          ? "grid-cols-[1fr_56px_76px_80px_auto]"
          : "grid-cols-[1fr_56px_76px_52px_80px_auto_auto]",
        line.is_staff && "border-amber-200 bg-amber-50/40",
      )}
    >
      <ProductSearch
        value={line.name}
        products={products}
        onPick={(p) => onChange({ product_id: p.id, name: p.name, unit: p.unit })}
        onTextChange={(text) => onChange({ name: text, product_id: null, unit: null })}
      />
      <Input
        type="number"
        min={0}
        placeholder="кол-во"
        value={line.quantity}
        onChange={(e) => onChange({ quantity: e.target.value })}
        title={line.unit ?? undefined}
      />
      <Input
        type="number"
        min={0}
        placeholder="цена"
        value={line.price}
        onChange={(e) => onChange({ price: e.target.value })}
      />
      {!barter ? (
        <Input
          type="number"
          min={0}
          placeholder="НДС%"
          value={line.vat}
          onChange={(e) => onChange({ vat: e.target.value })}
          title="Ставка НДС, %"
        />
      ) : null}
      <span className="text-right text-sm tabular-nums text-muted-foreground">{formatRub(sum)}</span>
      {!barter ? (
        <label className="flex items-center justify-center" title="Персонал — не уходит в iiko">
          <Checkbox
            checked={line.is_staff}
            onChange={() => onChange({ is_staff: !line.is_staff })}
            aria-label="Персонал"
          />
        </label>
      ) : null}
      <button
        type="button"
        className="text-muted-foreground hover:text-red-600"
        onClick={onRemove}
        aria-label="Удалить строку"
      >
        <Trash2 size={15} />
      </button>
    </div>
  );
}

function CounterpartySearch({
  items,
  value,
  onPick,
}: {
  items: RegistryItem[];
  value: string;
  onPick: (id: string) => void;
}) {
  const [term, setTerm] = useState("");
  const [focused, setFocused] = useState(false);
  const selected = items.find((i) => i.counterparty_id === value);

  const matches = useMemo(() => {
    const q = term.trim().toLowerCase();
    if (!q) return items.slice(0, 12);
    return items.filter((i) => i.name.toLowerCase().includes(q)).slice(0, 12);
  }, [items, term]);

  return (
    <div className="relative">
      <Input
        value={focused ? term : selected?.name ?? term}
        placeholder="Начните вводить имя"
        onChange={(e) => setTerm(e.target.value)}
        onFocus={() => {
          setFocused(true);
          setTerm("");
        }}
        onBlur={() => setTimeout(() => setFocused(false), 150)}
      />
      {focused && matches.length > 0 ? (
        <div className="absolute z-50 mt-1 max-h-56 w-full overflow-auto rounded-md border bg-popover p-1 shadow-md">
          {matches.map((i) => (
            <button
              key={i.counterparty_id}
              type="button"
              className="block w-full truncate rounded px-2 py-1.5 text-left text-sm hover:bg-muted"
              onMouseDown={(e) => {
                e.preventDefault();
                onPick(i.counterparty_id);
                setFocused(false);
              }}
            >
              {i.name}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ProductSearch({
  value,
  products,
  onPick,
  onTextChange,
}: {
  value: string;
  products: WarehouseProduct[];
  onPick: (p: WarehouseProduct) => void;
  onTextChange: (text: string) => void;
}) {
  const [focused, setFocused] = useState(false);

  // Клиентский фильтр по загруженному списку GOODS — как поиск контрагентов.
  const matches = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (!q) return products.slice(0, 12);
    return products
      .filter(
        (p) =>
          p.name.toLowerCase().includes(q) || (p.code ?? "").toLowerCase().includes(q),
      )
      .slice(0, 12);
  }, [products, value]);

  return (
    <div className="relative">
      <Input
        value={value}
        placeholder="Товар (GOODS)"
        onChange={(e) => onTextChange(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setTimeout(() => setFocused(false), 150)}
      />
      {focused && matches.length > 0 ? (
        <div className="absolute z-50 mt-1 max-h-56 w-full overflow-auto rounded-md border bg-popover p-1 shadow-md">
          {matches.map((p) => (
            <button
              key={p.id}
              type="button"
              className="flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-muted"
              onMouseDown={(e) => {
                e.preventDefault();
                onPick(p);
                setFocused(false);
              }}
            >
              <span className="truncate">{p.name}</span>
              <span className="shrink-0 text-xs text-muted-foreground">{p.unit ?? ""}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
