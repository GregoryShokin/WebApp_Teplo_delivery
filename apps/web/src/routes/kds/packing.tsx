import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Flame, RefreshCw, RotateCcw, Wifi, WifiOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  apiErrorMessage,
  getKdsQueue,
  recordKdsEvents,
  rollbackKdsEvent,
  type KdsQueueOrder,
  type KdsStage,
  type KdsTapEvent,
  type KdsTapType,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const BUFFER_KEY = "kds.tapBuffer.v1";
const EVENT_IDS_KEY = "kds.lastEventIds.v1";

const NEXT_TAP: Record<Exclude<KdsStage, "handed">, KdsTapType> = {
  pending: "ready",
  ready: "waiting_courier",
  waiting_courier: "handed",
};

const TAP_LABEL: Record<KdsTapType, string> = {
  ready: "Готово",
  waiting_courier: "Ждёт курьера",
  handed: "Передан курьеру",
};

const STAGE_TAP: Record<Exclude<KdsStage, "pending">, KdsTapType> = {
  ready: "ready",
  waiting_courier: "waiting_courier",
  handed: "handed",
};

// ---- localStorage helpers ----
function loadBuffer(): KdsTapEvent[] {
  try {
    const raw = window.localStorage.getItem(BUFFER_KEY);
    return raw ? (JSON.parse(raw) as KdsTapEvent[]) : [];
  } catch {
    return [];
  }
}
function saveBuffer(events: KdsTapEvent[]) {
  window.localStorage.setItem(BUFFER_KEY, JSON.stringify(events));
}
function loadEventIds(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(EVENT_IDS_KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}
function saveEventIds(map: Record<string, string>) {
  window.localStorage.setItem(EVENT_IDS_KEY, JSON.stringify(map));
}

let audioContext: AudioContext | null = null;
function beep() {
  try {
    audioContext = audioContext ?? new (window.AudioContext || (window as any).webkitAudioContext)();
    const osc = audioContext.createOscillator();
    const gain = audioContext.createGain();
    osc.connect(gain);
    gain.connect(audioContext.destination);
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.2, audioContext.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.4);
    osc.start();
    osc.stop(audioContext.currentTime + 0.4);
  } catch {
    /* звук недоступен — не критично */
  }
}

type MergedOrder = KdsQueueOrder & { _overdue: boolean; _minutesInStage: number | null };

function tapTimes(order: KdsQueueOrder, buffered: KdsTapEvent[]) {
  const times: Record<KdsTapType, string | null> = {
    ready: order.ready_at,
    waiting_courier: order.waiting_courier_at,
    handed: order.handed_to_courier_at,
  };
  const sorted = [...buffered].sort((a, b) => a.effective_at.localeCompare(b.effective_at));
  for (const event of sorted) {
    if (event.event_type in times) {
      times[event.event_type] = event.action === "rollback" ? null : event.effective_at;
    }
  }
  return times;
}

function stageOf(times: Record<KdsTapType, string | null>): KdsStage {
  if (times.handed) return "handed";
  if (times.waiting_courier) return "waiting_courier";
  if (times.ready) return "ready";
  return "pending";
}

