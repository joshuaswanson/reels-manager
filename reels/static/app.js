const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const state = {
  signedIn: false,
  username: null,
  library: null,
  byPk: new Map(),
  view: { kind: "uncollected" },
  reelsOnly: false,
  query: "",
  selecting: false,
  selected: new Set(),
  anchorPk: null,
  visible: [],
  viewerPk: null,
  viewerSlide: 0,
  syncCursor: 0,
  lastStreamedPk: null,
  arriving: new Set(),
  sync: { running: false, message: "", error: null },
};

class ApiError extends Error {
  constructor(message, signedOut) {
    super(message);
    this.signedOut = signedOut;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload.error || payload.detail || `Request failed with HTTP ${response.status}`;
    if (payload.signedOut) {
      state.signedIn = false;
      render();
    }
    throw new ApiError(message, payload.signedOut);
  }
  return payload;
}

function toast(message, { error = false, duration = 3800 } = {}) {
  const el = h("div", { class: `toast${error ? " error" : ""}`, role: error ? "alert" : "status" }, message);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), duration);
}

const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

function formatDuration(seconds) {
  if (!seconds) return null;
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function formatRelative(unixSeconds) {
  if (!unixSeconds) return "never";
  const minutes = Math.round((Date.now() / 1000 - unixSeconds) / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return new Date(unixSeconds * 1000).toLocaleDateString();
}

/* Library data */

function setLibrary(library) {
  state.library = library;
  state.byPk = new Map(library.media.map((m) => [m.pk, { ...m, collections: new Set() }]));
  for (const c of library.collections) {
    for (const pk of c.pks) state.byPk.get(pk)?.collections.add(c.id);
  }
  if (state.view.kind === "collection" && !collectionById(state.view.id)) state.view = { kind: "uncollected" };
}

const collectionById = (id) => state.library?.collections.find((c) => c.id === id);

function mediaForView(view) {
  if (!state.library) return [];
  if (view.kind === "collection") {
    return (collectionById(view.id)?.pks ?? []).map((pk) => state.byPk.get(pk)).filter(Boolean);
  }
  const all = state.library.media.map((m) => state.byPk.get(m.pk));
  return view.kind === "uncollected" ? all.filter((m) => m.collections.size === 0) : all;
}

function applyFilters(items) {
  const query = state.query.trim().toLowerCase();
  return items.filter((m) => {
    if (state.reelsOnly && !m.isReel) return false;
    if (!query) return true;
    return `${m.username} ${m.fullName} ${m.caption}`.toLowerCase().includes(query);
  });
}

const countFor = (view) => applyFilters(mediaForView({ ...view })).length;

function viewTitle(view) {
  if (view.kind === "uncollected") return "Not in a collection";
  if (view.kind === "collection") return collectionById(view.id)?.name ?? "Collection";
  return "All saved";
}

/* Collection edits */

function applyMembership(collectionId, pks, adding) {
  const c = collectionById(collectionId);
  if (!c) return;
  if (adding) {
    const fresh = pks.filter((pk) => !c.pks.includes(pk));
    c.pks = [...fresh, ...c.pks];
    fresh.forEach((pk) => state.byPk.get(pk)?.collections.add(collectionId));
  } else {
    const removing = new Set(pks);
    c.pks = c.pks.filter((pk) => !removing.has(pk));
    pks.forEach((pk) => state.byPk.get(pk)?.collections.delete(collectionId));
  }
}

let editQueue = Promise.resolve();
const pendingUnsaves = new Set();
let pendingEditCount = 0;

function reportPendingEdits(delta) {
  pendingEditCount += delta;
  fetch("/api/pending-edits", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count: pendingEditCount }),
  }).catch(() => {});
}

function editSaved(path, body, message, { onSettled } = {}) {
  reportPendingEdits(1);
  const run = editQueue
    .then(() => runEdit(path, body, message, onSettled))
    .finally(() => reportPendingEdits(-1));
  editQueue = run.catch(() => {});
  return run;
}

async function runEdit(path, body, message, onSettled) {
  let succeeded = false;
  try {
    await api(path, { method: "POST", body });
    succeeded = true;
    if (message) toast(message);
  } catch (err) {
    toast(`${err.message} Some earlier items may already have been updated.`, { error: true });
  } finally {
    onSettled?.();
    // Reload even after failure: bulk edits can succeed for only some posts.
    try {
      setLibrary(await api("/api/library"));
      removeLocally([...pendingUnsaves]);
      for (const pk of state.selected) if (!state.byPk.has(pk)) state.selected.delete(pk);
      renderSidebar();
      renderGrid();
      renderSummary();
      renderSelectionBar();
      if ($("#viewer").open) renderViewer();
    } catch (err) {
      toast(`Could not refresh the library: ${err.message}`, { error: true });
    }
  }
  return succeeded;
}

function removeLocally(pks) {
  if (!pks.length || !state.library) return;
  const gone = new Set(pks);
  state.library.media = state.library.media.filter((m) => !gone.has(m.pk));
  for (const c of state.library.collections) c.pks = c.pks.filter((pk) => !gone.has(pk));
  for (const pk of pks) {
    state.byPk.delete(pk);
    state.selected.delete(pk);
  }
}

async function addToCollection(collectionId, pks, { quiet = false } = {}) {
  const c = collectionById(collectionId);
  const fresh = pks.filter((pk) => !c.pks.includes(pk));
  if (!fresh.length) {
    if (!quiet) toast(`Already in ${c.name}`);
    return true;
  }
  return editSaved(`/api/collections/${collectionId}/add`, { pks: fresh },
    quiet ? null : `Added ${plural(fresh.length, "reel")} to ${c.name}`);
}

async function removeFromCollection(collectionId, pks, { quiet = false } = {}) {
  return editSaved(`/api/collections/${collectionId}/remove`, { pks },
    quiet ? null : `Removed ${plural(pks.length, "reel")} from ${collectionById(collectionId).name}`);
}

async function moveToCollection(fromId, toId, pks) {
  return editSaved(`/api/collections/${fromId}/move`, { pks, to_id: toId },
    `Moved ${plural(pks.length, "reel")} to ${collectionById(toId).name}`);
}

