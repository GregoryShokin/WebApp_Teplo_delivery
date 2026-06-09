import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  History,
  LoaderCircle,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  UserCog,
  UserMinus,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
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
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  assignUserRole,
  createAccessUser,
  getAccessAudit,
  getAccessPermissions,
  getAccessRoles,
  getAccessUsers,
  patchAccessUser,
  revokeUserRole,
  updateRolePermissions,
  type AccessAuditEvent,
  type AccessPermissionModule,
  type AccessRole,
  type AccessUser,
  type AccessUserCreatePayload,
  type AccessUserPatchPayload,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

const ACCESS_QUERY_KEY = ["access-control"] as const;
const USERS_QUERY_KEY = [...ACCESS_QUERY_KEY, "users"] as const;
const ROLES_QUERY_KEY = [...ACCESS_QUERY_KEY, "roles"] as const;
const PERMISSIONS_QUERY_KEY = [...ACCESS_QUERY_KEY, "permissions"] as const;
const AUDIT_QUERY_KEY = [...ACCESS_QUERY_KEY, "audit"] as const;
const AUDIT_PAGE_SIZE = 50;

const ACTION_LABELS: Record<string, string> = {
  added: "добавлено",
  assigned: "назначено",
  removed: "удалено",
  revoked: "снято",
  updated: "обновлено",
  created: "создано",
  blocked: "заблокировано",
  activated: "активировано",
};

type CreateUserForm = AccessUserCreatePayload;

type EditUserState = {
  user: AccessUser;
  full_name: string;
  is_active: boolean;
};

type PatchUserVariables = {
  userId: string;
  payload: AccessUserPatchPayload;
  successMessage: string;
  closeEditor?: boolean;
};

type PendingUserPatch = {
  title: string;
  description: string;
  variables: PatchUserVariables;
};

type PendingRoleRevoke = {
  user: AccessUser;
  role: AccessRole;
};

export function AccessControlRoute() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canViewUsers = permissions.hasPermission("settings.users.read");
  const canCreateUsers = permissions.hasPermission("settings.users.create");
  const canEditUsers = permissions.hasPermission("settings.users.edit");
  const canAssignAccess = permissions.hasPermission("settings.access.assign");
  const canViewRoles = permissions.hasPermission("settings.roles.edit");
  const canEditRoles = permissions.hasPermission("settings.roles.edit");
  const canViewAudit = permissions.hasPermission("settings.access_audit.read");
  const defaultTab = canViewUsers ? "users" : canViewRoles ? "roles" : "audit";

  return (
    <div className="space-y-5">
      <PageHeader
        title="Доступы"
        description="Пользователи, должности, права и журнал изменений в одном рабочем разделе."
        action={
          <Button
            onClick={() => void queryClient.invalidateQueries({ queryKey: ACCESS_QUERY_KEY })}
            title="Обновить"
            type="button"
            variant="outline"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Обновить
          </Button>
        }
      />

      {!canViewUsers && !canViewRoles && !canViewAudit ? (
        <EmptyState
          icon={<ShieldCheck size={18} aria-hidden="true" />}
          title="Недостаточно прав"
          description="Раздел доступов скрыт для текущего профиля."
        />
      ) : (
      <Tabs defaultValue={defaultTab}>
        <TabsList className="grid w-full grid-cols-3 sm:w-auto">
          {canViewUsers ? <TabsTrigger value="users">Пользователи</TabsTrigger> : null}
          {canViewRoles ? <TabsTrigger value="roles">Права должностей</TabsTrigger> : null}
          {canViewAudit ? <TabsTrigger value="audit">Аудит</TabsTrigger> : null}
        </TabsList>
        {canViewUsers ? (
          <TabsContent className="mt-4" value="users">
            <UsersTab
              canAssignAccess={canAssignAccess}
              canCreateUsers={canCreateUsers}
              canEditUsers={canEditUsers}
            />
          </TabsContent>
        ) : null}
        {canViewRoles ? (
          <TabsContent className="mt-4" value="roles">
            <RolePermissionsTab canEditRoles={canEditRoles} />
          </TabsContent>
        ) : null}
        {canViewAudit ? (
          <TabsContent className="mt-4" value="audit">
            <AuditTab />
          </TabsContent>
        ) : null}
      </Tabs>
      )}
    </div>
  );
}

