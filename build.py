#!/usr/bin/env python3
"""WaBook/mdBook 结构 markdown 图书的一键 PDF 构建工具。

用法:
    python build.py <图书根目录> [PDF输出目录]
  选项:
    --verbose / -v   详细模式:打印聚合进度、完整 pandoc 命令,
                     并把 --verbose 透传给 pandoc(显示 xelatex 多轮编译过程)
  环境变量:
    PANDOC   pandoc 可执行文件路径(默认 "pandoc", 需在 PATH 中)
    PDF_NAME 输出 PDF 文件名(默认 "book.pdf")

解析方式:按 SUMMARY.md 的顺序与包含关系展开各 md 文件,标题按原文格式
透传(章 = #、节 = ##、三级 = ###);目录只有 SUMMARY 中的条目,前置文档
(译者序/前言)内部的次级标题转为粗体段落以免进入目录。

不改动图书仓库任何文件;聚合中间文件在系统临时目录,编译后自动清理。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SELF_DIR: Path = Path(__file__).resolve().parent
HEADER_TEX: Path = SELF_DIR / "header.tex"

FENCE_RE: re.Pattern[str] = re.compile(r"^(`{3,}|~{3,})")
VERBATIM_INDENT_RE: re.Pattern[str] = re.compile(r"^( {4}|\t)")
INNER_HEADING_RE: re.Pattern[str] = re.compile(r"^#{2,6}\s+(.+)$")
SUMMARY_ENTRY_RE: re.Pattern[str] = re.compile(r"^(\s*)\*?\s*\[([^\]]+)\]\(([^)]+)\)\s*$")

# ---- 正文清洗用的正则 ----
CODE_SPAN_RE: re.Pattern[str] = re.compile(r"(`[^`\n]*`|``[^`\n]*``)")
# pandoc 把 <sup>/<i>/<b> 等 HTML 标签当作 RawInline 输出到 PDF 时会被丢弃,
# 这里统一转为 pandoc 原生标记;生成的上标/下标开闭符先用哨兵 \x01/\x02 占位,
# 等裸 ^ ~ 全部转义后再恢复,避免自生成的标记被再次转义。
STRONG_HTML_RE: re.Pattern[str] = re.compile(r"<\s*(?:strong|b)\s*>(.*?)<\s*/\s*(?:strong|b)\s*>", re.I | re.S)
EMPH_HTML_RE: re.Pattern[str] = re.compile(r"<\s*(?:em|i)\s*>(.*?)<\s*/\s*(?:em|i)\s*>", re.I | re.S)
# 原书示例路径惯用 <u><i>gopl.io/ch1/server1</i></u>:统一转行内代码样式(下划线在 PDF 无渲染)
UL_EMPH_HTML_RE: re.Pattern[str] = re.compile(r"<\s*u\s*>\s*<\s*i\s*>(.*?)<\s*/\s*i\s*>\s*<\s*/\s*u\s*>", re.I | re.S)
SUP_HTML_RE: re.Pattern[str] = re.compile(r"<\s*sup\s*>(.*?)<\s*/\s*sup\s*>", re.I | re.S)
SUB_HTML_RE: re.Pattern[str] = re.compile(r"<\s*sub\s*>(.*?)<\s*/\s*sub\s*>", re.I | re.S)
# $2^n$ 这类"简单幂"公式:$base^exp$,base 不含 \ _ { } $ ^ ~ 与空白,
# exp 仅限字母/数字/+-(如 2^n、2^64-1、O(n^2),exp 非贪婪防吞相邻内容)
MATH_POW_RE: re.Pattern[str] = re.compile(r"\$([^\s\\_{}~$^]+)\^([0-9A-Za-z+-]+?)\$")
# 其余 $...$ 公式:保留为字面文本(可读),内部 ^ ~ _ 转义防 pandoc 误解析
MATH_LITERAL_RE: re.Pattern[str] = re.compile(r"\$([^$\n]+)\$")
# 转义后剩余的裸 ^ ~(如 §8.2 的 ^C/^D 终端提示):防 pandoc 超尾/下尾扩展跨段误配对
BARE_MARK_RE: re.Pattern[str] = re.compile(r"(?<!\\)([\^~])")

SUP_SENTINEL: str = "\x01"
SUB_SENTINEL: str = "\x02"


def esc_math(s: str) -> str:
    """数学/上标内容转义:反斜杠、^、~、_ 都转为字面(防 pandoc 误解析)。"""
    return s.replace("\\", "\\\\").replace("^", r"\^").replace("~", r"\~").replace("_", r"\_")


def sanitize_line(line: str) -> str:
    """单行正文清洗:HTML 装饰标签 -> pandoc 原生标记;公式分类处理;裸 ^/~ 转义。

    顺序很重要:
    1. 行内代码先占位保护(其内容不受任何规则影响);
    2. strong/em/sup/sub/<sup> 式公式 -> pandoc 原生标记(上/下标记哨兵占位);
    3. $...$ 简单幂(2^n)转上标,其余公式保留字面;
    4. 剩余裸 ^ ~ 全部转义(否则 pandoc 会把一行内两个 ^ 误配成上标,
       吞掉中间整段正文——这是 $2^n$ 公式格式混乱的根源);
    5. 恢复哨兵与行内代码。"""
    codes: list[str] = []

    def hold_code(m: re.Match[str]) -> str:
        codes.append(m.group(0))
        return f"\x00{len(codes) - 1}\x00"

    s = CODE_SPAN_RE.sub(hold_code, line)
    s = UL_EMPH_HTML_RE.sub(lambda m: f"`{m.group(1)}`", s)  # 先于 STRONG/EMPH,避免 <i> 被二次转换
    s = STRONG_HTML_RE.sub(lambda m: f"**{esc_math(m.group(1))}**", s)
    s = EMPH_HTML_RE.sub(lambda m: f"*{esc_math(m.group(1))}*", s)
    s = SUP_HTML_RE.sub(lambda m: f"{SUP_SENTINEL}{esc_math(m.group(1))}{SUP_SENTINEL}", s)
    s = SUB_HTML_RE.sub(lambda m: f"{SUB_SENTINEL}{esc_math(m.group(1))}{SUB_SENTINEL}", s)
    s = MATH_POW_RE.sub(lambda m: f"{m.group(1)}{SUP_SENTINEL}{m.group(2)}{SUP_SENTINEL}", s)
    s = MATH_LITERAL_RE.sub(lambda m: f"${esc_math(m.group(1))}$", s)
    s = BARE_MARK_RE.sub(lambda m: "\\" + m.group(1), s)
    s = s.replace(SUP_SENTINEL, "^").replace(SUB_SENTINEL, "~")
    for i, code in enumerate(codes):
        s = s.replace(f"\x00{i}\x00", code)
    return s


def sanitize(body: str) -> str:
    """普通文本清洗:裸 \\r\\n 字面包为行内代码(防 LaTeX 误解析);
    HTML 标签、$...$ 公式等按 sanitize_line 规则处理;代码块/缩进块不动。"""
    lines: list[str] = body.split("\n")
    in_fence = False
    out: list[str] = []
    for ln in lines:
        stripped: str = ln.lstrip()
        if FENCE_RE.match(stripped):
            in_fence = not in_fence
            out.append(ln)
            continue
        if not in_fence and not VERBATIM_INDENT_RE.match(ln):
            out.append(sanitize_line(ln.replace(r"\r\n", r"`\r\n`")))
        else:
            out.append(ln)
    return "\n".join(out)


def flatten_inner_headings(body: str) -> str:
    """前置文档(译者序/前言)专用:将文件内部所有次级标题(## 及以上)转为
    粗体段落,使其进入目录(前置文档本身是 SUMMARY 顶层条目,内部小节不属于目录)。"""
    lines: list[str] = body.split("\n")
    in_fence = False
    out: list[str] = []
    for ln in lines:
        stripped: str = ln.lstrip()
        if FENCE_RE.match(stripped):
            in_fence = not in_fence
            out.append(ln)
            continue
        hm = None if in_fence else INNER_HEADING_RE.match(ln)
        if hm:
            out.append(f"**{hm.group(1).strip()}**")
        else:
            out.append(ln)
    return "\n".join(out)


def build_cover(file_path: Path) -> str:
    """index.md 作为封面(首页):标题转粗体段落,其余内容原样。"""
    body: str = file_path.read_text(encoding="utf-8").lstrip("\ufeff")
    body = re.sub(r"^#\s+(.+)$", r"**\1**", body, count=1, flags=re.M)
    body = body.replace("](../", "](./")
    return sanitize(body)


def load_chapter(file_path: Path, flatten: bool) -> str:
    """读取章节文件,按规则预处理后返回:
    - 去 BOM;
    - 图片相对路径 ../images/.. -> images/..(聚合文件在临时目录,由 --resource-path 指回图书目录);
    - 前置文档(flatten=True)内部次级标题转粗体;
    - 文本清洗(sanitize)。"""
    body: str = file_path.read_text(encoding="utf-8").lstrip("\ufeff")
    body = body.replace("](../", "](./")
    if flatten:
        body = flatten_inner_headings(body)
    return sanitize(body)


def main() -> int:
    raw_args: list[str] = sys.argv[1:]
    verbose: bool = "--verbose" in raw_args or "-v" in raw_args
    pos_args: list[str] = [a for a in raw_args if not a.startswith("-")]

    book_dir: Path = Path(pos_args[0]).resolve() if pos_args else Path.cwd()
    out_dir: Path = Path(pos_args[1]).resolve() if len(pos_args) > 1 else Path.cwd()
    pandoc: str = os.environ.get("PANDOC", "pandoc")
    pdf_name: str = os.environ.get("PDF_NAME", "book.pdf")
    cjk_font: str = os.environ.get("CJK_FONT", "SimHei")

    summary_path: Path = book_dir / "SUMMARY.md"
    if not summary_path.exists():
        print("错误: 未找到", summary_path, file=sys.stderr)
        print("用法: python build.py <图书根目录> [PDF输出目录]", file=sys.stderr)
        return 1

    # ---- 按 SUMMARY.md 展开各章节 ----
    summary: str = summary_path.read_text(encoding="utf-8")
    out: list[str] = []
    cover: str = ""

    for line in summary.split("\n"):
        if re.match(r"^\s*#", line):  # "# Summary"
            continue
        m = SUMMARY_ENTRY_RE.match(line)
        if not m:
            continue
        indent: int = len(m.group(1))
        file: str = m.group(3)
        if not file.endswith(".md"):
            continue
        file_path: Path = book_dir / file
        if not file_path.exists():
            print("MISSING:", file, file=sys.stderr)
            continue
        if file == "index.md":
            cover = build_cover(file_path)
            if verbose:
                print("[聚合] 封面:", file)
            continue

        is_top: bool = indent == 0 and not line.lstrip().startswith("*")
        body: str = load_chapter(file_path, flatten=is_top)
        out.append("\\clearpage\n\n" + body)  # 每个 SUMMARY 条目均从新页开始
        out.append("")
        if verbose:
            print("[聚合]", file, "(前置)" if is_top else "")

    # ---- 组装:封面(可选)→ 目录 → 正文 ----
    blocks: list[str] = []
    if cover:
        blocks.append(cover)
    blocks.append("\\clearpage\n\n\\tableofcontents")
    blocks.append("\n".join(out))
    final_md: str = "\n\n".join(blocks)

    # ---- 中间文件放系统临时目录(不污染图书目录) ----
    tmp_dir: Path = Path(tempfile.mkdtemp(prefix="wabook-pdf-"))
    md_file: Path = tmp_dir / "book.md"
    md_file.write_text(final_md, encoding="utf-8")

    # ---- 调用 pandoc 编译 PDF ----
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_out: Path = out_dir / pdf_name
    args: list[str] = [
        str(md_file), "-o", str(pdf_out),
        "--pdf-engine=xelatex",
        "--pdf-engine-opt=--enable-installer",
        f"--resource-path={book_dir}",
        "-f", "markdown-tex_math_dollars",
        f"--include-in-header={HEADER_TEX}",
        "-V", "mainfont=Cambria",
        "-V", f"CJKmainfont={cjk_font}",
        "-V", "monofont=Consolas",
        "-V", "fontsize=12pt",
        "-V", "linestretch=1.25",
        "-V", "geometry:margin=2.5cm",
        "--syntax-highlighting=tango",
    ]
    if verbose:
        args.append("--verbose")
        print("[pandoc]", pandoc, " ".join(args))

    print("图书目录:", book_dir)
    print("输出目录:", out_dir)
    print("调用 pandoc:", pandoc)
    proc: subprocess.CompletedProcess[str] = subprocess.run([pandoc, *args], check=False)
    if proc.returncode != 0:
        print(f"pandoc 编译失败 (退出码 {proc.returncode})", file=sys.stderr)
        return proc.returncode if proc.returncode is not None else 1
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("完成:", pdf_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())