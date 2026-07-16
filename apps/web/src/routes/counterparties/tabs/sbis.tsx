import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EyeOff, ExternalLink, FileText, LoaderCircle, Plug, RefreshCw, Undo2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { apiErrorMessage } from "@/lib/api";

import {
  dismissSbisDocument,
  enableSbisChannel,
  fetchSbisPdfUrl,
  getSbisDocuments,
  restoreSbisDocument,
  syncSbisDocuments,
  type SbisDocument,
} from "../api";
import { MetricCard, formatDate, formatRub } from "../shared";

// Человеческие подписи типов документов СБИС (Документ.Тип).
const DOC_TYPE_LABELS: Record<string, string> = {
  СчетВх: "Счёт на оплату",
  ДокОтгрВх: "УПД / отгрузка",
  АктСверВх: "Акт сверки",
  ДоговорВх: "Договор",
  КоррВх: "Корреспонденция",
};

type FilterValue =
  | "all"
  | "new_counterparty"
  | "materialized"
  | "duplicate"
  | "unmatched"
  | "matched"
  | "dismissed";

const FILTERS: Array<{ value: FilterValue; label: string }> = [
  { value: "all", label: "Все документы" },
  { value: "new_counterparty", label: "Новые контрагенты" },
  { value: "materialized", label: "Счета созданы" },
  { value: "duplicate", label: "Дубли счетов" },
  { value: "unmatched", label: "Зеркало: нет в iiko" },
  { value: "matched", label: "Зеркало: связаны" },
  { value: "dismissed", label: "Скрытые" },
];

function matchesFilter(doc: SbisDocument, filter: FilterValue): boolean {
  switch (filter) {
    case "all":
      return doc.match_status !== "dismissed";
    case "new_counterparty":
    case "materialized":
    case "duplicate":
      return doc.intake_status === filter && doc.match_status !== "dismissed";
    case "unmatched":
    case "matched":
      return doc.intake_status === "mirror" && doc.match_status === filter;
    case "dismissed":
      return doc.match_status === "dismissed";
  }
}