function UsersTab({
  canAssignAccess,
  canCreateUsers,
  canEditUsers,
}: {
  canAssignAccess: boolean;
  canCreateUsers: boolean;
  canEditUsers: boolean;
}) {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CreateUserForm>(() => emptyCreateForm());
  const [editState, setEditState] = useState<EditUserState | null>(null);
  const [rolesUserId, setRolesUserId] = useState<string | null>(null);
  const [pendingPatch, setPendingPatch] = useState<PendingUserPatch | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<PendingRoleRevoke | null>(null);

  const usersQuery = useQuery({
    queryKey: USERS_QUERY_KEY,
    queryFn: getAccessUsers,
  });
  const rolesQuery = useQuery({
    queryKey: ROLES_QUERY_KEY,
    queryFn: getAccessRoles,
  });

  const users = usersQuery.data ?? [];
  const roles = useMemo(() => sortRoles(rolesQuery.data ?? []), [rolesQuery.data]);
  const roleByCode = useMemo(
    () => new Map(roles.map((role) => [role.code, role] as const)),
    [roles],
  );
  const rolesDialogUser = users.find((user) => user.id === rolesUserId) ?? null;

  const createMutation = useMutation({
    mutationFn: createAccessUser,
    onSuccess: async () => {
      toast.success("Пользователь создан");
      setCreateForm(emptyCreateForm());
      setCreateOpen(false);
      await invalidateAccessLists(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось создать пользователя"));
    },
  });

  const patchMutation = useMutation({
    mutationFn: ({ userId, payload }: PatchUserVariables) => patchAccessUser(userId, payload),
    onSuccess: async (_, variables) => {
      toast.success(variables.successMessage);
      if (variables.closeEditor) {
        setEditState(null);
      }
      await invalidateAccessLists(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось обновить пользователя"));
    },
  });

  const assignRoleMutation = useMutation({
    mutationFn: ({ userId, roleCode }: { userId: string; roleCode: string }) =>
      assignUserRole(userId, roleCode),
    onSuccess: async (_, variables) => {
      const roleName = roleByCode.get(variables.roleCode)?.name ?? variables.roleCode;
      toast.success(`Должность «${roleName}» назначена`);
      await invalidateAccessLists(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось назначить должность"));
    },
  });

  const revokeRoleMutation = useMutation({
    mutationFn: ({ userId, roleCode }: { userId: string; roleCode: string }) =>
      revokeUserRole(userId, roleCode),
    onSuccess: async (_, variables) => {
      const roleName = roleByCode.get(variables.roleCode)?.name ?? variables.roleCode;
      toast.success(`Должность «${roleName}» снята`);
      await invalidateAccessLists(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось снять должность"));
    },
  });

  const columns = useMemo<Array<DataTableColumn<AccessUser>>>(
    () => {
      const baseColumns: Array<DataTableColumn<AccessUser>> = [
      {
        key: "email",
        header: "Email",
        cell: (user) => (
          <div>
            <div className="font-medium">{user.email}</div>
            <div className="text-xs text-muted-foreground">{user.id}</div>
          </div>
        ),
      },
      {
        key: "full_name",
        header: "ФИО",
        cell: (user) => user.full_name || "—",
      },
      {
        key: "status",
        header: "Статус",
        cell: (user) => (
          <Badge variant={user.is_active ? "default" : "secondary"}>
            {user.is_active ? "активен" : "заблокирован"}
          </Badge>
        ),
      },
      {
        key: "roles",
        header: "Должности",
        cell: (user) => (
          <div className="flex flex-wrap gap-1.5">
            {user.role_codes.length > 0 ? (
              user.role_codes.map((roleCode) => (
                <Badge key={roleCode} variant="outline">
                  {roleByCode.get(roleCode)?.name ?? roleCode}
                </Badge>
              ))
            ) : (
              <span className="text-sm text-muted-foreground">Нет должности</span>
            )}
          </div>
        ),
      },
      {
        key: "actions",
        header: "",
        headerClassName: "w-12",
        className: "w-12 text-right",
        cell: (user) => (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon" title="Действия" type="button" variant="ghost">
                <MoreHorizontal size={16} aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {canEditUsers ? (
                <DropdownMenuItem
                  onSelect={() =>
                    setEditState({
                      user,
                      full_name: user.full_name,
                      is_active: user.is_active,
                    })
                  }
                >
                  <Pencil size={15} aria-hidden="true" />
                  Редактировать
                </DropdownMenuItem>
              ) : null}
              {canAssignAccess ? (
                <DropdownMenuItem onSelect={() => setRolesUserId(user.id)}>
                  <UserCog size={15} aria-hidden="true" />
                  Должности
                </DropdownMenuItem>
              ) : null}
              {canEditUsers && user.is_active ? (
                <DropdownMenuItem
                  className="text-destructive focus:text-destructive"
                  onSelect={() =>
                    setPendingPatch({
                      title: "Заблокировать пользователя?",
                      description: `${user.email} потеряет доступ к приложению.`,
                      variables: {
                        userId: user.id,
                        payload: { is_active: false },
                        successMessage: "Пользователь заблокирован",
                      },
                    })
                  }
                >
                  <UserMinus size={15} aria-hidden="true" />
                  Заблокировать
                </DropdownMenuItem>
              ) : canEditUsers ? (
                <DropdownMenuItem
                  onSelect={() =>
                    patchMutation.mutate({
                      userId: user.id,
                      payload: { is_active: true },
                      successMessage: "Пользователь активирован",
                    })
                  }
                >
                  <CheckCircle2 size={15} aria-hidden="true" />
                  Активировать
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>
        ),
      },
    ];
      return canAssignAccess || canEditUsers
        ? baseColumns
        : baseColumns.filter((column) => column.key !== "actions");
    },
    [canAssignAccess, canEditUsers, patchMutation, roleByCode],
  );

  function handleCreateSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createMutation.mutate({
      ...createForm,
      email: createForm.email.trim(),
      full_name: createForm.full_name.trim(),
    });
  }

  function handleEditSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editState) {
      return;
    }

    const payload: AccessUserPatchPayload = {
      full_name: editState.full_name.trim(),
      is_active: editState.is_active,
    };
    const variables: PatchUserVariables = {
      userId: editState.user.id,
      payload,
      successMessage: "Пользователь обновлён",
      closeEditor: true,
    };

    if (editState.user.is_active && !editState.is_active) {
      setPendingPatch({
        title: "Заблокировать пользователя?",
        description: `${editState.user.email} потеряет доступ к приложению.`,
        variables,
      });
      return;
    }

    patchMutation.mutate(variables);
  }

  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Пользователи</h2>
          <p className="text-sm text-muted-foreground">
            Учётные записи, статус доступа и назначенные должности.
          </p>
        </div>
        {canCreateUsers ? (
          <Button onClick={() => setCreateOpen(true)} type="button">
            <Plus size={16} aria-hidden="true" />
            Добавить пользователя
          </Button>
        ) : null}
      </div>

      {usersQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(usersQuery.error, "Не удалось загрузить пользователей")}
        </div>
      ) : null}

      <DataTable
        columns={columns}
        emptyMessage="Нет пользователей"
        getRowKey={(user) => user.id}
        isLoading={usersQuery.isLoading}
        rows={users}
      />

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Добавить пользователя</DialogTitle>
            <DialogDescription>
              Создайте учётную запись. Должность можно назначить после создания.
            </DialogDescription>
          </DialogHeader>
          <form className="grid gap-4" onSubmit={handleCreateSubmit}>
            <div className="grid gap-2">
              <Label htmlFor="access-create-email">Email</Label>
              <Input
                autoComplete="email"
                id="access-create-email"
                onChange={(event) =>
                  setCreateForm((current) => ({ ...current, email: event.target.value }))
                }
                required
                type="email"
                value={createForm.email}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="access-create-name">ФИО</Label>
              <Input
                autoComplete="name"
                id="access-create-name"
                onChange={(event) =>
                  setCreateForm((current) => ({ ...current, full_name: event.target.value }))
                }
                required
                value={createForm.full_name}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="access-create-password">Пароль</Label>
              <Input
                autoComplete="new-password"
                id="access-create-password"
                minLength={8}
                onChange={(event) =>
                  setCreateForm((current) => ({ ...current, password: event.target.value }))
                }
                required
                type="password"
                value={createForm.password}
              />
            </div>
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <Label htmlFor="access-create-active">Активен</Label>
              <Switch
                checked={createForm.is_active}
                id="access-create-active"
                onCheckedChange={(checked) =>
                  setCreateForm((current) => ({ ...current, is_active: checked }))
                }
              />
            </div>
            <DialogFooter>
              <Button
                disabled={createMutation.isPending}
                onClick={() => setCreateOpen(false)}
                type="button"
                variant="outline"
              >
                Отмена
              </Button>
              <Button disabled={createMutation.isPending} type="submit">
                {createMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <Plus size={16} aria-hidden="true" />
                )}
                Создать
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(editState)} onOpenChange={(open) => !open && setEditState(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Редактировать пользователя</DialogTitle>
            <DialogDescription>{editState?.user.email}</DialogDescription>
          </DialogHeader>
          {editState ? (
            <form className="grid gap-4" onSubmit={handleEditSubmit}>
              <div className="grid gap-2">
                <Label htmlFor="access-edit-name">ФИО</Label>
                <Input
                  autoComplete="name"
                  id="access-edit-name"
                  onChange={(event) =>
                    setEditState((current) =>
                      current ? { ...current, full_name: event.target.value } : current,
                    )
                  }
                  required
                  value={editState.full_name}
                />
              </div>
              <div className="flex items-center justify-between rounded-md border px-3 py-2">
                <Label htmlFor="access-edit-active">Активен</Label>
                <Switch
                  checked={editState.is_active}
                  id="access-edit-active"
                  onCheckedChange={(checked) =>
                    setEditState((current) =>
                      current ? { ...current, is_active: checked } : current,
                    )
                  }
                />
              </div>
              <DialogFooter>
                <Button
                  disabled={patchMutation.isPending}
                  onClick={() => setEditState(null)}
                  type="button"
                  variant="outline"
                >
                  Отмена
                </Button>
                <Button disabled={patchMutation.isPending} type="submit">
                  {patchMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : (
                    <Save size={16} aria-hidden="true" />
                  )}
                  Сохранить
                </Button>
              </DialogFooter>
            </form>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(rolesUserId)} onOpenChange={(open) => !open && setRolesUserId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Должности пользователя</DialogTitle>
            <DialogDescription>{rolesDialogUser?.email ?? "Пользователь"}</DialogDescription>
          </DialogHeader>
          {rolesQuery.isLoading ? (
            <div className="text-sm text-muted-foreground">Загрузка должностей...</div>
          ) : null}
          {rolesQuery.isError ? (
            <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(rolesQuery.error, "Не удалось загрузить должности")}
            </div>
          ) : null}
          {rolesDialogUser ? (
            <div className="grid gap-2">
              {roles.map((role) => {
                const checked = rolesDialogUser.role_codes.includes(role.code);
                const disabled =
                  !canAssignAccess || assignRoleMutation.isPending || revokeRoleMutation.isPending;
                return (
                  <label
                    className={cn(
                      "flex items-start gap-3 rounded-md border px-3 py-2",
                      disabled && "opacity-70",
                    )}
                    key={role.code}
                  >
                    <Checkbox
                      checked={checked}
                      disabled={disabled}
                      onChange={(event) => {
                        if (event.currentTarget.checked) {
                          assignRoleMutation.mutate({
                            userId: rolesDialogUser.id,
                            roleCode: role.code,
                          });
                        } else {
                          setPendingRevoke({ user: rolesDialogUser, role });
                        }
                      }}
                    />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium">{role.name}</span>
                      <span className="block text-xs text-muted-foreground">{role.code}</span>
                    </span>
                  </label>
                );
              })}
            </div>
          ) : null}
          <DialogFooter>
            <Button onClick={() => setRolesUserId(null)} type="button" variant="outline">
              Закрыть
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={Boolean(pendingPatch)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingPatch(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{pendingPatch?.title}</AlertDialogTitle>
            <AlertDialogDescription>{pendingPatch?.description}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (pendingPatch) {
                  patchMutation.mutate(pendingPatch.variables);
                }
              }}
            >
              Подтвердить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(pendingRevoke)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingRevoke(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Снять должность?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingRevoke
                ? `У пользователя ${pendingRevoke.user.email} будет снята должность «${pendingRevoke.role.name}».`
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                if (pendingRevoke) {
                  revokeRoleMutation.mutate({
                    userId: pendingRevoke.user.id,
                    roleCode: pendingRevoke.role.code,
                  });
                }
              }}
            >
              Снять
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function RolePermissionsTab({ canEditRoles }: { canEditRoles: boolean }) {
  const queryClient = useQueryClient();
  const [selectedRoleId, setSelectedRoleId] = useState<string | undefined>();
  const [selectedCodes, setSelectedCodes] = useState<string[]>([]);

  const rolesQuery = useQuery({
    queryKey: ROLES_QUERY_KEY,
    queryFn: getAccessRoles,
  });
  const permissionsQuery = useQuery({
    queryKey: PERMISSIONS_QUERY_KEY,
    queryFn: getAccessPermissions,
  });

  const roles = useMemo(() => sortRoles(rolesQuery.data ?? []), [rolesQuery.data]);
  const permissionGroups = permissionsQuery.data ?? [];
  const selectedRole = roles.find((role) => role.id === selectedRoleId) ?? null;
  const serverCodes = useMemo(
    () => selectedRole?.permission_codes ?? [],
    [selectedRole?.permission_codes],
  );
  const serverCodesKey = useMemo(() => serverCodes.slice().sort().join("|"), [serverCodes]);
  const isReadOnly = !canEditRoles || !selectedRole || !selectedRole.is_editable;
  const developerMode = useMemo(
    () => import.meta.env.DEV || window.localStorage.getItem("teplo:developer-mode") === "1",
    [],
  );
  const isDirty = selectedRole ? !sameCodeSet(selectedCodes, serverCodes) : false;

  const updatePermissionsMutation = useMutation({
    mutationFn: ({ roleId, codes }: { roleId: string; codes: string[] }) =>
      updateRolePermissions(roleId, codes),
    onSuccess: async (role) => {
      toast.success("Права обновлены");
      setSelectedCodes(role.permission_codes);
      await queryClient.invalidateQueries({ queryKey: ROLES_QUERY_KEY });
      await queryClient.invalidateQueries({ queryKey: AUDIT_QUERY_KEY });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось обновить права"));
    },
  });

  useEffect(() => {
    if (roles.length === 0) {
      setSelectedRoleId(undefined);
      return;
    }
    if (!selectedRoleId || !roles.some((role) => role.id === selectedRoleId)) {
      setSelectedRoleId(roles[0].id);
    }
  }, [roles, selectedRoleId]);

  useEffect(() => {
    setSelectedCodes(serverCodes);
  }, [selectedRole?.id, serverCodesKey]);

  function togglePermission(code: string, checked: boolean) {
    setSelectedCodes((current) => {
      if (checked) {
        return current.includes(code) ? current : [...current, code];
      }
      return current.filter((currentCode) => currentCode !== code);
    });
  }

  function handleSave() {
    if (!selectedRole || isReadOnly) {
      return;
    }
    updatePermissionsMutation.mutate({
      roleId: selectedRole.id,
      codes: selectedCodes.slice().sort(),
    });
  }

  return (
    <section className="space-y-4">
      <div className="grid gap-4 rounded-lg border bg-card p-4 shadow-sm lg:grid-cols-[280px_minmax(0,1fr)]">
        <div className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold tracking-normal">Права должностей</h2>
            <p className="text-sm text-muted-foreground">
              Выберите должность и отметьте доступы по модулям.
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="access-role-select">Должность</Label>
            <Select
              disabled={rolesQuery.isLoading || roles.length === 0}
              onValueChange={setSelectedRoleId}
              value={selectedRoleId}
            >
              <SelectTrigger id="access-role-select">
                <SelectValue placeholder="Выберите должность" />
              </SelectTrigger>
              <SelectContent>
                {roles.map((role) => (
                  <SelectItem key={role.id} value={role.id}>
                    {role.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {selectedRole ? (
            <div className="rounded-md bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
              {selectedRole.is_editable
                ? canEditRoles
                  ? "Должность редактируется."
                  : "Режим просмотра. Изменять права должностей нельзя."
                : "Полный доступ, не редактируется."}
            </div>
          ) : null}
          <div className="flex flex-col-reverse gap-2 sm:flex-row lg:flex-col">
            <Button
              disabled={!isDirty || updatePermissionsMutation.isPending}
              onClick={() => setSelectedCodes(serverCodes)}
              type="button"
              variant="outline"
            >
              <RotateCcw size={16} aria-hidden="true" />
              Отмена
            </Button>
            <Button
              disabled={isReadOnly || !isDirty || updatePermissionsMutation.isPending}
              onClick={handleSave}
              type="button"
            >
              {updatePermissionsMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Save size={16} aria-hidden="true" />
              )}
              Сохранить
            </Button>
          </div>
        </div>

        <div className="min-w-0 space-y-3">
          {rolesQuery.isError || permissionsQuery.isError ? (
            <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(
                rolesQuery.error ?? permissionsQuery.error,
                "Не удалось загрузить права",
              )}
            </div>
          ) : null}

          {rolesQuery.isLoading || permissionsQuery.isLoading ? (
            <div className="rounded-md border bg-background px-4 py-8 text-sm text-muted-foreground">
              Загрузка прав...
            </div>
          ) : null}

          {!permissionsQuery.isLoading && permissionGroups.length === 0 ? (
            <EmptyState
              icon={<ShieldCheck size={18} aria-hidden="true" />}
              title="Нет прав"
              description="Backend пока не вернул список прав."
            />
          ) : null}

          {selectedRole && permissionGroups.length > 0 ? (
            <PermissionsAccordion
              disabled={isReadOnly || updatePermissionsMutation.isPending}
              developerMode={developerMode}
              groups={permissionGroups}
              selectedCodes={selectedCodes}
              onToggle={togglePermission}
            />
          ) : null}
        </div>
      </div>
    </section>
  );
}

function PermissionsAccordion({
  disabled,
  developerMode,
  groups,
  selectedCodes,
  onToggle,
}: {
  disabled: boolean;
  developerMode: boolean;
  groups: AccessPermissionModule[];
  selectedCodes: string[];
  onToggle: (code: string, checked: boolean) => void;
}) {
  const selected = useMemo(() => new Set(selectedCodes), [selectedCodes]);

  return (
    <Accordion type="multiple">
      {groups.map((group, index) => (
        <AccordionItem key={group.module} open={index < 3} value={group.module}>
          <AccordionTrigger>
            <span className="min-w-0">
              <span className="block truncate">{group.module}</span>
              <span className="block text-xs font-normal text-muted-foreground">
                {group.permissions.length} прав
              </span>
            </span>
          </AccordionTrigger>
          <AccordionContent>
            <div className="grid gap-4">
              {groupPermissionsByArea(group).map((area) => (
                <section className="grid gap-2" key={area.label}>
                  <div className="flex items-center justify-between gap-3">
                    <h3 className="text-sm font-semibold">{area.label}</h3>
                    <Badge variant="outline">
                      {area.permissions.filter((permission) => selected.has(permission.code)).length}
                      /{area.permissions.length}
                    </Badge>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {area.permissions.map((permission) => (
                      <label
                        className={cn(
                          "flex items-start gap-3 rounded-md border bg-card px-3 py-2",
                          disabled && "opacity-70",
                        )}
                        key={permission.code}
                      >
                        <Checkbox
                          checked={selected.has(permission.code)}
                          disabled={disabled}
                          onChange={(event) =>
                            onToggle(permission.code, event.currentTarget.checked)
                          }
                        />
                        <span className="min-w-0">
                          <span className="block text-sm font-medium">
                            {permission.description}
                          </span>
                          {developerMode ? (
                            <span className="block break-all text-xs text-muted-foreground">
                              {permission.code}
                            </span>
                          ) : null}
                        </span>
                      </label>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </AccordionContent>
        </AccordionItem>
      ))}
    </Accordion>
  );
}

function AuditTab() {
  const [limit, setLimit] = useState(AUDIT_PAGE_SIZE);
  const auditQuery = useQuery({
    queryKey: [...AUDIT_QUERY_KEY, limit],
    queryFn: () => getAccessAudit({ limit, offset: 0 }),
  });
  const events = auditQuery.data ?? [];

  const columns = useMemo<Array<DataTableColumn<AccessAuditEvent>>>(
    () => [
      {
        key: "created_at",
        header: "Время",
        cell: (event) => formatDateTimeRu(event.created_at),
      },
      {
        key: "actor",
        header: "Кто",
        cell: (event) => event.actor_email ?? "system",
      },
      {
        key: "object",
        header: "Объект",
        cell: (event) => <AuditObject event={event} />,
      },
      {
        key: "action",
        header: "Действие",
        cell: (event) => ACTION_LABELS[event.action] ?? event.action,
      },
    ],
    [],
  );

  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Аудит</h2>
          <p className="text-sm text-muted-foreground">
            Последние изменения прав, должностей и пользователей.
          </p>
        </div>
        <Badge variant="outline">{events.length} событий</Badge>
      </div>

      {auditQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(auditQuery.error, "Не удалось загрузить аудит")}
        </div>
      ) : null}

      <DataTable
        columns={columns}
        emptyMessage="Нет событий аудита"
        getRowKey={(event, index) =>
          `${event.created_at}-${event.actor_email ?? "system"}-${event.action}-${index}`
        }
        isLoading={auditQuery.isLoading}
        rows={events}
      />

      <div className="flex justify-center">
        <Button
          disabled={auditQuery.isFetching}
          onClick={() => setLimit((current) => current + AUDIT_PAGE_SIZE)}
          type="button"
          variant="outline"
        >
          {auditQuery.isFetching ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : (
            <History size={16} aria-hidden="true" />
          )}
          Загрузить ещё
        </Button>
      </div>
    </section>
  );
}

function AuditObject({ event }: { event: AccessAuditEvent }) {
  const objects = [
    event.role_code ? { label: event.role_code, variant: "default" as const } : null,
    event.permission_code ? { label: event.permission_code, variant: "outline" as const } : null,
  ].filter((item): item is { label: string; variant: "default" | "outline" } => Boolean(item));

  if (objects.length === 0) {
    return <span className="text-sm text-muted-foreground">{event.type || "—"}</span>;
  }

  return (
    <div className="flex flex-wrap gap-1.5">
      {objects.map((object) => (
        <Badge key={object.label} variant={object.variant}>
          {object.label}
        </Badge>
      ))}
    </div>
  );
}

function emptyCreateForm(): CreateUserForm {
  return {
    email: "",
    full_name: "",
    password: "",
    is_active: true,
  };
}

async function invalidateAccessLists(queryClient: ReturnType<typeof useQueryClient>) {
  await queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
  await queryClient.invalidateQueries({ queryKey: AUDIT_QUERY_KEY });
}

function sortRoles(roles: AccessRole[]) {
  const order = ["owner", "manager", "office_manager", "cashier", "senior_courier"];
  return roles
    .slice()
    .sort(
      (left, right) =>
        roleOrder(left.code, order) - roleOrder(right.code, order) ||
        left.name.localeCompare(right.name, "ru"),
    );
}

function roleOrder(code: string, order: string[]) {
  const index = order.indexOf(code);
  return index === -1 ? order.length : index;
}

function sameCodeSet(left: string[], right: string[]) {
  if (left.length !== right.length) {
    return false;
  }
  const rightSet = new Set(right);
  return left.every((code) => rightSet.has(code));
}

type PermissionAreaGroup = {
  label: string;
  permissions: AccessPermissionModule["permissions"];
};

function groupPermissionsByArea(module: AccessPermissionModule): PermissionAreaGroup[] {
  const areas = new Map<string, AccessPermissionModule["permissions"]>();
  module.permissions.forEach((permission) => {
    const label = permissionAreaLabel(permission.code, module.module);
    const permissions = areas.get(label) ?? [];
    permissions.push(permission);
    areas.set(label, permissions);
  });
  return Array.from(areas.entries()).map(([label, permissions]) => ({ label, permissions }));
}

function permissionAreaLabel(code: string, module: string) {
  if (code.startsWith("staff.administration.")) return "Администрация";
  if (code.startsWith("staff.cooks.")) return "Повара";
  if (code.startsWith("staff.cashiers.")) return "Кассиры";
  if (code.startsWith("staff.auxiliary.")) return "Вспомогательный персонал";
  if (code.startsWith("staff.couriers.")) return "Курьеры в Штате";

  if (code.startsWith("couriers.list.")) return "Список курьеров";
  if (code.startsWith("couriers.statistics.")) return "Статистика";
  if (code.startsWith("couriers.schedule.") || code.startsWith("couriers.shifts.")) {
    return "График и смены";
  }
  if (code.startsWith("couriers.evaluations.")) return "Оценки";
  if (code.startsWith("couriers.deposits.")) return "Депозиты";

  if (code.startsWith("payroll.runs.")) return "Расчёты зарплаты";
  if (code.startsWith("payroll.accruals.")) return "Начисления";
  if (code.startsWith("payroll.personal_reports.")) return "Персональные отчёты";
  if (
    code.startsWith("payroll.adjustments.") ||
    code.startsWith("payroll.bonuses.") ||
    code.startsWith("payroll.penalties.")
  ) {
    return "Премии, штрафы и корректировки";
  }
  if (code.startsWith("payroll.vacations.")) return "Отпуска";
  if (code.startsWith("payroll.fund.")) return "Накопительный фонд";
  if (code.startsWith("payroll.production_deposits.")) return "Депозиты производства";
  if (code === "revisions.read") return "Просмотр";
  if (code.startsWith("revisions.import") || code.startsWith("revisions.create")) {
    return "Создание и импорт";
  }
  if (code.startsWith("revisions.drafts.")) return "Черновик";
  if (code.startsWith("revisions.items.") || code.startsWith("revisions.employees.")) {
    return "Исключения";
  }
  if (
    code.startsWith("revisions.finalize") ||
    code.startsWith("revisions.cancel") ||
    code.startsWith("revisions.restore_draft")
  ) {
    return "Статус ревизии";
  }
  if (code.startsWith("revisions.deferrals.")) return "Распределение списаний";
  if (code.startsWith("revisions.rules.")) return "Правила распределения";

  if (code.startsWith("source.rates.")) return "Ставки";
  if (code.startsWith("source.revenue_percent.")) return "Проценты от выручки";
  if (code.startsWith("source.deductions.")) return "Удержания";
  if (code.startsWith("source.dismissal_reasons.")) return "Причины увольнения";
  if (code.startsWith("source.allowances.")) return "Надбавки";
  if (code.startsWith("source.fund_settings.")) return "Накопительный фонд";
  if (code.startsWith("source.deposit_settings.")) return "Депозиты";
  if (code.startsWith("source.revision_settings.")) return "Ревизии";
  if (code.startsWith("source.schedule.")) return "График смен";
  if (code.startsWith("source.shift_ledger.")) return "Учёт смен и выручки";
  if (code.startsWith("source.revenue.")) return "Выручка";
  if (code.startsWith("source.import") || code.startsWith("source.import_errors.")) {
    return "Загрузки и ошибки";
  }

  if (code.startsWith("finance.cashflow.")) return "Движение денег";
  if (code.startsWith("finance.classification_rules.")) return "Правила классификации";
  if (code.startsWith("finance.balance.")) return "Баланс";
  if (code.startsWith("finance.wallets.")) return "Кошельки и остатки";
  if (code.startsWith("finance.store_cash.")) return "Торговая касса";
  if (code.startsWith("finance.payment_calendar.")) return "Платёжный календарь";
  if (code.startsWith("finance.counterparties.")) return "Контрагенты";
  if (code.startsWith("finance.owner_review.")) return "Проверка собственником";

  if (code.startsWith("accounting.inventory")) return "Инвентаризации";
  if (code.startsWith("accounting.fixed_assets.")) return "Основные средства";
  if (code.startsWith("accounting.suppliers.")) return "Поставщики";
  if (code.startsWith("accounting.periods.")) return "Закрытие периода";

  if (code.startsWith("settings.general.")) return "Общие настройки";
  if (code.startsWith("settings.integrations.")) return "Обмен данными";
  if (code.startsWith("settings.users.")) return "Пользователи";
  if (code.startsWith("settings.access.")) return "Доступы пользователей";
  if (code.startsWith("settings.roles.")) return "Права должностей";
  if (code.startsWith("settings.access_audit.")) return "Журнал доступов";
  if (code.startsWith("settings.action_audit.")) return "Журнал действий";

  return module;
}

function formatDateTimeRu(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}
