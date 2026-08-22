// WaBook/mdBook 结构 markdown 图书的 PDF 构建工具
// 用法: node build.mjs <图书根目录> [PDF输出目录]
//   图书根目录: 含 SUMMARY.md 的图书仓库
//   PDF输出目录: 可选, 默认当前工作目录
// 环境变量: PANDOC 可指定 pandoc 可执行文件路径(默认 "pandoc", 需在 PATH 中)
//          PDF_NAME 可指定输出文件名(默认 "book.pdf")
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const SELF_DIR = path.dirname(fileURLToPath(import.meta.url));
const HEADER_TEX = path.join(SELF_DIR, "header.tex");
const PANDOC = process.env.PANDOC || "pandoc";
const OUT_PDF_NAME = process.env.PDF_NAME || "book.pdf";

// 详细模式: --verbose 或 -v 为选项(输出聚合进度、完整 pandoc 命令,并把 --verbose 透传给 pandoc)
const rawArgs = process.argv.slice(2);
const VERBOSE = rawArgs.includes("--verbose") || rawArgs.includes("-v");
const posArgs = rawArgs.filter((a) => !a.startsWith("-"));
const [bookDirArg, outDirArg] = posArgs;
const bookDir = path.resolve(bookDirArg || process.cwd());
const outDir = path.resolve(outDirArg || process.cwd());

if (!fs.existsSync(path.join(bookDir, "SUMMARY.md"))) {
  console.error("错误: 未找到", path.join(bookDir, "SUMMARY.md"));
  console.error("用法: node build.mjs <图书根目录> [PDF输出目录]");
  process.exit(1);
}

// ---- 聚合所有章节为单一 markdown(带层级标题) ----