function confirmAction({ title, text, confirmLabel }) {
  const dialog = $("#confirm-prompt");
  $("#confirm-prompt-title").textContent = title;
  $("#confirm-prompt-text").textContent = text;
  $("#confirm-prompt-submit").textContent = confirmLabel;
  return new Promise((resolve) => {
    let confirmed = false;
    $("#confirm-prompt-form").onsubmit = (event) => {
      event.preventDefault();
      confirmed = true;
      dialog.close();
    };
    $("#confirm-prompt-cancel").onclick = () => dialog.close();
    dialog.addEventListener("close", () => resolve(confirmed), { once: true });
    dialog.showModal();
    dialog.focus();
  });
}

async function unsavePosts(pks) {
  const confirmed = await confirmAction({
    title: `Unsave ${plural(pks.length, "post")}?`,
    text: "This cannot be undone.",
    confirmLabel: "Unsave",
  });
  if (!confirmed) return false;

  const viewer = $("#viewer");
  const viewingRemoved = viewer.open && pks.includes(state.viewerPk);
  const viewerIndex = viewingRemoved ? state.visible.findIndex((m) => m.pk === state.viewerPk) : -1;
  pks.forEach((pk) => pendingUnsaves.add(pk));
  removeLocally(pks);
  renderSidebar();
  renderGrid();
  renderSummary();
  renderSelectionBar();
  if (viewingRemoved) {
    if (state.visible.length) openViewer(Math.min(viewerIndex, state.visible.length - 1));
    else viewer.close();
  }
  toast(`Unsaved ${plural(pks.length, "post")}`);

  editSaved("/api/media/unsave", { pks }, null, {
    onSettled: () => pks.forEach((pk) => pendingUnsaves.delete(pk)),
  });
  return true;
}

function promptName({ title, hint, submitLabel, initial = "", onSubmit }) {
  const dialog = $("#name-prompt");
  const input = $("#name-prompt-input");
  $("#name-prompt-title").textContent = title;
  $("#name-prompt-hint").textContent = hint;
  $("#name-prompt-submit").textContent = submitLabel;
  input.value = initial;
  $("#name-prompt-form").onsubmit = async (event) => {
    event.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    dialog.close();
    try {
      await onSubmit(name);
    } catch (err) {
      toast(err.message, { error: true });
    }
  };
  $("#name-prompt-cancel").onclick = () => dialog.close();
  dialog.showModal();
  input.select();
}

function promptNewCollection(pks, onCreated) {
  promptName({
    title: "New collection",
    hint: pks.length
      ? `The ${plural(pks.length, "selected reel")} will go into it.`
      : "Collections you create here also appear in Instagram.",
    submitLabel: "Create collection",
    onSubmit: async (name) => {
      const created = await api("/api/collections", { method: "POST", body: { name, pks: onCreated ? [] : pks } });
      state.library.collections.unshift({ id: created.id, name: created.name, pks: [] });
      if (onCreated) {
        await onCreated(created.id);
        render();
        return;
      }
      applyMembership(created.id, pks, true);
      toast(`Created ${created.name}`);
      clearSelection();
      render();
      const section = $("#viewer[open] .viewer-section");
      if (section && pks.length === 1) renderViewerCollections(section, state.byPk.get(pks[0]));
    },
  });
}

function promptRename(collection) {
  promptName({
    title: "Rename collection",
    hint: "The new name is used in this app only. Instagram keeps the old name.",
    submitLabel: "Rename",
    initial: collection.name,
    onSubmit: async (name) => {
      const renamed = await api(`/api/collections/${collection.id}/rename`, { method: "POST", body: { name } });
      collection.name = renamed.name;
      toast(`Renamed to ${renamed.name}`);
      render();
    },
  });
}

/* Downloads */

function downloadOne(pk) {
  toast("Preparing download");
  const link = h("a", { href: `/media/${pk}/download`, download: "" });
  document.body.append(link);
  link.click();
  link.remove();
}

function downloadMany(pks, name) {
  if (pks.length === 1) return downloadOne(pks[0]);
  toast(`Preparing ${plural(pks.length, "file")}. Large batches take a while.`, { duration: 6000 });
  const form = h(
    "form",
    { method: "POST", action: "/api/download.zip", hidden: true },
    h("input", { type: "hidden", name: "pks", value: pks.join(",") }),
    h("input", { type: "hidden", name: "name", value: name }),
  );
  document.body.append(form);
  form.submit();
  form.remove();
}

/* Sync */

const SYNC_POLL_MS = 700;

async function startSync() {
  try {
    state.syncCursor = 0;
    state.lastStreamedPk = null;
    state.sync = await api("/api/sync", { method: "POST" });
    refreshDuringSync();
    pollSync();
  } catch (err) {
    toast(err.message, { error: true });
  }
}

function mergeStreamedMedia(items) {
  const lib = state.library;
  let insertAt = state.lastStreamedPk ? lib.media.findIndex((m) => m.pk === state.lastStreamedPk) + 1 : 0;
  for (const m of items) {
    if (state.byPk.has(m.pk)) {
      insertAt = lib.media.findIndex((x) => x.pk === m.pk) + 1;
    } else {
      lib.media.splice(insertAt, 0, m);
      state.byPk.set(m.pk, { ...m, collections: new Set() });
      state.arriving.add(m.pk);
      insertAt += 1;
    }
    state.lastStreamedPk = m.pk;
  }
}

function mergeStreamedCollections(collections) {
  for (const streamed of collections) {
    let c = collectionById(streamed.id);
    if (!c) {
      c = { id: streamed.id, name: streamed.name, pks: [] };
      state.library.collections.push(c);
    }
    c.pks.forEach((pk) => state.byPk.get(pk)?.collections.delete(c.id));
    c.name = streamed.name;
    c.pks = streamed.pks;
    c.pks.forEach((pk) => state.byPk.get(pk)?.collections.add(c.id));
  }
}

function refreshDuringSync() {
  renderSidebar();
  if ($("#sheet")) {
    renderGrid();
    renderSummary();
  } else {
    renderMain();
  }
}

async function pollSync() {
  const status = await api(`/api/sync?since=${state.syncCursor}`).catch(() => null);
  if (!status) return;
  state.sync = status;
  if (state.library) {
    mergeStreamedMedia(status.media);
    mergeStreamedCollections(status.collections);
  }
  state.syncCursor = status.total;
  if (status.running) {
    refreshDuringSync();
    setTimeout(pollSync, SYNC_POLL_MS);
    return;
  }
  state.arriving.clear();
  setLibrary(await api("/api/library"));
  refreshDuringSync();
  if (status.error) toast(status.error, { error: true, duration: 8000 });
  else toast(status.message);
}

