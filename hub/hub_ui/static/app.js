"use strict";

/* ===================================================================== utils */
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v; // only used with server-sanitised README HTML
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), isErr ? 5000 : 2500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/auth") {
    await requireAuth();
    return api(path, opts);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
const appUrl = (p) => `${location.protocol}//${location.hostname}:${p.port}${p.url_path || "/"}`;
const timeAgo = (t) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};
const STATUS_CLASS = { starting: "warn", running: "ok", exited: "", stopped: "", failed: "err" };
const statusPill = (status) =>
  h("span", { class: `pill ${STATUS_CLASS[status] ?? ""}` },
    h("span", { class: "dot" + (status === "starting" ? " pulse" : "") }), status);

/* ===================================================================== state */
const state = {
  projects: [],
  byId: {},
  tags: [],
  activeTags: new Set(),
  query: "",
  onlyApps: false,
  onlyReady: false,
  current: null,      // project detail
  tab: "run",
  procs: [],
  sessionProc: null,  // process id shown in the run tab
  sessionView: null,  // "app" | "logs"
  autoSwitched: new Set(),
};

/* ===================================================================== auth */
let authPromise = null;
function requireAuth() {
  if (authPromise) return authPromise;
  authPromise = new Promise((resolve) => {
    const dlg = $("#auth-dialog");
    const form = $("#auth-form");
    form.onsubmit = async (e) => {
      e.preventDefault();
      try {
        await fetch("/api/auth", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: $("#auth-token").value }),
        }).then((r) => { if (!r.ok) throw new Error("Invalid token"); });
        dlg.close();
        authPromise = null;
        resolve();
      } catch (err) {
        $("#auth-error").textContent = err.message;
      }
    };
    dlg.addEventListener("cancel", (e) => e.preventDefault());
    dlg.showModal();
  });
  return authPromise;
}

/* ===================================================================== sidebar */
function renderFilters() {
  const wrap = $("#tag-filters");
  wrap.replaceChildren(...state.tags.map((t) =>
    h("button", {
      class: "chip" + (state.activeTags.has(t) ? " on" : ""), type: "button",
      "aria-pressed": state.activeTags.has(t) ? "true" : "false",
      onclick: () => {
        state.activeTags.has(t) ? state.activeTags.delete(t) : state.activeTags.add(t);
        renderFilters(); renderList();
      },
    }, t)));
}

function filteredProjects() {
  const q = state.query.trim().toLowerCase();
  return state.projects.filter((p) => {
    if (state.onlyApps && !p.entrypoints.some((e) => e.kind !== "notebook")) return false;
    if (state.onlyReady && p.env_missing.length) return false;
    for (const t of state.activeTags) if (!p.tags.includes(t)) return false;
    if (!q) return true;
    return [p.title, p.name, p.summary, p.tags.join(" "), p.env_vars.join(" ")].join(" ").toLowerCase().includes(q);
  });
}

function renderList() {
  const list = $("#project-list");
  const items = filteredProjects();
  if (!items.length) {
    list.replaceChildren(h("div", { class: "empty" }, "No projects match."));
    return;
  }
  const running = new Set(state.procs.filter((p) => p.kind === "app" && ["starting", "running"].includes(p.status)).map((p) => p.project_id));
  list.replaceChildren(...items.map((p) => {
    const apps = p.entrypoints.filter((e) => e.kind !== "notebook").length;
    const nbs = p.entrypoints.length - apps;
    return h("button", {
      class: "project-item" + (state.current && state.current.id === p.id ? " active" : ""),
      type: "button",
      onclick: () => { location.hash = `#/p/${p.id}/${state.tab}`; closeSidebarMobile(); },
    },
      h("div", { class: "t" }, h("span", {}, p.title), running.has(p.id) && h("span", { class: "live-dot", title: "Running" })),
      p.summary && h("div", { class: "s" }, p.summary),
      h("div", { class: "meta" },
        apps ? h("span", { class: "mini" }, `${apps} app${apps > 1 ? "s" : ""}`) : null,
        nbs ? h("span", { class: "mini" }, `${nbs} notebook${nbs > 1 ? "s" : ""}`) : null,
        p.env_vars.length
          ? h("span", { class: "mini " + (p.env_missing.length ? "warn" : "ok") },
              p.env_missing.length ? `${p.env_missing.length} key${p.env_missing.length > 1 ? "s" : ""} missing` : "keys ready")
          : null,
        p.has_venv ? h("span", { class: "mini ok" }, "installed") : null,
      ));
  }));
}

