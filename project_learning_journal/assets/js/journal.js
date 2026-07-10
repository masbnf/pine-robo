/* journal.js — رفتارهای عمومی صفحات ژورنال: کدهایلایت آفلاین، دکمهٔ کپی، TOC.
   بدون هیچ وابستگی خارجی و بدون نیاز به اینترنت. */
(function () {
  "use strict";

  var PY_KEYWORDS = ("False None True and as assert async await break class continue def del elif else " +
    "except finally for from global if import in is lambda nonlocal not or pass raise return try " +
    "while with yield self").split(" ");

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // یک هایلایتر بسیار سبک و آفلاین برای پایتون: کامنت، رشته، عدد، کلیدواژه، self.
  // هدف زیبایی خوانایی است، نه یک پارسر دقیق زبان.
  function highlightPython(code) {
    var lines = code.split("\n");
    var out = lines.map(function (line) {
      var i = 0, buf = "", n = line.length;
      var result = "";
      // کامنت کل خط باقیمانده
      var commentIdx = -1;
      var inStr = false, strCh = "";
      for (i = 0; i < n; i++) {
        var ch = line[i];
        if (!inStr && (ch === "'" || ch === '"')) { inStr = true; strCh = ch; continue; }
        if (inStr && ch === strCh) { inStr = false; continue; }
        if (!inStr && ch === "#") { commentIdx = i; break; }
      }
      var code_part = commentIdx >= 0 ? line.slice(0, commentIdx) : line;
      var comment_part = commentIdx >= 0 ? line.slice(commentIdx) : "";

      // توکن‌سازی ساده روی code_part با regex ترکیبی
      var tokenRe = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\b\d+\.?\d*\b)|(\bdef\s+)([a-zA-Z_]\w*)|(\bself\b)|(\b(?:)" + "" + "\b)/g;
      // ساده‌تر: پردازش دستی توکن به توکن
      var re = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\d+\.\d+|\d+)|([a-zA-Z_]\w*)|(\s+)|(.)/g;
      var m, html = "";
      var prevWasDef = false;
      while ((m = re.exec(code_part)) !== null) {
        if (m[1]) { html += '<span class="tok-str">' + escapeHtml(m[1]) + "</span>"; prevWasDef = false; }
        else if (m[2]) { html += '<span class="tok-num">' + escapeHtml(m[2]) + "</span>"; prevWasDef = false; }
        else if (m[3]) {
          var w = m[3];
          if (w === "self" || w === "cls") { html += '<span class="tok-self">' + w + "</span>"; }
          else if (PY_KEYWORDS.indexOf(w) !== -1) {
            html += '<span class="tok-kw">' + w + "</span>";
            prevWasDef = (w === "def" || w === "class");
          } else if (prevWasDef) {
            html += '<span class="tok-def">' + w + "</span>";
            prevWasDef = false;
          } else {
            html += escapeHtml(w);
          }
        } else if (m[4]) { html += m[4]; }
        else if (m[5]) { html += escapeHtml(m[5]); }
      }
      if (comment_part) html += '<span class="tok-com">' + escapeHtml(comment_part) + "</span>";
      return html;
    });
    return out.join("\n");
  }

  function initCodeBlocks() {
    var blocks = document.querySelectorAll("pre code.language-python");
    blocks.forEach(function (block) {
      var raw = block.textContent;
      block.innerHTML = highlightPython(raw);
    });

    // دکمهٔ کپی برای همهٔ بلوک‌های pre
    document.querySelectorAll("pre").forEach(function (pre) {
      if (pre.querySelector(".copy-btn")) return;
      var btn = document.createElement("button");
      btn.className = "copy-btn";
      btn.textContent = "کپی";
      btn.style.cssText = "position:absolute;top:8px;left:8px;font-size:11px;padding:2px 8px;" +
        "background:#1b2028;border:1px solid #2a3140;color:#9aa5b8;border-radius:6px;cursor:pointer;";
      pre.style.position = "relative";
      pre.appendChild(btn);
      btn.addEventListener("click", function () {
        var text = pre.querySelector("code") ? pre.querySelector("code").textContent : pre.textContent;
        if (navigator.clipboard) {
          navigator.clipboard.writeText(text).then(function () {
            btn.textContent = "کپی شد";
            setTimeout(function () { btn.textContent = "کپی"; }, 1200);
          });
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initCodeBlocks();
  });
})();