/* Selection */

function clearSelection() {
  state.selected.clear();
  state.selecting = false;
  state.anchorPk = null;
}

function toggleSelect(pk, { range = false } = {}) {
  state.selecting = true;
  if (range && state.anchorPk) {
    const pks = state.visible.map((m) => m.pk);
    const [a, b] = [pks.indexOf(state.anchorPk), pks.indexOf(pk)].sort((x, y) => x - y);
    if (a >= 0) pks.slice(a, b + 1).forEach((p) => state.selected.add(p));
  } else if (state.selected.has(pk)) {
    state.selected.delete(pk);
  } else {
    state.selected.add(pk);
  }
  state.anchorPk = pk;
  renderSelectionState();
}

function selectedInOrder() {
  return state.visible.map((m) => m.pk).filter((pk) => state.selected.has(pk));
}

/* Menu */

const MENU_SEARCH_THRESHOLD = 6;

function openMenu(anchor, items) {
  const menu = $("#menu");
  (anchor.closest("dialog") ?? document.body).append(menu);
  const buttons = items.map((item) =>
    item === "divider"
      ? h("hr")
      : h("button", {
          type: "button",
          role: "menuitem",
          dataset: { label: item.label.toLowerCase(), searchable: String(!item.pinned) },
          onClick: () => { closeMenu(); item.action(); },
        }, item.label),
  );
  const searchable = items.filter((item) => item !== "divider" && !item.pinned).length > MENU_SEARCH_THRESHOLD;
  const search =
    searchable &&
    h("input", {
      type: "search",
      class: "menu-search",
      placeholder: "Find a collection",
      "aria-label": "Find a collection",
      onInput: (event) => {
        const query = event.target.value.trim().toLowerCase();
        for (const button of menu.querySelectorAll('button[data-searchable="true"]')) {
          button.hidden = Boolean(query) && !button.dataset.label.includes(query);
        }
      },
      onKeydown: (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        menu.querySelector("button[role=menuitem]:not([hidden])")?.click();
      },
    });
  menu.replaceChildren(...[search, h("div", { class: "menu-items" }, buttons)].filter(Boolean));
  menu.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const top = rect.top - menuRect.height - 8 > 8 ? rect.top - menuRect.height - 8 : rect.bottom + 8;
  menu.style.top = `${top}px`;
  menu.style.left = `${Math.min(rect.left, window.innerWidth - menuRect.width - 12)}px`;
  (search || menu.querySelector("button"))?.focus();
}

function closeMenu() {
  $("#menu").hidden = true;
}

document.addEventListener("pointerdown", (event) => {
  if (!$("#menu").hidden && !event.target.closest("#menu")) closeMenu();
});

function collectionMenuItems(onPick, { exclude, excludeIds, pks } = {}) {
  const items = state.library.collections
    .filter((c) => c.id !== exclude && !excludeIds?.has(c.id))
    .map((c) => ({ label: c.name, action: () => onPick(c.id) }));
  return [
    ...items,
    ...(items.length ? ["divider"] : []),
    { label: "New collection", pinned: true, action: () => promptNewCollection(pks || [], onPick) },
  ];
}

/* Rendering */

function render() {
  renderSidebar();
  renderMain();
  renderSelectionBar();
}

function navItem(view, label, count, { droppable = false } = {}) {
  const current =
    state.view.kind === view.kind && (view.kind !== "collection" || state.view.id === view.id);
  const item = h(
    "button",
    {
      type: "button",
      class: "nav-item",
      "aria-current": current ? "page" : false,
      onClick: () => {
        state.view = view;
        clearSelection();
        render();
        window.scrollTo({ top: 0 });
      },
    },
    h("span", { class: "name" }, label),
    h("span", { class: "count" }, count.toLocaleString()),
  );
  if (droppable) item.dataset.dropCollection = view.id;
  return item;
}

function renderSidebar() {
  const sidebar = $("#sidebar");
  if (!state.signedIn) {
    sidebar.replaceChildren(h("div", { class: "brand" }, "Reels manager", h("small", {}, "Not connected")));
    return;
  }
  const lib = state.library;
  const collections = lib?.collections ?? [];
  const sync = state.sync;
  const syncLabel = sync.running
    ? [h("span", { class: "spinner", "aria-hidden": "true" }), "Syncing"]
    : "Sync with Instagram";
  const statusText = sync.running
    ? sync.message
    : sync.error
      ? sync.error
      : `Last synced ${formatRelative(lib?.lastSync)}`;

  sidebar.replaceChildren(
    h("div", { class: "brand" }, "Reels manager", h("small", {}, state.username ? `@${state.username}` : "Connected")),
    h(
      "nav",
      { class: "nav-group", "aria-label": "Library" },
      navItem({ kind: "uncollected" }, "Not in a collection", countFor({ kind: "uncollected" })),
      navItem({ kind: "all" }, "All saved", countFor({ kind: "all" })),
    ),
    h(
      "div",
      { class: "nav-heading" },
      h("span", {}, "Collections"),
      lib && h("button", { type: "button", onClick: () => promptNewCollection([]), title: "New collection" }, "New"),
    ),
    h(
      "nav",
      { class: "nav-group nav-collections", "aria-label": "Collections" },
      collections.length
        ? collections.map((c) =>
            navItem({ kind: "collection", id: c.id }, c.name, countFor({ kind: "collection", id: c.id }), { droppable: true }),
          )
        : h("p", { class: "nav-empty" }, lib ? "No collections yet." : "Sync to load collections."),
    ),
    h(
      "div",
      { class: "sidebar-footer" },
      h("button", { type: "button", class: "btn btn-sync", disabled: sync.running, onClick: startSync }, syncLabel),
      h("p", { class: `sync-status${sync.error && !sync.running ? " error" : ""}`, "aria-live": "polite" }, statusText),
      h(
        "div",
        { class: "account" },
        h("span", {}, "Cookie stored locally"),
        h("button", { type: "button", onClick: signOut }, "Disconnect"),
      ),
    ),
  );
}