function closeSidebarMobile() { $("#sidebar").classList.remove("open"); }

/* ===================================================================== detail */
async function openProject(id, tab) {
  state.tab = ["run", "setup", "readme"].includes(tab) ? tab : "run";
  if (!state.current || state.current.id !== id) {
    try {
      state.current = await api(`/api/projects/${encodeURIComponent(id)}`);
    } catch (e) {
      toast(e.message, true);
      location.hash = "#/";
      return;
    }
    const latest = state.procs.find((p) => p.project_id === id && p.kind === "app");
    state.sessionProc = latest ? latest.id : null;
    state.sessionView = null;
  }
  $("#welcome").classList.add("hidden");
  $("#detail").classList.remove("hidden");
  renderDetail();
  renderList();
}

async function refreshCurrent() {
  if (!state.current) return;
  state.current = await api(`/api/projects/${encodeURIComponent(state.current.id)}`);
  const s = state.projects.findIndex((p) => p.id === state.current.id);
  if (s >= 0) Object.assign(state.projects[s], { env_missing: state.current.env_missing, has_venv: state.current.has_venv });
}

function renderDetail() {
  stopLogWatchers();
  const p = state.current;
  const missing = p.env_missing.length;
  const head = h("div", { class: "detail-head" },
    h("div", { style: "flex:1;min-width:240px" },
      h("h1", {}, p.title),
      h("div", { class: "path" }, p.name + "/"),
      h("div", { class: "tags" }, p.tags.map((t) => h("span", { class: "pill accent" }, t)))));
  const tabs = h("div", { class: "tabs", role: "tablist" },
    tabBtn("run", "Run"),
    tabBtn("setup", "Setup", missing ? h("span", { class: "mini warn" }, `${missing}`) : null),
    tabBtn("readme", "Guide"));
  const body = h("div", {});
  if (state.tab === "run") renderRunTab(body);
  else if (state.tab === "setup") renderSetupTab(body);
  else body.append(h("article", { class: "readme", html: p.readme_html }));
  $("#detail").replaceChildren(head, p.summary ? h("p", { class: "detail-summary" }, p.summary) : "", tabs, body);
}

function tabBtn(id, label, extra) {
  return h("button", {
    class: "tab" + (state.tab === id ? " on" : ""), role: "tab", type: "button",
    "aria-selected": state.tab === id ? "true" : "false",
    onclick: () => (location.hash = `#/p/${state.current.id}/${id}`),
  }, label, extra);
}

/* ---------------------------------------------------------------- run tab */
function renderRunTab(root) {
  const p = state.current;
  if (p.env_missing.length) {
    root.append(h("div", { class: "notice warn" },
      `Missing: ${p.env_missing.join(", ")}. `,
      h("a", { href: `#/p/${p.id}/setup` }, "Add keys in Setup"), " — some features may fail without them."));
  }
  if (!p.has_venv && p.entrypoints.length) {
    root.append(h("div", { class: "notice info" },
      "Dependencies are not installed in an isolated environment yet; apps will use the hub's Python. ",
      h("a", { href: `#/p/${p.id}/setup` }, "Install dependencies"), "."));
  }

  if (!p.entrypoints.length) {
    root.append(h("div", { class: "card" }, h("div", { class: "card-body" },
      h("p", {}, "This project has no web app or notebook the hub can launch (it is a script, MCP server or service)."),
      h("p", { class: "muted" }, "Follow the instructions in the "), h("a", { href: `#/p/${p.id}/readme` }, "Guide"),
      h("span", { class: "muted" }, " tab. You can still manage its API keys and dependencies in Setup."))));
    return;
  }

  const procsByEp = {};
  for (const pr of state.procs) {
    if (pr.project_id === p.id && pr.kind === "app" && !procsByEp[pr.entrypoint_id]) procsByEp[pr.entrypoint_id] = pr;
  }
  const rows = p.entrypoints.map((ep) => {
    const pr = procsByEp[ep.id];
    const live = pr && ["starting", "running"].includes(pr.status);
    return h("div", { class: "ep-row" },
      h("span", { class: `kind ${ep.kind}` }, ep.kind),
      h("span", { class: "name" }, ep.path),
      pr ? statusPill(pr.status) : null,
      h("div", { class: "actions" },
        live && pr.status === "running" ? h("a", { class: "btn sm", href: appUrl(pr), target: "_blank", rel: "noopener" }, "Open ↗") : null,
        pr ? h("button", { class: "btn sm ghost", type: "button", onclick: () => { state.sessionProc = pr.id; state.sessionView = null; renderDetail(); } }, "View") : null,
        live
          ? h("button", { class: "btn sm danger", type: "button", onclick: () => stopProc(pr.id) }, "Stop")
          : h("button", { class: "btn sm primary", type: "button", onclick: (e) => launch(ep, e.currentTarget) }, "Launch")));
  });
  root.append(h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h3", {}, "Apps & notebooks"),
      h("div", { class: "right muted" }, "Python: ", h("code", {}, p.python))),
    h("div", {}, rows)));

  const sess = state.procs.find((x) => x.id === state.sessionProc);
  if (sess) root.append(sessionCard(sess));
}

