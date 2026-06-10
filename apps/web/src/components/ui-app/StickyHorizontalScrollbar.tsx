import { useEffect, useRef } from "react";

/**
 * A single floating horizontal scrollbar pinned to the bottom of the viewport.
 *
 * Wide tables on long pages put their native horizontal scrollbar at the very
 * bottom of the (tall) table, so a mouse user has to scroll the page all the
 * way down before they can drag it right. This component watches every
 * horizontally-overflowing table scroller on the page, and whenever one is in
 * view but its own scrollbar has dropped below the fold, it mirrors that
 * scroller with a thin bar stuck to the bottom edge of the window — reachable
 * with a plain mouse at any scroll position. When the table's native bar is
 * already on screen the floating one hides, so there's never a double bar.
 *
 * It uses a real (browser-native) scrollbar on a proxy element, so it inherits
 * the same styling as every other scrollbar in the app (see index.css).
 */
const SCROLLER_SELECTOR = ".overflow-x-auto, .overflow-auto, .overflow-x-scroll";

function isHorizontallyScrollable(el: HTMLElement) {
  return el.scrollWidth - el.clientWidth > 1;
}

export function StickyHorizontalScrollbar() {
  const proxyRef = useRef<HTMLDivElement>(null);
  const spacerRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLElement | null>(null);
  const candidatesRef = useRef<HTMLElement[]>([]);
  const syncingRef = useRef(false);

  useEffect(() => {
    const proxy = proxyRef.current;
    const spacer = spacerRef.current;
    if (!proxy || !spacer) return;

    let frame = 0;
    let needsRefresh = true;

    const refreshCandidates = () => {
      candidatesRef.current = Array.from(
        document.querySelectorAll<HTMLElement>(SCROLLER_SELECTOR),
      ).filter((el) => el.querySelector("table") !== null);
    };

    const pickActive = (): HTMLElement | null => {
      const vh = window.innerHeight;
      let best: HTMLElement | null = null;
      let bestVisible = 0;
      for (const el of candidatesRef.current) {
        if (!el.isConnected || !isHorizontallyScrollable(el)) continue;
        const rect = el.getBoundingClientRect();
        // Table must be in view, and its own native bar must be below the fold.
        if (rect.top >= vh || rect.bottom <= 0) continue;
        if (rect.bottom <= vh) continue;
        const visible = Math.min(rect.bottom, vh) - Math.max(rect.top, 0);
        if (visible > bestVisible) {
          bestVisible = visible;
          best = el;
        }
      }
      return best;
    };

    const tick = () => {
      frame = 0;
      if (needsRefresh) {
        refreshCandidates();
        needsRefresh = false;
      }
      const active = pickActive();
      activeRef.current = active;
      if (!active) {
        proxy.style.display = "none";
        return;
      }
      const rect = active.getBoundingClientRect();
      proxy.style.display = "block";
      proxy.style.left = `${Math.round(rect.left)}px`;
      proxy.style.width = `${Math.round(rect.width)}px`;
      spacer.style.width = `${active.scrollWidth}px`;
      if (Math.abs(proxy.scrollLeft - active.scrollLeft) > 1) {
        syncingRef.current = true;
        proxy.scrollLeft = active.scrollLeft;
        syncingRef.current = false;
      }
    };

    const schedule = () => {
      if (frame) return;
      frame = requestAnimationFrame(tick);
    };

    const scheduleWithRefresh = () => {
      needsRefresh = true;
      schedule();
    };

    // Dragging the floating bar -> scroll the real table.
    const onProxyScroll = () => {
      const active = activeRef.current;
      if (!active || syncingRef.current) return;
      syncingRef.current = true;
      active.scrollLeft = proxy.scrollLeft;
      syncingRef.current = false;
    };

    // Scrolling the real table (trackpad, keyboard, page scroll) -> move the bar.
    const onAnyScroll = (event: Event) => {
      const active = activeRef.current;
      if (active && event.target === active && !syncingRef.current) {
        syncingRef.current = true;
        proxy.scrollLeft = active.scrollLeft;
        syncingRef.current = false;
      }
      schedule();
    };

    proxy.addEventListener("scroll", onProxyScroll, { passive: true });
    document.addEventListener("scroll", onAnyScroll, { passive: true, capture: true });
    window.addEventListener("resize", scheduleWithRefresh);
    // Only watch for nodes being added/removed (route changes, data loads) so a
    // new table registers. We deliberately do NOT watch attributes: this
    // component writes inline styles to its own bar every frame, and observing
    // style/class mutations would feed those writes straight back into a busy
    // rAF loop. Size changes are covered by `resize`; widths are read live each tick.
    const observer = new MutationObserver(scheduleWithRefresh);
    observer.observe(document.body, { childList: true, subtree: true });

    tick();

    return () => {
      proxy.removeEventListener("scroll", onProxyScroll);
      document.removeEventListener("scroll", onAnyScroll, { capture: true });
      window.removeEventListener("resize", scheduleWithRefresh);
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div
      aria-hidden="true"
      ref={proxyRef}
      style={{
        position: "fixed",
        bottom: 0,
        height: 14,
        display: "none",
        overflowX: "scroll",
        overflowY: "hidden",
        zIndex: 40,
      }}
    >
      <div ref={spacerRef} style={{ height: 1 }} />
    </div>
  );
}
