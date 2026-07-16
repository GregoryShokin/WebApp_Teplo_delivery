import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EyeOff, ExternalLink, FileText, LoaderCircle, RefreshCw, Undo2 } from "lucide-react";
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
  fetchSbisPdfUrl,
  getSbisDocuments,
  restoreSbisDocument,
  syncSbisDocuments,
  type SbisDocument,
} from "../api";
import { MetricCard, formatDate, formatRub } from "../shared";

// Человеческие подписи типов документов СБИС (Документ.Тип / Вложение.Тип).
const DOC_TYPE_LABELS: Record<string, string> = {
  ДокОтгрВх: "УПД / отгрузка",
  АктСверВх: "Акт сверки",
};

type FilterValue = "all" | "unmatched" | "matched" | "dismissed";

const FILTERS: Array<{ value: FilterValue; label: string }> = [
  { value: "all", label: "Все документы" },
  { value: "unmatched", label: "Нет в iiko" },
  { value: "matched", label: "Связаны с накладной" },
  { value: "dismissed", label: "Скрытые" },
];

function MatchBadge({ doc }: { doc: SbisDocument }) {
  if (doc.match_status === "matched") {
    const invoice = doc.matched_invoice;
    const note = doc.match_note === "manual" ? " (вручную)" : "";
    return (
      <Badge variant="outline" className="border-emerald-300 bg-emerald-50 text-emerald-700">
        В iiko{invoice?.number ? ` №${invoice.number}` : ""}
        {note}
      </Badge>
    );
  }
  if (doc.match_status === "dismissed") {
    return <Badge variant="secondary">Скрыт из сверки</Badge>;
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
    () => (filter === "all" ? documents.filter((d) => d.match_status !== "dismissed") : documents.filter((d) => d.match_status === filter)),
    [documents, filter],
  );
  const unmatchedCount = documents.filter((d) => d.match_status === "unmatched").length;
  const matchedCount = documents.filter((d) => d.match_status === "matched").length;
  const unmatchedSum = documents
    .filter((d) => d.match_status === "unmatched")
    .reduce((sum, d) => sum + Number(d.amount ?? 0), 0);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["sbis"] });

  const syncMutation = useMutation({
    mutationFn: syncSbisDocuments,
    onSuccess: async (r) => {
      await invalidate();
      toast.success(
        `СБИС: получено ${r.fetched}, новых ${r.created}, связано ${r.matched}`,
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
          {doc.counterparty_inn ? (
            <div className="text-xs text-muted-foreground">ИНН {doc.counterparty_inn}</div>
          ) : null}
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
      key: "match",
      header: "Сверка",
      cell: (doc) => <MatchBadge doc={doc} />,
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (doc) => (
        <div className="flex items-center justify-end gap-1">
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
          {canOperate && doc.match_status === "unmatched" ? (
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
          label="Нет в iiko"
          value={String(unmatchedCount)}
          accent={unmatchedCount > 0 ? "danger" : undefined}
        />
        <MetricCard label="Сумма несведённых" value={formatRub(unmatchedSum)} />
        <MetricCard label="Связаны с накладными" value={String(matchedCount)} />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Select value={filter} onValueChange={(value) => setFilter(value as FilterValue)}>
          <SelectTrigger className="w-56">
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

      <DataTable
        columns={columns}
        rows={visible}
        isLoading={documentsQuery.isLoading}
        getRowKey={(doc) => doc.id}
        emptyMessage="Документов СБИС нет — нажмите «Обновить из СБИС» или дождитесь автосинка."
      />
    </div>
  );
}
