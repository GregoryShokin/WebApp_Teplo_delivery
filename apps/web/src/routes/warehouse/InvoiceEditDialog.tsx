import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { formatRub } from "@/routes/counterparties/shared";

import {
  emptyLine,
  emptyStaffLine,
  LineRow,
  num,
  StaffLineRow,
  type DraftLine,
  type StaffLine,
} from "./CreateInvoiceDialog";
import { getProducts, getStaffArticles, getWarehouseInvoice, updateWarehouseInvoice } from "./api";

/** Правка позиций НЕОПЛАЧЕННОЙ (не бартерной) накладной: «переделать и отправить в iiko».
 * Контрагент/режим неизменны — меняем только товар/персонал. Переиспользует строки создания. */
export function InvoiceEditDialog({
  invoiceId,
  onOpenChange,
}: {
  invoiceId: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const open = Boolean(invoiceId);

  const detailQuery = useQuery({
    queryKey: ["wh", "invoice", invoiceId],
    queryFn: () => getWarehouseInvoice(invoiceId!),
    enabled: open,
  });
  const productsQuery = useQuery({
    queryKey: ["wh", "products", "goods-all"],
    queryFn: () => getProducts({ type: "GOODS", limit: 2000 }),
    enabled: open,
  });
  const staffArticlesQuery = useQuery({
    queryKey: ["wh", "staff-articles"],
    queryFn: getStaffArticles,
    enabled: open,
    staleTime: 60_000,
  });
  const detail = detailQuery.data;
  const staffArticles = staffArticlesQuery.data ?? [];

  const [lines, setLines] = useState<DraftLine[]>([]);
  const [staffLines, setStaffLines] = useState<StaffLine[]>([]);
  const [number, setNumber] = useState("");

  // Инициализируем из накладной при загрузке детали.
  useEffect(() => {
    if (!detail) return;
    setLines(
      detail.lines
        .filter((l) => !l.is_staff)
        .map((l) => ({
          key: l.id,
          product_id: l.iiko_product_id,
          name: l.name,
          unit: l.unit,
          quantity: String(l.quantity),
          price: String(l.price),
          vat: l.vat_percent ? String(l.vat_percent) : "",
          amount: String(l.sum),
        })),
    );
    setStaffLines(
      detail.lines
        .filter((l) => l.is_staff)
        .map((l) => ({
          key: l.id,
          articleId: l.dds_article_id ?? "",
          note: l.name,
          amount: String(l.sum),
        })),
    );
    setNumber(detail.number ?? "");
  }, [detail?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const totals = useMemo(() => {
    const store = lines.reduce(
      (s, l) => s + (l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price)),
      0,
    );
    const staff = staffLines.reduce((s, l) => s + num(l.amount), 0);
    return { store, staff, total: store + staff };
  }, [lines, staffLines]);

  const editable = detail?.payment_status === "unpaid" && !detail?.barter_role;

  const saveMutation = useMutation({
    mutationFn: () =>
      updateWarehouseInvoice(invoiceId!, {
        number: number.trim() || undefined,
        lines: [
          ...lines
            .filter((l) => l.name && num(l.quantity) > 0)
            .map((l) => ({
              name: l.name,
              quantity: num(l.quantity),
              price: num(l.price),
              iiko_product_id: l.product_id,
              vat_percent: num(l.vat) > 0 ? num(l.vat) : null,
              is_staff: false,
            })),
          ...staffLines
            .filter((l) => l.articleId && num(l.amount) > 0)
            .map((l) => ({
              name:
                l.note.trim() ||
                staffArticles.find((a) => a.id === l.articleId)?.name ||
                "Персонал",
              quantity: 1,
              price: num(l.amount),
              iiko_product_id: null,
              vat_percent: null,
              is_staff: true,
              dds_article_id: l.articleId,
            })),
        ],
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["wh", "invoice", invoiceId], updated);
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      void queryClient.invalidateQueries({ queryKey: ["kassa"] });
      toast.success("Накладная обновлена");
      onOpenChange(false);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось сохранить накладную")),
  });

  const filled = lines.filter((l) => l.name && num(l.quantity) > 0).length;
  const filledStaff = staffLines.filter((l) => l.articleId && num(l.amount) > 0).length;
  // Товарная строка без выбора из номенклатуры iiko теряется при выгрузке — блокируем сохранение.
  const goodsMissingProduct = lines.some(
    (l) => l.name.trim() && num(l.quantity) > 0 && !l.product_id,
  );
  const canSave =
    !!editable && filled + filledStaff > 0 && !saveMutation.isPending && !goodsMissingProduct;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            Редактировать накладную {detail?.number ? `№${detail.number}` : ""}
          </DialogTitle>
        </DialogHeader>

        {!detail ? (
          <div className="flex justify-center py-8 text-muted-foreground">
            <LoaderCircle className="animate-spin" aria-hidden="true" />
          </div>
        ) : !editable ? (
          <p className="py-4 text-sm text-amber-600">
            Редактировать можно только неоплаченную обычную накладную. Эта{" "}
            {detail.barter_role ? "бартерная" : "уже оплачена"} — снимите оплату или измените в
            iiko.
          </p>
        ) : (
          <div className="grid gap-4">
            <div className="grid max-w-[220px] gap-1.5">
              <Label htmlFor="invoice-number">Номер</Label>
              <Input
                id="invoice-number"
                value={number}
                onChange={(e) => setNumber(e.target.value)}
              />
              <span className="text-xs text-muted-foreground">
                Уникален на дату — иначе iiko отклонит дубль
              </span>
            </div>
            {/* Закупка на склад */}
            <div className="space-y-2 rounded-md border p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">Закупка на склад</span>
                <span className="text-xs text-muted-foreground">iiko · приходная накладная</span>
              </div>
              <div className="grid grid-cols-[minmax(0,1fr)_64px_88px_56px_104px_28px] gap-2 text-xs text-muted-foreground">
                <span>Товар</span>
                <span className="text-right">Кол-во</span>
                <span className="text-right">Цена</span>
                <span className="text-right">НДС%</span>
                <span className="text-right">Сумма</span>
                <span />
              </div>
              {lines.map((line) => (
                <LineRow
                  key={line.key}
                  line={line}
                  barter={false}
                  products={productsQuery.data ?? []}
                  onChange={(patch) =>
                    setLines((prev) =>
                      prev.map((l) => (l.key === line.key ? { ...l, ...patch } : l)),
                    )
                  }
                  onRemove={() => setLines((prev) => prev.filter((l) => l.key !== line.key))}
                />
              ))}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setLines((prev) => [...prev, emptyLine()])}
              >
                <Plus size={14} aria-hidden="true" /> товар
              </Button>
            </div>

            {/* Траты на персонал */}
            <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50/40 p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">Траты на персонал</span>
                <span className="text-xs text-muted-foreground">только ДДС · не в iiko</span>
              </div>
              {staffLines.map((line) => (
                <StaffLineRow
                  key={line.key}
                  line={line}
                  articles={staffArticles}
                  onChange={(patch) =>
                    setStaffLines((prev) =>
                      prev.map((l) => (l.key === line.key ? { ...l, ...patch } : l)),
                    )
                  }
                  onRemove={() => setStaffLines((prev) => prev.filter((l) => l.key !== line.key))}
                />
              ))}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setStaffLines((prev) => [...prev, emptyStaffLine()])}
              >
                <Plus size={14} aria-hidden="true" /> трата
              </Button>
            </div>

            <div className="text-sm">
              Итого: <span className="font-medium tabular-nums">{formatRub(totals.total)}</span>{" "}
              <span className="text-muted-foreground">
                (склад {formatRub(totals.store)} + персонал {formatRub(totals.staff)})
              </span>
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Отмена
          </Button>
          <Button disabled={!canSave} onClick={() => saveMutation.mutate()}>
            {saveMutation.isPending ? (
              <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
