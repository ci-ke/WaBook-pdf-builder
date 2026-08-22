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
MATH_DOLLAR_RE: re.Pattern[str] = re.compile(r"\$([^$\n]*\^[^$\n]*)\$")
SUMMARY_ENTRY_RE: re.Pattern[str] = re.compile(r"^(\s*)\*?\s*\[([^\]]+)\]\(([^)]+)\)\s*$")


def sanitize(body: str) -> str:
    """普通文本清洗:裸 \\r\\n 字面包为行内代码(防 LaTeX 误解析);
    含 ^ 的 $...$ 为刻意数学公式,转 \\(...\\) 分隔符。"""
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
            out.append(MATH_DOLLAR_RE.sub(lambda m: r"\(" + m.group(1) + r"\)",
                       ln.replace(r"\r\n", r"`\r\n`")))
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
        "-V", "CJKmainfont=SimSun",
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