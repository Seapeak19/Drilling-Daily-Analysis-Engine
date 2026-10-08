"""CJK 字体注册（reportlab）。

Windows 中文字体常见为 TTC 集合（simsun.ttc / msyh.ttc），reportlab 的 TTFont
只接受 .ttf/.otf。因此这里优先挑选独立的 .ttf 字体，并在 TTC 场景下用
subfontIndex 显式指定子字体。
"""

from __future__ import annotations

import functools
from pathlib import Path

FONT_CANDIDATES: list[tuple[str, int]] = [
    # (字体文件, TTC 子字体序号)
    (r"C:\Windows\Fonts\simhei.ttf", 0),      # 黑体：日报表格最接近的观感
    (r"C:\Windows\Fonts\NotoSansSC-VF.ttf", 0),
    (r"C:\Windows\Fonts\Deng.ttf", 0),        # 等线
    (r"C:\Windows\Fonts\simkai.ttf", 0),      # 楷体
    (r"C:\Windows\Fonts\msyh.ttc", 0),        # 微软雅黑（TTC）
    (r"C:\Windows\Fonts\simsun.ttc", 0),      # 宋体（TTC）
    (r"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    (r"/System/Library/Fonts/PingFang.ttc", 0),
]
BOLD_CANDIDATES: list[tuple[str, int]] = [
    (r"C:\Windows\Fonts\simhei.ttf", 0),
    (r"C:\Windows\Fonts\Dengb.ttf", 0),
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\simsunb.ttf", 0),
]

REGULAR_NAME = "DDR-CJK"
BOLD_NAME = "DDR-CJK-Bold"


def _register(name: str, candidates: list[tuple[str, int]]) -> str | None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if name in pdfmetrics.getRegisteredFontNames():
        return name
    for path, index in candidates:
        p = Path(path)
        if not p.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(p), subfontIndex=index))
            return name
        except Exception:
            continue
    return None


@functools.lru_cache(maxsize=1)
def register_cjk_fonts() -> tuple[str, str]:
    """注册并返回 (常规字体名, 加粗字体名)。

    找不到任何 CJK 字体时回落到内置 Helvetica（中文会显示为方块，
    此时调用方应警告而不是静默产出乱码 PDF）。
    """
    regular = _register(REGULAR_NAME, FONT_CANDIDATES) or "Helvetica"
    bold = _register(BOLD_NAME, BOLD_CANDIDATES) or regular
    return regular, bold


def has_cjk_font() -> bool:
    regular, _ = register_cjk_fonts()
    return regular != "Helvetica"