export function PackingKioskRoute() {
  const [buffer, setBuffer] = useState<KdsTapEvent[]>(loadBuffer);
  const [online, setOnline] = useState<boolean>(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  );
  const [nowMs, setNowMs] = useState(() => Date.now());
  const beepedRef = useRef<Set<string>>(new Set());
  const flushingRef = useRef(false);

  const queueQuery = useQuery({
    queryKey: ["kds-queue"],
    queryFn: getKdsQueue,
    refetchInterval: 12_000,
  });

  // 1-секундный тик для таймеров нуджа
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const persistBuffer = useCallback((next: KdsTapEvent[]) => {
    setBuffer(next);
    saveBuffer(next);
  }, []);

  const flush = useCallback(async () => {
    if (flushingRef.current) return;
    const pending = loadBuffer();
    if (pending.length === 0 || !navigator.onLine) return;
    flushingRef.current = true;
    try {
      const { results } = await recordKdsEvents(pending);
      const settled = new Set<string>();
      const eventIds = loadEventIds();
      for (const result of results) {
        if (result.status === "applied" || result.status === "duplicate") {
          settled.add(result.client_event_id);
        }
        if (result.status === "applied" && result.event_id) {
          const ev = pending.find((e) => e.client_event_id === result.client_event_id);
          if (ev) eventIds[`${ev.iiko_order_id}:${ev.event_type}`] = result.event_id;
        }
      }
      saveEventIds(eventIds);
      const remaining = loadBuffer().filter((e) => !settled.has(e.client_event_id));
      persistBuffer(remaining);
      await queueQuery.refetch();
    } catch {
      /* офлайн/сбой — оставляем буфер, дошлём позже */
    } finally {
      flushingRef.current = false;
    }
  }, [persistBuffer, queueQuery]);

  // online/offline + флаш при восстановлении сети и на каждом тике опроса
  useEffect(() => {
    const goOnline = () => {
      setOnline(true);
      void flush();
    };
    const goOffline = () => setOnline(false);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
    };
  }, [flush]);

  useEffect(() => {
    void flush();
  }, [flush, queueQuery.dataUpdatedAt]);

  const data = queueQuery.data;
  const nudge = data?.nudge ?? { ready_minutes: 10, waiting_courier_minutes: 15 };

  const merged: MergedOrder[] = useMemo(() => {
    if (!data) return [];
    return data.orders.map((order) => {
      const buffered = buffer.filter((e) => e.iiko_order_id === order.iiko_order_id);
      const times = tapTimes(order, buffered);
      const stage = stageOf(times);
      let minutesInStage: number | null = null;
      let overdue = false;
      const stamp =
        stage === "ready"
          ? times.ready
          : stage === "waiting_courier"
            ? times.waiting_courier
            : null;
      if (stamp) {
        minutesInStage = Math.floor((nowMs - new Date(stamp).getTime()) / 60000);
        const threshold =
          stage === "ready" ? nudge.ready_minutes : nudge.waiting_courier_minutes;
        overdue = minutesInStage >= threshold;
      }
      return {
        ...order,
        ready_at: times.ready,
        waiting_courier_at: times.waiting_courier,
        handed_to_courier_at: times.handed,
        stage,
        _overdue: overdue,
        _minutesInStage: minutesInStage,
      };
    });
  }, [data, buffer, nowMs, nudge.ready_minutes, nudge.waiting_courier_minutes]);

  // нудж-звук: один beep на каждый новоставший просроченным заказ
  useEffect(() => {
    const overdueNow = new Set(merged.filter((o) => o._overdue).map((o) => o.iiko_order_id));
    let isNew = false;
    overdueNow.forEach((id) => {
      if (!beepedRef.current.has(id)) isNew = true;
    });
    beepedRef.current = overdueNow;
    if (isNew) beep();
  }, [merged]);

  const tap = useCallback(
    (order: MergedOrder) => {
      if (order.stage === "handed") return;
      const tapType = NEXT_TAP[order.stage];
      const event: KdsTapEvent = {
        client_event_id:
          typeof crypto !== "undefined" && crypto.randomUUID
            ? crypto.randomUUID()
            : `${order.iiko_order_id}-${tapType}-${Date.now()}`,
        iiko_order_id: order.iiko_order_id,
        event_type: tapType,
        effective_at: new Date().toISOString(),
        action: "set",
      };
      persistBuffer([...loadBuffer(), event]);
      void flush();
    },
    [flush, persistBuffer],
  );

  const undo = useCallback(
    async (order: MergedOrder) => {
      if (order.stage === "pending") return;
      const tapType = STAGE_TAP[order.stage];
      // 1) неотправленный тап из буфера — убираем локально
      const pending = loadBuffer();
      const idx = [...pending]
        .map((e, i) => ({ e, i }))
        .filter(({ e }) => e.iiko_order_id === order.iiko_order_id && e.event_type === tapType)
        .pop();
      if (idx) {
        persistBuffer(pending.filter((_, i) => i !== idx.i));
        return;
      }
      // 2) уже подтверждённый тап — откат по event_id
      const eventIds = loadEventIds();
      const key = `${order.iiko_order_id}:${tapType}`;
      const eventId = eventIds[key];
      if (!eventId) return;
      try {
        await rollbackKdsEvent(eventId);
        delete eventIds[key];
        saveEventIds(eventIds);
        await queueQuery.refetch();
      } catch {
        /* событие уже ушло/недоступно — игнор */
      }
    },
    [persistBuffer, queueQuery],
  );

  return (
    <div className="min-h-screen bg-background p-4">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-2xl font-bold">Упаковка — очередь заказов</h1>
        <div className="flex items-center gap-3">
          {buffer.length > 0 ? (
            <span className="rounded-full bg-amber-100 px-3 py-1 text-sm font-medium text-amber-800">
              В очереди на отправку: {buffer.length}
            </span>
          ) : null}
          <span
            className={cn(
              "flex items-center gap-1 rounded-full px-3 py-1 text-sm font-medium",
              online ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800",
            )}
          >
            {online ? <Wifi size={16} /> : <WifiOff size={16} />}
            {online ? "Онлайн" : "Офлайн"}
          </span>
          <Button variant="outline" onClick={() => void queueQuery.refetch()} title="Обновить">
            <RefreshCw size={18} />
          </Button>
        </div>
      </div>

      {queueQuery.isError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-base text-destructive">
          {apiErrorMessage(queueQuery.error, "Не удалось загрузить очередь")}
        </div>
      ) : null}

      {data && merged.length === 0 ? (
        <div className="py-20 text-center text-lg text-muted-foreground">
          Активных заказов нет.
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {merged.map((order) => (
          <OrderCard key={order.iiko_order_id} order={order} onTap={tap} onUndo={undo} />
        ))}
      </div>
    </div>
  );
}