function sessionCard(pr) {
  const live = ["starting", "running"].includes(pr.status);
  const canEmbed = pr.status === "running" && pr.app_kind !== "notebook";
  if (!state.sessionView) state.sessionView = canEmbed ? "app" : "logs";
  if (canEmbed && !state.autoSwitched.has(pr.id)) {
    state.autoSwitched.add(pr.id);
    state.sessionView = "app";
  }
  const view = canEmbed ? state.sessionView : "logs";
  const card = h("div", { class: "card", id: "session" },
    h("div", { class: "card-head" },
      h("h3", {}, pr.label), statusPill(pr.status),
      pr.port ? h("span", { class: "muted" }, `port ${pr.port}`) : null,
      h("div", { class: "right" },
        canEmbed ? h("button", { class: "btn sm" + (view === "app" ? " primary" : ""), type: "button", onclick: () => { state.sessionView = "app"; renderDetail(); } }, "App") : null,
        h("button", { class: "btn sm" + (view === "logs" ? " primary" : ""), type: "button", onclick: () => { state.sessionView = "logs"; renderDetail(); } }, "Logs"),
        pr.status === "running" ? h("a", { class: "btn sm", href: appUrl(pr), target: "_blank", rel: "noopener" }, "Open in new tab ↗") : null,
        live ? h("button", { class: "btn sm danger", type: "button", onclick: () => stopProc(pr.id) }, "Stop") : null)));
  if (view === "app") {
    card.append(h("iframe", { class: "app-frame", src: appUrl(pr), title: pr.label, allow: "clipboard-write; microphone; camera" }));
  } else {
    const pre = h("pre", { class: "console", "aria-live": "polite" });
    card.append(pre);
    watchLogs(pr.id, pre);
  }
  return card;
}

async function launch(ep, btn) {
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    const pr = await api(`/api/projects/${state.current.id}/run`, { method: "POST", body: { entrypoint: ep.id } });
    state.sessionProc = pr.id;
    state.sessionView = "logs";
    await refreshProcs();
    renderDetail();
    toast(`Starting ${ep.path} on port ${pr.port}`);
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = "Launch";
  }
}

