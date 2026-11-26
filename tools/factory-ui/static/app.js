// The factory UI: hash routes, GET requests only, and views built as DOM
// nodes. File text reaches the page through text nodes or render.js.
(function (root) {
  "use strict";

  const FILE_REFRESH_MS = 15000;
  const FILE_VIEWS = ["jobs", "job", "verifier", "parallel"];

  // ------------------------------------------------------------ helpers --

  function h(tag, attrs, ...kids) {
    const e = root.document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      e.appendChild(
        typeof kid === "object" ? kid : root.document.createTextNode(String(kid)),
      );
    }
    return e;
  }

  function dash(v) {
    return v === null || v === undefined || v === "" ? "-" : String(v);
  }

  function fmtAge(s) {
    if (s === null || s === undefined) return "-";
    const sec = Math.max(0, Math.floor(s));
    if (sec < 60) return sec + "s";
    const min = Math.floor(sec / 60);
    if (min < 60) return min + "m";
    const hr = Math.floor(min / 60);
    if (hr < 24) return hr + "h " + (min % 60) + "m";
    return Math.floor(hr / 24) + "d " + (hr % 24) + "h";
  }

  function fmtTime(epoch) {
    if (!epoch) return "-";
    return new Date(epoch * 1000).toLocaleString();
  }

  function compact(v) {
    if (v === null || v === undefined) return "-";
    return typeof v === "object" ? JSON.stringify(v) : String(v);
  }

  const VERDICT_CLASS = {
    PASS: "v-pass",
    APPROVE: "v-pass",
    CHANGES: "v-changes",
    FAIL: "v-changes",
    INCONCLUSIVE: "v-inconclusive",
    ESCALATE: "v-escalated",
    merged: "v-pass",
    "pr-open": "v-pass",
    escalated: "v-escalated",
    blocked: "v-warn",
    closed: "v-muted",
    abandoned: "v-muted",
  };

  function badge(value) {
    const cls = VERDICT_CLASS[value] || "v-active";
    return h("span", { class: "badge " + cls }, dash(value));
  }

  function table(headers, rows) {
    return h(
      "table",
      null,
      h("thead", null, h("tr", null, headers.map((x) => h("th", null, x)))),
      h(
        "tbody",
        null,
        rows.map((cells) => h("tr", null, cells.map((c) => h("td", null, c)))),
      ),
    );
  }

  function keyValues(obj) {
    return table(
      ["Field", "Value"],
      Object.entries(obj || {}).map(([k, v]) => [k, h("span", { class: "mono" }, compact(v))]),
    );
  }

  function asOf(cmd) {
    return h(
      "p",
      { class: "asof" },
      "as of " +
        fmtTime(cmd.as_of) +
        ", next refresh possible at " +
        fmtTime(cmd.next_refresh_at),
    );
  }

  function commandStatus(cmd) {
    if (cmd.status === "answer") return null;
    return h(
      "p",
      { class: "error" },
      cmd.status === "helper bug" ? "helper bug (exit 6)" : cmd.status,
      cmd.error ? ": " + cmd.error : "",
      cmd.stderr ? h("pre", { class: "raw" }, cmd.stderr) : null,
    );
  }

  function refreshButton(actions) {
    const b = h("button", { type: "button" }, "Refresh");
    if (actions && actions.refresh) b.addEventListener("click", actions.refresh);
    return h("div", { class: "controls" }, b);
  }

  // -------------------------------------------------------------- views --

  function jobsView(data) {
    const rows = data.rows || [];
    const cached = rows.filter((r) => r.group === "cached");
    const noCache = rows.filter((r) => r.group === "no-cache");
    const unreadable = rows.filter((r) => r.group === "unreadable");
    return h(
      "section",
      null,
      h("h1", null, "Jobs"),
      h(
        "p",
        { class: "banner" },
        "This list is this Mac's cache: each job as this Mac last read it " +
          "from Linear (job.json). The live list is F list, run in a terminal.",
      ),
      table(
        ["Job", "Phase", "Round", "PR", "Home", "Age", "Linear", "Lease (from cache)", "Cache age"],
        cached.map((r) => [
          h(
            "div",
            null,
            h("a", { href: "#/jobs/" + r.id }, r.id),
            h("div", { class: "title" }, r.title || ""),
          ),
          h(
            "div",
            null,
            badge(r.phase),
            r.phase === "escalated" && r.kind ? " " + r.kind : null,
            r.hold ? h("div", { class: "hold" }, "hold: " + r.hold) : null,
          ),
          dash(r.round) + "/" + dash(r.max_rounds),
          r.pr ? "#" + r.pr : "-",
          dash(r.home),
          fmtAge(r.age_s),
          dash(r.linear),
          r.lease ? r.lease.host + ", beat " + fmtAge(r.lease_beat_age_s) + " ago" : "-",
          fmtAge(r.cache_age_s),
        ]),
      ),
      noCache.length
        ? [
            h("h2", null, "Jobs not in cache"),
            h("ul", null, noCache.map((r) => h("li", null, r.id))),
          ]
        : null,
      unreadable.length
        ? [
            h("h2", null, "Unreadable job.json"),
            h("ul", null, unreadable.map((r) => h("li", null, r.id + ": unreadable"))),
          ]
        : null,
    );
  }

  function verifyLine(v) {
    if (!v) return null;
    return h(
      "p",
      null,
      "Verify (" + (v.field === "verify_shadow" ? "shadow" : v.field ? "gate" : "file only") + "): ",
      badge(v.verdict),
      " why: " + dash(v.why) + ", mode: " + dash(v.mode) + ", diff-finding: " + (v.diff_finding ? "yes " : "no"),
      v.diff_finding ? h("span", { class: "badge v-warn" }, "diff-finding") : null,
    );
  }

  function artifactBlock(a, data) {
    const actions = data.actions || {};
    const open = (data.open || []).includes(a.key);
    const raw = (data.raw || []).includes(a.key);
    const loaded = (data.texts || {})[a.key];
    if (!a.present) {
      return h(
        "div",
        { class: "artifact" },
        h("strong", null, a.key),
        h("span", { class: "missing" }, " not written yet"),
      );
    }
    const toggle = h("button", { type: "button" }, raw ? "Show rendered" : "Show raw");
    if (actions.raw) toggle.addEventListener("click", () => actions.raw(a.key));
    let body = null;
    if (open) {
      body = loaded
        ? [h("div", { class: "controls" }, toggle), raw ? root.FactoryRender.raw(loaded.text) : root.FactoryRender.markdown(loaded.text)]
        : h("p", { class: "note" }, "loading");
    }
    const details = h("details", { class: "artifact", open: open }, h("summary", null, a.key), body);
    if (actions.open) {
      details.addEventListener("toggle", () => actions.open(a.key, details.open));
    }
    return details;
  }

  function jobView(data) {
    const d = data.detail;
    const r = d.row || {};
    return h(
      "section",
      null,
      h("p", null, h("a", { href: "#/jobs" }, "All jobs")),
      h("h1", null, d.id),
      r.title ? h("p", { class: "title" }, r.title) : null,
      r.group === "unreadable" ? h("p", { class: "error" }, "job.json is unreadable") : null,
      r.group === "no-cache" ? h("p", { class: "note" }, "not in cache") : null,
      r.group === "cached"
        ? h(
            "p",
            null,
            badge(r.phase),
            " round " + dash(r.round) + "/" + dash(r.max_rounds) +
              ", PR " + (r.pr ? "#" + r.pr : "-") +
              ", home " + dash(r.home) +
              ", age " + fmtAge(r.age_s) +
              ", " + dash(r.linear) +
              ", cache age " + fmtAge(r.cache_age_s),
          )
        : null,
      h("h2", null, "Rounds"),
      d.rounds.length
        ? d.rounds.map((rd) =>
            h(
              "div",
              null,
              h("h3", null, "Round " + rd.round),
              table(
                ["Lens", "Recorded (job.json)", "File line 1", ""],
                rd.reviews.map((x) => [
                  x.lens,
                  x.state ? badge(x.state) : "-",
                  x.file ? badge(x.file) : "-",
                  x.mismatch ? h("span", { class: "badge v-warn" }, "mismatch") : "",
                ]),
              ),
              verifyLine(rd.verify),
            ),
          )
        : h("p", { class: "note" }, "no rounds yet"),
      h("h2", null, "Artifacts"),
      d.artifacts.map((a) => artifactBlock(a, data)),
      h("h2", null, "job.json summary"),
      h("p", { class: "note" }, "write_token, lease.session and docs are not shown."),
      h("h3", null, "This Mac's fields"),
      keyValues(d.summary.local),
      h("h3", null, "Cache (cache_at " + fmtTime(d.summary.cache_at) + ")"),
      keyValues(d.summary.cache),
    );
  }

  function flagsCell(row) {
    const out = [];
    for (const f of row.flags || []) out.push(f);
    if (row.flip) out.push("flip");
    if (row.overclaim) out.push("overclaim");
    if (row.revised) out.push("revised");
    return out.length ? out.join(", ") : "-";
  }

  function verifierView(data) {
    const bt = data.backtests || {};
    return h(
      "section",
      null,
      h("h1", null, "Verifier"),
      h(
        "p",
        null,
        "verify.mode: ",
        badge(data.mode.mode),
        data.mode.since ? " since " + data.mode.since : "",
      ),
      h("h2", null, "Backtests block"),
      h("p", { class: "note" }, "recomputed from this Mac's files (verify-backtest.jsonl), F stats key names"),
      table(
        ["Key", "Value"],
        Object.entries(bt).map(([k, v]) => [
          k,
          typeof v === "object" && v
            ? Object.entries(v).map(([a, b]) => a + " " + b).join(", ") || "-"
            : String(v),
        ]),
      ),
      h("h2", null, "Backtest rows"),
      data.skipped ? h("p", { class: "note" }, data.skipped + " malformed line(s) skipped") : null,
      table(
        ["Job", "Verdict", "Why", "Flags", "Expect", "Match", "Owner verdict", "Owner note", "At", "Proof"],
        (data.rows || []).map((r) => [
          dash(r.job),
          badge(r.verdict),
          dash(r.why),
          flagsCell(r),
          dash(r.expect),
          compact(r.match),
          dash(r.owner_verdict),
          dash(r.owner_note),
          dash(r.at),
          dash(r.proof),
        ]),
      ),
      h("h2", null, "Verify runs per round"),
      (data.rounds || []).length
        ? table(
            ["Job", "Round", "Mode", "Verdict", "Why", "diff-finding"],
            data.rounds.map((r) => [
              h("a", { href: "#/jobs/" + r.job }, r.job),
              r.round,
              dash(r.mode) + (r.field === "verify_shadow" ? " (shadow)" : ""),
              badge(r.verdict),
              dash(r.why),
              r.diff_finding ? "yes" : "no",
            ]),
          )
        : h("p", { class: "note" }, "no verify runs recorded"),
    );
  }

  function windowCells(w) {
    if (!w) return ["-", "-"];
    return [
      w.used_percentage === null || w.used_percentage === undefined ? "-" : w.used_percentage + "%",
      fmtTime(w.resets_at),
    ];
  }

  function budgetView(data) {
    const cmd = data.command || {};
    const a = data.answer;
    const reading = (data.readings || []).find((r) => r.session === data.session);
    const windows = (a && a.usage) || (reading && reading.windows) || {};
    return h(
      "section",
      null,
      h("h1", null, "Budget"),
      asOf(cmd),
      refreshButton(data.actions),
      commandStatus(cmd),
      a
        ? [
            h("p", { class: "go" }, h("span", { class: "badge " + (a.go ? "v-pass" : "v-changes") }, a.go ? "GO" : "WAIT")),
            h("p", null, "why: " + dash(a.why)),
            a.wake_at ? h("p", null, "wake at " + fmtTime(a.wake_at)) : null,
            h("p", null, "reading: " + dash(a.reading) + (a.stale ? " (stale)" : "")),
            a.limits_error ? h("p", { class: "error" }, a.limits_error) : null,
          ]
        : null,
      h("p", { class: "note" }, "session " + dash(data.session) + " (newest usage reading)"),
      table(
        ["Window", "Used", "Resets", "Default limit"],
        Object.keys(data.limits || {}).map((w) => [w, ...windowCells(windows[w]), data.limits[w] + "%"]),
      ),
      h("h2", null, "Readings of the last 6 hours"),
      (data.readings || []).length
        ? table(
            ["Session", "Seen", "five_hour", "five_hour resets", "seven_day", "seven_day resets"],
            data.readings.map((r) => [
              r.session,
              fmtTime(r.seen_at) + (r.stale ? " (stale)" : ""),
              ...windowCells(r.windows.five_hour),
              ...windowCells(r.windows.seven_day),
            ]),
          )
        : h("p", { class: "note" }, "no readings"),
    );
  }

  function listOrNone(items) {
    return items && items.length
      ? h("ul", null, items.map((x) => h("li", null, compact(x))))
      : h("p", { class: "note" }, "none");
  }

  function healthView(data) {
    const cmd = data.command || {};
    const d = data.doctor || {};
    const v = d.verify || {};
    return h(
      "section",
      null,
      h("h1", null, "Health"),
      asOf(cmd),
      refreshButton(data.actions),
      commandStatus(cmd),
      h("h2", null, "Problems"),
      listOrNone(data.problems),
      data.doctor
        ? [
            h("h2", null, "Doctor"),
            table(
              ["Field", "Value"],
              [
                ["host", dash(d.host)],
                ["key file", dash(d.key_file)],
                ["key", dash(d.key)],
                ["Linear", dash(d.linear)],
                ["jobs in Linear", dash(d.jobs_in_linear)],
                ["verify mode", dash(v.mode) + (v.since ? " since " + v.since : "")],
                ["inbound blocked", compact(v.inbound_blocked)],
              ],
            ),
            h("h2", null, "Tools"),
            keyValues(v.tools),
            h("h2", null, "Leftovers"),
            keyValues(v.leftovers),
            h("h2", null, "Residuals"),
            keyValues(v.residuals),
          ]
        : null,
    );
  }

  function parallelView(data) {
    const mode = data.mode || {};
    return h(
      "section",
      null,
      h("h1", null, "Parallel jobs"),
      data.state === "dormant"
        ? h("p", { class: "banner" }, "Parallel jobs: off (no mode file on this Mac)")
        : h(
            "p",
            null,
            "mode: ",
            badge(mode.mode),
            mode.slots ? ", slots " + mode.slots : "",
            mode.since ? ", since " + mode.since : "",
          ),
      ["Slots", "Claims", "Waiting jobs"].map((t) =>
        h("div", { class: "panel" }, h("h2", null, t), h("p", { class: "note" }, "nothing to show yet")),
      ),
      data.state === "lit"
        ? [
            h("h2", null, "Log (last 200 rows)"),
            data.skipped ? h("p", { class: "note" }, data.skipped + " malformed line(s) skipped") : null,
            table(
              ["At", "Host", "Mode", "Kind", "Job", "Other"],
              data.rows.map((r) => [
                dash(r.at),
                dash(r.host),
                dash(r.mode),
                dash(r.kind),
                dash(r.job),
                Object.keys(r.extra || {}).length ? h("span", { class: "mono" }, JSON.stringify(r.extra)) : "",
              ]),
            ),
          ]
        : null,
    );
  }

  const views = {
    jobs: jobsView,
    job: jobView,
    verifier: verifierView,
    budget: budgetView,
    health: healthView,
    parallel: parallelView,
  };

  // -------------------------------------------------------------- app --

  const NAV = [
    ["jobs", "Jobs"],
    ["verifier", "Verifier"],
    ["budget", "Budget"],
    ["health", "Health"],
    ["parallel", "Parallel"],
  ];
  const state = { route: null, job: null, seq: 0 };

  function parseRoute(hash) {
    const m = /^#\/(jobs|verifier|budget|health|parallel)(?:\/([0-9a-z-]+))?$/.exec(hash || "");
    if (!m) return null;
    if (m[2] && m[1] !== "jobs") return null;
    return m[2] ? { view: "job", id: m[2] } : { view: m[1] };
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-store", credentials: "same-origin" }).then((res) =>
      res.json().then((body) => ({ ok: res.ok, status: res.status, body })),
    );
  }

  function mount(node) {
    root.document.getElementById("app").replaceChildren(node);
  }

  function showError(message) {
    mount(h("p", { class: "error" }, message));
  }

  function renderNav() {
    const current = state.route ? (state.route.view === "job" ? "jobs" : state.route.view) : "";
    root.document.getElementById("nav").replaceChildren(
      ...NAV.map(([k, label]) => h("a", { href: "#/" + k, class: k === current ? "active" : null }, label)),
    );
  }

  function jobActions() {
    return {
      open(key, isOpen) {
        const ui = state.job;
        if (!ui || ui.open.has(key) === isOpen) return;
        if (isOpen) {
          ui.open.add(key);
          loadArtifact(key);
        } else {
          ui.open.delete(key);
        }
      },
      raw(key) {
        const ui = state.job;
        if (ui.raw.has(key)) ui.raw.delete(key);
        else ui.raw.add(key);
        drawJob();
      },
    };
  }

  function drawJob() {
    const ui = state.job;
    if (!ui || !ui.detail) return;
    mount(
      views.job({
        detail: ui.detail,
        texts: ui.texts,
        open: [...ui.open],
        raw: [...ui.raw],
        actions: jobActions(),
      }),
    );
  }

  function loadArtifact(key) {
    const ui = state.job;
    return getJSON("/api/jobs/" + ui.id + "/artifact/" + key).then((res) => {
      if (state.job !== ui) return;
      ui.texts[key] = res.ok ? res.body : { text: "not written yet", truncated: false };
      drawJob();
    });
  }

  function load(refresh) {
    const route = state.route;
    const seq = ++state.seq;
    if (!route) return Promise.resolve();
    if (route.view === "job") {
      if (!state.job || state.job.id !== route.id) {
        state.job = { id: route.id, open: new Set(), raw: new Set(), texts: {}, detail: null };
      }
      const ui = state.job;
      return getJSON("/api/jobs/" + route.id)
        .then((res) => {
          if (seq !== state.seq) return;
          if (!res.ok) return showError("no such job: " + route.id);
          ui.detail = res.body;
          drawJob();
          return Promise.all([...ui.open].map(loadArtifact));
        })
        .catch(() => showError("could not reach the server"));
    }
    const url = "/api/" + route.view + (refresh ? "?refresh=1" : "");
    return getJSON(url)
      .then((res) => {
        if (seq !== state.seq) return;
        if (!res.ok) return showError("error " + res.status);
        const data = res.body;
        data.actions = { refresh: () => load(true) };
        mount(views[route.view](data));
      })
      .catch(() => showError("could not reach the server"));
  }

  function onHash() {
    const route = parseRoute(root.location.hash);
    if (!route) {
      // An in-page anchor from a rendered file keeps the current view.
      if (state.route) return;
      root.location.hash = "#/jobs";
      return;
    }
    state.route = route;
    renderNav();
    load(false);
  }

  function start() {
    root.addEventListener("hashchange", onHash);
    onHash();
    root.setInterval(() => {
      if (root.document.visibilityState !== "visible" || !state.route) return;
      if (FILE_VIEWS.includes(state.route.view)) load(false);
    }, FILE_REFRESH_MS);
  }

  root.FactoryUI = { views, fmtAge, parseRoute };
  if (root.location && root.document.readyState) {
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
