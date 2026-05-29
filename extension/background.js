// Cagentic Bridge — service worker.
//
// Long-polls the local Cagentic bridge (http://127.0.0.1:<port>/next) for
// commands, runs them with Chrome's APIs, and posts results back. The whole
// channel is localhost-only; Cagentic gates every mutating action behind an
// approval prompt before it ever reaches here.

const DEFAULT_PORT = 8765;
const CAG_ORANGE = "#ff8c42";   // the "iconic" Cagentic orange
let looping = false;
let cagGroupId = null;          // id of the "Cagentic" tab group
const glowTimers = new Map();   // tabId -> timeoutHandle for hide-soon

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function getPort() {
  const { port } = await chrome.storage.local.get("port");
  return port || DEFAULT_PORT;
}

// ---- the poll loop --------------------------------------------------------

async function pollLoop() {
  if (looping) return;
  looping = true;
  try {
    while (true) {
      const port = await getPort();
      let command = null;
      try {
        const res = await fetch(`http://127.0.0.1:${port}/next`, { method: "GET" });
        if (res.ok) {
          command = (await res.json()).command;
        }
      } catch (e) {
        await sleep(3000);
        continue;
      }
      if (command) {
        let ok = true;
        let result;
        try {
          result = await dispatch(command.action, command.params || {});
        } catch (e) {
          ok = false;
          result = String((e && e.message) || e);
        }
        await postResult(port, command.id, ok, result);
      }
    }
  } finally {
    looping = false;
  }
}

