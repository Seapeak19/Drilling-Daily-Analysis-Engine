"""CJK 字体注册（reportlab）。

## 为什么这件事比看起来麻烦

渲染中文日报必须有中文字体，而各平台差异很大：

- Windows 常见为 TTC 集合（simsun.ttc / msyh.ttc），reportlab 的 TTFont
  只接受 .ttf/.otf，因此 TTC 场景要用 subfontIndex 显式指定子字体；
- Linux 上字体是否安装、装到哪个目录，取决于发行版与包的版本
  （`fonts-noto-cjk` 在旧版 Ubuntu 装到 `truetype/noto/`，新版装到
  `opentype/noto/`），**硬编码单个路径很容易过期**。

所以这里的策略是三层，从最可靠到最兜底：

1. **fc-match 反查**（Linux/macOS）：问 fontconfig "支持中文的 sans 字体
   到底是哪个文件"。这是唯一不靠猜路径的办法。
2. **静态候选列表**：覆盖各平台常见路径，含两种 Ubuntu 布局。
3. **都没有 → 显式失败**：由 `render._ensure_cjk_font()` 抛
   `FontUnavailableError`。绝不静默退回 Helvetica —— 那会产出满屏方块、
   看起来生成成功实则不可用的 PDF，比直接失败危险得多。

> CI 首次运行时所有任务失败，根因就是 ubuntu-latest 默认不含中文字体。
> 修法是在工作流里 apt 安装字体（`render.py` 的报错信息早就写明了做法），
> 而不是绕过这个守卫。`tests/test_golden_sample.py::TestNoCjkFontGuard`
> 与 CI 的 `no-cjk-font-guard` job 一起锁住这条失败路径。
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from pathlib import Path

FONT_CANDIDATES: list[tuple[str, int]] = [
    # (字体文件, TTC 子字体序号)
    # ---- Windows ----
    (r"C:\Windows\Fonts\simhei.ttf", 0),      # 黑体：日报表格最接近的观感
    (r"C:\Windows\Fonts\NotoSansSC-VF.ttf", 0),
    (r"C:\Windows\Fonts\Deng.ttf", 0),        # 等线
    (r"C:\Windows\Fonts\simkai.ttf", 0),      # 楷体
    (r"C:\Windows\Fonts\msyh.ttc", 0),        # 微软雅黑（TTC）
    (r"C:\Windows\Fonts\simsun.ttc", 0),      # 宋体（TTC）
    # ---- Linux（两种常见布局都列上：Ubuntu 旧版 truetype/、新版 opentype/）----
    (r"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    (r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    (r"/usr/share/fonts/opentype/noto/NotoSansCJK-VF.otf.ttc", 0),
    # fonts-wqy-zenhei（CI 里一并安装，作为 Noto 之外的第二个来源）
    (r"/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    (r"/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc", 0),
    # ---- macOS ----
    (r"/System/Library/Fonts/PingFang.ttc", 0),
    (r"/System/Library/Fonts/STHeiti Light.ttc", 0),
]
BOLD_CANDIDATES: list[tuple[str, int]] = [
    (r"C:\Windows\Fonts\simhei.ttf", 0),
    (r"C:\Windows\Fonts\Dengb.ttf", 0),
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\simsunb.ttf", 0),
    (r"/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", 0),
    (r"/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    (r"/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
]

REGULAR_NAME = "DDR-CJK"
BOLD_NAME = "DDR-CJK-Bold"

# fc-match 失败时的等待上限（秒）。字体查询是本地操作，给太长没意义。
_FC_TIMEOUT = 10


def _fc_match_font() -> str | None:
    """用 fontconfig 反查"支持中文的 sans 字体"对应的文件路径。

    为什么值得这么做：Linux 上字体的安装路径随发行版与包版本变化，
    静态候选列表总有落后的风险。fc-match 是系统自己给出的权威答案，
    比我们猜路径可靠。Windows 没有 fontconfig，直接跳过（返回 None）。
    """
    if sys.platform == "win32":
        return None
    exe = shutil.which("fc-match")
    if not exe:
        return None
    try:
        cp = subprocess.run(
            [exe, "--format=%{file}", "sans:lang=zh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FC_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0 or not cp.stdout:
        return None
    path = cp.stdout.strip()
    if not path:
        return None
    p = Path(path)
    # 只接受 reportlab 能直接加载的扩展名（.ttf/.otf/.ttc）
    if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
        return None
    return str(p) if p.exists() else None


def _merged_candidates(static: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """静态候选 + fc-match 结果（放最前，因为它是系统给出的权威答案）。"""
    found = _fc_match_font()
    if not found:
        return list(static)
    if any(path == found for path, _ in static):
        return list(static)
    return [(found, 0), *static]


def _find_font(candidates: list[tuple[str, int]]) -> tuple[str, int] | None:
    """在候选（含 fc-match 结果）里挑出第一个真实存在且可加载的字体。

    与 `_register` 分开是为了**可测试**：reportlab 的字体注册表是全局的，
    一旦本进程注册过 DDR-CJK，"字体是否存在"就无法再从注册表判断出来
    （注册表只会说"这个名字已注册"）。把"发现"与"注册"拆开后，
    "系统里没有中文字体"这个场景才能被稳定地断言。
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path, index in _merged_candidates(candidates):
        p = Path(path)
        if not p.exists():
            continue
        # 探测字体名用确定性命名（同一文件只加载一次）。
        # 不用 hash()：它每次进程都变，会白白重复注册，而 Linux 的
        # NotoSansCJK-Regular.ttc 有 20 MB，重复解析代价可观。
        probe = f"_ddr_probe_{p.stem}_{index}"
        try:
            if probe not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(probe, str(p), subfontIndex=index))
            return path, index
        except Exception:
            continue
    return None