async function connect(request, button, error) {
  button.disabled = true;
  error.hidden = true;
  try {
    const result = await request();
    state.signedIn = true;
    state.username = result.username;
    await boot();
    if (!state.library?.lastSync) startSync();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function renderConnect() {
  const browserError = h("p", { class: "form-error", role: "alert", hidden: true });
  const browserButton = h("button", { type: "button", class: "btn btn-ink btn-large" }, "Use my browser login");
  browserButton.addEventListener("click", () =>
    connect(() => api("/api/session/browser", { method: "POST" }), browserButton, browserError),
  );

  const pasteError = h("p", { class: "form-error", role: "alert", hidden: true });
  const textarea = h("textarea", {
    id: "cookie",
    name: "cookie",
    placeholder: "sessionid=...; csrftoken=...; ds_user_id=...",
    spellcheck: "false",
    autocomplete: "off",
  });
  const pasteButton = h("button", { type: "submit", class: "btn btn-line" }, "Connect with this cookie");
  const pasteForm = h(
    "form",
    {
      onSubmit: (event) => {
        event.preventDefault();
        connect(() => api("/api/session", { method: "POST", body: { cookie: textarea.value } }), pasteButton, pasteError);
      },
    },
    h(
      "ol",
      {},
      h("li", {}, "Open instagram.com in your browser and log in."),
      h("li", {}, "Press ", h("kbd", {}, "⌥ ⌘ I"), " to open DevTools, choose the Network tab, and reload the page."),
      h("li", {}, "Click any request to www.instagram.com and find Cookie under Request Headers."),
      h("li", {}, "Copy the whole value and paste it below."),
    ),
    h("label", { for: "cookie", class: "note" }, "Instagram cookie"),
    textarea,
    pasteError,
    h("div", { class: "form-row" }, pasteButton),
  );

  return h(
    "section",
    { class: "connect" },
    h("h1", {}, "Connect your Instagram account"),
    h(
      "p",
      {},
      "Log in at instagram.com in Safari, Chrome, Arc, Brave, or Edge, then click the button. The app uses the browser where you used Instagram most recently.",
    ),
    h(
      "p",
      { class: "connect-warning" },
      "For Chrome, Arc, Brave, and Edge, macOS asks for permission to read the browser's login. Click Allow. Always Allow would let any program on this Mac read all of that browser's cookies without asking.",
    ),
    browserButton,
    browserError,
    h("p", { class: "note connect-note" }, "Your login stays on this computer in data/session.json."),
    h("details", { class: "connect-manual" }, h("summary", {}, "Paste a cookie by hand"), pasteForm),
  );
}

function renderEmpty(title, text, action) {
  return h("div", { class: "empty" }, h("h2", {}, title), h("p", {}, text), action);
}

function profileLink(username, label, attrs = {}) {
  return h("a", { ...attrs, href: `https://www.instagram.com/${encodeURIComponent(username)}/`, target: "_blank", rel: "noopener" }, label);
}

function tile(m) {
  const duration = formatDuration(m.duration);
  const frame = h(
    "span",
    { class: "tile-frame" },
    h("img", {
      src: `/media/${m.pk}/thumb`,
      alt: "",
      loading: "lazy",
      decoding: "async",
      onError: (event) => event.target.parentElement.classList.add("missing"),
    }),
    duration && h("span", { class: "tile-duration" }, duration),
    m.slides.length > 1 &&
      h("span", { class: "tile-slides", "aria-label": `${m.slides.length} items` }, icon("slides"), String(m.slides.length)),
  );
  const mark = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  mark.setAttribute("class", "mark");
  mark.setAttribute("viewBox", "0 0 100 160");
  mark.setAttribute("preserveAspectRatio", "none");
  mark.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#loop");
  mark.append(use);

  const el = h(
    "div",
    {
      role: "button",
      tabindex: "0",
      class: "tile",
      dataset: { pk: m.pk },
      "aria-pressed": String(state.selected.has(m.pk)),
      "aria-label": `${m.username ? `@${m.username}` : "Reel"}${m.caption ? `, ${m.caption.slice(0, 80)}` : ""}`,
    },
    frame,
    h(
      "span",
      { class: "tile-caption" },
      h("span", { class: "tile-user" }, m.username ? `@${m.username}` : "Unknown account"),
      m.caption && h("span", { class: "tile-text" }, m.caption),
    ),
  );
  frame.append(mark);
  return el;
}

function renderMain() {
  const main = $("#main");
  if (!state.signedIn) {
    main.replaceChildren(renderConnect());
    return;
  }
  if (!state.library?.media.length && (state.sync.running || !state.library?.lastSync)) {
    main.replaceChildren(
      h("header", { class: "view-header" }, h("h1", { class: "view-title" }, "Reels manager")),
      renderEmpty(
        state.sync.running ? "Connecting to Instagram" : "Nothing synced yet",
        state.sync.running
          ? "Your saves will appear here as Instagram sends them."
          : "Sync pulls your saved posts and collections from Instagram into this app.",
        !state.sync.running && h("button", { type: "button", class: "btn btn-ink", onClick: startSync }, "Sync with Instagram"),
      ),
    );
    return;
  }

  const base = mediaForView(state.view);
  state.visible = applyFilters(base);
  const reelWord = state.reelsOnly ? "reel" : "saved post";

  const search = h("input", {
    type: "search",
    class: "search",
    placeholder: "Search captions and accounts",
    "aria-label": "Search captions and accounts",
    value: state.query,
    onInput: (event) => {
      state.query = event.target.value;
      renderGrid();
      renderSidebar();
      renderSummary();
    },
  });

  const reelsToggle = h(
    "label",
    { class: "toggle" },
    h("input", {
      type: "checkbox",
      checked: state.reelsOnly,
      onChange: (event) => {
        state.reelsOnly = event.target.checked;
        render();
      },
    }),
    h("span", { class: "track", "aria-hidden": "true" }),
    "Reels only",
  );

  main.replaceChildren(
    h(
      "header",
      { class: "view-header" },
      h("h1", { class: "view-title" }, viewTitle(state.view)),
      h("p", { class: "view-summary", id: "view-summary" }),
      h(
        "div",
        { class: "toolbar" },
        search,
        reelsToggle,
        h("span", { class: "toolbar-spacer" }),
        h(
          "button",
          {
            type: "button",
            class: "btn btn-line",
            "aria-pressed": String(state.selecting),
            onClick: () => {
              if (state.selecting) clearSelection();
              else state.selecting = true;
              renderSelectionState();
            },
          },
          state.selecting ? "Done selecting" : "Select",
        ),
        state.view.kind === "collection" &&
          h(
            "button",
            { type: "button", class: "btn btn-line", onClick: () => promptRename(collectionById(state.view.id)) },
            "Rename",
          ),
        state.view.kind === "collection" &&
          h(
            "button",
            {
              type: "button",
              class: "btn btn-line",
              id: "download-all",
              onClick: () => downloadMany(state.visible.map((m) => m.pk), viewTitle(state.view)),
            },
            "Download all",
          ),
      ),
    ),
    h("section", { class: "sheet", id: "sheet", "aria-label": `${viewTitle(state.view)} ${reelWord}s` }),
  );
  renderSummary();
  renderGrid();
}

function renderSummary() {
  const summary = $("#view-summary");
  if (!summary) return;
  const word = state.reelsOnly ? "reel" : "saved post";
  const shown = state.visible.length;
  const text = state.query ? `${plural(shown, word)} match “${state.query}”` : plural(shown, word);
  summary.replaceChildren(text);
  const downloadAll = $("#download-all");
  if (downloadAll) downloadAll.disabled = shown === 0;
  if (state.sync.running) {
    summary.append(
      h(
        "span",
        { class: "summary-sync" },
        h("span", { class: "spinner", "aria-hidden": "true" }),
        `Syncing, ${plural(state.sync.total ?? 0, "save")} read so far`,
      ),
    );
  }
}

const tileCache = new Map();

function tileFor(m) {
  let el = tileCache.get(m.pk);
  if (!el) {
    el = tile(m);
    tileCache.set(m.pk, el);
    if (state.arriving.has(m.pk)) {
      el.classList.add("arriving");
      el.addEventListener("animationend", () => el.classList.remove("arriving"), { once: true });
    }
  }
  state.arriving.delete(m.pk);
  el.setAttribute("aria-pressed", String(state.selected.has(m.pk)));
  return el;
}

function renderGrid() {
  const sheet = $("#sheet");
  if (!sheet) return;
  state.visible = applyFilters(mediaForView(state.view));
  sheet.classList.toggle("selecting", state.selecting);
  if (state.visible.length) {
    const key = state.visible.map((m) => m.pk).join(",");
    if (sheet.dataset.key !== key) {
      sheet.replaceChildren(...state.visible.map(tileFor));
      sheet.dataset.key = key;
    }
    return;
  }
  sheet.dataset.key = "";
  let empty;
  if (state.query) {
    empty = renderEmpty("No matches", `No ${state.reelsOnly ? "reels" : "saved posts"} match “${state.query}”.`);
  } else if (state.view.kind === "uncollected") {
    empty = renderEmpty("Everything is filed", "Every saved reel is in at least one collection.");
  } else if (state.view.kind === "collection") {
    empty = renderEmpty("This collection is empty", "Drag reels onto its name in the sidebar to add them.");
  } else {
    empty = renderEmpty("No saved reels", "Save a reel on Instagram, then sync. Turn off Reels only to see your other saves.");
  }
  sheet.replaceChildren(empty);
}

function renderSelectionState() {
  $("#sheet")?.classList.toggle("selecting", state.selecting);
  for (const el of document.querySelectorAll(".tile")) {
    el.setAttribute("aria-pressed", String(state.selected.has(el.dataset.pk)));
  }
  const selectButton = document.querySelector('.toolbar [aria-pressed]');
  if (selectButton) {
    selectButton.setAttribute("aria-pressed", String(state.selecting));
    selectButton.textContent = state.selecting ? "Done selecting" : "Select";
  }
  renderSelectionBar();
}

function renderSelectionBar() {
  const bar = $("#selection-bar");
  const pks = selectedInOrder();
  if (!state.signedIn || !pks.length) {
    bar.hidden = true;
    return;
  }
  const inCollection = state.view.kind === "collection" ? collectionById(state.view.id) : null;
  const finish = () => {
    clearSelection();
    render();
  };
  const addButton = h("button", {
    type: "button",
    onClick: () =>
      openMenu(addButton, collectionMenuItems(async (id) => (await addToCollection(id, pks)) && finish(), { pks })),
  }, "Add to collection");
  const moveButton =
    inCollection &&
    h("button", {
      type: "button",
      onClick: () =>
        openMenu(
          moveButton,
          collectionMenuItems(async (id) => (await moveToCollection(inCollection.id, id, pks)) && finish(), {
            exclude: inCollection.id,
            pks,
          }),
        ),
    }, "Move to");

  bar.replaceChildren(
    ...[
    h("span", { class: "count" }, `${pks.length.toLocaleString()} selected`),
    addButton,
    moveButton,
    inCollection &&
      h("button", {
        type: "button",
        class: "danger",
        onClick: async () => (await removeFromCollection(inCollection.id, pks)) && finish(),
      }, `Remove from ${inCollection.name}`),
    h("button", { type: "button", class: "danger", onClick: async () => (await unsavePosts(pks)) && finish() }, "Unsave"),
    h("button", { type: "button", onClick: () => downloadMany(pks, `${viewTitle(state.view)} selection`) }, "Download"),
    h("button", { type: "button", onClick: () => { clearSelection(); renderSelectionState(); } }, "Clear"),
    ].filter(Boolean),
  );
  bar.hidden = false;
}

/* Grid interaction */

document.addEventListener("click", (event) => {
  const el = event.target.closest(".tile");
  if (!el) return;
  const pk = el.dataset.pk;
  if (event.shiftKey || event.metaKey || event.ctrlKey || state.selecting) {
    toggleSelect(pk, { range: event.shiftKey });
  } else {
    openViewer(state.visible.findIndex((m) => m.pk === pk));
  }
});

const DRAG_PREVIEW_CARDS = 3;
const DRAG_START_DISTANCE = 6;
const DRAG_CURSOR_OFFSET = { x: 48, y: 70 };

function dragPreview(pks) {
  const cards = pks.slice(0, DRAG_PREVIEW_CARDS).map((pk, depth) =>
    h("img", { class: "drag-card", src: `/media/${pk}/thumb`, alt: "", style: `--depth: ${depth}` }),
  );
  const preview = h(
    "div",
    { class: "drag-stack", "aria-hidden": "true" },
    cards.reverse(),
    pks.length > 1 && h("span", { class: "drag-count" }, plural(pks.length, "reel")),
  );
  document.body.append(preview);
  return preview;
}

let pointerDrag = null;
let suppressNextClick = false;

function dropTargetAt(x, y) {
  return document.elementFromPoint(x, y)?.closest("[data-drop-collection]") ?? null;
}

function moveDrag(x, y) {
  pointerDrag.x = x;
  pointerDrag.y = y;
  if (pointerDrag.frame) return;
  pointerDrag.frame = requestAnimationFrame(() => {
    if (!pointerDrag) return;
    pointerDrag.frame = null;
    const { preview } = pointerDrag;
    preview.style.transform = `translate(${pointerDrag.x - DRAG_CURSOR_OFFSET.x}px, ${pointerDrag.y - DRAG_CURSOR_OFFSET.y}px)`;
    const target = dropTargetAt(pointerDrag.x, pointerDrag.y);
    if (target !== pointerDrag.target) {
      pointerDrag.target?.classList.remove("drop-target");
      target?.classList.add("drop-target");
      pointerDrag.target = target;
    }
  });
}

function beginDrag(drag) {
  const pk = drag.tile.dataset.pk;
  drag.pks = state.selected.has(pk) ? selectedInOrder() : [pk];
  drag.preview = dragPreview(drag.pks);
  drag.preview.classList.add("live");
  drag.started = true;
  document.body.classList.add("dragging-reels");
  drag.pks.forEach((p) => document.querySelector(`.tile[data-pk="${p}"]`)?.classList.add("dragging"));
}

function endDrag() {
  const drag = pointerDrag;
  pointerDrag = null;
  if (!drag?.started) return null;
  if (drag.frame) cancelAnimationFrame(drag.frame);
  drag.preview.remove();
  drag.target?.classList.remove("drop-target");
  document.body.classList.remove("dragging-reels");
  document.querySelectorAll(".tile.dragging").forEach((el) => el.classList.remove("dragging"));
  return drag;
}

document.addEventListener("dragstart", (event) => {
  if (event.target.closest?.(".tile")) event.preventDefault();
});

document.addEventListener("pointerdown", (event) => {
  const tile = event.target.closest?.(".tile");
  if (!tile || event.button !== 0 || $("#viewer").open) return;
  pointerDrag = { tile, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, started: false };
});

document.addEventListener("pointermove", (event) => {
  if (!pointerDrag || event.pointerId !== pointerDrag.pointerId) return;
  if (!pointerDrag.started) {
    const distance = Math.hypot(event.clientX - pointerDrag.startX, event.clientY - pointerDrag.startY);
    if (distance < DRAG_START_DISTANCE) return;
    beginDrag(pointerDrag);
  }
  event.preventDefault();
  moveDrag(event.clientX, event.clientY);
});

document.addEventListener("pointerup", async (event) => {
  if (!pointerDrag || event.pointerId !== pointerDrag.pointerId) return;
  const target = pointerDrag.started ? dropTargetAt(event.clientX, event.clientY) : null;
  const drag = endDrag();
  if (!drag) return;
  suppressNextClick = true;
  setTimeout(() => { suppressNextClick = false; }, 0);
  if (target && (await addToCollection(target.dataset.dropCollection, drag.pks))) {
    clearSelection();
    render();
  }
});

document.addEventListener("pointercancel", () => endDrag());

document.addEventListener(
  "click",
  (event) => {
    if (!suppressNextClick) return;
    suppressNextClick = false;
    event.preventDefault();
    event.stopPropagation();
  },
  { capture: true },
);

document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const el = event.target.closest?.(".tile");
  if (!el || el !== event.target) return;
  event.preventDefault();
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: event.shiftKey, metaKey: event.metaKey }));
});

