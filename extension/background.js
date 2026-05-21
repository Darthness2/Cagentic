// Cagentic Bridge — service worker.
//
// Long-polls the local Cagentic bridge (http://127.0.0.1:<port>/next) for
// commands, runs them with Chrome's APIs, and posts results back. The whole
// channel is localhost-only; Cagentic gates every mutating action behind an
// approval prompt before it ever reaches here.

const DEFAULT_PORT = 8765;
let looping = false;

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
        // Bridge not up yet (Cagentic not running) — back off and retry.
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

async function dispatch(action, p) {
  switch (action) {
    case "tabs": {
      const tabs = await chrome.tabs.query({});
      return tabs.map((t) => ({
        id: t.id,
        title: t.title,
        url: t.url,
        active: t.active,
      }));
    }
    case "open": {
      const tab = await chrome.tabs.create({
        url: p.url,
        active: p.active !== false,
      });
      return { id: tab.id, url: tab.url };
    }
    case "navigate": {
      const tabId = await resolveTab(p.tab_id);
      const tab = await chrome.tabs.update(tabId, { url: p.url });
      return { id: tab.id, url: p.url };
    }
    case "close": {
      const tabId = await resolveTab(p.tab_id);
      await chrome.tabs.remove(tabId);
      return { closed: tabId };
    }
    case "read": {
      const tabId = await resolveTab(p.tab_id);
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
      const tabId = await resolveTab(p.tab_id);
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func: clickInPage,
        args: [p.selector || null, p.text || null],
      });
      return result;
    }
    case "fill": {
      const tabId = await resolveTab(p.tab_id);
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func: fillInPage,
        args: [p.selector, p.value == null ? "" : String(p.value)],
      });
      return result;
    }
    case "eval": {
      const tabId = await resolveTab(p.tab_id);
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (code) => {
          try {
            // eslint-disable-next-line no-eval
            return { ok: true, value: String(eval(code)) };
          } catch (e) {
            return { ok: false, error: String(e) };
          }
        },
        args: [p.code],
      });
      return result;
    }
    default:
      throw new Error("unknown action: " + action);
  }
}

async function resolveTab(tabId) {
  if (tabId) return tabId;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("no active tab");
  return tab.id;
}

// ---- injected page functions (must be self-contained) ---------------------

function clickInPage(selector, text) {
  let el = null;
  if (selector) el = document.querySelector(selector);
  if (!el && text) {
    const want = text.toLowerCase();
    const candidates = Array.from(
      document.querySelectorAll(
        "a, button, input[type=submit], input[type=button], [role=button], [onclick]"
      )
    );
    el = candidates.find((e) =>
      ((e.innerText || e.value || "").toLowerCase()).includes(want)
    );
  }
  if (!el) return { ok: false, error: "no matching element" };
  el.scrollIntoView({ block: "center" });
  el.click();
  return {
    ok: true,
    clicked: el.tagName.toLowerCase() + (el.id ? "#" + el.id : ""),
  };
}

function fillInPage(selector, value) {
  const el = document.querySelector(selector);
  if (!el) return { ok: false, error: "no field matching: " + selector };
  el.focus();
  if ("value" in el) el.value = value;
  else el.textContent = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return { ok: true, filled: selector };
}

// ---- keep the loop alive --------------------------------------------------

chrome.runtime.onInstalled.addListener(() => pollLoop());
chrome.runtime.onStartup.addListener(() => pollLoop());
chrome.alarms.create("cagentic-keepalive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(() => pollLoop());
pollLoop();