@functools.lru_cache(maxsize=1)
def resolve_cjk_font() -> tuple[str | None, str | None]:
    """解析出可用的 (常规字体文件, 加粗字体文件)，找不到则为 None。

    这是"系统里到底有没有中文字体"的**唯一权威判断**，
    与 reportlab 的全局注册表无关。

    带缓存：解析要真实加载字体文件（Linux 的 TTC 可达 20 MB），
    每次渲染都重新解析会让批量渲染明显变慢。
    测试若需模拟"没有字体"，必须同时 `cache_clear()`，见
    `tests/test_golden_sample.py::TestNoCjkFontGuard`。
    """
    regular = _find_font(FONT_CANDIDATES)
    bold = _find_font(BOLD_CANDIDATES) or regular
    return (regular[0] if regular else None, bold[0] if bold else None)


def _register(name: str, found: tuple[str, int] | None) -> str | None:
    """把已解析出的字体注册成 `name`。found 为 None 表示系统里没有。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if found is None:
        return None
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    path, index = found
    try:
        pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=index))
        return name
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def register_cjk_fonts() -> tuple[str, str]:
    """注册并返回 (常规字体名, 加粗字体名)。

    找不到任何 CJK 字体时回落到内置 Helvetica —— 注意这只是**本函数的**
    兜底，调用方必须用 `has_cjk_font()` 判断并显式报错，
    否则会产出中文显示为方块的 PDF（`render._ensure_cjk_font()` 就是这么做的）。
    """
    regular_path, bold_path = resolve_cjk_font()
    regular = _register(REGULAR_NAME, (regular_path, 0) if regular_path else None) or "Helvetica"
    bold = _register(BOLD_NAME, (bold_path, 0) if bold_path else None) or regular
    return regular, bold


def has_cjk_font() -> bool:
    """系统里是否存在可用的中文字体。

    刻意**不**依赖 reportlab 的全局注册表 —— 那会让"是否找到字体"在
    同进程内被首次调用永久固化，测试与调用方都无法可靠判断。
    """
    regular, _ = resolve_cjk_font()
    return regular is not None
