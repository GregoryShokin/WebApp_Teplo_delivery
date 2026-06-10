import type { CSSProperties, ReactNode } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import {
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
  /**
   * Max height of the scroll body. The table scrolls internally so the
   * horizontal scrollbar stays at the bottom of the visible block — you don't
   * have to scroll past the whole table to reach it. The header stays pinned.
   */
  maxHeight?: string;
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
  maxHeight = "70vh",
}: DataTableProps<T>) {
  const scrollStyle: CSSProperties = { maxHeight };

  return (
    <div className={cn("overflow-hidden rounded-lg border bg-card", className)}>
      <div className="pinned-scrollbar overflow-auto" style={scrollStyle}>
        <table className="w-full caption-bottom text-sm">
          <TableHeader>
            <TableRow className="bg-muted hover:bg-muted">
              {columns.map((column) => (
                <TableHead
                  className={cn(
                    "sticky top-0 z-10 h-10 whitespace-nowrap bg-muted text-xs font-semibold uppercase",
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
        </table>
      </div>
    </div>
  );
}
