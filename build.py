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
OUT_PDF_NAME: str = "book.pdf"

FENCE_RE: re.Pattern[str] = re.compile(r"^(`{3,}|~{3,})")
VERBATIM_INDENT_RE: re.Pattern[str] = re.compile(r"^( {4}|\t)")
INNER_HEADING_RE: re.Pattern[str] = re.compile(r"^(#{2,6})\s+(.+)$")
NUM_PREFIX_RE: re.Pattern[str] = re.compile(r"^\d+(?:\.\d+)+\s*[.:：]?\s*")
ZH_PREFIX_RE: re.Pattern[str] = re.compile(r"^第\s*\d+\s*章\s*[：:.]?\s*")
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


def clean_inner_headings(body: str, flatten_all: bool = False) -> str:
    """清洗文件内部(非首个)标题行:剥离手写编号前缀,避免与自动编号重复;
    三级及以下标题转为粗体段落(不进目录);flatten_all=True 时二级标题
    也转粗体(用于译者序/前言等前置文档,其内部小节不进目录)。"""
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
            text: str = hm.group(2).strip()  # 标题文本(## 标记与空白之后的内容)
            text = NUM_PREFIX_RE.sub("", text)
            text = ZH_PREFIX_RE.sub("", text).strip()
            if flatten_all or len(hm.group(1)) >= 3:
                out.append(f"**{text}**")
            else:
                out.append(f"## {text}")
        else:
            out.append(ln)
    return "\n".join(out)


def build_cover(file_path: Path) -> str:
    """index.md 单独处理:作为封面(首页),标题转粗体,不进目录/书签/编号。"""
    body: str = file_path.read_text(encoding="utf-8").lstrip("\ufeff")
    body = re.sub(r"^#\s+(.+)$", r"**\1**", body, count=1, flags=re.M)
    body = body.replace("](../", "](./")
    return sanitize(body)


def split_src_lines(body: str) -> list[str]:
    """按行拆分,兼容 \\n 与 \\r\\n 行尾。"""
    return body.replace("\r\n", "\n").split("\n")


def find_first_title_line(src_lines: list[str]) -> tuple[str | None, int]:
    """定位文件中首个标题行(H1/H2,忽略围栏代码块内),返回 (行文本, 行号)。"""
    in_fence = False
    for i, ln in enumerate(src_lines):
        stripped: str = ln.lstrip()
        if FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#{1,2}\s+\S", ln):
            return ln, i
    return None, -1


def strip_title_prefixes(text: str) -> str:
    """去掉标题中手写的编号前缀(如 "3.4. "、"第 3 章 "),使自动编号唯一。"""
    text = NUM_PREFIX_RE.sub("", text).strip()
    text = ZH_PREFIX_RE.sub("", text).strip()
    return text


def main() -> int:
    raw_args: list[str] = sys.argv[1:]
    verbose: bool = "--verbose" in raw_args or "-v" in raw_args
    pos_args: list[str] = [a for a in raw_args if not a.startswith("-")]

    book_dir: Path = Path(pos_args[0]).resolve() if pos_args else Path.cwd()
    out_dir: Path = Path(pos_args[1]).resolve() if len(pos_args) > 1 else Path.cwd()
    pandoc: str = os.environ.get("PANDOC", "pandoc")
    pdf_name: str = os.environ.get("PDF_NAME", OUT_PDF_NAME)

    summary_path: Path = book_dir / "SUMMARY.md"
    if not summary_path.exists():
        print("错误: 未找到", summary_path, file=sys.stderr)
        print("用法: python build.py <图书根目录> [PDF输出目录]", file=sys.stderr)
        return 1

    # ---- 聚合所有章节为单一 markdown(带层级标题) ----
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
        level: int = 1 if (is_top or indent == 0) else 2  # 前置/章 => H1, 节 => H2

        body: str = file_path.read_text(encoding="utf-8").lstrip("\ufeff")
        src_lines: list[str] = split_src_lines(body)
        title_line: str | None
        title_idx: int
        title_line, title_idx = find_first_title_line(src_lines)
        if title_line is not None:
            t_text: str = strip_title_prefixes(re.sub(r"^#{1,2}\s+", "", title_line).strip())
            del src_lines[title_idx]
            out.append("\\clearpage")  # 章/节均从新页开始
            out.append(f"{'#' * level} {t_text}{' {-}' if is_top else ''}")
            if verbose:
                print(f"[聚合] {file} -> {'#' * level} {t_text}")
        else:
            out.append("\\clearpage")  # 章/节均从新页开始
            out.append(f"{'#' * level} {m.group(2).strip()}{' {-}' if is_top else ''}")
            if verbose:
                print(f"[聚合] {file} -> {'#' * level} {m.group(2).strip()}")

        body = "\n".join(src_lines)
        # 清洗文件内部其余标题:剥离手写编号前缀;前置文档内部标题也转粗体
        body = clean_inner_headings(body, flatten_all=is_top)
        # 图片路径:聚合文件位于临时目录,../images/.. 需改为 images/..(由 --resource-path 指回图书目录)
        body = body.replace("](../", "](./")
        body = sanitize(body)
        out.append(body)
        out.append("")

    final_md: str = "\n\n".join(x for x in (cover, "\\clearpage\n\n\\tableofcontents", "\n".join(out)) if x)

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
        "--number-sections",
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