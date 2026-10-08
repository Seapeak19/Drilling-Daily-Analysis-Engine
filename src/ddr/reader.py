"""读取层：把 PDF / Excel 日报统一抽象成「页 + 文本行 + 表格」。

设计原则：
- 只负责"取到什么"，不做任何字段语义判断（语义判断在 extract 层）。
- 同时保留表格结构与纯文本，因为不同日报的表格线质量差异极大：
  有框线的走表格解析，无框线的走文本行正则解析。
- 扫描件（无文本层）显式报错并给出可操作提示，不做静默降级。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------- 数据结构
@dataclass
class Table:
    """一个表格。cells 为二维字符串（None 表示空单元格）。"""

    cells: list[list[str | None]]
    page: int = 1
    bbox: tuple[float, float, float, float] | None = None

    @property
    def n_rows(self) -> int:
        return len(self.cells)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.cells), default=0)

    def row(self, i: int) -> list[str]:
        if i < 0 or i >= len(self.cells):
            return []
        return [(c if c is not None else "").strip() for c in self.cells[i]]

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page, "cells": self.cells}


@dataclass
class Page:
    number: int
    text: str
    lines: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    has_text_layer: bool = True


@dataclass
class Document:
    path: Path
    file_type: str
    pages: list[Page] = field(default_factory=list)
    sheet_names: list[str] = field(default_factory=list)
    xlsx_grids: dict[str, list[list[str]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def lines(self) -> list[str]:
        out: list[str] = []
        for p in self.pages:
            out.extend(p.lines)
        return out

    @property
    def tables(self) -> list[Table]:
        out: list[Table] = []
        for p in self.pages:
            out.extend(p.tables)
        return out


class ReaderError(RuntimeError):
    """无法读取输入文件（格式不支持、无文本层、文件损坏等）。"""


# --------------------------------------------------------------------- 文本工具
_WS_RE = re.compile(r"[ \t\u3000]+")


def normalize_line(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").replace("\xa0", " ")).strip()


# --------------------------------------------------------------------- PDF 读取
def read_pdf(path: str | Path) -> Document:
    path = Path(path)
    doc = Document(path=path, file_type="pdf")
    try:
        import pdfplumber  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ReaderError("缺少 pdfplumber：pip install pdfplumber") from exc

    no_text_pages: list[int] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text(layout=False) or ""
                text_layout = page.extract_text(layout=True) or text
                lines = [normalize_line(ln) for ln in text_layout.splitlines()]
                lines = [ln for ln in lines if ln]

                # 无框线表格也用 lines 兜底；有框线的尝试结构抽取
                tables: list[Table] = []
                try:
                    for t in page.find_tables():
                        data = t.extract()
                        if data and len(data) >= 2:
                            cells = [[(c if c is None else str(c)) for c in row] for row in data]
                            tables.append(
                                Table(cells=cells, page=i, bbox=(t.bbox[0], t.bbox[1], t.bbox[2], t.bbox[3]))
                            )
                except Exception as exc:  # 表格识别失败不应阻断整份文档
                    doc.warnings.append(f"第 {i} 页表格结构抽取失败，已退化为文本行解析：{exc}")

                has_text = bool(text.strip())
                if not has_text:
                    no_text_pages.append(i)

                doc.pages.append(
                    Page(
                        number=i,
                        text=text,
                        lines=lines,
                        tables=tables,
                        width=float(page.width),
                        height=float(page.height),
                        has_text_layer=has_text,
                    )
                )
    except ReaderError:
        raise
    except Exception as exc:
        # 文件损坏/加密/被截断时底层库会抛各种异常，统一转成可操作的中文提示，
        # 而不是把库的原始崩溃栈丢给用户（真实踩过的坑）。
        raise ReaderError(
            f"{path.name}: PDF 无法打开或已损坏（{type(exc).__name__}: {exc}）。"
            "请确认文件完整性，或先用 PDF 阅读器重新另存后再试。"
        ) from exc

    if not doc.pages:
        raise ReaderError(f"{path.name}: PDF 中没有任何页面")
    if no_text_pages and len(no_text_pages) == len(doc.pages):
        total_chars = sum(len(p.text.strip()) for p in doc.pages)
        has_images = False
        try:
            import pymupdf  # type: ignore

            with pymupdf.open(str(path)) as _d:
                has_images = any(page.get_images() for page in _d)
        except Exception:
            has_images = False  # 判断不了就不影响主流程

        if total_chars == 0 and not has_images:
            raise ReaderError(
                f"{path.name}: PDF 里没有可提取的文字，也没有图片内容，判断为空文档。"
                "请确认导出/打印时没有丢内容。"
            )
        raise ReaderError(
            f"{path.name}: 全部 {len(doc.pages)} 页均无文本层，判断为扫描件/纯图片日报。"
            "本引擎 M1 阶段不内置 OCR，请先用 OCR 转为带文本层的 PDF，或提供电子版原件。"
        )
    if no_text_pages:
        doc.warnings.append(f"第 {no_text_pages} 页无文本层（扫描页），这些页的字段将缺失")
    return doc


# --------------------------------------------------------------------- Excel 读取
def read_xlsx(path: str | Path) -> Document:
    path = Path(path)
    doc = Document(path=path, file_type=path.suffix.lower().lstrip("."))
    try:
        from openpyxl import load_workbook  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ReaderError("缺少 openpyxl：pip install openpyxl") from exc

    wb = load_workbook(str(path), data_only=True, read_only=False)
    doc.sheet_names = list(wb.sheetnames)
    for ws in wb.worksheets:
        raw_grid: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            raw_grid.append(["" if v is None else str(v) for v in row])
        # 去掉尾部全空行
        while raw_grid and not any(c.strip() for c in raw_grid[-1]):
            raw_grid.pop()

        # 空列/空行切分：Excel 没有表格对象，但工作表本身就是一张大表。
        # 这里按"连续非空列段"切成若干张逻辑表，使 extract 层可以用与非表格
        # 结构相同的 find_table_by_signature 逻辑处理（不能按 "|" 拼字符串——
        # 单元格内容里本身可能含 "|" 或 ":"，会把行结构拼坏）。
        tables = _split_grid_into_tables(raw_grid, page_number=1)
        doc.xlsx_grids[ws.title] = raw_grid

        lines: list[str] = []
        for row in raw_grid:
            cells = [c.strip() for c in row if c and c.strip()]
            if cells:
                lines.append(normalize_line(" | ".join(cells)))
        doc.pages.append(
            Page(
                number=ws.max_row or 0,
                text="\n".join(lines),
                lines=lines,
                tables=tables,
                has_text_layer=True,
            )
        )
    if not doc.xlsx_grids:
        raise ReaderError(f"{path.name}: 工作簿中没有任何工作表")
    return doc


def _split_grid_into_tables(grid: list[list[str]], *, page_number: int) -> list[Table]:
    """把工作表网格切成逻辑表格：连续非空行 + 连续非空列构成一张表。

    切分依据：整行为空 → 表间断；整列为空 → 列段间断。
    这样"表头区块 / 时间分解 / 钻头记录 / 泥浆性能"会各自成为一张表，
    extract 层就能按表头签名稳定地选中目标表。
    """
    tables: list[Table] = []
    if not grid:
        return tables
    n_cols = max(len(r) for r in grid)

    # 1) 按空行切成行段
    row_spans: list[tuple[int, int]] = []
    start: int | None = None
    for i, row in enumerate(grid):
        non_empty = any((c or "").strip() for c in row)
        if non_empty and start is None:
            start = i
        elif not non_empty and start is not None:
            row_spans.append((start, i))
            start = None
    if start is not None:
        row_spans.append((start, len(grid)))

    # 2) 每个行段内再按空列切成列段
    for r0, r1 in row_spans:
        col_has_value = [
            any((grid[r][c].strip() if c < len(grid[r]) else "") for r in range(r0, r1))
            for c in range(n_cols)
        ]
        c0: int | None = None
        spans: list[tuple[int, int]] = []
        for c in range(n_cols + 1):
            filled = col_has_value[c] if c < n_cols else False
            if filled and c0 is None:
                c0 = c
            elif not filled and c0 is not None:
                spans.append((c0, c))
                c0 = None
        for c0, c1 in spans:
            cells: list[list[str | None]] = []
            for r in range(r0, r1):
                row = grid[r]
                cells.append([(row[c] if c < len(row) else "") for c in range(c0, c1)])
            if any(any((c or "").strip() for c in row) for row in cells):
                tables.append(Table(cells=cells, page=page_number))
    return tables


# --------------------------------------------------------------------- 统一入口
SUPPORTED_SUFFIXES = {".pdf", ".xlsx", ".xlsm", ".xltx", ".xls"}


def read_document(path: str | Path) -> Document:
    path = Path(path)
    if not path.exists():
        raise ReaderError(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path)
    if suffix in (".xlsx", ".xlsm", ".xltx"):
        return read_xlsx(path)
    if suffix == ".xls":
        raise ReaderError(
            f"{path.name}: 旧版 .xls 二进制格式不受支持，请另存为 .xlsx 后重试"
            "（或安装 xlrd 并调用 read_xls 扩展点）"
        )
    raise ReaderError(f"{path.name}: 暂不支持的文件类型 {suffix!r}，支持：{sorted(SUPPORTED_SUFFIXES)}")