function OrderCard({
  order,
  onTap,
  onUndo,
}: {
  order: MergedOrder;
  onTap: (order: MergedOrder) => void;
  onUndo: (order: MergedOrder) => void;
}) {
  const stageStyle: Record<KdsStage, string> = {
    pending: "border-border bg-card",
    ready: "border-blue-300 bg-blue-50/60 dark:bg-blue-950/20",
    waiting_courier: "border-amber-300 bg-amber-50/60 dark:bg-amber-950/20",
    handed: "border-emerald-300 bg-emerald-50/50 opacity-70 dark:bg-emerald-950/20",
  };
  const overdue = order._overdue;

  return (
    <div
      className={cn(
        "flex flex-col gap-3 rounded-xl border-2 p-4 transition-colors",
        stageStyle[order.stage],
        overdue && "animate-pulse border-red-500 bg-red-50 dark:bg-red-950/30",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-3xl font-bold tabular-nums">№{order.order_number ?? "—"}</div>
          {order.complete_before ? (
            <div className="text-sm text-muted-foreground">
              к {new Date(order.complete_before).toLocaleTimeString("ru-RU", {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </div>
          ) : null}
        </div>
        <div className="flex flex-col items-end gap-1">
          {order.is_preorder ? (
            <span className="rounded bg-purple-100 px-2 py-0.5 text-xs font-medium text-purple-800">
              Предзаказ
            </span>
          ) : null}
          {order.has_hot_item ? (
            <span className="flex items-center gap-1 rounded bg-orange-100 px-2 py-0.5 text-xs font-medium text-orange-800">
              <Flame size={12} /> Горячее
            </span>
          ) : null}
        </div>
      </div>

      {order._minutesInStage !== null ? (
        <div
          className={cn(
            "text-sm font-medium",
            overdue ? "text-red-600" : "text-muted-foreground",
          )}
        >
          {order.stage === "ready" ? "Готов" : "Ждёт курьера"} · {order._minutesInStage} мин
          {overdue ? " — превышен порог!" : ""}
        </div>
      ) : null}

      {order.stage !== "handed" ? (
        <Button
          className="h-16 w-full text-xl font-semibold"
          onClick={() => onTap(order)}
        >
          {TAP_LABEL[NEXT_TAP[order.stage]]}
        </Button>
      ) : (
        <div className="py-2 text-center text-base font-medium text-emerald-700">
          Передан курьеру ✓
        </div>
      )}

      {order.stage !== "pending" ? (
        <Button
          variant="ghost"
          className="h-10 w-full text-muted-foreground"
          onClick={() => onUndo(order)}
        >
          <RotateCcw size={16} />
          Отменить «{TAP_LABEL[STAGE_TAP[order.stage]]}»
        </Button>
      ) : null}
    </div>
  );
}
