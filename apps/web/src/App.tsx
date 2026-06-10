import { StickyHorizontalScrollbar } from "./components/ui-app/StickyHorizontalScrollbar";
import { AppRouter } from "./router";

export function App() {
  return (
    <>
      <AppRouter />
      <StickyHorizontalScrollbar />
    </>
  );
}
