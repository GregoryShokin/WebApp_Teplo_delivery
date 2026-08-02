import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
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
import {
  apiErrorMessage,
  getUtilityIntakeFileUrl,
  promoteUtilityIntake,
  recognizeUtilityIntake,
  updateUtilityIntake,
  type UtilityAccountRecord,
  type UtilityIntakeRecord,
} from "@/lib/api";

/**
 * Сумма к отправке: запятую меняем на точку и убираем пробелы-разделители разрядов.
 * На русской раскладке «9878,79» набирается само собой, а сервер ждёт Decimal — без нормализации
 * человек получал 422 с простынёй валидатора вместо цифры.
 */
function normalizeAmount(raw: string): string {
  return raw.replace(/\s|\u00a0/g, "").replace(",", ".");
}

/** Границы месяца по его номеру: период набивать руками не нужно, счёт всегда за месяц. */
function monthBounds(month: string): { start: string; end: string } {
  const [year, index] = month.split("-").map(Number);
  const last = new Date(year, index, 0).getDate();
  const pad = (n: number) => String(n).padStart(2, "0");
  return { start: `${year}-${pad(index)}-01`, end: `${year}-${pad(index)}-${pad(last)}` };
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <div className="grid gap-1">
      <Label className="text-muted-foreground text-xs">{label}</Label>
      <Input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

/**
 * Разбор коммунальной платёжки: документ слева, поля справа.
 *
 * Тот же порядок работы, что и в разборе счёта на «Странице на оплату»: человек видит бумагу
 * и то, что из неё вычитали, рядом — и правит, не переключая окон. Разница одна: сюда
 * приносят ФОТО, а не PDF, поэтому картинку показываем тегом img (iframe отрисовал бы её как
 * файл, без масштабирования по месту).
 */
export function UtilityReviewDialog({
  intake,
  accounts,
  onClose,
  onSaved,
}: {
  intake: UtilityIntakeRecord | null;
  accounts: UtilityAccountRecord[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [fileUrl, setFileUrl] = useState<string | null>(null);
  const [accountId, setAccountId] = useState("");
  const [month, setMonth] = useState("");
  const [amount, setAmount] = useState("");
  const [documentNumber, setDocumentNumber] = useState("");
  const [documentDate, setDocumentDate] = useState("");
  const [note, setNote] = useState("");
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  // Форму наполняем один раз на открытие: пока человек правит поля, обновление списка в
  // фоне не должно затирать введённое.
  if (intake && loadedFor !== intake.id) {
    setLoadedFor(intake.id);
    setAccountId(intake.account_id ?? "");
    setMonth((intake.period_end ?? new Date().toISOString()).slice(0, 7));
    setAmount(intake.amount ?? "");
    setDocumentNumber(intake.document_number ?? "");
    setDocumentDate(intake.document_date ?? "");
    setNote(intake.note ?? "");
  }

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    setFileUrl(null);
    if (intake?.has_document) {
      getUtilityIntakeFileUrl(intake.id)
        .then((value) => {
          if (cancelled) {
            URL.revokeObjectURL(value);
            return;
          }
          url = value;
          setFileUrl(value);
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
      // Освобождаем objectURL: иначе каждое открытие диалога оставляет копию файла в памяти
      // вкладки до перезагрузки страницы.
      if (url) URL.revokeObjectURL(url);
    };
  }, [intake?.id, intake?.has_document]);

  const save = useMutation({
    mutationFn: async (thenPromote: boolean) => {
      if (!intake) return;
      const bounds = monthBounds(month);
      await updateUtilityIntake(intake.id, {
        account_id: accountId || null,
        period_start: bounds.start,
        period_end: bounds.end,
        amount: normalizeAmount(amount).trim() || null,
        document_number: documentNumber.trim() || null,
        document_date: documentDate || null,
        note: note.trim() || null,
        status: "ready",
      });
      if (thenPromote) await promoteUtilityIntake(intake.id);
    },
    onSuccess: async (_data, thenPromote) => {
      toast.success(
        thenPromote ? "Долг перед арендодателем создан, расход признан" : "Данные сохранены",
      );
      await onSaved();
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  const recognize = useMutation({
    mutationFn: async () => {
      if (!intake) throw new Error("нет платёжки");
      return recognizeUtilityIntake(intake.id);
    },
    onSuccess: async (updated) => {
      // Подставляем только то, что человек ещё не заполнил: его правка старше машинной.
      if (!accountId && updated.account_id) setAccountId(updated.account_id);
      if (!amount.trim() && updated.amount) setAmount(updated.amount);
      if (updated.period_end) setMonth(updated.period_end.slice(0, 7));
      if (!documentNumber.trim() && updated.document_number)
        setDocumentNumber(updated.document_number);
      if (!documentDate && updated.document_date) setDocumentDate(updated.document_date);
      const status = (updated.recognition as { status?: string } | null)?.status;
      if (status === "recognized") toast.success("Документ распознан — сверьте сумму и месяц");
      else if (status === "not_utility")
        toast.warning("Это не похоже на коммунальный документ — заполните поля вручную");
      else toast.warning("Распознать не удалось — введите сумму, глядя на документ");
      await onSaved();
    },
    onError: (error) => toast.error(apiErrorMessage(error)),
  });

  const isImage = (intake?.mime ?? "").startsWith("image/");
  const parsedAmount = Number(normalizeAmount(amount));
  const amountValid = amount.trim() !== "" && Number.isFinite(parsedAmount) && parsedAmount > 0;
  const canPromote = accountId !== "" && amountValid && month !== "";
  const recognised = Boolean(intake?.recognition);

  return (
    <Dialog open={intake !== null} onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>Разбор коммунальной платёжки</DialogTitle>
          <DialogDescription>
            Сверьте сумму и месяц с документом, при необходимости поправьте. После проведения
            появится долг перед арендодателем, а расход встанет в месяц потребления.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="bg-muted/30 min-h-[60vh] rounded-md border">
            {!intake?.has_document ? (
              <div className="text-muted-foreground flex h-[60vh] items-center justify-center px-6 text-center text-sm">
                Документа нет — сумму называет арендодатель. Так приходит газ.
              </div>
            ) : fileUrl === null ? (
              <div className="text-muted-foreground flex h-[60vh] items-center justify-center text-sm">
                Загрузка документа…
              </div>
            ) : isImage ? (
              <div className="h-[60vh] overflow-auto rounded-md p-2">
                {/* max-w-full, а не w-full: мелкий снимок не должен растягиваться во всю
                    ширину — размытую квитанцию не сверить. */}
                <img src={fileUrl} alt="Платёжка" className="mx-auto max-w-full" />
              </div>
            ) : (
              <iframe title="Платёжка" src={fileUrl} className="h-[60vh] w-full rounded-md" />
            )}
          </div>

          <div className="grid max-h-[60vh] gap-3 overflow-auto pr-1">
            <div className="grid gap-1">
              <Label className="text-muted-foreground text-xs">Поток</Label>
              <Select value={accountId} onValueChange={setAccountId}>
                <SelectTrigger>
                  <SelectValue placeholder="Помещение и вид услуги" />
                </SelectTrigger>
                <SelectContent>
                  {accounts
                    .filter((account) => account.is_active)
                    .map((account) => (
                      <SelectItem key={account.id} value={account.id}>
                        {account.kind_label} · {account.location_name}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
              {accountId ? (
                <p className="text-muted-foreground text-xs">
                  Возмещаем: {accounts.find((a) => a.id === accountId)?.counterparty_name}
                </p>
              ) : null}
            </div>

            <div className="grid gap-2 rounded-md border p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-muted-foreground text-xs font-medium uppercase">
                  Период потребления
                </div>
                {recognised ? (
                  <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700">
                    определён автоматически
                  </span>
                ) : null}
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <div className="grid gap-1">
                  <Label className="text-muted-foreground text-xs">Месяц</Label>
                  <Input type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
                </div>
                <div className="grid gap-1">
                  <Label className="text-muted-foreground text-xs">Сумма к возмещению, ₽</Label>
                  <Input
                    inputMode="decimal"
                    value={amount}
                    onChange={(event) => setAmount(event.target.value)}
                  />
                  {amount.trim() !== "" && !amountValid ? (
                    <p className="text-destructive text-xs">Введите сумму больше нуля</p>
                  ) : null}
                </div>
              </div>
              <p className="text-muted-foreground text-xs">
                Расход встанет в этот месяц, даже если платёжку принесли позже. По окончании
                месяца он признаётся сам.
              </p>
            </div>

            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Номер документа" value={documentNumber} onChange={setDocumentNumber} />
              <Field
                label="Дата документа"
                type="date"
                value={documentDate}
                onChange={setDocumentDate}
              />
            </div>

            <div className="grid gap-1">
              <Label className="text-muted-foreground text-xs">Заметка</Label>
              <Textarea value={note} onChange={(event) => setNote(event.target.value)} rows={2} />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Отмена
          </Button>
          {intake?.has_document ? (
            <Button
              type="button"
              variant="outline"
              disabled={recognize.isPending}
              onClick={() => recognize.mutate()}
            >
              {recognize.isPending ? <LoaderCircle className="mr-2 size-4 animate-spin" /> : null}
              Распознать
            </Button>
          ) : null}
          <Button
            type="button"
            variant="outline"
            disabled={save.isPending}
            onClick={() => save.mutate(false)}
          >
            Сохранить
          </Button>
          <Button
            type="button"
            disabled={save.isPending || !canPromote}
            onClick={() => save.mutate(true)}
          >
            Провести
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