// WebKit shows :focus-visible after clicks and after a dialog returns focus, which reads as a second selection style.
document.addEventListener("keydown", (event) => {
  if (event.key === "Tab") document.body.classList.add("tabbing");
});
document.addEventListener("pointerdown", () => document.body.classList.remove("tabbing"));

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && pointerDrag?.started) {
    endDrag();
    return;
  }
  if (event.key === "Escape" && state.selected.size && !$("#viewer").open && $("#menu").hidden) {
    clearSelection();
    renderSelectionState();
  }
  if ($("#viewer").open && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? -1 : 1;
    if (!stepSlide(delta)) stepViewer(delta);
  }
});

document.addEventListener(
  "keydown",
  (event) => {
    if (event.key !== " " || !$("#viewer").open) return;
    if ($("#confirm-prompt").open || $("#name-prompt").open) return;
    if (event.target.closest("input, textarea, select")) return;
    const video = $("#viewer .viewer-stage video");
    if (!video) return;
    event.preventDefault();
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  },
  { capture: true },
);

/* Viewer */

const ICON_PATHS = {
  prev: "M15 5 L8 12 L15 19",
  next: "M9 5 L16 12 L9 19",
  close: "M6 6 L18 18 M18 6 L6 18",
  slides: "M8 4 H18 A2 2 0 0 1 20 6 V16 M4 8 H14 A2 2 0 0 1 16 10 V20 H6 A2 2 0 0 1 4 18 Z",
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "icon");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICON_PATHS[name]);
  svg.append(path);
  return svg;
}

