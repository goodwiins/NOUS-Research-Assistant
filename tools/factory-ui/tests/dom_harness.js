// Renders one view of static/app.js under node with a minimal DOM and
// prints {html, text, tags, hrefs} as JSON. Usage: node dom_harness.js <view> <data.json>
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.data = String(text);
  }
  get textContent() {
    return this.data;
  }
  serialize() {
    return esc(this.data);
  }
}

class Element {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = {};
    this.listeners = {};
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  append(...items) {
    for (const item of items) {
      this.appendChild(typeof item === "string" ? new TextNode(item) : item);
    }
  }
  replaceChildren(...items) {
    this.children = [];
    this.append(...items);
  }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }
  getAttribute(name) {
    return name in this.attrs ? this.attrs[name] : null;
  }
  removeAttribute(name) {
    delete this.attrs[name];
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  set className(v) {
    this.attrs.class = String(v);
  }
  get className() {
    return this.attrs.class || "";
  }
  set textContent(v) {
    this.children = [new TextNode(v)];
  }
  get textContent() {
    return this.children.map((c) => c.textContent).join("");
  }
  serialize() {
    const attrs = Object.entries(this.attrs)
      .map(([k, v]) => ` ${k}="${esc(v)}"`)
      .join("");
    const inner = this.children.map((c) => c.serialize()).join("");
    const tag = this.tagName.toLowerCase();
    return `<${tag}${attrs}>${inner}</${tag}>`;
  }
}

const mountPoints = { app: new Element("main"), nav: new Element("nav") };
const documentShim = {
  getElementById: (id) => mountPoints[id] || null,
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => new TextNode(text),
  createDocumentFragment: () => new Element("fragment"),
};

globalThis.document = documentShim;

const staticDir = path.join(__dirname, "..", "static");
for (const name of ["render.js", "app.js"]) {
  vm.runInThisContext(fs.readFileSync(path.join(staticDir, name), "utf8"), {
    filename: name,
  });
}

function walk(node, tags, hrefs) {
  if (node.nodeType !== 1) return;
  tags.push(node.tagName.toLowerCase());
  if (node.attrs.href !== undefined) hrefs.push(node.attrs.href);
  for (const c of node.children) walk(c, tags, hrefs);
}

const [view, file] = process.argv.slice(2);
const data = JSON.parse(fs.readFileSync(file, "utf8"));
if (view === "stale-draw") {
  // A job response that lands after the user moved to another view.
  const ui = globalThis.FactoryUI;
  const id = data.detail.id;
  ui._state.job = { id, detail: data.detail, open: new Set(), raw: new Set(), texts: {} };
  ui._state.route = { view: "health" };
  ui._drawJob();
  const elsewhere = mountPoints.app.children.length;
  ui._state.route = { view: "job", id };
  ui._drawJob();
  const onPage = mountPoints.app.children.length;
  process.stdout.write(JSON.stringify({ elsewhere, onPage }));
  process.exit(0);
}
if (view === "stale-error") {
  // Jobs is requested, the user moves to Parallel, Parallel renders, and only
  // then does the Jobs request fail: the Parallel view must stay.
  const ui = globalThis.FactoryUI;
  const pending = {};
  globalThis.fetch = (url) =>
    new Promise((resolve, reject) => {
      pending[url] = { resolve, reject };
    });
  ui._state.route = { view: "jobs" };
  const first = ui._load(false);
  ui._state.route = { view: "parallel" };
  const second = ui._load(false);
  pending["/api/parallel"].resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
  second
    .then(() => {
      pending["/api/jobs"].reject(new Error("connection reset"));
      return first;
    })
    .then(() => {
      const text = mountPoints.app.textContent;
      process.stdout.write(
        JSON.stringify({ parallel: text.includes("Parallel jobs"), error: text.includes("could not reach") }),
      );
    });
  return;
}
let node;
if (view === "markdown") node = globalThis.FactoryRender.markdown(data.text);
else if (view === "raw") node = globalThis.FactoryRender.raw(data.text);
else node = globalThis.FactoryUI.views[view](data);
const tags = [];
const hrefs = [];
walk(node, tags, hrefs);
process.stdout.write(
  JSON.stringify({ html: node.serialize(), text: node.textContent, tags, hrefs }),
);
