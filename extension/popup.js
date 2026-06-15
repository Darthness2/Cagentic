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
  t.style.color = idle ? "var(--text-2)" : "var(--gold)";
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

function renderOnline(s) {
  $("dot").className = "dot on";
  $("statusText").textContent = "Connected";
  $("statusSub").textContent = ":" + port;
  $("details").classList.remove("hidden");
  $("vModel").textContent = s.model || "not loaded yet";
  renderActivity(s.activity);
  renderRecent(s.recent);
  $("ver").textContent = "cagentic v" + (s.version || "?") + " · bridge on 127.0.0.1:" + port;
}

function renderOffline(msg) {
  $("dot").className = "dot off";
  $("statusText").textContent = msg || "Cagentic not running";
  $("statusSub").textContent = "";
  $("details").classList.add("hidden");
  $("ver").textContent = "start Cagentic, then this connects automatically";
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
  port = parseInt($("port").value, 10) || 8765;
  token = $("token").value.trim();
  await chrome.storage.local.set({ port, token });
  poll();
});

(async () => {
  await loadSettings();
  poll();
  timer = setInterval(poll, POLL_MS);
})();