function sanitize(body) {
  const ls = body.split(/\r?\n/);
  let inFence = false;
  const out = [];
  for (const ln of ls) {
    const t = ln.trimStart();
    if (/^(`{3,}|~{3,})/.test(t)) { inFence = !inFence; out.push(ln); continue; }
    if (!inFence && !/^( {4}|\t)/.test(ln)) {
      out.push(
        ln
          .replace(/\\r\\n/g, "`\\r\\n`")
          .replace(/\$([^$\n]*\^[^$\n]*)\$/g, "\\($1\\)")
      );
    } else {
      out.push(ln);
    }
  }
  return out.join("\n");
}

// 清洗文件内部(非首个)标题行:剥离手写编号前缀(如 "8.4.1. "、"第 3 章 "),以免与自动编号重复;
// 三级及以下标题转为粗体段落(与在线版一致的二级目录结构,不进目录);
// flattenAll=true 时二级标题也转粗体(用于译者序/前言等前置文档,其内部小节不进目录)
function cleanInnerHeadings(body, flattenAll = false) {
  const ls = body.split(/\r?\n/);
  let inFence = false;
  const out = [];
  for (const ln of ls) {
    const t = ln.trimStart();
    if (/^(`{3,}|~{3,})/.test(t)) { inFence = !inFence; out.push(ln); continue; }
    const hm = !inFence && ln.match(/^(#{2,6})\s+(.+)$/);
    if (hm) {
      let text = hm[2].trim();
      text = text
        .replace(/^\d+(?:\.\d+)+\s*[.:：]?\s*/, "")
        .replace(/^第\s*\d+\s*章\s*[：:.]?\s*/, "");
      if (flattenAll || hm[1].length >= 3) {
        out.push(`**${text}**`);
      } else {
        out.push(`## ${text}`);
      }
    } else {
      out.push(ln);
    }
  }
  return out.join("\n");
}

// index.md 单独处理:作为封面(首页),标题转粗体,不进目录/书签/编号
function buildCover(filePath) {
  let body = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "");
  body = body.replace(/^#\s+(.+)$/m, "**$1**");
  body = body.replace(/\]\(\.\.\//g, "](./");
  return sanitize(body);
}

const summary = fs.readFileSync(path.join(bookDir, "SUMMARY.md"), "utf8");
const lines = summary.split(/\r?\n/);
const out = [];
const re = /^(\s*)\*?\s*\[([^\]]+)\]\(([^)]+)\)\s*$/;
let cover = "";

for (const line of lines) {
  if (/^\s*#/.test(line)) continue; // "# Summary"
  const m = line.match(re);
  if (!m) continue;
  const indent = m[1].length;
  const file = m[3];
  if (!file.endsWith(".md")) continue;
  const filePath = path.join(bookDir, file);
  if (!fs.existsSync(filePath)) {
    console.warn("MISSING:", file);
    continue;
  }
  if (file === "index.md") {
    cover = buildCover(filePath);
    if (VERBOSE) console.log("[聚合] 封面:", file);
    continue;
  }

  const isTop = indent === 0 && !line.trimStart().startsWith("*");
  const level = isTop ? 1 : indent === 0 ? 1 : 2; // 前置/章 => H1, 节 => H2

  let body = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "");
  // 剥离文件中首个标题行(H1 或 H2,忽略围栏代码块内的),替换为按 SUMMARY 层级重排的标题
  const srcLines = body.split(/\r?\n/);
  let titleLine = null, titleIdx = -1, inFence = false;
  for (let i = 0; i < srcLines.length; i++) {
    const t = srcLines[i].trimStart();
    if (/^(`{3,}|~{3,})/.test(t)) inFence = !inFence;
    if (!inFence && /^#{1,2}\s+\S/.test(t)) { titleLine = t; titleIdx = i; break; }
  }
  if (titleLine) {
    let tText = titleLine.replace(/^#{1,2}\s+/, "").trim();
    // 去掉文件标题中手写的编号前缀(如 "3.4. "、"第 3 章 "),以免与自动编号重复
    tText = tText
      .replace(/^\d+(?:\.\d+)+\s*[.:：]?\s*/, "")
      .replace(/^第\s*\d+\s*章\s*[：:.]?\s*/, "");
    srcLines.splice(titleIdx, 1);
    out.push("\\clearpage"); // 章/节均从新页开始
    out.push(`${"#".repeat(level)} ${tText}${isTop ? " {-}" : ""}`);
  } else {
    out.push("\\clearpage"); // 章/节均从新页开始
    out.push(`${"#".repeat(level)} ${m[2].trim()}${isTop ? " {-}" : ""}`);
  }
  if (VERBOSE) console.log(`[聚合] ${file} -> ${"#".repeat(level)} ${titleLine ? titleLine.replace(/^#{1,2}\s+/, "").trim() : m[2].trim()}`);
  body = srcLines.join("\n");
  // 清洗文件内部其余标题:剥离手写编号前缀;前置文档(isTop)内部标题也转粗体(不进目录)
  body = cleanInnerHeadings(body, isTop);
  // 图片路径:聚合文件位于临时目录,../images/.. 需改为 images/..(由 --resource-path 指回图书目录)
  body = body.replace(/\]\(\.\.\//g, "](./");
  body = sanitize(body);
  out.push(body);
  out.push("");
}

const finalMd = [cover, "\\clearpage\n\n\\tableofcontents", out.join("\n")]
  .filter(Boolean)
  .join("\n\n");

// ---- 中间文件放系统临时目录(不污染图书目录) ----
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "wabook-pdf-"));
const mdFile = path.join(tmpDir, "book.md");
fs.writeFileSync(mdFile, finalMd, "utf8");

// ---- 调用 pandoc 编译 PDF ----
fs.mkdirSync(outDir, { recursive: true });
const pdfOut = path.join(outDir, OUT_PDF_NAME);
const args = [
  mdFile, "-o", pdfOut,
  "--pdf-engine=xelatex",
  "--pdf-engine-opt=--enable-installer",
  `--resource-path=${bookDir}`,
  "-f", "markdown-tex_math_dollars",
  `--include-in-header=${HEADER_TEX}`,
  "-V", "mainfont=Cambria",
  "-V", "CJKmainfont=SimSun",
  "-V", "monofont=Consolas",
  "-V", "fontsize=12pt",
  "-V", "linestretch=1.25",
  "-V", "geometry:margin=2.5cm",
  "--number-sections",
  "--syntax-highlighting=tango",
];
if (VERBOSE) {
  args.push("--verbose");
  console.log("[pandoc]", PANDOC, args.join(" "));
}
console.log("图书目录:", bookDir);
console.log("输出目录:", outDir);
console.log("调用 pandoc:", PANDOC);
const r = spawnSync(PANDOC, args, { stdio: "inherit" });
if (r.status !== 0) {
  console.error("pandoc 编译失败 (退出码 " + r.status + ")");
  process.exit(r.status ?? 1);
}
fs.rmSync(tmpDir, { recursive: true, force: true });
console.log("完成:", pdfOut);