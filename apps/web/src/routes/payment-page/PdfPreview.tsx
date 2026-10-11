import { useEffect, useRef, useState } from "react";
import { getDocument, GlobalWorkerOptions } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

GlobalWorkerOptions.workerSrc = workerUrl;

/** Показывает исходные страницы и в браузере без встроенного PDF-плагина. */
export function PdfPreview({ url }: { url: string }) {
  const container = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setError(false);
    const loading = getDocument({ url });
    const host = container.current;
    host?.replaceChildren();
    void loading.promise
      .then(async (pdf) => {
        for (let number = 1; number <= pdf.numPages; number++) {
          if (cancelled || !host) break;
          const page = await pdf.getPage(number);
          if (cancelled) break;
          const viewport = page.getViewport({ scale: 1.5 });
          const canvas = document.createElement("canvas");
          canvas.width = Math.ceil(viewport.width);
          canvas.height = Math.ceil(viewport.height);
          canvas.className = "w-full h-auto bg-white shadow-sm";
          canvas.setAttribute("aria-label", `Страница ${number} исходного документа`);
          host.appendChild(canvas);
          await page.render({ canvas, viewport }).promise;
        }
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
      void loading.destroy();
    };
  }, [url]);
  return (
    <div
      className="h-[60vh] overflow-auto rounded-md border bg-muted/30 p-2"
      aria-label="Исходный PDF"
    >
      {error ? (
        <p className="p-4 text-sm text-muted-foreground">
          Не удалось показать PDF.{" "}
          <a className="underline" href={url} target="_blank" rel="noreferrer">
            Открыть исходный файл
          </a>
        </p>
      ) : null}
      <div ref={container} className="grid gap-2" />
    </div>
  );
}
