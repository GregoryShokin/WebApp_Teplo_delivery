import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeftRight,
  Banknote,
  Boxes,
  CalendarClock,
  CalendarDays,
  CalendarRange,
  ChartNoAxesColumn,
  ChevronDown,
  ClipboardCheck,
  ClipboardList,
  Flame,
  Home,
  LogOut,
  Menu,
  ReceiptText,
  Settings,
  UsersRound,
  Warehouse,
  type LucideIcon,
} from "lucide-react";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { RoleBadge } from "@/components/ui-app/RoleBadge";
import { clearSession, getAuthSnapshot, subscribeAuth, type AuthUser } from "@/lib/auth";
import { logout } from "@/lib/api";
import { usePermissions, type AppSection } from "@/lib/permissions";
import { cn } from "@/lib/utils";

type Navigate = (path: string) => void;

type AppLayoutProps = {
  children: ReactNode;
  currentPath: string;
  onNavigate: Navigate;
};

type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  section?: AppSection;
};

type NavGroup = {
  title?: string;
  items: NavItem[];
};

const navGroups: NavGroup[] = [
  {
    items: [{ label: "Главная", href: "/", icon: Home }],
  },
  {
    title: "Кадры",
    items: [{ label: "Штат", href: "/staff", icon: UsersRound, section: "staff" }],
  },
  {
    title: "Зарплата",
    items: [
      { label: "Расчёты", href: "/payroll", icon: Banknote, section: "payroll" },
      { label: "График сотрудников", href: "/schedule", icon: CalendarDays, section: "schedule" },
      { label: "Исходные данные", href: "/source-data", icon: ClipboardList, section: "source-data" },
    ],
  },
  {
    title: "Курьеры",
    items: [
      { label: "Смена", href: "/couriers/shift", icon: ClipboardCheck, section: "couriers.shift" },
      {
        label: "Статистика",
        href: "/couriers/statistics",
        icon: ChartNoAxesColumn,
        section: "couriers.statistics",
      },
      { label: "График", href: "/couriers/schedule", icon: CalendarRange, section: "couriers.schedule" },
    ],
  },
  {
    title: "Финансы",
    items: [
      { label: "ДДС", href: "/dds", icon: Banknote, section: "finance.dds" },
      {
        label: "Платёжный календарь",
        href: "/payment-calendar",
        icon: CalendarClock,
        section: "finance.payment-calendar",
      },
      { label: "Баланс", href: "/balance", icon: ReceiptText, section: "finance.balance" },
    ],
  },
  {
    title: "Управление складом",
    items: [
      {
        label: "Накладные",
        href: "/warehouse/invoices",
        icon: Warehouse,
        section: "counterparties",
      },
    ],
  },
  {
    title: "Учёт",
    items: [
      { label: "Учёт ОС", href: "/fixed-assets", icon: Boxes, section: "accounting.fixed-assets" },
      { label: "Учёт ДЗ/КЗ", href: "/dz-kz", icon: ArrowLeftRight, section: "accounting.dz-kz" },
    ],
  },
  {
    items: [{ label: "Настройки", href: "/settings", icon: Settings, section: "settings" }],
  },
];

export function AppLayout({ children, currentPath, onNavigate }: AppLayoutProps) {
  const [isMobileOpen, setIsMobileOpen] = useState(false);
  const [auth, setAuth] = useState(getAuthSnapshot);

  useEffect(() => {
    const unsubscribe = subscribeAuth(setAuth);
    return () => {
      unsubscribe();
    };
  }, []);

  const user = auth.user;

  return (
    <div className="min-h-screen bg-background text-foreground lg:grid lg:grid-cols-[240px_minmax(0,1fr)]">
      <aside className="hidden min-h-screen border-r bg-card lg:block">
        <SidebarContent currentPath={currentPath} onNavigate={onNavigate} />
      </aside>

      <Sheet open={isMobileOpen} onOpenChange={setIsMobileOpen}>
        <SheetContent className="w-[280px] p-0" side="left">
          <SheetTitle className="sr-only">Навигация</SheetTitle>
          <SidebarContent
            currentPath={currentPath}
            onNavigate={(path) => {
              setIsMobileOpen(false);
              onNavigate(path);
            }}
          />
        </SheetContent>
      </Sheet>

      <div className="flex min-h-screen min-w-0 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b bg-background/95 px-4 backdrop-blur sm:px-6">
          <div className="flex min-w-0 items-center gap-2">
            <Button
              className="lg:hidden"
              onClick={() => setIsMobileOpen(true)}
              size="icon"
              title="Открыть навигацию"
              variant="ghost"
            >
              <Menu aria-hidden="true" />
            </Button>
            <div className="hidden min-w-0 text-sm font-medium text-muted-foreground sm:block">
              Управление бизнесом
            </div>
          </div>

          <UserMenu user={user} onNavigate={onNavigate} />
        </header>

        <main className="flex-1 px-4 py-5 sm:px-6 lg:px-8">
          <div className="mx-auto w-full max-w-[1320px]">{children}</div>
        </main>
      </div>
    </div>
  );
}

