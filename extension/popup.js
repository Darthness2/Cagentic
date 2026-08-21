// Cagentic Bridge popup — a live status dashboard.
//
// Polls the local bridge's /status endpoint and shows whether Cagentic is
// connected, which AI model is loaded, what it's currently doing, and the
// most recent browser actions it took.

const POLL_MS = 1500;
let port = 8765;
let token = "";
let timer = null;

const $ = (id) => document.getElementById(id);

async function loadSettings() {
  const r = await chrome.storage.local.get(["port", "token"]);
  port = r.port || 8765;
  token = r.token || "";
  $("port").value = port;
  $("token").value = token;
}

function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 5) return "just now";
  if (d < 60) return Math.floor(d) + "s ago";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  return Math.floor(d / 3600) + "h ago";
}

function cap(s) {
  s = String(s || "");
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function renderActivity(activity) {
  const wrap = $("vActivity");
  const idle = !activity || activity === "idle";
  wrap.innerHTML = "";
  if (!idle) {
    const p = document.createElement("span");
    p.className = "pulse";
    wrap.appendChild(p);
  }
  const t = document.createElement("span");
  t.textContent = idle ? "Idle" : cap(activity);
  t.style.color = idle ? "var(--cag-text-2)" : "var(--cag-warn)";
  wrap.appendChild(t);
}

function renderRecent(recent) {
  const box = $("recent");
  box.innerHTML = "";
  if (!recent || !recent.length) {
    box.innerHTML = '<div class="empty">No browser actions yet.</div>';
    return;
  }
  recent.forEach((r) => {
    const row = document.createElement("div");
    row.className = "act " + (r.ok ? "ok" : "bad");
    // Build with textContent (never innerHTML): r.action / r.summary are
    // influenced by the bridge / page content, so interpolating them as HTML
    // would be an XSS sink in the popup.
    const mk = document.createElement("span");
    mk.className = "mk";
    mk.textContent = r.ok ? "✓" : "✗";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = r.action || "?";
    const sum = document.createElement("span");
    sum.className = "sum";
    sum.textContent = r.summary || "";
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = ago(r.ts);
    row.append(mk, name, sum, when);
    box.appendChild(row);
  });
}

// ---- site permissions -----------------------------------------------------
// Mirrors the bridge's rules and lets the user change them for the tab they're
// looking at. Deny wins; a non-empty allow list turns it into an allow-list.
let siteRules = { allow: [], deny: [] };

async function currentHost() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url) return "";
    return new URL(tab.url).hostname.toLowerCase();
  } catch (e) {
    return "";
  }
}

function renderSites(rules) {
  siteRules = { allow: (rules && rules.allow) || [], deny: (rules && rules.deny) || [] };
  const box = $("sites");
  box.innerHTML = "";
  if (!siteRules.allow.length && !siteRules.deny.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "Any site (no restrictions set).";
    box.appendChild(d);
    return;
  }
  const add = (host, kind) => {
    const row = document.createElement("div");
    row.className = "site " + kind;
    const mk = document.createElement("span");
    mk.className = "mk";
    mk.textContent = kind === "deny" ? "\u2717" : "\u2713";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = host;
    const x = document.createElement("button");
    x.className = "site-x";
    x.textContent = "Remove";
    x.title = "Remove " + host;
    x.setAttribute("aria-label", "Remove " + host + " permission");
    x.addEventListener("click", () => {
      siteRules[kind] = siteRules[kind].filter((h) => h !== host);
      saveSites();
    });
    row.append(mk, name, x);
    box.appendChild(row);
  };
  siteRules.deny.forEach((h) => add(h, "deny"));
  siteRules.allow.forEach((h) => add(h, "allow"));
  if (siteRules.allow.length) {
    const note = document.createElement("div");
    note.className = "empty";
    note.textContent = "Allow-list active: every other site is blocked.";
    box.appendChild(note);
  }
}

async function saveSites() {
  try {
    const res = await fetch("http://127.0.0.1:" + port + "/sites", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Cagentic-Token": token },
      body: JSON.stringify(siteRules),
    });
    const j = await res.json();
    renderSites(j.sites || siteRules);
  } catch (e) {
    console.warn("Cagentic: could not save site rules", e);
  }
}

async function setCurrentSite(kind) {
  const host = await currentHost();
  if (!host) return;
  siteRules.allow = siteRules.allow.filter((h) => h !== host);
  siteRules.deny = siteRules.deny.filter((h) => h !== host);
  siteRules[kind].push(host);
  await saveSites();
}

// Opening the side panel must happen synchronously inside the click handler —
// Chrome rejects sidePanel.open() once the call stack loses its user gesture.
$("openPanel").addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    await chrome.sidePanel.open({ windowId: tab.windowId });
    window.close();
  } catch (e) {
    console.warn("Cagentic: could not open the side panel", e);
  }
});

$("allowSite").addEventListener("click", () => setCurrentSite("allow"));
$("denySite").addEventListener("click", () => setCurrentSite("deny"));

function renderOnline(s) {
  $("dot").className = "dot on";
  $("statusText").textContent = "Connected";
  $("statusSub").textContent = ":" + port;
  $("details").classList.remove("hidden");
  $("vModel").textContent = s.model || "not loaded yet";
  renderActivity(s.activity);
  renderSites(s.sites);
  renderRecent(s.recent);
  $("ver").textContent = "Cagentic v" + (s.version || "?") + " · bridge on 127.0.0.1:" + port;
}

function renderOffline(msg) {
  $("dot").className = "dot off";
  $("statusText").textContent = msg || "Cagentic not running";
  $("statusSub").textContent = "";
  $("details").classList.add("hidden");
  $("ver").textContent = "Start Cagentic; this connects automatically.";
}

async function poll() {
  if (!token) {
    // Not paired: /status would 403, so prompt the user to paste the token
    // Cagentic printed instead of showing a misleading "not running".
    renderOffline("Paste the bridge token below to connect");
    return;
  }
  try {
    const res = await fetch("http://127.0.0.1:" + port + "/status", {
      method: "GET",
      headers: { "X-Cagentic-Token": token },
    });
    if (res.status === 403) {
      renderOffline("Bridge token rejected — re-paste it");
      return;
    }
    if (!res.ok) throw new Error("bad status");
    renderOnline(await res.json());
  } catch (e) {
    console.warn("Cagentic: status poll failed", e);
    renderOffline();
  }
}

$("save").addEventListener("click", async () => {
  const save = $("save");
  save.disabled = true;
  save.textContent = "Saving…";
  port = parseInt($("port").value, 10) || 8765;
  token = $("token").value.trim();
  try {
    await chrome.storage.local.set({ port, token });
    await poll();
  } finally {
    save.disabled = false;
    save.textContent = "Save";
  }
});

(async () => {
  await loadSettings();
  poll();
  timer = setInterval(poll, POLL_MS);
})();