async function stopProc(id) {
  try {
    await api(`/api/processes/${id}/stop`, { method: "POST" });
    await refreshProcs();
    if (state.current) renderDetail();
    renderProcsDialog();
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- logs */
const watchers = new Set();
function stopLogWatchers() { for (const w of watchers) w.stopped = true; watchers.clear(); }

function watchLogs(procId, pre) {
  const w = { stopped: false, after: -1 };
  watchers.add(w);
  const tick = async () => {
    if (w.stopped || !pre.isConnected) { watchers.delete(w); return; }
    try {
      const data = await api(`/api/processes/${procId}/logs?after=${w.after}`);
      if (data.lines.length) {
        const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
        const frag = document.createDocumentFragment();
        for (const l of data.lines) {
          const text = l.text.replace(ANSI, "");
          const cls = text.startsWith("$ ") ? "cmd" : text.startsWith("[hub]") ? "hub"
            : /\b(error|traceback|exception)\b/i.test(text) ? "errl" : null;
          frag.append(cls ? h("span", { class: cls }, text + "\n") : text + "\n");
        }
        pre.append(frag);
        if (nearBottom || w.after === -1) pre.scrollTop = pre.scrollHeight;
        w.after = data.last;
      }
      const alive = ["starting", "running"].includes(data.process.status);
      const known = state.procs.find((x) => x.id === procId);
      if (known && known.status !== data.process.status) {
        Object.assign(known, data.process);
        if (state.current) { renderDetail(); return; }
      }
      setTimeout(tick, alive ? 1000 : 4000);
    } catch {
      setTimeout(tick, 3000);
    }
  };
  tick();
}

/* ---------------------------------------------------------------- setup tab */
function envEditor(rows, save, { allowAdd = true } = {}) {
  const inputs = new Map();
  const table = h("table", { class: "env-table" });
  const tbody = h("tbody");
  table.append(tbody);
  const addRow = (r) => {
    const inp = h("input", {
      type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: r.set ? `${r.preview}  (from ${r.source})` : "not set",
      "aria-label": r.name,
    });
    inputs.set(r.name, inp);
    const show = h("button", { class: "icon-btn", type: "button", title: "Show / hide", "aria-label": `Show ${r.name}`,
      onclick: () => (inp.type = inp.type === "password" ? "text" : "password") }, "👁");
    const canClear = r.set && (r.source === "project" || r.source === "global");
    const clear = canClear ? h("button", { class: "icon-btn", type: "button", title: "Remove saved value", "aria-label": `Remove ${r.name}`,
      onclick: () => save({ [r.name]: null }) }, "✕") : null;
    tbody.append(h("tr", {},
      h("td", { class: "k" }, r.name),
      h("td", {}, inp),
      h("td", { class: "src" }, r.set ? h("span", { class: "pill ok" }, r.source) : h("span", { class: "pill warn" }, "missing")),
      h("td", { style: "white-space:nowrap;width:76px" }, show, clear)));
  };
  rows.forEach(addRow);

  const saveBtn = h("button", { class: "btn primary", type: "button", onclick: () => {
    const changes = {};
    for (const [k, inp] of inputs) if (inp.value.trim()) changes[k] = inp.value.trim();
    if (!Object.keys(changes).length) { toast("Nothing to save"); return; }
    save(changes);
  } }, "Save keys");

  const wrap = h("div", {}, rows.length ? table : h("p", { class: "muted" }, "No environment variables detected."));
  if (allowAdd) {
    const nameInp = h("input", { type: "text", placeholder: "NEW_VARIABLE_NAME", "aria-label": "New variable name", spellcheck: "false" });
    const addBtn = h("button", { class: "btn", type: "button", onclick: () => {
      const n = nameInp.value.trim().toUpperCase();
      if (!/^[A-Z_][A-Z0-9_]*$/.test(n)) { toast("Use letters, digits and underscores", true); return; }
      if (inputs.has(n)) { inputs.get(n).focus(); return; }
      if (!rows.length) wrap.replaceChildren(table, ...[...wrap.children].slice(1));
      addRow({ name: n, set: false, preview: "", source: null });
      nameInp.value = "";
      inputs.get(n).focus();
    } }, "Add variable");
    wrap.append(h("div", { class: "add-var" }, nameInp, addBtn));
  }
  wrap.append(h("div", { class: "env-actions" }, saveBtn));
  return wrap;
}

async function renderSetupTab(root) {
  const p = state.current;
  const envCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h3", {}, "API keys & environment"),
      h("div", { class: "right muted" }, "Saved to ", h("code", {}, `${p.name}/.env`))),
    h("div", { class: "card-body" }, h("p", { class: "muted" }, "Loading…")));
  root.append(envCard);

  const installProc = state.procs.find((x) => x.project_id === p.id && x.kind === "install");
  const venvChk = h("input", { type: "checkbox", id: "use-venv", checked: true });
  const installBtn = h("button", { class: "btn primary", type: "button", onclick: async () => {
    installBtn.disabled = true;
    try {
      await api(`/api/projects/${p.id}/install`, { method: "POST", body: { venv: venvChk.checked } });
      await refreshProcs();
      renderDetail();
    } catch (e) { toast(e.message, true); installBtn.disabled = false; }
  } }, installProc && ["starting", "running"].includes(installProc.status) ? "Installing…" : "Install dependencies");
  if (installProc && ["starting", "running"].includes(installProc.status)) installBtn.disabled = true;

  const depCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h3", {}, "Dependencies"),
      p.has_venv ? h("span", { class: "pill ok" }, ".venv ready") : h("span", { class: "pill" }, "not installed"),
      h("div", { class: "right" },
        h("label", { class: "muted", for: "use-venv", style: "display:inline-flex;gap:6px;align-items:center" }, venvChk, "Isolated .venv (recommended)"),
        installBtn)),
    h("div", { class: "card-body" },
      p.requirements_files.length ? h("p", {}, "Requirement files: ", p.requirements_files.map((f) => h("code", {}, f))) : null,
      p.packages.length
        ? h("div", { class: "pkg-list" }, p.packages.map((x) => h("span", { class: "pkg" }, x)))
        : h("p", { class: "muted" }, "No dependencies were detected — check the Guide tab."),
      h("p", { class: "muted", style: "margin-bottom:0" },
        "Detected from requirements.txt, pyproject.toml and pip commands in the README. ",
        "Some projects also need ", h("a", { href: "https://ollama.com", target: "_blank", rel: "noopener" }, "Ollama"),
        " models or GPU libraries — see the Guide.")));
  if (installProc) {
    const pre = h("pre", { class: "console" });
    depCard.append(h("div", { class: "card-head" }, h("h3", {}, "Install log"), statusPill(installProc.status)), pre);
    watchLogs(installProc.id, pre);
  }
  root.append(depCard);

  try {
    const rows = await api(`/api/projects/${p.id}/env`);
    const save = async (changes) => {
      try {
        await api(`/api/projects/${p.id}/env`, { method: "PUT", body: { values: changes } });
        toast("Saved");
        await refreshCurrent();
        renderDetail();
        renderList();
      } catch (e) { toast(e.message, true); }
    };
    envCard.lastChild.replaceChildren(
      h("p", { class: "muted", style: "margin-top:0" },
        "Values set here apply to this project only. Shared keys can be set once under ",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); openSettings(); } }, "API keys"), "."),
      envEditor(rows, save));
  } catch (e) {
    envCard.lastChild.replaceChildren(h("p", { class: "error" }, e.message));
  }
}

