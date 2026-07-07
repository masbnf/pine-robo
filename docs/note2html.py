#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
note2html — تبدیل فایل‌های نوت مارک‌داون به HTML تمیز با تایپوگرافی فارسی

استفاده:
    python3 note2html.py NOTE.md                # خروجی: NOTE.html کنار فایل ورودی
    python3 note2html.py NOTE.md -o out.html    # مسیر خروجی دلخواه
    python3 note2html.py NOTE.md --title "عنوان دلخواه"

نیازمندی‌ها:
    pip install markdown pygments
"""

import argparse
import re
import sys
from pathlib import Path

try:
    import markdown
    from pygments.formatters import HtmlFormatter
except ImportError:
    sys.exit("کتابخانه‌ها نصب نیستند. اجرا کنید:  pip install markdown pygments")

# ----------------------------------------------------------------------------
# تشخیص جهت متن: اگر سهم حروف عربی/فارسی از حروف الفبایی بالا باشد → RTL
# ----------------------------------------------------------------------------
RTL_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


def detect_direction(text: str) -> str:
    # کدبلاک‌ها را از شمارش حذف می‌کنیم تا کد انگلیسی جهت سند را عوض نکند
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]+`", "", stripped)
    letters = LETTERS.findall(stripped)
    if not letters:
        return "rtl"
    rtl = sum(1 for ch in letters if RTL_RANGE.match(ch))
    return "rtl" if rtl / len(letters) > 0.3 else "ltr"


def extract_title(md_text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+?)\s*$", md_text, flags=re.MULTILINE)
    return m.group(1).strip() if m else fallback


def build_toc(toc_tokens, max_level: int = 3) -> str:
    """ساخت فهرست مطالب تودرتو از توکن‌های TOC مارک‌داون"""
    items = []

    def walk(tokens):
        for t in tokens:
            if t["level"] <= max_level:
                items.append(
                    f'<li class="lv{t["level"]}">'
                    f'<a href="#{t["id"]}">{t["name"]}</a></li>'
                )
            if t.get("children"):
                walk(t["children"])

    walk(toc_tokens)
    if not items:
        return ""
    return "<ul>" + "\n".join(items) + "</ul>"


# ----------------------------------------------------------------------------
# قالب HTML
# ----------------------------------------------------------------------------
TEMPLATE = """<!DOCTYPE html>
<html lang="{lang}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root {{
  --paper:   #f6f8f7;
  --card:    #ffffff;
  --ink:     #182420;
  --ink-2:   #55655e;
  --line:    #dde5e1;
  --accent:  #0d6b5c;   /* سبز پترولی */
  --accent-2:#0d6b5c1a;
  --amber:   #a8560f;   /* کد درون‌خطی */
  --code-bg: #10201c;
  --code-fg: #d7e5e0;
  --radius:  10px;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "Vazirmatn", "Segoe UI", Tahoma, sans-serif;
  font-size: 16.5px;
  line-height: 2.05;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}}

/* ---------- چیدمان کلی: سند + فهرست چسبان ---------- */
.wrap {{
  max-width: 1180px;
  margin: 0 auto;
  padding: 32px 24px 96px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 264px;
  gap: 48px;
}}
main {{ min-width: 0; }}

/* ---------- سربرگ سند ---------- */
header.doc {{
  border-bottom: 3px solid var(--ink);
  padding-bottom: 20px;
  margin-bottom: 36px;
}}
header.doc .kicker {{
  display: inline-block;
  font-size: 12.5px;
  font-weight: 700;
  letter-spacing: .06em;
  color: var(--accent);
  background: var(--accent-2);
  border: 1px solid var(--accent);
  border-radius: 999px;
  padding: 2px 14px;
  margin-bottom: 14px;
}}
header.doc h1 {{
  margin: 0;
  font-size: clamp(26px, 4vw, 38px);
  font-weight: 900;
  line-height: 1.55;
  letter-spacing: -0.01em;
}}

/* ---------- تیترها ---------- */
main h2, main h3, main h4 {{
  font-weight: 800;
  line-height: 1.7;
  scroll-margin-top: 24px;
  position: relative;
}}
main h2 {{
  font-size: 24px;
  margin: 56px 0 14px;
  padding-inline-start: 16px;
  border-inline-start: 5px solid var(--accent);
}}
main h3 {{
  font-size: 19px;
  margin: 36px 0 10px;
  color: var(--ink);
}}
main h3::before {{
  content: "◆";
  color: var(--accent);
  font-size: 11px;
  margin-inline-end: 8px;
  vertical-align: 2px;
}}
main h4 {{ font-size: 16.5px; margin: 28px 0 8px; color: var(--ink-2); }}
a.headerlink {{ display: none; }}