async function postResult(port, id, ok, result) {
  try {
    await fetch(`http://127.0.0.1:${port}/result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, ok, result }),
    });
  } catch (e) {
    /* the agent will time out gracefully */
  }
}

// ---- command dispatch -----------------------------------------------------

const PER_TAB_ACTIONS = new Set([
  "read", "navigate", "click", "fill", "scroll", "eval", "close",
  "screenshot", "click_at", "links",
]);

async function dispatch(action, p) {
  // Resolve the target tab up-front so we can apply the orange glow and the
  // "Cagentic" tab group around the actual work.
  let tabId = null;
  if (PER_TAB_ACTIONS.has(action)) {
    tabId = await resolveTab(p.tab_id);
  }

  if (tabId != null) {
    ensureGroup(tabId).catch(() => {});
    glowOn(tabId).catch(() => {});
  }

  try {
    switch (action) {
      case "tabs": {
        const tabs = await chrome.tabs.query({});
        return tabs.map((t) => ({
          id: t.id, title: t.title, url: t.url, active: t.active,
        }));
      }
      case "open": {
        const tab = await chrome.tabs.create({
          url: p.url, active: p.active !== false,
        });
        ensureGroup(tab.id).catch(() => {});
        // Give the new tab a moment to load before we paint on it.
        setTimeout(() => {
          glowOn(tab.id).catch(() => {});
          glowOffSoon(tab.id);
        }, 350);
        return { id: tab.id, url: tab.url };
      }
      case "navigate": {
        const tab = await chrome.tabs.update(tabId, { url: p.url });
        // Wait for the page to start, then re-glow (the previous DOM is gone).
        setTimeout(() => {
          glowOn(tabId).catch(() => {});
          glowOffSoon(tabId);
        }, 600);
        return { id: tab.id, url: p.url };
      }
      case "close": {
        await chrome.tabs.remove(tabId);
        return { closed: tabId };
      }
      case "read": {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => ({
            title: document.title,
            url: location.href,
            text: document.body ? document.body.innerText : "",
          }),
        });
        return result;
      }
      case "click": {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: clickInPage,
          args: [p.selector || null, p.text || null],
        });
        return result;
      }
      case "fill": {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: fillInPage,
          args: [p.selector, p.value == null ? "" : String(p.value)],
        });
        return result;
      }
      case "eval": {
        // Run in the page's MAIN world so the PAGE's CSP applies (most pages
        // allow eval) instead of the extension's strict no-unsafe-eval CSP.
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          world: "MAIN",
          func: (code) => {
            try {
              // eslint-disable-next-line no-eval
              return { ok: true, value: String(eval(code)) };
            } catch (e) {
              return { ok: false, error: String((e && e.message) || e) };
            }
          },
          args: [p.code],
        });
        return result;
      }
      case "scroll": {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: scrollInPage,
          args: [p.to || null,
                 p.y == null ? null : Number(p.y),
                 p.selector || null],
        });
        return result;
      }
      case "screenshot": {
        // captureVisibleTab grabs the active tab in the focused window —
        // good enough since the model just told us which tab to act on.
        const dataUrl = await chrome.tabs.captureVisibleTab(null, {
          format: "png",
        });
        // Strip "data:image/png;base64," prefix → raw base64 for Ollama.
        const base64 = dataUrl.replace(/^data:image\/[a-z]+;base64,/, "");
        // Also report the viewport size so the model knows the coord space.
        const [{ result: size }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => ({ w: window.innerWidth, h: window.innerHeight }),
        });
        return { format: "png", data: base64, width: size.w, height: size.h };
      }
      case "click_at": {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: clickAtPoint,
          args: [Number(p.x), Number(p.y)],
        });
        return result;
      }
      case "links": {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId },
          func: harvestLinks,
        });
        return result;
      }
      case "download": {
        // The extension's own fetch carries the user's cookies because
        // <all_urls> is in host_permissions — that's how we authenticate
        // against Google Drive / Docs export endpoints without an OAuth
        // flow. No tab needed.
        try {
          const r = await fetch(p.url, {
            credentials: "include",
            redirect: "follow",
          });
          if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
          const buf = await r.arrayBuffer();
          const MAX = 25 * 1024 * 1024;     // 25 MB
          if (buf.byteLength > MAX) {
            return { ok: false, error:
              `file is ${(buf.byteLength/1e6).toFixed(1)} MB — too large (cap ${MAX/1e6} MB)` };
          }
          const bytes = new Uint8Array(buf);
          let s = "";
          const CHUNK = 0x8000;
          for (let i = 0; i < bytes.length; i += CHUNK) {
            s += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
          }
          return {
            ok: true,
            url: r.url,
            contentType: r.headers.get("content-type") || "",
            size: bytes.length,
            data: btoa(s),
          };
        } catch (e) {
          return { ok: false, error: String((e && e.message) || e) };
        }
      }
      default:
        throw new Error("unknown action: " + action);
    }
  } finally {
    if (tabId != null) glowOffSoon(tabId);
  }
}

async function resolveTab(tabId) {
  if (tabId) return tabId;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("no active tab");
  return tab.id;
}

// ---- "Cagentic" tab group -------------------------------------------------

async function ensureGroup(tabId) {
  if (!chrome.tabGroups) return;       // older Chrome, incognito, etc.
  try {
    // Verify the saved group still exists; Chrome forgets it across restarts.
    if (cagGroupId != null) {
      try { await chrome.tabGroups.get(cagGroupId); }
      catch (e) { cagGroupId = null; }
    }
    if (cagGroupId == null) {
      cagGroupId = await chrome.tabs.group({ tabIds: [tabId] });
      await chrome.tabGroups.update(cagGroupId, {
        title: "Cagentic", color: "orange",
      });
      return;
    }
    // Add the tab only if it isn't already in the group — avoids fighting
    // a user who has moved it out.
    const t = await chrome.tabs.get(tabId);
    if (t.groupId !== cagGroupId) {
      await chrome.tabs.group({ tabIds: [tabId], groupId: cagGroupId });
    }
  } catch (e) {
    // tabGroups not allowed on this tab (chrome://, devtools, incognito)
  }
}

// ---- orange "AI is working" glow -----------------------------------------

async function glowOn(tabId) {
  // Cancel any pending hide so a fresh action keeps the glow continuous.
  const prev = glowTimers.get(tabId);
  if (prev) { clearTimeout(prev); glowTimers.delete(tabId); }
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: showCagGlow,
      args: [CAG_ORANGE],
    });
  } catch (e) { /* chrome:// page, detached, or restricted */ }
}

function glowOffSoon(tabId) {
  const prev = glowTimers.get(tabId);
  if (prev) clearTimeout(prev);
  const t = setTimeout(async () => {
    glowTimers.delete(tabId);
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: hideCagGlow,
      });
    } catch (e) { /* tab gone or restricted */ }
  }, 900);
  glowTimers.set(tabId, t);
}

// ---- injected page functions (must be self-contained) ---------------------

function showCagGlow(orange) {
  if (document.getElementById("__cag-glow")) return;
  const style = document.createElement("style");
  style.id = "__cag-glow-style";
  style.textContent = `
    @keyframes __cagGlowPulse {
      0%, 100% { box-shadow: inset 0 0 0 3px ${orange}cc,
                             inset 0 0 36px 4px ${orange}55; }
      50%      { box-shadow: inset 0 0 0 3px ${orange},
                             inset 0 0 56px 9px ${orange}88; }
    }
    #__cag-glow {
      position: fixed; inset: 0; pointer-events: none;
      z-index: 2147483647;
      animation: __cagGlowPulse 1.6s ease-in-out infinite;
    }
  `;
  document.head.appendChild(style);
  const div = document.createElement("div");
  div.id = "__cag-glow";
  document.documentElement.appendChild(div);
}

function hideCagGlow() {
  const a = document.getElementById("__cag-glow");
  const b = document.getElementById("__cag-glow-style");
  if (a) a.remove();
  if (b) b.remove();
}

// Cursor helpers are inlined inside each action below — Chrome serializes
// the `func` source, so anything the action references has to be defined
// in the same function body.

async function clickInPage(selector, text) {
  // Cursor helpers — inlined so the serialized function is self-contained.
  function ensureCagCursor() {
    if (document.getElementById("__cag-cursor")) return;
    const s = document.createElement("style");
    s.id = "__cag-cursor-style";
    s.textContent =
      "#__cag-cursor{position:fixed;width:26px;height:26px;pointer-events:none;" +
      "z-index:2147483647;background:radial-gradient(circle,rgba(255,140,66,1) 0 30%," +
      "rgba(255,140,66,.45) 55%,transparent 72%);border-radius:50%;opacity:0;" +
      "left:50vw;top:50vh;transform:translate(-50%,-50%);" +
      "transition:left .36s cubic-bezier(.4,0,.2,1),top .36s cubic-bezier(.4,0,.2,1)," +
      "opacity .2s,transform .18s}" +
      "#__cag-cursor.on{opacity:1}" +
      "#__cag-cursor.click{transform:translate(-50%,-50%) scale(1.65);" +
      "background:radial-gradient(circle,rgba(255,200,120,1) 0 38%," +
      "rgba(255,140,66,.55) 60%,transparent 75%)}";
    document.head.appendChild(s);
    const c = document.createElement("div");
    c.id = "__cag-cursor";
    document.documentElement.appendChild(c);
  }
  function cagCursorTo(x, y) {
    ensureCagCursor();
    const c = document.getElementById("__cag-cursor");
    c.style.left = x + "px";
    c.style.top = y + "px";
    c.classList.add("on");
    return new Promise((r) => setTimeout(r, 380));
  }
  function cagCursorClick() {
    const c = document.getElementById("__cag-cursor");
    if (!c) return;
    c.classList.add("click");
    setTimeout(() => c.classList.remove("click"), 280);
  }

  let el = null;
  if (selector) el = document.querySelector(selector);
  if (!el && text) {
    const want = text.toLowerCase();
    const candidates = Array.from(document.querySelectorAll(
      "a, button, input[type=submit], input[type=button], [role=button], [onclick]"
    ));
    el = candidates.find((e) =>
      ((e.innerText || e.value || "").toLowerCase()).includes(want));
  }
  if (!el) return { ok: false, error: "no matching element" };

  el.scrollIntoView({ block: "center" });
  await new Promise((r) => setTimeout(r, 90));
  const r = el.getBoundingClientRect();
  await cagCursorTo(r.left + r.width / 2, r.top + r.height / 2);
  cagCursorClick();
  await new Promise((r) => setTimeout(r, 170));
  el.click();
  return {
    ok: true,
    clicked: el.tagName.toLowerCase() + (el.id ? "#" + el.id : ""),
  };
}

async function fillInPage(selector, value) {
  function ensureCagCursor() {
    if (document.getElementById("__cag-cursor")) return;
    const s = document.createElement("style");
    s.id = "__cag-cursor-style";
    s.textContent =
      "#__cag-cursor{position:fixed;width:26px;height:26px;pointer-events:none;" +
      "z-index:2147483647;background:radial-gradient(circle,rgba(255,140,66,1) 0 30%," +
      "rgba(255,140,66,.45) 55%,transparent 72%);border-radius:50%;opacity:0;" +
      "left:50vw;top:50vh;transform:translate(-50%,-50%);" +
      "transition:left .36s cubic-bezier(.4,0,.2,1),top .36s cubic-bezier(.4,0,.2,1)," +
      "opacity .2s,transform .18s}" +
      "#__cag-cursor.on{opacity:1}";
    document.head.appendChild(s);
    const c = document.createElement("div");
    c.id = "__cag-cursor";
    document.documentElement.appendChild(c);
  }
  function cagCursorTo(x, y) {
    ensureCagCursor();
    const c = document.getElementById("__cag-cursor");
    c.style.left = x + "px";
    c.style.top = y + "px";
    c.classList.add("on");
    return new Promise((r) => setTimeout(r, 380));
  }

  const el = document.querySelector(selector);
  if (!el) return { ok: false, error: "no field matching: " + selector };
  el.scrollIntoView({ block: "center" });
  await new Promise((r) => setTimeout(r, 90));
  const r = el.getBoundingClientRect();
  await cagCursorTo(r.left + r.width / 2, r.top + r.height / 2);
  el.focus();
  if ("value" in el) el.value = value;
  else el.textContent = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return { ok: true, filled: selector };
}

async function clickAtPoint(x, y) {
  // Inline cursor helpers (Chrome serializes func source).
  function ensureCagCursor() {
    if (document.getElementById("__cag-cursor")) return;
    const s = document.createElement("style");
    s.id = "__cag-cursor-style";
    s.textContent =
      "#__cag-cursor{position:fixed;width:26px;height:26px;pointer-events:none;" +
      "z-index:2147483647;background:radial-gradient(circle,rgba(255,140,66,1) 0 30%," +
      "rgba(255,140,66,.45) 55%,transparent 72%);border-radius:50%;opacity:0;" +
      "left:50vw;top:50vh;transform:translate(-50%,-50%);" +
      "transition:left .36s cubic-bezier(.4,0,.2,1),top .36s cubic-bezier(.4,0,.2,1)," +
      "opacity .2s,transform .18s}" +
      "#__cag-cursor.on{opacity:1}" +
      "#__cag-cursor.click{transform:translate(-50%,-50%) scale(1.65);" +
      "background:radial-gradient(circle,rgba(255,200,120,1) 0 38%," +
      "rgba(255,140,66,.55) 60%,transparent 75%)}";
    document.head.appendChild(s);
    const c = document.createElement("div");
    c.id = "__cag-cursor";
    document.documentElement.appendChild(c);
  }
  function cagCursorTo(cx, cy) {
    ensureCagCursor();
    const c = document.getElementById("__cag-cursor");
    c.style.left = cx + "px";
    c.style.top = cy + "px";
    c.classList.add("on");
    return new Promise((r) => setTimeout(r, 380));
  }
  function cagCursorClick() {
    const c = document.getElementById("__cag-cursor");
    if (!c) return;
    c.classList.add("click");
    setTimeout(() => c.classList.remove("click"), 280);
  }

  const el = document.elementFromPoint(x, y);
  if (!el) return { ok: false, error: `no element at (${x}, ${y})` };

  await cagCursorTo(x, y);
  cagCursorClick();
  await new Promise((r) => setTimeout(r, 170));

  // Full mouse-event sequence so frameworks (React, Vue) react properly,
  // not just the bare .click() — some apps only listen for mousedown/up.
  const opts = {
    bubbles: true, cancelable: true, composed: true,
    clientX: x, clientY: y, button: 0, view: window,
  };
  el.dispatchEvent(new MouseEvent("mousedown", opts));
  el.dispatchEvent(new MouseEvent("mouseup", opts));
  el.dispatchEvent(new MouseEvent("click", opts));

  return {
    ok: true,
    clicked: el.tagName.toLowerCase() + (el.id ? "#" + el.id : ""),
    at: { x, y },
  };
}

function harvestLinks() {
  // Pull every clickable thing on the page with the bits a model needs to
  // pick the right one — text, href, aria-label, role — without ever
  // touching eval. Works on strict-CSP pages and on the page-internal
  // links that browser_read's innerText doesn't surface (Drive's file
  // list, classroom tiles, etc.).
  const out = [];
  const seen = new Set();
  const sel = 'a[href], [role="link"], [role="button"], button';
  const nodes = document.querySelectorAll(sel);
  for (let i = 0; i < nodes.length && out.length < 250; i++) {
    const el = nodes[i];
    // Skip hidden things.
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    const text = (el.innerText || el.textContent || "").trim().slice(0, 160);
    const href = el.href ||
                 el.getAttribute("data-href") ||
                 el.getAttribute("href") || "";
    const aria = el.getAttribute("aria-label") || "";
    const key = href || (text + "\x00" + aria);
    if (!key || seen.has(key)) continue;
    if (!text && !href && !aria) continue;
    seen.add(key);
    out.push({ text, href, aria });
  }
  return { ok: true, count: out.length, links: out };
}

function scrollInPage(to, y, selector) {
  try {
    if (selector) {
      const el = document.querySelector(selector);
      if (!el) return { ok: false, error: "no element matching: " + selector };
      el.scrollIntoView({ block: "center", behavior: "instant" });
      return { ok: true, scrolled: "into-view", selector };
    }
    if (typeof y === "number") {
      window.scrollTo({ left: 0, top: y, behavior: "instant" });
      return { ok: true, scrolled: y };
    }
    if (to === "top") {
      window.scrollTo({ left: 0, top: 0, behavior: "instant" });
      return { ok: true, scrolled: "top" };
    }
    if (to === "bottom" || to == null) {
      const h = Math.max(
        document.body ? document.body.scrollHeight : 0,
        document.documentElement ? document.documentElement.scrollHeight : 0
      );
      window.scrollTo({ left: 0, top: h, behavior: "instant" });
      return { ok: true, scrolled: "bottom" };
    }
    return { ok: false, error: "unknown scroll target: " + to };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

// ---- keep the loop alive --------------------------------------------------

chrome.runtime.onInstalled.addListener(() => pollLoop());
chrome.runtime.onStartup.addListener(() => pollLoop());
chrome.alarms.create("cagentic-keepalive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(() => pollLoop());
pollLoop();
