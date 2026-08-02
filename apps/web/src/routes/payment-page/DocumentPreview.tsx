/**
 * Показ исходного документа в окне разбора: PDF — во фрейме, фотография — картинкой.
 *
 * Пока источник был один, здесь стоял безусловный `<iframe>`. С приходом коммунальных платёжек
 * он превратился в пустой прямоугольник: браузер не рисует JPEG во фрейме предсказуемо, и
 * сверять разбор человеку стало не по чему — а сверка это единственное, ради чего окно открыто.
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
  const isImage = (mime ?? "").startsWith("image/");
  return (
    <div className="min-h-[60vh] rounded-md border bg-muted/30">
      {url ? (
        isImage ? (
          // Снимок квитанции держим целиком в поле зрения: обрезать его нельзя — обрезанной
          // окажется как раз строка с суммой.
          <img
            src={url}
            alt="Снимок документа"
            className="h-[60vh] w-full rounded-md object-contain"
          />
        ) : (
          <iframe title="Документ" src={url} className="h-[60vh] w-full rounded-md" />
        )
      ) : (
        <div className="flex h-[60vh] items-center justify-center text-sm text-muted-foreground">
          {hasFile ? "Загрузка документа…" : "Документ недоступен"}
        </div>
      )}
    </div>
  );
}