/* ===================================================================== dialogs */
async function openSettings() {
  const dlg = $("#settings-dialog");
  const box = $("#global-env");
  box.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  if (!dlg.open) dlg.showModal();
  try {
    const rows = await api("/api/settings/env");
    box.replaceChildren(envEditor(rows, async (changes) => {
      try {
        await api("/api/settings/env", { method: "PUT", body: { values: changes } });
        toast("Saved");
        await loadProjects();
        if (state.current) { await refreshCurrent(); renderDetail(); }
        openSettings();
      } catch (e) { toast(e.message, true); }
    }));
  } catch (e) { box.replaceChildren(h("p", { class: "error" }, e.message)); }
}

function renderProcsDialog() {
  const box = $("#procs-list");
  if (!state.procs.length) { box.replaceChildren(h("p", { class: "muted" }, "Nothing has been started yet.")); return; }
  box.replaceChildren(...state.procs.map((pr) => {
    const proj = state.byId[pr.project_id];
    const live = ["starting", "running"].includes(pr.status);
    return h("div", { class: "proc-row" },
      statusPill(pr.status),
      h("div", { class: "info" },
        h("b", {}, `${proj ? proj.title : pr.project_id} — ${pr.label}`),
        h("span", {}, `${pr.port ? "port " + pr.port + " · " : ""}started ${timeAgo(pr.started_at)}`)),
      pr.status === "running" && pr.port ? h("a", { class: "btn sm", href: appUrl(pr), target: "_blank", rel: "noopener" }, "Open ↗") : null,
      h("button", { class: "btn sm ghost", type: "button", onclick: () => {
        $("#procs-dialog").close();
        state.sessionProc = pr.kind === "app" ? pr.id : state.sessionProc;
        state.sessionView = "logs";
        if (state.current && state.current.id === pr.project_id) {
          state.tab = pr.kind === "app" ? "run" : "setup"; renderDetail();
        }
        location.hash = `#/p/${pr.project_id}/${pr.kind === "app" ? "run" : "setup"}`;
      } }, "Logs"),
      live
        ? h("button", { class: "btn sm danger", type: "button", onclick: () => stopProc(pr.id) }, "Stop")
        : h("button", { class: "btn sm ghost", type: "button", onclick: async () => {
            await api(`/api/processes/${pr.id}`, { method: "DELETE" }).catch((e) => toast(e.message, true));
            await refreshProcs(); renderProcsDialog();
          } }, "Dismiss"));
  }));
}