function openViewer(index) {
  if (index < 0) return;
  state.viewerPk = state.visible[index].pk;
  state.viewerSlide = 0;
  renderViewer();
  const viewer = $("#viewer");
  if (!viewer.open) {
    viewer.showModal();
    viewer.focus();
  }
}

function renderViewerCollections(section, m) {
  const refresh = () => {
    renderViewerCollections(section, state.byPk.get(m.pk) || m);
    renderSidebar();
  };
  const current = state.library.collections.filter((c) => m.collections.has(c.id));
  const chips = current.map((c) =>
    h(
      "span",
      { class: "chip" },
      c.name,
      h(
        "button",
        {
          type: "button",
          "aria-label": `Remove from ${c.name}`,
          title: `Remove from ${c.name}`,
          onClick: async (event) => {
            event.currentTarget.disabled = true;
            await removeFromCollection(c.id, [m.pk]);
            refresh();
          },
        },
        icon("close"),
      ),
    ),
  );
  const addButton = h(
    "button",
    {
      type: "button",
      class: "chip-add",
      onClick: () =>
        openMenu(
          addButton,
          collectionMenuItems(async (id) => { await addToCollection(id, [m.pk]); refresh(); }, {
            pks: [m.pk],
            excludeIds: m.collections,
          }),
        ),
    },
    "Add to collection",
  );
  section.replaceChildren(
    h("h3", {}, "Collections"),
    h(
      "div",
      { class: "chips" },
      chips.length ? chips : h("span", { class: "chips-empty" }, "Not in a collection"),
      addButton,
    ),
  );
}