// Единый статус строки: итог маршрутизации, для зеркальных — статус сверки с iiko.
function StatusBadge({ doc }: { doc: SbisDocument }) {
  if (doc.match_status === "dismissed") {
    return <Badge variant="secondary">Скрыт из сверки</Badge>;
  }
  if (doc.intake_status === "new_counterparty") {
    return (
      <Badge variant="outline" className="border-amber-300 bg-amber-50 text-amber-800">
        Новый контрагент — настроить карточку
      </Badge>
    );
  }
  if (doc.intake_status === "materialized") {
    const paid = doc.invoice?.payment_status === "paid";
    const partial = doc.invoice?.payment_status === "partially_paid";
    return (
      <Badge variant="outline" className="border-emerald-300 bg-emerald-50 text-emerald-700">
        {paid
          ? "Закрывающий — погашен предоплатой"
          : partial
            ? "Счёт частично погашен предоплатой"
            : "Счёт к оплате"}
        {doc.invoice?.number ? ` №${doc.invoice.number}` : ""}
      </Badge>
    );
  }
  if (doc.intake_status === "sent_to_recognition") {
    return (
      <Badge variant="outline" className="border-sky-300 bg-sky-50 text-sky-700">
        Счёт-письмо — в разборе на «Странице на оплату»
      </Badge>
    );
  }
  if (doc.intake_status === "duplicate") {
    return (
      <Badge variant="secondary" title="Тот же счёт уже пришёл другим каналом (почта/вручную)">
        Дубль счёта{doc.invoice?.number ? ` №${doc.invoice.number}` : ""}
      </Badge>
    );
  }
  if (doc.match_status === "matched") {
    const note = doc.match_note === "manual" ? " (вручную)" : "";
    return (
      <Badge variant="outline" className="border-emerald-300 bg-emerald-50 text-emerald-700">
        В iiko{doc.matched_invoice?.number ? ` №${doc.matched_invoice.number}` : ""}
        {note}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="border-amber-300 bg-amber-50 text-amber-800">
      Нет в iiko
    </Badge>
  );
}

type Props = {
  canOperate: boolean;
};

export function SbisTab({ canOperate }: Props) {
  const [filter, setFilter] = useState<FilterValue>("all");
  const queryClient = useQueryClient();

  const documentsQuery = useQuery({
    queryKey: ["sbis", "documents"],
    queryFn: () => getSbisDocuments(),
  });
  const documents = useMemo(() => documentsQuery.data ?? [], [documentsQuery.data]);
  const visible = useMemo(
    () => documents.filter((doc) => matchesFilter(doc, filter)),
    [documents, filter],
  );
  const newCounterpartyInns = new Set(
    documents
      .filter((d) => d.intake_status === "new_counterparty")
      .map((d) => d.counterparty_inn ?? d.id),
  );
  const materializedCount = documents.filter((d) => d.intake_status === "materialized").length;
  const mirrorUnmatched = documents.filter(
    (d) => d.intake_status === "mirror" && d.match_status === "unmatched",
  );
  const mirrorUnmatchedSum = mirrorUnmatched.reduce((sum, d) => sum + Number(d.amount ?? 0), 0);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["sbis"] });

  const syncMutation = useMutation({
    mutationFn: syncSbisDocuments,
    onSuccess: async (r) => {
      await invalidate();
      // Материализация создаёт накладные — обновляем и их списки.
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      const extras: string[] = [];
      if (r.materialized) extras.push(`счетов создано ${r.materialized}`);
      if (r.settled_from_prepayments) {
        extras.push(`закрыто предоплатой ${r.settled_from_prepayments}`);
      }
      if (r.sent_to_recognition) extras.push(`писем-счетов в разбор ${r.sent_to_recognition}`);
      if (r.duplicates) extras.push(`дублей ${r.duplicates}`);
      if (r.new_counterparties) extras.push(`новых контрагентов ${r.new_counterparties}`);
      toast.success(
        `СБИС: получено ${r.fetched}, связано с iiko ${r.matched}` +
          (extras.length ? `, ${extras.join(", ")}` : ""),
      );
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Синхронизация со СБИС не удалась")),
  });
  const dismissMutation = useMutation({
    mutationFn: dismissSbisDocument,
    onSuccess: invalidate,
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось скрыть документ")),
  });
  const restoreMutation = useMutation({
    mutationFn: restoreSbisDocument,
    onSuccess: invalidate,
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось вернуть документ")),
  });
  const enableChannelMutation = useMutation({
    mutationFn: enableSbisChannel,
    onSuccess: async () => {
      await invalidate();
      toast.success("Канал СБИС подключён — счета материализуются следующим обновлением из СБИС");
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось подключить канал")),
  });

  // PDF готовится на стороне СБИС асинхронно и качается через наш прокси с Bearer —
  // вкладку открываем СИНХРОННО в обработчике клика (иначе Safari блокирует попап),
  // blob подставляем после загрузки.
  const [pdfLoadingId, setPdfLoadingId] = useState<string | null>(null);
  const openPdf = async (doc: SbisDocument) => {
    const win = window.open("", "_blank");
    setPdfLoadingId(doc.id);
    try {
      const url = await fetchSbisPdfUrl(doc.id);
      if (win) {
        win.location.href = url;
      } else {
        const link = document.createElement("a");
        link.href = url;
        link.download = `sbis-${doc.number ?? doc.id}.pdf`;
        link.click();
      }
    } catch (e) {
      win?.close();
      toast.error(apiErrorMessage(e, "Не удалось получить PDF из СБИС"));
    } finally {
      setPdfLoadingId(null);
    }
  };

  // «Быстрое переподключение» почтовика на ЭДО: контрагент настроен, канала sbis нет,
  // но почтовый канал есть — кнопка прямо в строке. Остальным канал включается из
  // карточки контрагента (секция источников, kind «СБИС (ЭДО)»).
  const showEnableChannel = (doc: SbisDocument) =>
    canOperate &&
    !doc.channel_enabled &&
    doc.has_email_channel &&
    doc.counterparty_id !== null &&
    doc.counterparty_status !== "requires_setup" &&
    doc.intake_status === "mirror";

  const columns: Array<DataTableColumn<SbisDocument>> = [
    {
      key: "date",
      header: "Дата",
      cell: (doc) => formatDate(doc.doc_date),
    },
    {
      key: "counterparty",
      header: "Контрагент",
      cell: (doc) => (
        <div>
          <div className="font-medium">{doc.counterparty_name ?? "—"}</div>
          <div className="text-xs text-muted-foreground">
            {doc.counterparty_inn ? `ИНН ${doc.counterparty_inn}` : null}
            {doc.channel_enabled ? " · канал СБИС" : null}
          </div>
        </div>
      ),
    },
    {
      key: "number",
      header: "Номер",
      cell: (doc) => doc.number ?? "—",
    },
    {
      key: "amount",
      header: "Сумма",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (doc) => (doc.amount != null ? formatRub(doc.amount) : "—"),
    },
    {
      key: "type",
      header: "Тип",
      cell: (doc) => DOC_TYPE_LABELS[doc.doc_type ?? ""] ?? doc.doc_type ?? "—",
    },
    {
      key: "state",
      header: "Состояние в СБИС",
      cell: (doc) => doc.state_name ?? "—",
    },
    {
      key: "status",
      header: "Статус",
      cell: (doc) => <StatusBadge doc={doc} />,
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (doc) => (
        <div className="flex items-center justify-end gap-1">
          {showEnableChannel(doc) ? (
            <Button
              variant="outline"
              size="sm"
              title="Поставщик с почты появился в ЭДО — включить канал СБИС"
              disabled={enableChannelMutation.isPending}
              onClick={(event) => {
                event.stopPropagation();
                enableChannelMutation.mutate(doc.id);
              }}
            >
              <Plug size={15} aria-hidden="true" />
              Подключить СБИС
            </Button>
          ) : null}
          {doc.has_pdf ? (
            <Button
              variant="ghost"
              size="sm"
              title="Открыть печатную форму (PDF)"
              disabled={pdfLoadingId === doc.id}
              onClick={(event) => {
                event.stopPropagation();
                void openPdf(doc);
              }}
            >
              {pdfLoadingId === doc.id ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : (
                <FileText size={15} aria-hidden="true" />
              )}
            </Button>
          ) : null}
          {doc.link_cabinet ? (
            <Button
              variant="ghost"
              size="sm"
              title="Открыть в кабинете СБИС"
              onClick={(event) => {
                event.stopPropagation();
                window.open(doc.link_cabinet ?? "", "_blank", "noopener");
              }}
            >
              <ExternalLink size={15} aria-hidden="true" />
            </Button>
          ) : null}
          {canOperate && doc.intake_status === "mirror" && doc.match_status === "unmatched" ? (
            <Button
              variant="ghost"
              size="sm"
              title="Скрыть из сверки (не ждём в iiko)"
              disabled={dismissMutation.isPending}
              onClick={(event) => {
                event.stopPropagation();
                dismissMutation.mutate(doc.id);
              }}
            >
              <EyeOff size={15} aria-hidden="true" />
            </Button>
          ) : null}
          {canOperate && doc.match_status === "dismissed" ? (
            <Button
              variant="ghost"
              size="sm"
              title="Вернуть в сверку"
              disabled={restoreMutation.isPending}
              onClick={(event) => {
                event.stopPropagation();
                restoreMutation.mutate(doc.id);
              }}
            >
              <Undo2 size={15} aria-hidden="true" />
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <MetricCard
          label="Новые контрагенты"
          value={String(newCounterpartyInns.size)}
          accent={newCounterpartyInns.size > 0 ? "danger" : undefined}
        />
        <MetricCard
          label="Зеркало: нет в iiko"
          value={`${mirrorUnmatched.length} · ${formatRub(mirrorUnmatchedSum)}`}
        />
        <MetricCard label="Счетов создано из ЭДО" value={String(materializedCount)} accent="info" />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Select value={filter} onValueChange={(value) => setFilter(value as FilterValue)}>
          <SelectTrigger className="w-60">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {FILTERS.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {canOperate ? (
          <Button
            variant="outline"
            size="sm"
            disabled={syncMutation.isPending}
            onClick={() => syncMutation.mutate()}
          >
            {syncMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <RefreshCw size={15} aria-hidden="true" />
            )}
            Обновить из СБИС
          </Button>
        ) : null}
      </div>

      {documentsQuery.isError ? (
        // Ошибка запроса (403/500/сеть) — НЕ маскируем под «документов нет»: пустой
        // реестр и недоступный реестр — разные состояния, владелец должен видеть разницу.
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
          Не удалось загрузить реестр ЭДО:{" "}
          {apiErrorMessage(documentsQuery.error, "ошибка запроса")}.{" "}
          <button className="underline" onClick={() => documentsQuery.refetch()} type="button">
            Повторить
          </button>
        </div>
      ) : (
        <DataTable
          columns={columns}
          rows={visible}
          isLoading={documentsQuery.isLoading}
          getRowKey={(doc) => doc.id}
          emptyMessage="Документов СБИС нет — нажмите «Обновить из СБИС» или дождитесь автосинка."
        />
      )}
    </div>
  );
}
