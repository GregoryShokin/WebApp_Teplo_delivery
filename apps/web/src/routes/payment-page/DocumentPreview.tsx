import { useState } from "react";
import { Maximize2, Minimize2 } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Показ исходного документа в окне разбора: PDF — во фрейме, фотография — картинкой.
 *
 * Пока источник был один, здесь стоял безусловный `<iframe>`. С приходом коммунальных платёжек
 * он превратился в пустой прямоугольник: браузер не рисует JPEG во фрейме предсказуемо, и
 * сверять разбор человеку стало не по чему — а сверка это единственное, ради чего окно открыто.
 *
 * Отсюда же увеличение. Страница акта, вписанная в половину окна, читается плохо: строка
 * «Оплачено аванс: 65000р» — четыре миллиметра высотой, а именно её и надо сличить с полем.
 * Поэтому снимок переключается в натуральный размер и прокручивается — щелчком по нему или
 * кнопкой. PDF этого не требует: у него своя навигация внутри фрейма.
 */
export function DocumentPreview({
  url,
  mime,
  hasFile,
}: {
  url: string | null;
  mime: string | null;
  hasFile: boolean;
}) {
  const [zoomed, setZoomed] = useState(false);
  const isImage = (mime ?? "").startsWith("image/");

  if (!url) {
    return (
      <div className="min-h-[60vh] rounded-md border bg-muted/30">
        <div className="flex h-[60vh] items-center justify-center text-sm text-muted-foreground">
          {hasFile ? "Загрузка документа…" : "Документ недоступен"}
        </div>
      </div>
    );
  }

  if (!isImage) {
    return (
      <div className="min-h-[60vh] rounded-md border bg-muted/30">
        <iframe title="Документ" src={url} className="h-[60vh] w-full rounded-md" />
      </div>
    );
  }

  return (
    // Кнопка вынесена ИЗ прокручиваемой области: внутри неё она уезжала вместе со снимком и
    // при увеличении оказывалась за нижним краем — вернуться к «вписать» было нечем.
    <div className="relative min-h-[60vh] rounded-md border bg-muted/30">
      <div className="h-[60vh] overflow-auto rounded-md">
        <img
          src={url}
          alt="Снимок документа"
          onClick={() => setZoomed((value) => !value)}
          className={
            zoomed
              ? "max-w-none cursor-zoom-out"
              : "h-[60vh] w-full cursor-zoom-in rounded-md object-contain"
          }
        />
      </div>
      {/* Щелчок по самой картинке делает то же самое, но об этом надо сказать вслух. */}
      <Button
        type="button"
        size="sm"
        variant="secondary"
        className="absolute right-2 top-2 z-10 shadow-sm"
        onClick={() => setZoomed((value) => !value)}
      >
        {zoomed ? (
          <Minimize2 className="h-4 w-4" aria-hidden="true" />
        ) : (
          <Maximize2 className="h-4 w-4" aria-hidden="true" />
        )}
        {zoomed ? "Вписать" : "Увеличить"}
      </Button>
    </div>
  );
}