/* ===================================================================== polling */
async function refreshProcs() {
  try {
    const prev = JSON.stringify(state.procs.map((p) => [p.id, p.status]));
    state.procs = await api("/api/processes");
    const live = state.procs.filter((p) => ["starting", "running"].includes(p.status));
    const c = $("#proc-count");
    c.textContent = live.length;
    c.classList.toggle("live", live.length > 0);
    const changed = prev !== JSON.stringify(state.procs.map((p) => [p.id, p.status]));
    if (changed) {
      renderList();
      if ($("#procs-dialog").open) renderProcsDialog();
      // Re-render run tab on status changes (but don't rebuild an embedded app iframe needlessly).
      if (state.current && state.tab === "run" && !watchers.size) renderDetail();
    }
    return changed;
  } catch { return false; }
}

async function refreshSystem() {
  try {
    const s = await api("/api/system");
    const pill = $("#ollama-pill");
    pill.className = "pill " + (s.ollama_running ? "ok" : "");
    pill.replaceChildren(h("span", { class: "dot" }), s.ollama_running ? "Ollama running" : "Ollama offline");
    return s;
  } catch { return null; }
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  state.byId = Object.fromEntries(state.projects.map((p) => [p.id, p]));
  const counts = {};
  for (const p of state.projects) for (const t of p.tags) counts[t] = (counts[t] || 0) + 1;
  state.tags = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  renderFilters();
  renderList();
  const apps = state.projects.reduce((n, p) => n + p.entrypoints.filter((e) => e.kind !== "notebook").length, 0);
  const nbs = state.projects.reduce((n, p) => n + p.entrypoints.filter((e) => e.kind === "notebook").length, 0);
  $("#stats").replaceChildren(
    h("div", { class: "stat" }, h("b", {}, state.projects.length), h("span", {}, "projects")),
    h("div", { class: "stat" }, h("b", {}, apps), h("span", {}, "launchable web apps")),
    h("div", { class: "stat" }, h("b", {}, nbs), h("span", {}, "notebooks")),
    h("div", { class: "stat" }, h("b", {}, state.projects.filter((p) => p.has_venv).length), h("span", {}, "installed")));
}

/* ===================================================================== routing */
function route() {
  const m = location.hash.match(/^#\/p\/([^/]+)(?:\/(\w+))?/);
  if (m) {
    openProject(decodeURIComponent(m[1]), m[2]);
  } else {
    stopLogWatchers();
    state.current = null;
    $("#detail").classList.add("hidden");
    $("#welcome").classList.remove("hidden");
    renderList();
  }
}

/* ===================================================================== boot */
async function boot() {
  $("#search").addEventListener("input", (e) => { state.query = e.target.value; renderList(); });
  $("#only-apps").addEventListener("change", (e) => { state.onlyApps = e.target.checked; renderList(); });
  $("#only-ready").addEventListener("change", (e) => { state.onlyReady = e.target.checked; renderList(); });
  $("#open-settings").addEventListener("click", openSettings);
  $("#open-procs").addEventListener("click", () => { renderProcsDialog(); $("#procs-dialog").showModal(); });
  $("#toggle-sidebar").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("#search").focus(); }
  });
  window.addEventListener("hashchange", route);

  await refreshSystem();
  await refreshProcs();
  await loadProjects();
  route();
  setInterval(refreshProcs, 2000);
  setInterval(refreshSystem, 15000);
}

boot().catch((e) => toast(e.message, true));
