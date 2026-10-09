"""CJK 字体注册（reportlab）。

## 核心原则：判断"能不能渲染"，而不是"文件在不在"

这是本项目最重要的一条经验，值得放在模块最前面。

渲染中文日报必须有中文字体，而各平台差异很大：

- Windows 常见为 TTC 集合（simsun.ttc / msyh.ttc），reportlab 的 TTFont
  只接受 .ttf/.otf/.ttc，TTC 要用 subfontIndex 指定子字体；
- Linux 上字体是否安装、装到哪个目录，取决于发行版与包版本
  （`fonts-noto-cjk` 在旧版 Ubuntu 装到 `truetype/noto/`，新版装到
  `opentype/noto/`），硬编码单个路径很容易过期。

但**真正致命的问题不是"找不到字体"，而是"找到的字体渲染不出中文"**：

    字体文件存在        -> True
    TTFont 注册         -> 成功（不抛异常）
    渲染"旋转钻进"后提取 -> '\\x00\\x00\\x00\\x00 2066.06'   ← 中文变成空字节

没有字形时 reportlab **不报错**，而是画空字形，文本层里留下空字节。
后果是链式静默失败：日报里所有中文消失 → 模板识别失败 →
时间分解表找不到 → 解析器补记一条 `UNKNOWN 24h` →
黄金样本与端到端测试全线失败，而报错信息离根因极远。

CI 首次运行（Linux）就是这个样子：verify job 全挂，真正的报错却是
"时间分解只有 1 条"。

所以本模块的判据是 `_validate_cjk_render()` 的**往返验证**：
渲染一段探针文本到 PDF，再用 pymupdf 读回来确认中文没丢。
只有通过验证的字体才会被采用。

## 三层字体发现（从最可靠到最兜底）

1. **fc-match 反查**（Linux/macOS）：问 fontconfig "支持中文的 sans 字体
   到底是哪个文件"。这是唯一不靠猜路径的办法。
2. **静态候选列表**：覆盖各平台常见路径，含 Ubuntu 新旧两种布局。
3. **都没有 → 显式失败**：由 `render._ensure_cjk_font()` 抛
   `FontUnavailableError`。绝不静默退回 Helvetica —— 那会产出满屏方块、
   看起来生成成功实则不可用的 PDF，比直接失败危险得多。

> `tests/test_golden_sample.py::TestNoCjkFontGuard` 与 CI 的
> `no-cjk-font-guard` job 一起锁住这条失败路径。
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
import tempfile
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
    # ---- Linux ----
    # 顺序有讲究：wqy-zenhei 是静态 TTC，reportlab 兼容性最好；
    # NotoSansCJK 在部分版本里是 CFF/可变字体集合，实测有渲染不出中文的情况，
    # 所以排在 wqy 之后 —— 有往返验证兜底，顺序只影响性能不影响正确性。
    (r"/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    (r"/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc", 0),
    # Ubuntu 旧版布局 / 新版布局（24.04 noble 用 opentype/）
    (r"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    (r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    (r"/usr/share/fonts/opentype/noto/NotoSansCJK-VF.otf.ttc", 0),
    # ---- macOS ----
    (r"/System/Library/Fonts/PingFang.ttc", 0),
    (r"/System/Library/Fonts/STHeiti Light.ttc", 0),
]
BOLD_CANDIDATES: list[tuple[str, int]] = [
    (r"C:\Windows\Fonts\simhei.ttf", 0),
    (r"C:\Windows\Fonts\Dengb.ttf", 0),
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\simsunb.ttf", 0),
    (r"/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    (r"/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc", 0),
    (r"/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", 0),
    (r"/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
]

REGULAR_NAME = "DDR-CJK"
BOLD_NAME = "DDR-CJK-Bold"

# fc-match 失败时的等待上限（秒）。字体查询是本地操作，给太长没意义。
_FC_TIMEOUT = 10

# 往返验证用的探针文本：既有中文也有数字。
# 两种都要：只测中文的话，某些字体可能把数字也画坏；
# 只测数字的话，纯拉丁字体会通过（那正是要排除的）。
_PROBE_TEXT = "钻井日报 2066.06"


# --------------------------------------------------------------------- 往返验证
def _validate_cjk_render(path: str, index: int) -> tuple[bool, str]:
    """往返验证：这个字体**真的**能把中文渲染进 PDF 并被读回吗？

    返回 (是否可用, 失败原因)。判据是"能渲染"，不是"文件存在"，
    也不是"注册没报错" —— 理由见模块 docstring。
    """
    probe_name = f"_ddr_probe_{Path(path).stem}_{index}"
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        if probe_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(probe_name, path, subfontIndex=index))

        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph, SimpleDocTemplate

        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "probe.pdf"
            SimpleDocTemplate(str(pdf)).build(
                [Paragraph(_PROBE_TEXT, ParagraphStyle("s", fontName=probe_name, fontSize=10))]
            )
            import pymupdf

            with pymupdf.open(str(pdf)) as d:
                got = "".join(p.get_text() for p in d)
    except Exception as exc:  # 任何异常都视为"这个候选不可用"
        return False, f"{type(exc).__name__}: {exc}"

    missing = [ch for ch in _PROBE_TEXT if ch != " " and ch not in got]
    if missing:
        return False, f"中文无法读回（缺 {''.join(missing)}；实测 {got.strip()[:24]!r}）"
    return True, ""


# --------------------------------------------------------------------- 字体发现
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
    """挑出第一个**经往返验证可用**的字体。

    与 `_register` 分开是为了可测试：reportlab 的字体注册表是全局的，
    一旦本进程注册过 DDR-CJK，"字体是否可用"就无法再从注册表判断出来。
    """
    for path, index in _merged_candidates(candidates):
        if not Path(path).exists():
            continue
        ok, _why = _validate_cjk_render(path, index)
        if ok:
            return path, index
    return None


@functools.lru_cache(maxsize=1)
def resolve_cjk_font() -> tuple[str | None, str | None]:
    """解析出**经验证可渲染中文**的 (常规字体, 加粗字体)，找不到则为 None。

    这是"系统里到底有没有可用的中文字体"的唯一权威判断。

    带缓存：解析要做真实渲染验证（Linux 的 TTC 可达 20 MB），
    每次渲染都重做会明显变慢。测试若需模拟"没有字体"，
    必须 `cache_clear()` 并同时堵住 `_fc_match_font`，
    见 `tests/test_golden_sample.py::TestNoCjkFontGuard`。
    """
    regular = _find_font(FONT_CANDIDATES)
    # 粗体单独验证；不可用就退回常规体（宁可没有粗体，不可中文缺失）
    bold = _find_font(BOLD_CANDIDATES) or regular
    return (regular[0] if regular else None, bold[0] if bold else None)


def describe_candidates() -> str:
    """给报错信息用：列出尝试过的候选与各自失败原因。

    为什么需要：CI 上"中文渲染不出来"的表象是"时间分解只有 1 条"，
    离根因非常远。把"试过哪些字体、各自为什么不行"直接写进报错，
    下一个遇到的人就不用再花一轮 CI 去猜。
    """
    lines: list[str] = []
    for path, index in _merged_candidates(FONT_CANDIDATES):
        if not Path(path).exists():
            lines.append(f"  - {path}（文件不存在）")
            continue
        ok, why = _validate_cjk_render(path, index)
        lines.append(f"  - {path}（{'可用' if ok else why}）")
    return "\n".join(lines) if lines else "  （候选列表为空）"


# --------------------------------------------------------------------- 注册
def _register(name: str, found: tuple[str, int] | None) -> str | None:
    """把已解析出的字体注册成 `name`。found 为 None 表示系统里没有可用字体。"""
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

    找不到可用字体时回落到内置 Helvetica —— 注意这只是**本函数的**兜底，
    调用方必须用 `has_cjk_font()` 判断并显式报错，
    否则会产出中文显示为方块的 PDF（`render._ensure_cjk_font()` 就是这么做的）。
    """
    regular_path, bold_path = resolve_cjk_font()
    regular = _register(REGULAR_NAME, (regular_path, 0) if regular_path else None) or "Helvetica"
    bold = _register(BOLD_NAME, (bold_path, 0) if bold_path else None) or regular
    return regular, bold


def has_cjk_font() -> bool:
    """系统里是否存在**能真正渲染中文**的字体。

    刻意不依赖 reportlab 的全局注册表 —— 那会让"是否可用"在同进程内
    被首次调用永久固化，测试与调用方都无法可靠判断。
    """
    regular, _ = resolve_cjk_font()
    return regular is not None
