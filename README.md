# WaBook-pdf-builder

WaBook / mdBook 结构 markdown 图书的一键 PDF 构建工具。
读取图书仓库(含 `SUMMARY.md`),生成排版完整的中文 PDF;不改动图书仓库任何文件,PDF 输出到自定目录。

## 依赖

| 依赖 | 说明 |
|---|---|
| **pandoc 3.x** | 从 [pandoc.org](https://pandoc.org) 下载便携版解压即可;可用环境变量 `PANDOC` 指定可执行文件路径 |
| **XeLaTeX** | 推荐 MiKTeX;编译中缺失宏包时自动下载安装(`--enable-installer`,不改任何配置) |
| **系统中文字体** | 正文 Cambria、中文 SimHei(黑体,可用 `CJK_FONT` 环境变量切换)、代码 Consolas(均为 Windows 自带) |

## 用法

```bash
python build.py <图书根目录> [PDF输出目录]
```

示例:

```bash
python build.py D:\books\my-book            # 输出到当前目录
python build.py D:\books\my-book D:\out     # 输出到指定目录
python build.py --verbose D:\books\my-book  # 详细模式
```

产物: `<PDF输出目录>/book.pdf`,可用环境变量 `PDF_NAME` 改名。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PANDOC` | `pandoc` | pandoc 可执行文件路径(PATH 中可直接调用时无需设置) |
| `PDF_NAME` | `book.pdf` | 输出 PDF 文件名 |
| `CJK_FONT` | `SimHei` | 中文字体名(如 `Noto Serif SC`、`STZhongsong` 等,需系统已安装) |

## 解析方式

按 `SUMMARY.md` 的顺序与包含关系逐文件展开;标题沿用 md 原文格式(章 = `#`、节 = `##`、三级 = `###`),编号即原书编号,书内交叉引用自然自洽。

| 输入 | 处理 |
|---|---|
| `SUMMARY.md` | 顺序清单:前置文档(顶层、无 `*` 条目,如译者序/前言)、章(顶层 `*`)、节(缩进 `*`) |
| `index.md` | 封面页:标题转粗体段落,内容原样,进正文首页 |
| 各章节 `*.md` | 原文透传(去 BOM;图片 `../images/` 相对路径改写;文本清洗) |
| 前置文档内部次级标题 | 转为粗体段落(保持目录为 SUMMARY 两级结构) |

## PDF 布局

- 封面 → 目录 → 各章/节/三级小节(每个 SUMMARY 条目从新页开始)
- 目录包含全部三级小节(`header.tex` 中 `tocdepth=3`);阅读器书签文本即标题原文
- 代码块浅灰底、语法高亮,超长行自动折行(不超出页边距)
- 正文 12pt、1.25 倍行距、2.5cm 边距;目录链接跳转保持阅读器当前缩放

## 内部结构

| 文件 | 作用 |
|---|---|
| `build.py` | 展开 SUMMARY + 调用 pandoc 编译(Python 3,类型注解) |
| `header.tex` | LaTeX 定制:目录后另起一页、两级目录、代码灰底、超长行折行 |

中间文件在系统临时目录,编译后自动清理;不在图书目录留下任何文件。

## 可调项

| 项 | 位置 |
|---|---|
| 封面文件(默认 `index.md`) | `build.py` 中 `file == "index.md"` 分支 |
| 目录深度(默认三级) | `header.tex` 中 `tocdepth` |
| 字号/行距/边距/字体 | `build.py` 的 `-V` 参数与 `header.tex` |
| 输出文件名 | 环境变量 `PDF_NAME` |