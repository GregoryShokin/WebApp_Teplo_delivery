import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "/Users/grigorii/Developer/Projects/Teplo-all-for-business";
const outputDir = `${root}/outputs/019f5b2c-12d3-7812-8188-f0c00240007d`;
const outputPath = `${outputDir}/Сопоставление_инвентаризации_IIKO_13-07-2026.xlsx`;
const previewPath = `${root}/tmp/Сопоставление_инвентаризации_IIKO_превью.png`;
const data = JSON.parse(await fs.readFile(`${root}/tmp/final_iiko_matches.json`, "utf8"));

const typeLabels = {
  GOODS: "Товар",
  PREPARED: "Полуфабрикат",
  DISH: "Блюдо",
  MODIFIER: "Модификатор",
  SERVICE: "Услуга",
};

const rows = data.map((row) => [
  row.pages,
  row.sections,
  row.source_name,
  row.iiko_name,
  String(row.iiko_code || ""),
  row.iiko_unit,
  typeLabels[row.iiko_type] || row.iiko_type,
  row.confidence,
  row.status,
  row.comment,
  row.iiko_id,
]);

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("Сопоставление");
sheet.showGridLines = false;

sheet.getRange("A1:K1").merge();
sheet.getRange("A1").values = [["Сопоставление инвентаризационных листов с номенклатурой IIKO"]];
sheet.getRange("A1:K1").format = {
  fill: "#17324D",
  font: { bold: true, color: "#FFFFFF", size: 18 },
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
sheet.getRange("A1:K1").format.rowHeight = 34;

sheet.getRange("A2:K2").merge();
sheet.getRange("A2").values = [[
  "Источник: PDF, 18 страниц; дата ревизии 13.07.2026. Справочник IIKO: 996 активных позиций, локальный кэш от 18.06.2026. Неоднозначные пары отмечены для ручной проверки.",
]];
sheet.getRange("A2:K2").format = {
  fill: "#EAF1F5",
  font: { color: "#334E68", size: 10 },
  wrapText: true,
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
sheet.getRange("A2:K2").format.rowHeight = 38;

sheet.getRange("A3:K3").values = [[
  "Всего названий", null,
  "Точные", null,
  "По смыслу", null,
  "Проверить", null,
  "Не найдено", null,
  "Кэш IIKO: 18.06.2026",
]];
sheet.getRange("B3").formulas = [["=COUNTA(C6:C200)"]];
sheet.getRange("D3").formulas = [["=COUNTIF(I6:I200,\"Точное\")"]];
sheet.getRange("F3").formulas = [["=COUNTIF(I6:I200,\"По смыслу\")"]];
sheet.getRange("H3").formulas = [["=COUNTIF(I6:I200,\"Требует проверки\")"]];
sheet.getRange("J3").formulas = [["=COUNTIF(I6:I200,\"Не найдено\")"]];
sheet.getRange("A3:K3").format = {
  fill: "#F7FAFC",
  font: { bold: true, color: "#243B53", size: 10 },
  verticalAlignment: "center",
};
sheet.getRange("A3:K3").format.rowHeight = 25;
for (const cell of ["B3", "D3", "F3", "H3", "J3"]) {
  sheet.getRange(cell).format = {
    fill: "#D9EAF0",
    font: { bold: true, color: "#17324D", size: 11 },
    horizontalAlignment: "center",
    verticalAlignment: "center",
  };
}
sheet.getRange("K3").format.font = { italic: true, color: "#486581", size: 9 };

const headers = [[
  "Стр. PDF",
  "Разделы листов",
  "Название в листах",
  "Название в IIKO",
  "Код IIKO",
  "Ед. изм.",
  "Тип IIKO",
  "Уверенность, %",
  "Статус",
  "Комментарий",
  "ID IIKO",
]];
sheet.getRange("A5:K5").values = headers;
sheet.getRange("A5:K5").format = {
  fill: "#2F5D62",
  font: { bold: true, color: "#FFFFFF", size: 10 },
  wrapText: true,
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#D5E1E7" },
};
sheet.getRange("A5:K5").format.rowHeight = 32;

const firstDataRow = 6;
const lastDataRow = firstDataRow + rows.length - 1;
sheet.getRange(`A${firstDataRow}:K${lastDataRow}`).values = rows;
sheet.getRange(`A${firstDataRow}:K${lastDataRow}`).format = {
  font: { color: "#243B53", size: 9 },
  verticalAlignment: "top",
  borders: { preset: "all", style: "thin", color: "#E2E8F0" },
};
sheet.getRange(`A${firstDataRow}:A${lastDataRow}`).format.horizontalAlignment = "center";
sheet.getRange(`E${firstDataRow}:I${lastDataRow}`).format.horizontalAlignment = "center";
sheet.getRange(`B${firstDataRow}:D${lastDataRow}`).format.wrapText = true;
sheet.getRange(`I${firstDataRow}:J${lastDataRow}`).format.wrapText = true;
sheet.getRange(`H${firstDataRow}:H${lastDataRow}`).format.numberFormat = '0"%"';
sheet.getRange(`K${firstDataRow}:K${lastDataRow}`).format.font = { color: "#627D98", size: 8 };

const body = sheet.getRange(`A${firstDataRow}:K${lastDataRow}`);
body.conditionalFormats.addCustom(`=$I${firstDataRow}="Точное"`, { fill: "#EDF7F0" });
body.conditionalFormats.addCustom(`=$I${firstDataRow}="По смыслу"`, { fill: "#EEF6FB" });
body.conditionalFormats.addCustom(`=$I${firstDataRow}="Требует проверки"`, { fill: "#FFF4CC", font: { color: "#7C5A00" } });
body.conditionalFormats.addCustom(`=$I${firstDataRow}="Не найдено"`, { fill: "#FDE8E7", font: { color: "#9B1C1C", bold: true } });

const table = sheet.tables.add(`A5:K${lastDataRow}`, true, "InventoryIikoMapping");
table.style = "TableStyleMedium2";
table.showFilterButton = true;
table.showBandedRows = false;

const widths = {
  A: 9,
  B: 35,
  C: 35,
  D: 34,
  E: 11,
  F: 10,
  G: 15,
  H: 14,
  I: 21,
  J: 58,
  K: 38,
};
for (const [column, width] of Object.entries(widths)) {
  sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}
sheet.getRange(`A${firstDataRow}:K${lastDataRow}`).format.autofitRows();
sheet.freezePanes.freezeRows(5);
sheet.freezePanes.freezeColumns(3);

await fs.mkdir(outputDir, { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);

const checkBlob = await FileBlob.load(outputPath);
const checkWorkbook = await SpreadsheetFile.importXlsx(checkBlob);
const checkSheet = checkWorkbook.worksheets.getItem("Сопоставление");
const values = checkSheet.getRange(`A1:K${lastDataRow}`).values;
const errors = values.flat().filter((value) => typeof value === "string" && /^#(REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!)/.test(value));
if (errors.length) {
  throw new Error(`Formula errors found: ${errors.join(", ")}`);
}

const preview = await checkWorkbook.render({
  sheetName: "Сопоставление",
  autoCrop: "all",
  scale: 0.65,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));

const region = await checkWorkbook.inspect({
  kind: "region",
  sheetId: "Сопоставление",
  range: "A1:K12",
  maxChars: 6000,
});
const formulas = await checkWorkbook.inspect({
  kind: "formula",
  sheetId: "Сопоставление",
  range: "A1:K5",
  maxChars: 3000,
});

console.log(JSON.stringify({ outputPath, previewPath, lastDataRow, errors, region: region.ndjson, formulas: formulas.ndjson }, null, 2));
