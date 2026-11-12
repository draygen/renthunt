"use strict";

// ---------- small helpers ----------
const $ = (sel) => document.querySelector(sel);

/** Build a DOM node. Strings become text nodes, so listing content is never parsed as HTML. */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, options = {}) {
  if (options.body && typeof options.body !== "string") {
    options.body = JSON.stringify(options.body);
    options.headers = { "Content-Type": "application/json" };
  }
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return resp.json();
}

let toastTimer;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

const money = (n) => (n === null || n === undefined ? "—" : "$" + Number(n).toLocaleString());
const SOURCE_NAMES = { craigslist: "Craigslist", rentcom: "Rent.com", watch: "Watched page" };
const STATUS_NAMES = { new: "New", seen: "Seen", contacted: "Contacted", touring: "Touring",
  applied: "Applied", rejected: "Not interested" };

function bedsText(l) {
  const one = (b) => (b === 0 ? "Studio" : `${b} bd`);
  if (l.beds_min === null) return "";
  if (l.beds_max === null || l.beds_max === l.beds_min) return one(l.beds_min);
  return `${l.beds_min === 0 ? "Studio" : l.beds_min}–${l.beds_max} bd`;
}

function ago(iso) {
  if (!iso) return "";
  const hours = (Date.now() - new Date(iso).getTime()) / 36e5;
  if (hours < 1) return "just now";
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

// ---------- state ----------
const TABS = [["matches", "Matches"], ["new", "New"], ["favorites", "Favorites"],
  ["pipeline", "In progress"], ["all", "All"], ["hidden", "Hidden"]];
const VISITOR_TABS = ["matches", "new", "all"];
const state = { view: "matches", listings: [], counts: {}, wasRunning: false, mapOn: false, admin: false, local: false,
  spotlight: null,   // {ids, label, center, distances}: the assistant's current result set
  byId: {} };        // every listing seen so far, for labelling #ids in chat
let sessionId = localStorage.getItem("renthunt-session");
if (!sessionId) {
  sessionId = "s" + Date.now().toString(36);
  localStorage.setItem("renthunt-session", sessionId);
}

// ---------- overview / status ----------
function renderTabs() {
  const tabs = TABS.filter(([key]) => state.admin || VISITOR_TABS.includes(key));
  $("#tabs").replaceChildren(...tabs.map(([key, label]) =>
    h("button", { class: key === state.view ? "active" : "", onclick: () => { state.view = key; renderTabs(); loadListings(); } },
      label, h("span", { class: "n" }, state.counts[key] ?? ""))));
  for (const button of $("#tabs").children) button.addEventListener("click", () => { state.spotlight = null; }, true);
}

async function loadOverview() {
  let o;
  try { o = await api("/api/overview"); } catch (_) { $("#status").textContent = "Server not reachable"; return; }
  state.counts = o.counts;
  renderTabs();
  renderTowns(o.towns || []);
  const status = $("#status");
  status.classList.toggle("running", o.running);
  $("#btn-hunt").disabled = o.running;
  if (o.running) {
    status.textContent = o.step || "Hunting…";
  } else {
    const parts = [];
    if (o.last_run) parts.push(`Last hunt ${ago(o.last_run.finished_at || o.last_run.started_at)}` +
      (o.last_run.status !== "ok" ? ` (${o.last_run.status})` : ""));
    parts.push(o.next_run
      ? `next at ${new Date(o.next_run).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
      : "schedule off");
    status.textContent = parts.join(" · ");
  }
  if (state.wasRunning && !o.running) {
    const found = o.last_run?.summary?.new_matches ?? 0;
    toast(found ? `Hunt finished: ${found} new match${found === 1 ? "" : "es"}` : "Hunt finished: nothing new");
    loadListings();
  }
  state.wasRunning = o.running;
  $("#ai-status").textContent = o.ai.ok
    ? (o.ai.model_installed ? o.ai.model : `${o.ai.model} is not installed`)
    : "offline";
}

// ---------- listings ----------
function filterQuery() {
  const q = new URLSearchParams({ view: state.view, sort: $("#f-sort").value });
  if ($("#f-text").value.trim()) q.set("text", $("#f-text").value.trim());
  if ($("#f-town").value) q.set("towns", $("#f-town").value);
  if ($("#f-source").value) q.set("sources", $("#f-source").value);
  if ($("#f-max").value) q.set("max_price", $("#f-max").value);
  const beds = $("#f-beds").value;
  if (beds !== "") {
    q.set("min_beds", beds);
    if (beds !== "4") q.set("max_beds", beds);
  }
  return q;
}

function renderSpotlight() {
  const bar = $("#spotlight");
  bar.hidden = !state.spotlight;
  if (!state.spotlight) return;
  const n = state.spotlight.ids.length;
  bar.replaceChildren(`Showing ${n} listing${n === 1 ? "" : "s"} ${state.spotlight.label}`,
    h("div", { class: "spacer" }),
    h("button", { onclick: () => { state.spotlight = null; loadListings(); } }, "Back to all listings"));
}

async function loadListings() {
  try {
    const data = await api("/api/listings?" + (state.spotlight
      ? "ids=" + state.spotlight.ids.join(",") : filterQuery()));
    state.listings = data.listings;
    for (const l of data.listings) state.byId[l.id] = l;
    renderSpotlight();
    $("#result-count").textContent = `${data.total} listing${data.total === 1 ? "" : "s"}`;
    renderGrid();
    if (state.mapOn) renderMap();
  } catch (err) { toast(err.message); }
}

function flagBadges(l) {
  const out = [];
  if (l.status === "new" && l.is_match) out.push(h("span", { class: "badge new" }, "NEW"));
  if (!l.active) out.push(h("span", { class: "badge bad" }, "No longer listed"));
  for (const flag of l.flags) {
    const kind = flag.startsWith("possible scam") ? "bad" : flag.startsWith("price drop") ? "good" : "warn";
    out.push(h("span", { class: "badge " + kind, title: flag }, flag.split(":")[0]));
  }
  if (!["new", "seen"].includes(l.status)) out.push(h("span", { class: "badge good" }, STATUS_NAMES[l.status]));
  out.push(h("span", { class: "badge" }, SOURCE_NAMES[l.source] || l.source));
  return out;
}

async function patchListing(id, patch, quiet) {
  try {
    const updated = await api(`/api/listings/${id}`, { method: "PATCH", body: patch });
    if (!quiet) { loadListings(); loadOverview(); }
    return updated;
  } catch (err) { toast(err.message); }
}

function renderCard(l) {
  const price = l.match_price ?? l.price;
  const range = l.price_max && l.price_max > price;
  const stop = (fn) => (event) => { event.stopPropagation(); fn(); };
  const distance = state.spotlight?.distances?.[l.id];
  return h("article", { class: "card" + (l.active ? "" : " inactive"), id: "card-" + l.id, onclick: () => openDetail(l.id) },
    h("div", { class: "photo", style: l.images[0] ? `background-image:url("${encodeURI(l.images[0])}")` : "" },
      l.images[0] ? "" : "🏠"),
    l.is_match ? h("div", { class: "score", title: "Match score out of 100" }, l.score) : null,
    h("div", { class: "body" },
      h("div", { class: "top" },
        h("span", { class: "price" }, (range ? "from " : "") + money(price)),
        l.price_prev && l.price_prev > l.price ? h("span", { class: "was" }, money(l.price_prev)) : null),
      h("div", { class: "facts" }, [bedsText(l), l.baths ? `${l.baths} ba` : "", l.sqft ? `${l.sqft} sq ft` : "",
        l.town || ""].filter(Boolean).join(" · "),
        distance !== undefined ? h("span", { class: "dist" }, ` · ${distance} mi away`) : null),
      h("div", { class: "title" }, l.title),
      h("div", { class: "why" }, (l.is_match ? l.reasons.slice(1) : l.reasons).join(" · ")),
      h("div", { class: "badges" }, flagBadges(l), h("span", { class: "badge" }, ago(l.posted_at || l.first_seen))),
      h("div", { class: "actions" },
        state.admin ? h("button", { class: "fav" + (l.favorite ? " on" : ""), title: "Favorite",
          onclick: stop(() => patchListing(l.id, { favorite: !l.favorite })) }, l.favorite ? "♥ Saved" : "♡ Save") : null,
        state.admin ? h("button", { title: "Hide this listing",
          onclick: stop(() => patchListing(l.id, { hidden: !l.hidden })) }, l.hidden ? "Unhide" : "Hide") : null,
        h("div", { class: "spacer" }),
        h("button", { title: "Open the original listing", onclick: stop(() => window.open(l.url, "_blank", "noopener")) }, "Open ↗"))));
}

function renderGrid() {
  const grid = $("#grid");
  if (!state.listings.length) {
    const firstRun = !state.counts.all;
    grid.replaceChildren(h("div", { class: "empty" }, firstRun
      ? "No listings yet. Press “Hunt now” to run the first search (it takes a couple of minutes)."
      : "Nothing here with these filters."));
    return;
  }
  grid.replaceChildren(...state.listings.map(renderCard));
}

// ---------- map ----------
let map, markerLayer;
const markers = {};

/** Bring one listing forward: flash its card and open its pin, or open the detail view. */
function focusListing(id) {
  const card = document.getElementById("card-" + id);
  if (!card || isMobile()) { openDetail(id); return; }
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 2500);
  if (state.mapOn && markers[id]) markers[id].openPopup();
}

function setMap(on) {
  state.mapOn = on;
  $("#map").hidden = !on;
  $("#btn-map").textContent = on ? "Hide map" : "Map";
  if (on) renderMap();
}
function renderMap() {
  if (!window.L) { toast("The map library could not be loaded (offline?)"); return; }
  if (!map) {
    map = L.map("map").setView([42.65, -71.25], 11);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 18, attribution: "© OpenStreetMap contributors" }).addTo(map);
    markerLayer = L.layerGroup().addTo(map);
  }
  markerLayer.clearLayers();
  for (const id of Object.keys(markers)) delete markers[id];
  const points = [];
  const spot = state.spotlight;
  if (spot?.center) {
    L.circleMarker([spot.center.lat, spot.center.lng], { radius: 9, color: "#fff", fillColor: "#1d4ed8", fillOpacity: 1, weight: 3 })
      .bindTooltip(spot.center.label, { permanent: true, direction: "bottom", className: "home" }).addTo(markerLayer);
    points.push([spot.center.lat, spot.center.lng]);
  }
  for (const l of state.listings) {
    if (l.lat === null || l.lng === null) continue;
    const color = l.score >= 75 ? "#067647" : l.score >= 50 ? "#b45309" : "#667085";
    const popup = h("div", {},
      h("strong", {}, `${money(l.match_price ?? l.price)} · ${bedsText(l)}`), h("br"),
      l.title.slice(0, 70), h("br"),
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); openDetail(l.id); } }, "Details"));
    const marker = L.circleMarker([l.lat, l.lng], spot
      ? { radius: 10, color: "#fff", fillColor: "#0f766e", fillOpacity: 1, weight: 2 }
      : { radius: 8, color, fillColor: color, fillOpacity: 0.75, weight: 1 }).bindPopup(popup).addTo(markerLayer);
    if (spot) marker.bindTooltip((isMobile() ? "" : `#${l.id} `) + money(l.match_price ?? l.price), { permanent: true, direction: "top", className: "pin", offset: [0, -8] });
    markers[l.id] = marker;
    points.push([l.lat, l.lng]);
  }
  if (points.length) map.fitBounds(points, { padding: [30, 30], maxZoom: 14 });
  setTimeout(() => map.invalidateSize(), 50);
}

// ---------- listing detail ----------
async function openDetail(id) {
  let l;
  try { l = await api(`/api/listings/${id}`); } catch (err) { toast(err.message); return; }
  if (l.status === "new" && state.admin) { l = (await patchListing(id, { status: "seen" }, true)) || l; }
  const dlg = $("#detail");
  const close = () => { dlg.close(); loadListings(); loadOverview(); };
  const statusSelect = h("select", { onchange: (e) => patchListing(id, { status: e.target.value }, true) },
    Object.entries(STATUS_NAMES).filter(([k]) => k !== "new").map(([k, label]) =>
      h("option", { value: k, selected: l.status === k }, label)));
  const notes = h("textarea", { placeholder: "Your notes: who you spoke to, tour times, impressions…",
    onchange: (e) => patchListing(id, { notes: e.target.value }, true) }, l.notes);
  const favorite = h("button", { class: "fav" + (l.favorite ? " on" : "") }, l.favorite ? "♥ Saved" : "♡ Save");
  favorite.onclick = async () => {
    l = (await patchListing(id, { favorite: !l.favorite }, true)) || l;
    favorite.className = "fav" + (l.favorite ? " on" : "");
    favorite.textContent = l.favorite ? "♥ Saved" : "♡ Save";
  };
  const facts = [bedsText(l), l.baths ? `${l.baths} ba` : "", l.sqft ? `${l.sqft} sq ft` : ""].filter(Boolean).join(" · ");

  dlg.replaceChildren(
    h("div", { class: "dlg-head" },
      h("h2", {}, `#${l.id} · ${l.title}`),
      h("a", { href: l.url, target: "_blank", rel: "noopener" }, h("button", { class: "primary" }, "Open listing ↗")),
      h("button", { onclick: close }, "Close")),
    h("div", { class: "dlg-body" },
      l.images.length ? h("div", { class: "gallery" }, l.images.map((src) => h("img", { src, loading: "lazy", alt: "" }))) : null,
      h("div", { class: "detail-grid" },
        h("div", {},
          h("h3", {}, `${money(l.match_price ?? l.price)}${l.price_max > (l.match_price ?? l.price) ? " – " + money(l.price_max) : ""} / month`),
          h("div", { class: "facts" }, [facts, l.address || l.town, l.available ? `Available: ${l.available}` : ""].filter(Boolean).join(" · ")),
          h("div", { class: "badges", style: "margin-top:8px" }, flagBadges(l)),
          l.is_match
            ? h("p", {}, h("strong", {}, `Match score ${l.score}/100. `), l.reasons.join(" · "))
            : h("p", { class: "muted" }, h("strong", {}, "Not a match: "), l.reasons.join(" · ")),
          l.flags.length ? h("p", { class: "muted" }, l.flags.join(" · ")) : null,
          l.units.length > 1 ? [h("h3", {}, "Floor plans"), h("table", {},
            h("tr", {}, h("th", {}, "Beds"), h("th", {}, "Rent"), h("th", {}, "Sq ft")),
            l.units.map((u) => h("tr", {},
              h("td", {}, u.beds === 0 ? "Studio" : u.beds ?? "—"),
              h("td", {}, money(u.price_low) + (u.price_high && u.price_high !== u.price_low ? " – " + money(u.price_high) : "")),
              h("td", {}, u.sqft ?? "—"))))] : null,
          l.amenities.length ? [h("h3", {}, "Features"), h("div", { class: "badges" }, l.amenities.map((a) => h("span", { class: "badge" }, a)))] : null,
          l.description ? [h("h3", {}, "Description"), h("div", { class: "desc" }, l.description)] : null,
          l.price_history.length > 1 ? [h("h3", {}, "Rent history"), h("table", {},
            l.price_history.map((p) => h("tr", {}, h("td", {}, new Date(p.at).toLocaleDateString()), h("td", {}, money(p.price)))))] : null),
        !state.admin ? h("div", { class: "side" },
          l.contact ? h("div", {}, h("span", { class: "muted" }, "Contact: "), l.contact) : null,
          h("div", { class: "muted" }, `${SOURCE_NAMES[l.source] || l.source} · first seen ${ago(l.first_seen)}`)) :
        h("div", { class: "side" },
          h("label", { class: "muted" }, "Where this stands"), statusSelect,
          favorite,
          h("button", { onclick: async () => { await patchListing(id, { hidden: !l.hidden }, true); close(); } }, l.hidden ? "Unhide" : "Hide listing"),
          h("label", { class: "muted" }, "Notes"), notes,
          l.contact ? h("div", {}, h("span", { class: "muted" }, "Contact: "), l.contact) : null,
          h("div", { class: "muted" }, `${SOURCE_NAMES[l.source] || l.source} · first seen ${ago(l.first_seen)}` +
            (l.posted_at ? ` · posted ${new Date(l.posted_at).toLocaleDateString()}` : "")),
          h("button", { class: "link", onclick: () => { close(); askAssistant(`Tell me about listing #${l.id}. Anything I should watch out for?`); } }, "Ask the assistant about this"),
          h("button", { class: "link", onclick: () => { close(); askAssistant(`Draft a short inquiry message for listing #${l.id}.`); } }, "Draft an inquiry message")))));
  dlg.oncancel = (e) => { e.preventDefault(); close(); };
  if (!dlg.open) dlg.showModal();
  dlg.scrollTop = 0;
}

// ---------- chat ----------
const SUGGESTIONS = ["What's new since yesterday?", "Show me the 5 best matches",
  "What's the typical rent for a 2 bedroom in each town?", "I need a place that allows cats, under $2,000"];

function richText(text) {
  const frag = document.createDocumentFragment();
  const pattern = /(\*\*[^*\n]+\*\*)|(#\d+\b)|(https?:\/\/[^\s)<>\]]+)/g;
  let last = 0, m;
  while ((m = pattern.exec(text))) {
    frag.append(text.slice(last, m.index));
    if (m[1]) frag.append(h("strong", {}, m[1].slice(2, -2)));
    else if (m[2]) {
      const id = Number(m[2].slice(1));
      const known = state.byId[id];
      const label = known ? `${m[2]} · ${money(known.match_price ?? known.price)} · ${[bedsText(known), known.town].filter(Boolean).join(" · ")}` : m[2];
      frag.append(h("a", { class: "ref" + (known ? " chip" : ""), title: known ? known.title : "Open this listing",
        onclick: () => focusListing(id), ondblclick: () => openDetail(id) }, label));
    } else frag.append(h("a", { href: m[3], target: "_blank", rel: "noopener" }, m[3]));
    last = pattern.lastIndex;
  }
  frag.append(text.slice(last));
  return frag;
}

function addMessage(role, text) {
  const el = h("div", { class: "msg " + role }, role === "assistant" ? richText(text) : text);
  $("#chat-log").append(el);
  el.scrollIntoView({ block: "end" });
  return el;
}

function renderSuggestions(show) {
  $("#chat-suggest").replaceChildren(...(show ? SUGGESTIONS.map((s) =>
    h("button", { onclick: () => askAssistant(s) }, s)) : []));
}

async function loadChat() {
  $("#chat-log").replaceChildren();
  let history = [];
  try { history = await api(`/api/chat/${sessionId}`); } catch (_) { /* fresh start */ }
  for (const m of history) addMessage(m.role, m.content);
  if (!history.length) {
    addMessage("assistant", "Hi! I can search your listings, compare places, remember what you're looking for, " +
      "and draft messages to landlords. Tell me what you want in plain words.");
  }
  renderSuggestions(!history.length);
}

let chatBusy = false;
async function askAssistant(text) {
  text = text.trim();
  if (!text || chatBusy) return;
  $("#chat").classList.remove("hidden");
  chatBusy = true;
  $("#chat-send").disabled = true;
  renderSuggestions(false);
  addMessage("user", text);
  const bubble = addMessage("assistant", "");
  const working = h("span", { class: "tool" }, "Thinking…");
  bubble.append(working);
  let answer = "", shown = 0;
  const redraw = () => {
    bubble.replaceChildren(richText(answer), working);
    bubble.scrollIntoView({ block: "end" });
  };
  try {
    const resp = await fetch(`/api/chat/${sessionId}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text }) });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "text") { answer += event.delta; working.textContent = ""; redraw(); }
        else if (event.type === "tool") { working.textContent = event.label + "…"; redraw(); }
        else if (event.type === "show") {
          state.spotlight = { ids: event.ids, label: event.label, center: event.center, distances: event.distances };
          shown = event.ids.length;
          await loadListings();
          setMap(true);
          $("#content").scrollTo({ top: 0, behavior: "smooth" });
          redraw();
        }
        else if (event.type === "refresh") { loadListings(); loadOverview(); loadTowns(); }
        else if (event.type === "error") addMessage("error", event.message);
      }
    }
  } catch (err) {
    addMessage("error", err.message);
  }
  working.remove();
  if (answer.trim()) bubble.replaceChildren(richText(answer)); else bubble.remove();
  if (shown && isMobile()) {  // on a phone the results sit behind the assistant sheet
    $("#chat-log").append(h("button", { class: "primary", onclick: () => $("#chat").classList.add("hidden") },
      `View ${shown === 1 ? "this listing" : `these ${shown} listings`} on the map`));
    $("#chat-log").lastChild.scrollIntoView({ block: "end" });
  }
  chatBusy = false;
  $("#chat-send").disabled = false;
}

// ---------- settings ----------
const csv = (list) => (list || []).join(", ");
const uncsv = (text) => text.split(",").map((s) => s.trim()).filter(Boolean);

async function openSettings() {
  let data, runs;
  try { [data, runs] = await Promise.all([api("/api/settings"), api("/api/runs")]); }
  catch (err) { toast(err.message); return; }
  const s = data.settings;
  const dlg = $("#settings");
  const field = (label, input, cls = "") => h("label", { class: "field " + cls }, label, input);
  const num = (value, extra = {}) => h("input", { type: "number", value: value ?? "", ...extra });
  const check = (checked) => h("input", { type: "checkbox", checked: !!checked });
  const c = s.criteria;

  const townBoxes = s.towns.map((t) => ({ town: t, box: check(t.enabled) }));
  const newTown = { name: h("input", { placeholder: "Town name" }), zip: h("input", { placeholder: "ZIP", size: 6 }) };
  const f = {
    minPrice: num(c.min_price, { step: 50 }), maxPrice: num(c.max_price, { step: 50 }),
    minBeds: num(c.min_beds, { min: 0, max: 6 }), maxBeds: num(c.max_beds, { min: 0, max: 6 }),
    minBaths: num(c.min_baths, { min: 0, step: 0.5 }), maxAge: num(c.max_age_days, { min: 1 }),
    cats: check(c.cats), dogs: check(c.dogs),
    must: h("input", { class: "wide", value: csv(c.must_have), placeholder: "e.g. parking, first floor" }),
    nice: h("input", { class: "wide", value: csv(c.nice_to_have) }),
    exclude: h("input", { class: "wide", value: csv(c.exclude) }),
    notes: h("input", { class: "wide", value: c.notes, placeholder: "Anything else the assistant should know" }),
    cl: check(s.sources.craigslist.enabled), clRadius: num(s.sources.craigslist.radius_miles, { min: 1, max: 30 }),
    clDetail: num(s.sources.craigslist.detail_fetch_limit, { min: 0, max: 100 }),
    rc: check(s.sources.rentcom.enabled), rcPages: num(s.sources.rentcom.pages_per_town, { min: 1, max: 5 }),
    watch: check(s.sources.watch.enabled),
    sched: check(s.schedule.enabled), interval: num(s.schedule.interval_hours, { min: 0.5, step: 0.5 }),
    quietStart: num(s.schedule.quiet_start, { min: 0, max: 23 }), quietEnd: num(s.schedule.quiet_end, { min: 0, max: 23 }),
    minScore: num(s.notify.min_score, { min: 0, max: 100 }),
    webhook: h("input", { class: "wide", value: s.notify.webhook_url, placeholder: "https://ntfy.sh/your-private-topic or a Discord/Slack webhook" }),
    aiUrl: h("input", { value: s.ai.base_url, size: 28 }),
    aiModel: h("select", {}, [...new Set([s.ai.model, ...data.ai.models])].map((m) => h("option", { value: m, selected: m === s.ai.model }, m))),
    aiCtx: num(s.ai.num_ctx, { min: 4096, step: 4096 }), aiThink: check(s.ai.think),
  };

  const watchList = h("div", {});
  const drawWatches = (watches) => watchList.replaceChildren(...watches.map((w) => h("div", { class: "row" },
    h("span", {}, w.label ? `${w.label}: ` : "", h("a", { href: w.url, target: "_blank", rel: "noopener" }, w.url)),
    h("span", { class: "muted" }, w.last_status || "not read yet"),
    h("button", { class: "link", onclick: async () => drawWatches(await api(`/api/watches/${w.id}`, { method: "DELETE" })) }, "Remove"))));
  drawWatches(data.watches);
  const newWatch = { url: h("input", { placeholder: "https://… page that lists vacancies", size: 40 }), label: h("input", { placeholder: "Label (optional)" }) };

  const collect = () => {
    for (const { town, box } of townBoxes) town.enabled = box.checked;
    Object.assign(s.criteria, {
      min_price: +f.minPrice.value || 0, max_price: +f.maxPrice.value || 0,
      min_beds: +f.minBeds.value || 0, max_beds: +f.maxBeds.value || 0, min_baths: +f.minBaths.value || 0,
      max_age_days: +f.maxAge.value || 30, cats: f.cats.checked, dogs: f.dogs.checked,
      must_have: uncsv(f.must.value), nice_to_have: uncsv(f.nice.value), exclude: uncsv(f.exclude.value),
      notes: f.notes.value.trim(),
    });
    Object.assign(s.sources.craigslist, { enabled: f.cl.checked, radius_miles: +f.clRadius.value || 4, detail_fetch_limit: +f.clDetail.value || 0 });
    Object.assign(s.sources.rentcom, { enabled: f.rc.checked, pages_per_town: +f.rcPages.value || 1 });
    s.sources.watch.enabled = f.watch.checked;
    Object.assign(s.schedule, { enabled: f.sched.checked, interval_hours: Math.max(0.5, +f.interval.value || 4),
      quiet_start: +f.quietStart.value || 0, quiet_end: +f.quietEnd.value || 0 });
    Object.assign(s.notify, { min_score: +f.minScore.value || 0, webhook_url: f.webhook.value.trim() });
    Object.assign(s.ai, { base_url: f.aiUrl.value.trim(), model: f.aiModel.value, num_ctx: +f.aiCtx.value || 16384, think: f.aiThink.checked });
    return s;
  };
  const save = async () => {
    const body = collect();
    if (body.criteria.min_price > body.criteria.max_price) { toast("Minimum rent is above maximum rent"); return; }
    if (body.criteria.min_beds > body.criteria.max_beds) { toast("Minimum beds is above maximum beds"); return; }
    try {
      await api("/api/settings", { method: "PUT", body });
      dlg.close();
      toast("Settings saved");
      loadTowns(); loadListings(); loadOverview();
    } catch (err) { toast(err.message); }
  };

  dlg.replaceChildren(
    h("div", { class: "dlg-head" }, h("h2", {}, "Settings"), h("button", { onclick: () => dlg.close() }, "Close")),
    h("div", { class: "dlg-body set" },
      h("h3", {}, "Towns"),
      h("p", { class: "hint" }, "Towns earlier in the list score slightly higher."),
      h("div", { class: "towns" }, townBoxes.map(({ town, box }) => h("label", {}, box, town.name))),
      h("div", { class: "row" }, newTown.name, newTown.zip, h("button", { onclick: async () => {
        try {
          collect();
          await api("/api/settings", { method: "PUT", body: s });
          await api("/api/towns", { method: "POST", body: { name: newTown.name.value, zip: newTown.zip.value } });
          openSettings();
        } catch (err) { toast(err.message); }
      } }, "Add town")),

      h("h3", {}, "What you're looking for"),
      h("div", { class: "row" }, field("Min rent $", f.minPrice), field("Max rent $", f.maxPrice),
        field("Min beds (0 = studio)", f.minBeds), field("Max beds", f.maxBeds), field("Min baths", f.minBaths),
        field("Ignore posts older than (days)", f.maxAge)),
      h("div", { class: "row" }, h("label", {}, f.cats, "Must allow cats"), h("label", {}, f.dogs, "Must allow dogs")),
      h("div", { class: "row" }, field("Must have (comma separated; listings that don't mention these are dropped)", f.must, "wide-field")),
      h("div", { class: "row" }, field("Nice to have (raises the score)", f.nice, "wide-field")),
      h("div", { class: "row" }, field("Exclude listings that mention", f.exclude, "wide-field")),
      h("div", { class: "row" }, field("Notes for the assistant", f.notes, "wide-field")),

      h("h3", {}, "Sources"),
      h("div", { class: "row" }, h("label", {}, f.cl, "Craigslist"), field("Radius around each town (miles)", f.clRadius),
        field("Full posts to read per hunt", f.clDetail)),
      h("div", { class: "row" }, h("label", {}, f.rc, "Rent.com"), field("Result pages per town", f.rcPages)),
      h("div", { class: "row" }, h("label", {}, f.watch, "Watched pages"),
        h("span", { class: "hint" }, "Any page that lists vacancies (a local landlord, a housing authority). Your local model reads it on each hunt.")),
      watchList,
      h("div", { class: "row" }, newWatch.url, newWatch.label, h("button", { onclick: async () => {
        try {
          drawWatches(await api("/api/watches", { method: "POST", body: { url: newWatch.url.value, label: newWatch.label.value } }));
          newWatch.url.value = newWatch.label.value = "";
        } catch (err) { toast(err.message); }
      } }, "Watch page")),

      h("h3", {}, "Schedule"),
      h("div", { class: "row" }, h("label", {}, f.sched, "Hunt automatically"), field("Every (hours)", f.interval),
        field("Quiet from (hour, 0–23)", f.quietStart), field("Quiet until (hour)", f.quietEnd)),
      h("p", { class: "hint" }, "Automatic hunts run while RentHunt itself is running."),

      h("h3", {}, "Alerts"),
      h("div", { class: "row" }, field("Alert me about new matches scoring at least", f.minScore)),
      h("div", { class: "row" }, field("Push alerts to (leave empty for in-app only)", f.webhook, "wide-field")),

      h("h3", {}, "Assistant (local model)"),
      h("p", { class: "hint" }, data.ai.ok ? `Connected to Ollama, ${data.ai.models.length} models installed.` : `Not connected: ${data.ai.error}`),
      h("div", { class: "row" }, field("Ollama address", f.aiUrl), field("Model", f.aiModel), field("Context size (tokens)", f.aiCtx),
        h("label", {}, f.aiThink, "Let the model think first (slower)")),

      h("h3", {}, "Recent hunts"),
      runs.length ? runs.slice(0, 8).map((r) => h("details", {},
        h("summary", {}, `${new Date(r.started_at).toLocaleString()} · ${r.trigger} · ${r.status}` +
          (r.summary.new !== undefined ? ` · ${r.summary.new} new listings, ${r.summary.new_matches} alerts` : "")),
        h("pre", { class: "log" }, r.log || "(still running)"))) : h("p", { class: "hint" }, "No hunts yet.")),
    h("div", { class: "dlg-foot" }, h("button", { onclick: () => dlg.close() }, "Cancel"), h("button", { class: "primary", onclick: save }, "Save")));
  if (!dlg.open) dlg.showModal();
}

function renderTowns(towns) {
  const select = $("#f-town");
  if ([...select.options].slice(1).map((o) => o.value).join("|") === towns.join("|")) return;
  const current = select.value;
  select.replaceChildren(h("option", { value: "" }, "All towns"), ...towns.map((t) => h("option", { value: t }, t)));
  select.value = towns.includes(current) ? current : "";
}
const loadTowns = () => loadOverview();

// ---------- sign in (for visitors arriving over the public address) ----------
function openLogin() {
  const dlg = $("#login");
  const password = h("input", { type: "password", placeholder: "Admin password", autocomplete: "current-password" });
  dlg.replaceChildren(h("form", { onsubmit: async (e) => {
    e.preventDefault();
    try {
      await api("/api/login", { method: "POST", body: { password: password.value } });
      location.reload();
    } catch (err) { toast(err.message); }
  } },
    h("h2", {}, "Sign in"),
    h("p", { class: "hint" }, "Visitors can browse listings. Signing in unlocks the assistant, settings and tracking."),
    password,
    h("div", { class: "row", style: "display:flex;gap:8px;justify-content:flex-end" },
      h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"), h("button", { class: "primary" }, "Sign in"))));
  dlg.showModal();
  password.focus();
}

async function start() {
  try { Object.assign(state, await api("/api/me")); } catch (_) { /* treated as a visitor */ }
  document.body.classList.toggle("visitor", !state.admin);
  const login = $("#btn-login");
  login.hidden = state.local;
  login.textContent = state.admin ? "Sign out" : "Sign in";
  login.onclick = state.admin
    ? async () => { await api("/api/logout", { method: "POST" }); location.reload(); }
    : openLogin;
  await loadOverview();
  loadListings();
  if (isMobile()) $("#chat").classList.add("hidden");
  if (state.admin) {
    loadChat();
    fetch("/api/chat-warm", { method: "POST" }).catch(() => {});  // load the model before the first question
  }
  setInterval(loadOverview, 5000);
}

// ---------- wiring ----------
$("#btn-hunt").onclick = async () => {
  try {
    const { started } = await api("/api/hunt", { method: "POST" });
    toast(started ? "Hunt started. This takes a couple of minutes." : "A hunt is already running.");
    loadOverview();
  } catch (err) { toast(err.message); }
};
$("#btn-map").onclick = () => setMap(!state.mapOn);
$("#btn-settings").onclick = openSettings;
const isMobile = () => window.matchMedia("(max-width: 900px)").matches;
$("#btn-chat").onclick = () => $("#chat").classList.toggle("hidden");
$("#chat-close").onclick = () => $("#chat").classList.add("hidden");
$("#btn-seen").onclick = async () => { await api("/api/listings/mark-seen", { method: "POST" }); loadListings(); loadOverview(); };
$("#chat-new").onclick = async () => { await api(`/api/chat/${sessionId}`, { method: "DELETE" }); loadChat(); };
$("#chat-form").onsubmit = (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value;
  if (chatBusy || !text.trim()) return;
  input.value = "";
  askAssistant(text);
};
$("#chat-input").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); }
};
let filterTimer;
for (const id of ["f-text", "f-town", "f-beds", "f-max", "f-source", "f-sort"]) {
  $("#" + id).addEventListener("input", () => { state.spotlight = null; clearTimeout(filterTimer); filterTimer = setTimeout(loadListings, 250); });
}

start();
