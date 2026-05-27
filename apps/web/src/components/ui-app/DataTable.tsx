import type { ReactNode } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

export type DataTableColumn<T> = {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  className?: string;
  headerClassName?: string;
};

type DataTableProps<T> = {
  columns: Array<DataTableColumn<T>>;
  rows: T[];
  isLoading?: boolean;
  getRowKey?: (row: T, index: number) => string;
  onRowClick?: (row: T) => void;
  rowClassName?: string | ((row: T) => string | undefined);
  emptyMessage?: string;
  className?: string;
};

export function DataTable<T>({
  columns,
  rows,
  isLoading = false,
  getRowKey,
  onRowClick,
  rowClassName,
  emptyMessage = "Нет данных",
  className,
}: DataTableProps<T>) {
  return (
    <div className={cn("overflow-hidden rounded-lg border bg-card", className)}>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="bg-muted/70 hover:bg-muted/70">
              {columns.map((column) => (
                <TableHead
                  className={cn(
                    "h-10 whitespace-nowrap text-xs font-semibold uppercase",
                    column.headerClassName,
                  )}
                  key={column.key}
                >
                  {column.header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading
              ? Array.from({ length: 4 }).map((_, rowIndex) => (
                  <TableRow key={rowIndex}>
                    {columns.map((column) => (
                      <TableCell className={column.className} key={column.key}>
                        <Skeleton className="h-5 w-full" />
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              : rows.map((row, rowIndex) => (
                  <TableRow
                    className={cn(
                      onRowClick ? "cursor-pointer" : undefined,
                      typeof rowClassName === "function" ? rowClassName(row) : rowClassName,
                    )}
                    key={getRowKey?.(row, rowIndex) ?? rowIndex}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                  >
                    {columns.map((column) => (
                      <TableCell className={column.className} key={column.key}>
                        {column.cell(row)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
            {!isLoading && rows.length === 0 ? (
              <TableRow>
                <TableCell
                  className="h-24 text-center text-sm text-muted-foreground"
                  colSpan={columns.length}
                >
                  {emptyMessage}
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
