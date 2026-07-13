/* lab-client.js — موتور عمومی «آزمایشگاه تعاملی» برای تمام فصل‌های ژورنال.
 *
 * این فایل هیچ کد پایتونی اجرا نمی‌کند و هیچ eval/exec ندارد. فقط:
 *   1) فرم ورودی‌های مجاز هر آزمایش را از روی یک config اعلانی می‌سازد،
 *   2) قبل از ارسال، مقدارها را با schema همان config اعتبارسنجی می‌کند،
 *   3) یک درخواست POST به سرور محلی serve_journal.py می‌زند (endpoint نسبی)،
 *   4) پاسخ JSON را در چند سطح (خلاصه/مراحل/متریک/JSON خام) نمایش می‌دهد.
 *
 * اگر serve_journal.py اجرا نشده باشد (یا صفحه با file:// باز شده باشد)،
 * fetch شکست می‌خورد و به‌جای صفحهٔ خراب، یک پیام راهنما نمایش داده می‌شود.
 */
(function (global) {
  "use strict";

  var DEFAULT_ENDPOINT = "./api/run";

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k === "html") node.innerHTML = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function fieldDefault(f) {
    if (f.type === "candles") return f.default || [];
    return f.default;
  }

  function validateField(f, raw) {
    // برمی‌گرداند {ok, value, error}
    if (f.type === "int" || f.type === "float" || f.type === "range") {
      if (raw === "" || raw === null || raw === undefined || isNaN(Number(raw))) {
        return { ok: false, error: "این مقدار باید عدد باشد." };
      }
      var num = Number(raw);
      if (f.type === "int" && !Number.isInteger(num)) {
        return { ok: false, error: "این مقدار باید عدد صحیح باشد." };
      }
      if (typeof f.min === "number" && num < f.min) {
        return { ok: false, error: "مقدار نباید کمتر از " + f.min + " باشد." };
      }
      if (typeof f.max === "number" && num > f.max) {
        return { ok: false, error: "مقدار نباید بیشتر از " + f.max + " باشد." };
      }
      return { ok: true, value: num };
    }
    if (f.type === "checkbox") {
      return { ok: true, value: !!raw };
    }
    if (f.type === "select" || f.type === "radio" || f.type === "scenario") {
      var options = (f.options || []).map(function (o) { return String(o.value); });
      if (options.indexOf(String(raw)) === -1) {
        return { ok: false, error: "گزینهٔ نامعتبر." };
      }
      return { ok: true, value: raw };
    }
    if (f.type === "candles") {
      if (!Array.isArray(raw) || raw.length === 0) {
        return { ok: false, error: "حداقل یک کندل لازم است." };
      }
      if (raw.length > (f.maxRows || 300)) {
        return { ok: false, error: "تعداد کندل از حد مجاز (" + (f.maxRows || 300) + ") بیشتر است." };
      }
      for (var i = 0; i < raw.length; i++) {
        var c = raw[i];
        var o = Number(c.open), h = Number(c.high), l = Number(c.low), cl = Number(c.close);
        if ([o, h, l, cl].some(isNaN)) return { ok: false, error: "کندل ردیف " + (i + 1) + ": مقادیر باید عددی باشند." };
        if (h < l) return { ok: false, error: "کندل ردیف " + (i + 1) + ": high نمی‌تواند کمتر از low باشد." };
        if (o > h || o < l || cl > h || cl < l) {
          return { ok: false, error: "کندل ردیف " + (i + 1) + ": open/close باید داخل بازهٔ [low, high] باشند." };
        }
      }
      return { ok: true, value: raw };
    }
    return { ok: true, value: raw };
  }

  function renderField(f, wrap) {
    var fieldWrap = el("div", { class: "lab-field", "data-field": f.name });
    var label = el("label", { text: f.label });
    var fname = el("div", { class: "fname", text: f.name });
    fieldWrap.appendChild(label);
    fieldWrap.appendChild(fname);

    var input;
    if (f.type === "select" || f.type === "scenario") {
      input = el("select", {});
      (f.options || []).forEach(function (o) {
        var opt = el("option", { value: o.value, text: o.label });
        input.appendChild(opt);
      });
      if (f.default !== undefined) input.value = f.default;
    } else if (f.type === "radio") {
      input = el("div", { class: "radio-group" });
      (f.options || []).forEach(function (o, idx) {
        var id = f.name + "_" + idx;
        var r = el("input", { type: "radio", name: f.name, id: id, value: o.value });
        if (String(o.value) === String(f.default)) r.checked = true;
        var l = el("label", { for: id, text: " " + o.label + "  " });
        input.appendChild(r); input.appendChild(l);
      });
    } else if (f.type === "checkbox") {
      input = el("input", { type: "checkbox" });
      input.checked = !!f.default;
    } else if (f.type === "range") {
      input = el("input", { type: "range", min: f.min, max: f.max, step: f.step || 1 });
      input.value = f.default;
      var valSpan = el("span", { class: "range-value", text: String(f.default) });
      input.addEventListener("input", function () { valSpan.textContent = input.value; });
      fieldWrap.appendChild(valSpan);
    } else if (f.type === "int" || f.type === "float") {
      input = el("input", { type: "number", min: f.min, max: f.max, step: f.step || (f.type === "int" ? 1 : "any") });
      input.value = f.default;
    } else if (f.type === "candles") {
      input = renderCandleTable(f);
    } else {
      input = el("input", { type: "text" });
      input.value = f.default || "";
    }
    if (f.type !== "candles" && f.type !== "radio") fieldWrap.appendChild(input);
    else fieldWrap.appendChild(input);

    if (f.help) fieldWrap.appendChild(el("div", { class: "small", text: f.help }));
    var err = el("div", { class: "err-msg" });
    fieldWrap.appendChild(err);
    fieldWrap._input = input;
    fieldWrap._errNode = err;
    return fieldWrap;
  }

  function renderCandleTable(f) {
    var rows = f.default || [];
    var table = el("table", { class: "lab-candles-table" });
    var thead = el("tr", {}, ["time", "open", "high", "low", "close"].map(function (h) {
      return el("th", { text: h });
    }));
    table.appendChild(el("thead", {}, [thead]));
    var tbody = el("tbody");
    rows.forEach(function (r) {
      var tr = el("tr");
      ["time", "open", "high", "low", "close"].forEach(function (k) {
        var td = el("td");
        var inp = el("input", { value: r[k] !== undefined ? r[k] : "" });
        inp.dataset.k = k;
        td.appendChild(inp);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    table._getValue = function () {
      var out = [];
      tbody.querySelectorAll("tr").forEach(function (tr) {
        var row = {};
        tr.querySelectorAll("input").forEach(function (inp) { row[inp.dataset.k] = inp.value; });
        out.push(row);
      });
      return out;
    };
    return table;
  }

  function readFieldValue(f, fieldWrap) {
    var input = fieldWrap._input;
    if (f.type === "checkbox") return input.checked;
    if (f.type === "radio") {
      var checked = fieldWrap.querySelector('input[type=radio]:checked');
      return checked ? checked.value : f.default;
    }
    if (f.type === "candles") return input._getValue();
    return input.value;
  }

  function setFieldError(fieldWrap, msg) {
    if (msg) {
      fieldWrap.classList.add("has-error");
      fieldWrap._errNode.textContent = msg;
    } else {
      fieldWrap.classList.remove("has-error");
      fieldWrap._errNode.textContent = "";
    }
  }

  function collectAndValidate(fieldWraps, fields) {
    var inputs = {}, ok = true;
    fields.forEach(function (f) {
      var wrap = fieldWraps[f.name];
      var raw = readFieldValue(f, wrap);
      var v = validateField(f, raw);
      if (!v.ok) { setFieldError(wrap, v.error); ok = false; }
      else { setFieldError(wrap, null); inputs[f.name] = v.value; }
    });
    return { ok: ok, inputs: inputs };
  }

  function renderOutput(outEl, resp, timedOut) {
    outEl.innerHTML = "";
    if (timedOut) {
      outEl.appendChild(el("div", { class: "server-hint", text: "درخواست بیش از حد طول کشید و متوقف شد (Timeout)." }));
      return;
    }
    if (!resp || resp.success === false) {
      var msg = (resp && resp.error) ? resp.error : "اجرای آزمایش ناموفق بود.";
      outEl.appendChild(el("div", { class: "server-hint", text: msg }));
      return;
    }
    var result = resp.result || {};
    var summary = result.summary || "اجرا با موفقیت انجام شد.";
    outEl.appendChild(el("div", { class: "lab-result-summary", text: summary }));

    if (Array.isArray(resp.steps) && resp.steps.length) {
      var ol = el("ol", { class: "lab-steps" });
      resp.steps.forEach(function (s) { ol.appendChild(el("li", { text: s })); });
      outEl.appendChild(el("div", {}, [el("strong", { text: "مراحل اجرا:" }), ol]));
    }

    if (resp.metrics && Object.keys(resp.metrics).length) {
      var mrow = el("div", { class: "lab-metrics" });
      Object.keys(resp.metrics).forEach(function (k) {
        var chip = el("div", { class: "lab-metric" });
        chip.appendChild(document.createTextNode(k + ": "));
        chip.appendChild(el("b", { text: String(resp.metrics[k]) }));
        mrow.appendChild(chip);
      });
      outEl.appendChild(mrow);
    }

    if (Array.isArray(resp.warnings) && resp.warnings.length) {
      var wbox = el("div", { class: "lab-warnings" });
      resp.warnings.forEach(function (w) { wbox.appendChild(el("div", { text: "⚠ " + w })); });
      outEl.appendChild(wbox);
    }

    var jsonDetails = el("details", { class: "lab-json" });
    jsonDetails.appendChild(el("summary", { text: "خروجی خام JSON" }));
    var pre = el("pre");
    var code = el("code");
    code.textContent = JSON.stringify(resp, null, 2);
    pre.appendChild(code);
    jsonDetails.appendChild(pre);
    outEl.appendChild(jsonDetails);

    if (typeof resp.execution_time_ms === "number") {
      outEl.appendChild(el("div", { class: "lab-exectime", text: "زمان اجرا: " + resp.execution_time_ms.toFixed(2) + " میلی‌ثانیه" }));
    }
  }

  function callApi(endpoint, experimentId, inputs) {
    var controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var timeoutMs = 8000;
    var timer = controller ? setTimeout(function () { controller.abort(); }, timeoutMs) : null;
    return fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ experiment_id: experimentId, inputs: inputs }),
      signal: controller ? controller.signal : undefined
    }).then(function (r) {
      if (timer) clearTimeout(timer);
      return r.json();
    }).catch(function (e) {
      if (timer) clearTimeout(timer);
      throw e;
    });
  }

  function mount(config) {
    var container = document.getElementById(config.containerId);
    if (!container) return;
    var endpoint = config.endpoint || DEFAULT_ENDPOINT;

    var head = el("div", { class: "lab-title" }, [
      document.createTextNode(config.title || "آزمایش تعاملی"),
      el("span", { class: "lab-badge", text: "تعاملی" })
    ]);
    container.appendChild(head);
    if (config.goal) container.appendChild(el("div", { class: "lab-goal", text: config.goal }));
    if (config.sourceRef) {
      container.appendChild(el("div", {
        class: "code-path",
        text: config.sourceRef
      }));
    }

    var grid = el("div", { class: "lab-grid" });
    var inputsBox = el("div", { class: "lab-inputs" });
    var fieldWraps = {};
    (config.fields || []).forEach(function (f) {
      var w = renderField(f);
      fieldWraps[f.name] = w;
      inputsBox.appendChild(w);
    });

    var actions = el("div", { class: "lab-actions" });
    var runBtn = el("button", { class: "lab-btn run", type: "button", text: "اجرا" });
    var resetBtn = el("button", { class: "lab-btn reset", type: "button", text: "بازنشانی" });
    actions.appendChild(runBtn);
    actions.appendChild(resetBtn);
    inputsBox.appendChild(actions);

    var outputBox = el("div", { class: "lab-output" });
    outputBox.appendChild(el("div", {
      class: "placeholder",
      text: "برای مشاهدهٔ نتیجه، مقادیر را تنظیم کنید و «اجرا» را بزنید."
    }));

    grid.appendChild(inputsBox);
    grid.appendChild(outputBox);
    container.appendChild(grid);

    resetBtn.addEventListener("click", function () {
      container.innerHTML = "";
      mount(config);
    });

    runBtn.addEventListener("click", function () {
      var v = collectAndValidate(fieldWraps, config.fields || []);
      if (!v.ok) return;
      runBtn.disabled = true;
      outputBox.innerHTML = '<div class="placeholder">در حال اجرا…</div>';
      callApi(endpoint, config.experimentId, v.inputs).then(function (resp) {
        renderOutput(outputBox, resp, false);
      }).catch(function () {
        outputBox.innerHTML = "";
        outputBox.appendChild(el("div", {
          class: "server-hint",
          text: "برای اجرای تعاملی، ابتدا project_learning_journal/serve_journal.py را اجرا کنید " +
            "و این صفحه را از آدرس http://127.0.0.1:8766 باز کنید. در حالت فعلی (بدون سرور یا با خطای اتصال) " +
            "فقط می‌توانید متن و کدها را مطالعه کنید."
        }));
      }).finally(function () {
        runBtn.disabled = false;
      });
    });
  }

  global.JournalLab = { mount: mount, validateField: validateField };
})(window);