p {{ margin: 0 0 16px; }}
a {{ color: var(--accent); text-decoration: none; border-bottom: 1px dashed currentColor; }}
a:hover {{ border-bottom-style: solid; }}
strong {{ font-weight: 700; }}
hr {{ border: none; border-top: 1px solid var(--line); margin: 40px 0; }}

ul, ol {{ padding-inline-start: 26px; margin: 0 0 18px; }}
li {{ margin-bottom: 6px; }}
li::marker {{ color: var(--accent); font-weight: 700; }}

/* ---------- نقل‌قول ---------- */
blockquote {{
  margin: 22px 0;
  padding: 14px 20px;
  background: var(--card);
  border-inline-start: 4px solid var(--accent);
  border-radius: var(--radius);
  color: var(--ink-2);
  box-shadow: 0 1px 3px rgb(24 36 32 / 6%);
}}
blockquote p:last-child {{ margin-bottom: 0; }}

/* ---------- کد ---------- */
code, pre, kbd, samp {{
  font-family: "JetBrains Mono", "Consolas", monospace;
  direction: ltr;
  unicode-bidi: isolate;
}}
p code, li code, td code, h2 code, h3 code, h4 code, blockquote code {{
  background: #f0ebe2;
  color: var(--amber);
  font-size: .82em;
  padding: 2px 7px;
  border-radius: 6px;
  border: 1px solid #e4dccd;
}}
pre, .codehilite {{
  direction: ltr;
  text-align: left;
}}
.codehilite {{
  background: var(--code-bg);
  border-radius: var(--radius);
  margin: 22px 0;
  overflow: hidden;
}}
.codehilite pre {{
  margin: 0;
  padding: 18px 22px;
  overflow-x: auto;
  color: var(--code-fg);
  font-size: 13.5px;
  line-height: 1.8;
}}
pre:not(.codehilite pre) {{
  background: var(--code-bg);
  color: var(--code-fg);
  border-radius: var(--radius);
  padding: 18px 22px;
  overflow-x: auto;
  font-size: 13.5px;
  line-height: 1.8;
  margin: 22px 0;
}}