function SidebarContent({
  currentPath,
  onNavigate,
}: {
  currentPath: string;
  onNavigate: Navigate;
}) {
  const permissions = usePermissions();
  const initialOpen = useMemo(
    () =>
      Object.fromEntries(
        navGroups.filter((group) => group.title).map((group) => [group.title as string, true]),
      ),
    [],
  );
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>(initialOpen);
  const visibleGroups = useMemo(
    () =>
      navGroups
        .map((group) => ({
          ...group,
          items: group.items.filter(
            (item) => !item.section || permissions.canOpenSection(item.section),
          ),
        }))
        .filter((group) => group.items.length > 0),
    [permissions],
  );

  return (
    <div className="flex h-full min-h-screen flex-col">
      <button
        className="flex h-14 items-center gap-3 border-b px-4 text-left"
        onClick={() => onNavigate("/")}
        type="button"
      >
        <span className="flex h-9 w-9 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Flame className="h-4 w-4" aria-hidden="true" />
        </span>
        <span className="min-w-0">
          <span className="block text-base font-semibold leading-5">Тепло</span>
          <span className="block text-xs leading-5 text-muted-foreground">финансовый контур</span>
        </span>
      </button>

      <ScrollArea className="flex-1">
        <nav className="grid gap-3 px-3 py-4">
          {visibleGroups.map((group, index) => {
            if (!group.title) {
              return (
                <div className="grid gap-1" key={group.items[0].href}>
                  {group.items.map((item) => (
                    <NavLink
                      currentPath={currentPath}
                      item={item}
                      key={item.href}
                      onNavigate={onNavigate}
                    />
                  ))}
                  {index === 0 ? <Separator className="mt-2" /> : null}
                </div>
              );
            }

            const isOpen = openGroups[group.title] ?? true;

            return (
              <section className="grid gap-1" key={group.title}>
                <button
                  className="flex h-8 items-center justify-between rounded-md px-2 text-xs font-semibold uppercase text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  onClick={() =>
                    setOpenGroups((current) => ({
                      ...current,
                      [group.title as string]: !isOpen,
                    }))
                  }
                  type="button"
                >
                  {group.title}
                  <ChevronDown
                    className={cn("h-4 w-4 transition-transform", !isOpen && "-rotate-90")}
                    aria-hidden="true"
                  />
                </button>
                {isOpen ? (
                  <div className="grid gap-1">
                    {group.items.map((item) => (
                      <NavLink
                        currentPath={currentPath}
                        item={item}
                        key={`${item.href}-${item.label}`}
                        onNavigate={onNavigate}
                      />
                    ))}
                  </div>
                ) : null}
              </section>
            );
          })}
        </nav>
      </ScrollArea>
    </div>
  );
}

function NavLink({
  currentPath,
  item,
  onNavigate,
}: {
  currentPath: string;
  item: NavItem;
  onNavigate: Navigate;
}) {
  const isActive = isActiveRoute(currentPath, item.href);
  const Icon = item.icon;

  return (
    <a
      className={cn(
        "flex h-9 items-center gap-2 rounded-md px-2 text-sm font-medium transition-colors",
        isActive
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
      href={item.href}
      onClick={(event) => {
        event.preventDefault();
        onNavigate(item.href);
      }}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="truncate">{item.label}</span>
    </a>
  );
}

function UserMenu({ user, onNavigate }: { user: AuthUser | null; onNavigate: Navigate }) {
  const displayName = user?.full_name || user?.email || "Пользователь";
  const role = user?.roles[0] ?? "guest";
  const initials = getInitials(displayName);

  async function handleLogout() {
    try {
      await logout();
    } catch {
      clearSession();
    }
    onNavigate("/login");
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button className="h-10 gap-3 px-2" variant="ghost">
          <Avatar className="h-8 w-8">
            <AvatarFallback className="bg-muted text-xs font-semibold text-foreground">
              {initials}
            </AvatarFallback>
          </Avatar>
          <span className="hidden min-w-0 text-left sm:block">
            <span className="block max-w-[180px] truncate text-sm font-medium leading-5">
              {displayName}
            </span>
            <span className="block text-xs leading-4 text-muted-foreground">Текущий профиль</span>
          </span>
          <RoleBadge role={role} />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel>
          <span className="block truncate">{displayName}</span>
          {user?.email ? (
            <span className="block truncate text-xs font-normal text-muted-foreground">
              {user.email}
            </span>
          ) : null}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="text-destructive focus:text-destructive"
          onClick={handleLogout}
        >
          <LogOut aria-hidden="true" />
          Выйти
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function isActiveRoute(currentPath: string, href: string) {
  if (href === "/") {
    return currentPath === "/";
  }
  if (href === "/payroll") {
    return (
      currentPath === href ||
      currentPath === "/payroll/fund" ||
      currentPath === "/payroll/adjustments" ||
      currentPath.startsWith("/payroll/admin") ||
      currentPath.startsWith("/payroll/runs/")
    );
  }
  if (href === "/schedule") {
    return currentPath === href || currentPath.startsWith("/schedule/");
  }
  if (href === "/dds") {
    return currentPath === href || currentPath.startsWith("/dds/");
  }
  return currentPath === href;
}

function getInitials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return (parts[0]?.[0] ?? "Т") + (parts[1]?.[0] ?? "");
}
