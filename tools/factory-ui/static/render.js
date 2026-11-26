// Escape-first markdown renderer. It builds DOM nodes and sets text with
// text nodes only, so no HTML in a file ever becomes an element. Headings,
// lists, tables, code, quotes, emphasis and links; a link keeps its href
// only for http:, https: or an in-page "#" target.
(function (root) {
  "use strict";

  function el(tag, cls) {
    const e = root.document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }

  function text(s) {
    return root.document.createTextNode(s);
  }

  function safeHref(href) {
    const h = String(href).trim();
    if (/^https?:/i.test(h) || h.startsWith("#")) return h;
    return null;
  }

  const INLINE =
    /(`+)([^`]*?)\1|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|\[([^\]]+)\]\(([^)\s]+)\)/;

  function inline(s, parent) {
    let rest = s;
    while (rest.length) {
      const m = INLINE.exec(rest);
      if (!m) {
        parent.appendChild(text(rest));
        break;
      }
      if (m.index) parent.appendChild(text(rest.slice(0, m.index)));
      if (m[1]) {
        const c = el("code");
        c.textContent = m[2];
        parent.appendChild(c);
      } else if (m[3] !== undefined) {
        const b = el("strong");
        inline(m[3], b);
        parent.appendChild(b);
      } else if (m[4] !== undefined) {
        const i = el("em");
        inline(m[4], i);
        parent.appendChild(i);
      } else {
        const href = safeHref(m[6]);
        if (href) {
          const a = el("a");
          a.setAttribute("href", href);
          a.setAttribute("rel", "noreferrer noopener");
          inline(m[5], a);
          parent.appendChild(a);
        } else {
          parent.appendChild(text(m[0]));
        }
      }
      rest = rest.slice(m.index + m[0].length);
    }
  }

  const FENCE = /^\s*(`{3,}|~{3,})/;
  const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
  const QUOTE = /^\s*>\s?/;
  const ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const TABLE_ROW = /^\s*\|.*\|\s*$/;
  const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  function isBlockStart(line, next) {
    return (
      FENCE.test(line) ||
      HEADING.test(line) ||
      RULE.test(line) ||
      QUOTE.test(line) ||
      ITEM.test(line) ||
      (TABLE_ROW.test(line) && next !== undefined && TABLE_SEP.test(next))
    );
  }

  function cells(line) {
    return line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim());
  }

  function blocks(lines, out) {
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*$/.test(line)) {
        i++;
        continue;
      }
      let m = FENCE.exec(line);
      if (m) {
        const fence = m[1];
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) {
          body.push(lines[i]);
          i++;
        }
        i++;
        const pre = el("pre");
        const code = el("code");
        code.textContent = body.join("\n");
        pre.appendChild(code);
        out.appendChild(pre);
        continue;
      }
      m = HEADING.exec(line);
      if (m) {
        const hd = el("h" + m[1].length);
        inline(m[2], hd);
        out.appendChild(hd);
        i++;
        continue;
      }
      if (RULE.test(line)) {
        out.appendChild(el("hr"));
        i++;
        continue;
      }
      if (QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && QUOTE.test(lines[i])) {
          inner.push(lines[i].replace(QUOTE, ""));
          i++;
        }
        const q = el("blockquote");
        blocks(inner, q);
        out.appendChild(q);
        continue;
      }
      m = ITEM.exec(line);
      if (m) {
        const ordered = /\d/.test(m[2]);
        const list = el(ordered ? "ol" : "ul");
        while (i < lines.length) {
          const it = ITEM.exec(lines[i]);
          if (!it || /\d/.test(it[2]) !== ordered) break;
          const li = el("li");
          if (it[1].length >= 2) li.className = "nested";
          let body = it[3];
          i++;
          while (
            i < lines.length &&
            /^\s{2,}\S/.test(lines[i]) &&
            !ITEM.test(lines[i])
          ) {
            body += " " + lines[i].trim();
            i++;
          }
          inline(body, li);
          list.appendChild(li);
        }
        out.appendChild(list);
        continue;
      }
      if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
        const table = el("table");
        const thead = el("thead");
        const hr = el("tr");
        for (const c of cells(line)) {
          const th = el("th");
          inline(c, th);
          hr.appendChild(th);
        }
        thead.appendChild(hr);
        table.appendChild(thead);
        const tbody = el("tbody");
        i += 2;
        while (i < lines.length && TABLE_ROW.test(lines[i])) {
          const tr = el("tr");
          for (const c of cells(lines[i])) {
            const td = el("td");
            inline(c, td);
            tr.appendChild(td);
          }
          tbody.appendChild(tr);
          i++;
        }
        table.appendChild(tbody);
        out.appendChild(table);
        continue;
      }
      const para = [line.trim()];
      i++;
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !isBlockStart(lines[i], lines[i + 1])
      ) {
        para.push(lines[i].trim());
        i++;
      }
      const p = el("p");
      inline(para.join(" "), p);
      out.appendChild(p);
    }
  }

  function markdown(src) {
    const out = el("div", "md");
    blocks(String(src).replace(/\r\n?/g, "\n").split("\n"), out);
    return out;
  }

  function raw(src) {
    const pre = el("pre", "raw");
    pre.textContent = String(src);
    return pre;
  }

  root.FactoryRender = { markdown, raw, safeHref };
})(typeof window !== "undefined" ? window : globalThis);