// Waits for the viewer to settle so arrowing through reels does not queue a comments request per reel behind the 1 s API spacing.
const COMMENTS_SETTLE_MS = 350;
const commentThreads = new Map();
let commentsTimer = null;

const cursorQuery = (cursor) => (cursor ? `?cursor=${encodeURIComponent(cursor)}` : "");

function refreshComments(pk) {
  const section = $("#viewer .viewer-comments");
  if (section?.dataset.pk === pk) renderViewerComments(section, pk);
}

async function loadPage(pk, page, path) {
  page.loading = true;
  page.error = null;
  refreshComments(pk);
  try {
    const result = await api(`${path}${cursorQuery(page.cursor)}`);
    page.comments.push(...result.comments);
    page.cursor = result.cursor;
    if (result.count != null) page.count = result.count;
    page.loaded = true;
  } catch (err) {
    page.error = err.message;
  } finally {
    page.loading = false;
    refreshComments(pk);
  }
}

function loadComments(pk) {
  if (!commentThreads.has(pk)) commentThreads.set(pk, { comments: [], cursor: null, count: 0, replies: new Map() });
  return loadPage(pk, commentThreads.get(pk), `/api/media/${pk}/comments`);
}

function toggleReplies(pk, comment) {
  const replies = commentThreads.get(pk).replies;
  if (!replies.has(comment.pk)) replies.set(comment.pk, { comments: [], cursor: null, open: false });
  const page = replies.get(comment.pk);
  page.open = !page.open;
  if (page.open && !page.loaded && !page.loading) loadPage(pk, page, `/api/media/${pk}/comments/${comment.pk}/replies`);
  else refreshComments(pk);
}

function scheduleComments(pk) {
  clearTimeout(commentsTimer);
  if (commentThreads.has(pk)) return;
  commentsTimer = setTimeout(() => {
    if (state.viewerPk === pk && !commentThreads.has(pk)) loadComments(pk);
  }, COMMENTS_SETTLE_MS);
}