/* ---------- جدول ---------- */
.table-scroll {{ overflow-x: auto; margin: 22px 0; }}
table {{
  border-collapse: collapse;
  width: 100%;
  background: var(--card);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: 0 1px 4px rgb(24 36 32 / 8%);
  font-size: 15px;
}}
thead th {{
  background: var(--ink);
  color: #fff;
  font-weight: 700;
  padding: 11px 16px;
  text-align: start;
  white-space: nowrap;
}}
tbody td {{
  padding: 10px 16px;
  border-top: 1px solid var(--line);
  vertical-align: top;
}}
tbody tr:nth-child(even) {{ background: #f2f6f4; }}

img {{ max-width: 100%; border-radius: var(--radius); }}

/* ---------- فهرست مطالب ---------- */
nav.toc {{
  position: sticky;
  top: 28px;
  align-self: start;
  max-height: calc(100vh - 56px);
  overflow-y: auto;
  padding: 18px 20px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  font-size: 13.5px;
  line-height: 1.9;
}}
nav.toc .toc-title {{
  font-weight: 800;
  font-size: 13px;
  color: var(--accent);
  letter-spacing: .05em;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--accent);
}}
nav.toc ul {{ list-style: none; margin: 0; padding: 0; }}
nav.toc li {{ margin: 0; }}
nav.toc li.lv2 {{ padding-inline-start: 12px; }}
nav.toc li.lv3 {{ padding-inline-start: 26px; font-size: 12.5px; }}
nav.toc a {{
  display: block;
  color: var(--ink-2);
  border: none;
  border-inline-start: 2px solid transparent;
  padding: 3px 10px;
  border-radius: 0;
}}
nav.toc a:hover {{ color: var(--ink); }}
nav.toc a.active {{
  color: var(--accent);
  font-weight: 700;
  border-inline-start-color: var(--accent);
  background: var(--accent-2);
}}

/* ---------- پاورقی سند ---------- */
footer.doc {{
  margin-top: 64px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
  color: var(--ink-2);
  font-size: 12.5px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}}

/* ---------- موبایل و چاپ ---------- */
@media (max-width: 900px) {{
  .wrap {{ grid-template-columns: 1fr; gap: 28px; padding: 20px 16px 64px; }}
  nav.toc {{ position: static; max-height: none; order: -1; }}
}}
@media print {{
  nav.toc, footer.doc {{ display: none; }}
  .wrap {{ display: block; max-width: none; padding: 0; }}
  body {{ background: #fff; font-size: 12pt; }}
  .codehilite, pre {{ box-shadow: none; }}
}}
@media (prefers-reduced-motion: reduce) {{
  html {{ scroll-behavior: auto; }}
}}

/* ---------- Pygments (روی زمینه تیره) ---------- */
{pygments_css}
</style>
</head>
<body>
<div class="wrap">
<main>
  <header class="doc">
    <span class="kicker">یادداشت فنی</span>
    <h1>{title}</h1>
  </header>
  {body}
  <footer class="doc">
    <span>منبع: {source_name}</span>
    <span>تولیدشده با note2html</span>
  </footer>
</main>
{toc_html}
</div>
<script>
// هایلایت آیتم فعال فهرست هنگام اسکرول
(function () {{
  const links = document.querySelectorAll("nav.toc a[href^='#']");
  if (!links.length || !("IntersectionObserver" in window)) return;
  const map = new Map();
  links.forEach(a => {{
    const el = document.getElementById(decodeURIComponent(a.hash.slice(1)));
    if (el) map.set(el, a);
  }});
  let current = null;
  const io = new IntersectionObserver(entries => {{
    entries.forEach(e => {{
      if (e.isIntersecting) {{
        if (current) current.classList.remove("active");
        current = map.get(e.target);
        if (current) current.classList.add("active");
      }}
    }});
  }}, {{ rootMargin: "0px 0px -75% 0px" }});
  map.forEach((_, el) => io.observe(el));
}})();
</script>
</body>
</html>
"""

MD_EXTENSIONS = [
    "extra",          # جدول، کدبلاک fenced، تعریف‌ها و ...
    "sane_lists",
    "toc",
    "codehilite",
    "admonition",
]
MD_EXT_CONFIGS = {
    "toc": {"permalink": False, "toc_depth": "2-3"},
    "codehilite": {"guess_lang": False, "linenums": False},
}


def convert(md_path: Path, out_path: Path, title_override: str | None) -> Path:
    md_text = md_path.read_text(encoding="utf-8")
    direction = detect_direction(md_text)
    lang = "fa" if direction == "rtl" else "en"
    title = title_override or extract_title(md_text, md_path.stem)

    # حذف H1 اول از بدنه چون در سربرگ سند نمایش داده می‌شود
    body_md = re.sub(r"^#\s+.+?\n", "", md_text, count=1, flags=re.MULTILINE)

    md = markdown.Markdown(extensions=MD_EXTENSIONS, extension_configs=MD_EXT_CONFIGS)
    body_html = md.convert(body_md)

    # جدول‌ها را در ظرف اسکرول افقی می‌گذاریم تا در موبایل نشکنند
    body_html = body_html.replace("<table>", '<div class="table-scroll"><table>')
    body_html = body_html.replace("</table>", "</table></div>")

    toc_list = build_toc(getattr(md, "toc_tokens", []))
    toc_html = (
        f'<nav class="toc"><div class="toc-title">فهرست مطالب</div>{toc_list}</nav>'
        if toc_list else ""
    )

    pygments_css = HtmlFormatter(style="monokai").get_style_defs(".codehilite")
    # زمینه‌ی pygments را با زمینه‌ی خودمان جایگزین می‌کنیم
    pygments_css = re.sub(r"background:\s*#272822;?", "background: var(--code-bg);", pygments_css)

    html = TEMPLATE.format(
        lang=lang,
        direction=direction,
        title=title,
        body=body_html,
        toc_html=toc_html,
        source_name=md_path.name,
        pygments_css=pygments_css,
    )
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="تبدیل نوت مارک‌داون به HTML زیبا با پشتیبانی فارسی")
    ap.add_argument("input", help="مسیر فایل .md ورودی")
    ap.add_argument("-o", "--output", help="مسیر فایل .html خروجی (پیش‌فرض: کنار ورودی)")
    ap.add_argument("--title", help="عنوان دلخواه به‌جای H1 اول سند")
    args = ap.parse_args()

    md_path = Path(args.input)
    if not md_path.is_file():
        sys.exit(f"فایل پیدا نشد: {md_path}")

    out_path = Path(args.output) if args.output else md_path.with_suffix(".html")
    result = convert(md_path, out_path, args.title)
    print(f"✓ ساخته شد: {result}")


if __name__ == "__main__":
    main()
