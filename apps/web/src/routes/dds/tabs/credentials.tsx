import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
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
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import {
  apiErrorMessage,
  createDdsCredential,
  deleteDdsCredential,
  getDdsCredentials,
  type CredentialCreate,
  type CredentialRead,
  type DdsProvider,
} from "@/lib/api";
import { ProviderBadge, formatDateTime, providerLabels, toDateTimeLocalInput } from "@/routes/dds/shared";

type CredentialKind = CredentialRead["credential_kind"];

const credentialKinds: CredentialKind[] = [
  "access_token",
  "client_secret",
  "bearer_token",
  "mtls_cert_path",
  "mtls_key_path",
];

const kindLabels: Record<CredentialKind, string> = {
  access_token: "Access token",
  client_secret: "Client secret",
  bearer_token: "Bearer token",
  mtls_cert_path: "mTLS cert path",
  mtls_key_path: "mTLS key path",
};

export function CredentialsTab({ canManage }: { canManage: boolean }) {
  const queryClient = useQueryClient();
  const credentialsQuery = useQuery({
    queryKey: ["dds", "credentials"],
    queryFn: getDdsCredentials,
  });
  const [dialogState, setDialogState] = useState<CredentialDialogState | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CredentialRead | null>(null);

  const activeCredentials = credentialsQuery.data ?? [];
  const missingPairs = useMemo(() => getMissingPairs(activeCredentials), [activeCredentials]);

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteDdsCredential(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "credentials"] });
      setDeleteTarget(null);
      toast.success("Credential отключён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отключить credential")),
  });

  const columns: Array<DataTableColumn<CredentialRead>> = [
    {
      key: "provider",
      header: "Провайдер",
      cell: (credential) => <ProviderBadge provider={credential.provider} />,
    },
    {
      key: "kind",
      header: "Тип",
      cell: (credential) => kindLabels[credential.credential_kind],
      className: "font-medium",
    },
    {
      key: "expires",
      header: "Истекает",
      cell: (credential) => <ExpiresBadge credential={credential} />,
    },
    {
      key: "created",
      header: "Создан",
      cell: (credential) => formatDateTime(credential.created_at),
      className: "whitespace-nowrap",
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (credential) =>
        canManage ? (
        <div className="flex justify-end gap-2">
          <Button
            onClick={(event) => {
              event.stopPropagation();
              setDialogState({
                mode: "update",
                provider: credential.provider,
                credential_kind: credential.credential_kind,
              });
            }}
            size="sm"
            variant="outline"
          >
            Обновить
          </Button>
          <Button
            onClick={(event) => {
              event.stopPropagation();
              setDeleteTarget(credential);
            }}
            size="icon"
            title="Отключить"
            variant="ghost"
          >
            <Trash2 size={15} aria-hidden="true" />
          </Button>
        </div>
        ) : null,
    },
  ];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Доступы банков</h2>
          <p className="text-sm text-muted-foreground">Активные токены без показа секретного значения.</p>
        </div>
        {canManage ? (
          <Button
            onClick={() => {
              const pair = missingPairs[0] ?? { provider: "sber", credential_kind: "access_token" };
              setDialogState({ mode: "create", ...pair });
            }}
          >
            <Plus size={16} aria-hidden="true" />
            Добавить credential
          </Button>
        ) : null}
      </div>

      <DataTable
        columns={columns}
        rows={activeCredentials}
        isLoading={credentialsQuery.isLoading}
        getRowKey={(credential) => credential.id}
        emptyMessage="Активные credentials не найдены"
      />

      {canManage && missingPairs.length > 0 ? (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <KeyRound size={16} aria-hidden="true" />
              Не настроены
            </div>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {missingPairs.map((pair) => (
              <Button
                key={`${pair.provider}:${pair.credential_kind}`}
                onClick={() => setDialogState({ mode: "create", ...pair })}
                size="sm"
                variant="outline"
              >
                {providerLabels[pair.provider]} · {kindLabels[pair.credential_kind]}
              </Button>
            ))}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="pb-3">
          <h3 className="text-sm font-semibold">Как получить токен</h3>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm leading-6 text-muted-foreground md:grid-cols-2">
          <div>
            <div className="font-medium text-foreground">Сбер</div>
            <p>В личном кабинете бизнеса обновите access token или client secret и внесите новое значение здесь.</p>
          </div>
          <div>
            <div className="font-medium text-foreground">T-Bank</div>
            <p>В Business API откройте раздел ключей, выпустите bearer token и сохраните его как активный credential.</p>
          </div>
        </CardContent>
      </Card>

      <CredentialDialog
        state={canManage ? dialogState : null}
        onOpenChange={(open) => !open && setDialogState(null)}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={() => setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Отключить credential?</AlertDialogTitle>
            <AlertDialogDescription>
              Запись останется в истории, но перестанет использоваться как активная.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleteMutation.isPending}
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
            >
              Отключить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

type CredentialDialogState = {
  credential_kind: CredentialKind;
  mode: "create" | "update";
  provider: DdsProvider;
};

function CredentialDialog({
  onOpenChange,
  state,
}: {
  onOpenChange: (open: boolean) => void;
  state: CredentialDialogState | null;
}) {
  const queryClient = useQueryClient();
  const [provider, setProvider] = useState<DdsProvider>("sber");
  const [kind, setKind] = useState<CredentialKind>("access_token");
  const [value, setValue] = useState("");
  const [expiresAt, setExpiresAt] = useState("");

  useEffect(() => {
    if (!state) {
      return;
    }
    setProvider(state.provider);
    setKind(state.credential_kind);
    setValue("");
    setExpiresAt(defaultExpiresAt(state.provider, state.credential_kind));
  }, [state]);

  const saveMutation = useMutation({
    mutationFn: () => createDdsCredential(toPayload(provider, kind, value, expiresAt)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "credentials"] });
      onOpenChange(false);
      toast.success("Токен обновлён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить credential")),
  });

  return (
    <Dialog open={Boolean(state)} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{state?.mode === "update" ? "Обновить credential" : "Добавить credential"}</DialogTitle>
          <DialogDescription>Секретное значение будет сохранено без отображения в списке.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label>Провайдер</Label>
              <Select value={provider} onValueChange={(next) => setProvider(next as DdsProvider)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="sber">Сбер</SelectItem>
                  <SelectItem value="tbank">T-Bank</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>Тип</Label>
              <Select
                value={kind}
                onValueChange={(next) => {
                  const nextKind = next as CredentialKind;
                  setKind(nextKind);
                  setExpiresAt(defaultExpiresAt(provider, nextKind));
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {credentialKinds.map((item) => (
                    <SelectItem key={item} value={item}>
                      {kindLabels[item]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="grid gap-2">
            <Label>Value</Label>
            <Textarea
              className="min-h-[140px]"
              value={value}
              onChange={(event) => setValue(event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label>Истекает</Label>
            <Input
              type="datetime-local"
              value={expiresAt}
              onChange={(event) => setExpiresAt(event.target.value)}
            />
          </div>
        </div>
        <DialogFooter>
          <Button
            disabled={!value.trim() || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            {saveMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ExpiresBadge({ credential }: { credential: CredentialRead }) {
  if (!credential.expires_at) {
    return <Badge variant="outline">Без срока</Badge>;
  }
  const expiresAt = new Date(credential.expires_at).getTime();
  const now = Date.now();
  const day = 24 * 60 * 60 * 1000;
  const className =
    expiresAt < now
      ? "border-rose-200 bg-rose-50 text-rose-700"
      : expiresAt - now < day
        ? "border-amber-200 bg-amber-50 text-amber-700"
        : "border-emerald-200 bg-emerald-50 text-emerald-700";
  return <Badge className={className}>{formatDateTime(credential.expires_at)}</Badge>;
}

function getMissingPairs(credentials: CredentialRead[]) {
  const activePairs = new Set(
    credentials.map((credential) => `${credential.provider}:${credential.credential_kind}`),
  );
  return (["sber", "tbank"] as DdsProvider[]).flatMap((provider) =>
    credentialKinds
      .filter((credential_kind) => !activePairs.has(`${provider}:${credential_kind}`))
      .map((credential_kind) => ({ provider, credential_kind })),
  );
}

function defaultExpiresAt(provider: DdsProvider, kind: CredentialKind) {
  if (provider === "sber" && kind === "access_token") {
    const date = new Date();
    date.setMinutes(date.getMinutes() + 60);
    return toDateTimeLocalInput(date);
  }
  if (provider === "sber" && kind === "client_secret") {
    const date = new Date();
    date.setDate(date.getDate() + 40);
    return toDateTimeLocalInput(date);
  }
  return "";
}

function toPayload(
  provider: DdsProvider,
  credential_kind: CredentialKind,
  value: string,
  expiresAt: string,
): CredentialCreate {
  return {
    credential_kind,
    expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
    provider,
    value,
  };
}