function pageFooter(page, onMore, moreLabel) {
  if (page.loading) return h("p", { class: "comments-note" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Loading");
  if (page.error) {
    return h(
      "p",
      { class: "comments-note error" },
      page.error,
      " ",
      h("button", { type: "button", class: "link-button", onClick: onMore }, "Try again"),
    );
  }
  return page.cursor && h("button", { type: "button", class: "link-button comments-more", onClick: onMore }, moreLabel);
}

function commentItem(pk, comment, replyPage) {
  const meta = [formatRelative(comment.createdAt), comment.likeCount > 0 && plural(comment.likeCount, "like")].filter(Boolean);
  const repliesUrl = `/api/media/${pk}/comments/${comment.pk}/replies`;
  return h(
    "li",
    { class: "comment" },
    h(
      "p",
      { class: "comment-text" },
      comment.username ? profileLink(comment.username, comment.username, { class: "comment-user" }) : h("strong", {}, "unknown"),
      " ",
      comment.text,
    ),
    h(
      "p",
      { class: "comment-meta" },
      meta.join(" · "),
      comment.replyCount > 0 &&
        h(
          "button",
          { type: "button", class: "link-button", "aria-expanded": String(Boolean(replyPage?.open)), onClick: () => toggleReplies(pk, comment) },
          replyPage?.open ? "Hide replies" : comment.replyCount === 1 ? "View 1 reply" : `View ${comment.replyCount.toLocaleString()} replies`,
        ),
    ),
    replyPage?.open &&
      h(
        "div",
        { class: "comment-replies" },
        replyPage.comments.length > 0 && h("ul", { class: "comment-list" }, replyPage.comments.map((reply) => commentItem(pk, reply))),
        pageFooter(replyPage, () => loadPage(pk, replyPage, repliesUrl), "More replies"),
      ),
  );
}

function renderViewerComments(section, pk) {
  section.dataset.pk = pk;
  const thread = commentThreads.get(pk);
  const heading = h("h3", {}, thread?.count ? `Comments (${thread.count.toLocaleString()})` : "Comments");
  if (!thread) {
    section.replaceChildren(heading, h("p", { class: "comments-note" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Loading"));
    return;
  }
  const body = thread.comments.length
    ? h("ul", { class: "comment-list" }, thread.comments.map((comment) => commentItem(pk, comment, thread.replies.get(comment.pk))))
    : thread.loaded && h("p", { class: "comments-note" }, "No comments.");
  section.replaceChildren(h("div", {}, heading, body, pageFooter(thread, () => loadComments(pk), "More comments")));
}

const PREFETCH_DISTANCE = 3;
const prefetched = new Set();

const VIDEO_READY_EVENTS = ["loadeddata", "canplay", "playing"];
const videoPool = new Map();
const viewerVolume = { muted: false, volume: 1 };

const slideQuery = (slide) => (slide ? `?slide=${slide}` : "");

function pooledVideo(m, slide = 0) {
  const key = `${m.pk}:${slide}`;
  let video = videoPool.get(key);
  if (!video) {
    video = h("video", {
      src: `/media/${m.pk}/file${slideQuery(slide)}`,
      preload: "auto",
      loop: true,
      playsinline: true,
      poster: `/media/${m.pk}/thumb${slideQuery(slide)}`,
    });
    video.addEventListener("volumechange", () => {
      viewerVolume.muted = video.muted;
      viewerVolume.volume = video.volume;
    });
    videoPool.set(key, video);
  }
  video.muted = viewerVolume.muted;
  video.volume = viewerVolume.volume;
  return video;
}

function trimVideoPool(keep) {
  for (const [key, video] of videoPool) {
    if (keep.has(key)) continue;
    video.pause();
    video.removeAttribute("src");
    video.load();
    videoPool.delete(key);
  }
}

const preloadedImages = new Map();

function preloadSlide(m, slide, keep) {
  if (!m || slide >= m.slides.length) return;
  if (m.slides[slide].isVideo) {
    pooledVideo(m, slide);
    keep.add(`${m.pk}:${slide}`);
    return;
  }
  const url = `/media/${m.pk}/file${slideQuery(slide)}`;
  if (!preloadedImages.has(url)) {
    const image = new Image();
    image.src = url;
    preloadedImages.set(url, image);
  }
}

function prefetchAround(index) {
  const current = state.visible[index];
  const keep = new Set([`${current?.pk}:${state.viewerSlide}`]);
  preloadSlide(current, state.viewerSlide + 1, keep);
  preloadSlide(state.visible[index - 1], 0, keep);
  preloadSlide(state.visible[index + 1], 0, keep);
  trimVideoPool(keep);
  for (let offset = 2; offset <= PREFETCH_DISTANCE; offset++) {
    for (const neighbor of [state.visible[index + offset], state.visible[index - offset]]) {
      if (!neighbor || prefetched.has(neighbor.pk)) continue;
      prefetched.add(neighbor.pk);
      fetch(`/media/${neighbor.pk}/prefetch`, { method: "POST" }).catch(() => prefetched.delete(neighbor.pk));
    }
  }
}

function stepSlide(delta) {
  const m = state.byPk.get(state.viewerPk);
  const next = state.viewerSlide + delta;
  if (!m || next < 0 || next >= m.slides.length) return false;
  state.viewerSlide = next;
  renderViewer();
  return true;
}

function stepViewer(delta) {
  const next = state.visible.findIndex((m) => m.pk === state.viewerPk) + delta;
  if (next >= 0 && next < state.visible.length) openViewer(next);
}

function renderViewer() {
  const index = state.visible.findIndex((x) => x.pk === state.viewerPk);
  const m = state.visible[index];
  const viewer = $("#viewer");
  if (!m) {
    viewer.close();
    return;
  }

  const loading = h("div", { class: "loading" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Loading");
  const slide = Math.min(state.viewerSlide ?? 0, m.slides.length - 1);
  let media;
  let mediaReady;
  if (m.slides[slide].isVideo) {
    media = pooledVideo(m, slide);
    media.controls = true;
    media.onerror = () => { loading.textContent = "Video unavailable. Sync and try again."; };
    mediaReady = media.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA;
    if (!mediaReady) for (const type of VIDEO_READY_EVENTS) media.addEventListener(type, () => loading.remove(), { once: true });
  } else {
    media = h("img", { src: `/media/${m.pk}/file${slideQuery(slide)}`, alt: m.caption || "", onLoad: () => loading.remove() });
    mediaReady = media.complete;
  }

  const slideControls =
    m.slides.length > 1 && [
      slide > 0 &&
        h("button", { type: "button", class: "slide-nav prev", "aria-label": "Previous item", onClick: () => stepSlide(-1) }, icon("prev")),
      slide < m.slides.length - 1 &&
        h("button", { type: "button", class: "slide-nav next", "aria-label": "Next item", onClick: () => stepSlide(1) }, icon("next")),
      h("span", { class: "slide-count" }, `${slide + 1} / ${m.slides.length}`),
      h(
        "span",
        { class: "slide-dots", "aria-hidden": "true" },
        m.slides.map((_, i) => h("span", { class: i === slide ? "dot current" : "dot" })),
      ),
    ];

  const meta = [
    m.takenAt && `Posted ${new Date(m.takenAt * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}`,
    m.playCount && `${m.playCount.toLocaleString()} plays`,
    m.likeCount && `${m.likeCount.toLocaleString()} likes`,
  ].filter(Boolean);

  const collectionsSection = h("div", { class: "viewer-section" });
  renderViewerCollections(collectionsSection, m);
  const commentsSection = h("section", { class: "viewer-section viewer-comments", "aria-label": "Comments" });
  renderViewerComments(commentsSection, m.pk);

  const navButton = (delta, label, enabled) =>
    h(
      "button",
      {
        type: "button",
        class: `viewer-nav ${delta < 0 ? "prev" : "next"}`,
        "aria-label": label,
        disabled: !enabled,
        onClick: () => stepViewer(delta),
      },
      icon(delta < 0 ? "prev" : "next"),
    );

  const alreadyShowing = viewer.contains(media);
  const wasPlaying = alreadyShowing && !media.paused;
  for (const playing of viewer.querySelectorAll("video")) {
    if (playing !== media) playing.pause();
  }
  viewer.replaceChildren(
    navButton(-1, "Previous reel", index > 0),
    h(
      "div",
      { class: "viewer-body" },
      h("div", { class: "viewer-stage" }, !mediaReady && loading, media, slideControls),
      h(
        "div",
        { class: "viewer-panel" },
        h("h2", { class: "viewer-user" }, m.username ? profileLink(m.username, `@${m.username}`) : "Unknown account"),
        meta.length > 0 && h("p", { class: "viewer-meta" }, meta.join(", ")),
        h(
          "div",
          { class: "viewer-scroll" },
          h("p", { class: `viewer-caption${m.caption ? "" : " blank"}` }, m.caption || "No caption."),
          commentsSection,
        ),
        collectionsSection,
        h(
          "div",
          { class: "viewer-actions" },
          h("button", { type: "button", class: "btn btn-ink", onClick: () => downloadOne(m.pk) }, "Download"),
          h("button", { type: "button", class: "btn btn-line danger", onClick: () => unsavePosts([m.pk]) }, "Unsave"),
          m.code &&
            h("a", { class: "btn btn-line", href: `https://www.instagram.com/reel/${m.code}/`, target: "_blank", rel: "noopener" }, "Open in Instagram"),
        ),
      ),
      h("button", { type: "button", class: "viewer-close", "aria-label": "Close", onClick: () => viewer.close() }, icon("close")),
    ),
    navButton(1, "Next reel", index < state.visible.length - 1),
  );
  if (m.slides[slide].isVideo && (!alreadyShowing || wasPlaying)) {
    if (!alreadyShowing) media.currentTime = 0;
    // WebKit refuses autoplay outside a direct click and then never loads frames for the paused video.
    media.play().catch(() => loading.remove());
  } else if (m.slides[slide].isVideo) {
    loading.remove();
  }
  prefetchAround(index);
  scheduleComments(m.pk);
}

$("#viewer").addEventListener("close", () => {
  clearTimeout(commentsTimer);
  trimVideoPool(new Set());
  closeMenu();
  document.body.append($("#menu"));
  $("#viewer").replaceChildren();
  renderGrid();
  renderSummary();
});

$("#viewer").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});

/* Boot */

async function signOut() {
  await api("/api/session", { method: "DELETE" });
  state.signedIn = false;
  state.library = null;
  clearSelection();
  render();
}

async function boot() {
  const session = await api("/api/state");
  state.signedIn = session.signedIn;
  state.username = session.username;
  if (state.signedIn) {
    setLibrary(await api("/api/library"));
    state.sync = await api("/api/sync");
    if (state.sync.running) pollSync();
  }
  render();
}

boot().catch((err) => toast(err.message, { error: true }));
