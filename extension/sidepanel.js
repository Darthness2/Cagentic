// Cagentic side panel.
//
// Hosts the gateway's own chat UI in an iframe rather than reimplementing it:
// the panel, the browser tab at localhost:8700 and the terminal all drive the
// same engine, so a second chat implementation here would be a second thing to
// keep in step for no benefit.
//
// The panel's own job is the two things the iframe can't do: find the gateway,
// and push the current page's context into the conversation.

const DEFAULT_GATEWAY_PORT = 8700;

const $ = (id) => document.getElementById(id);

async function gatewayPort() {
  const { gateway_port: p } = await chrome.storage.local.get("gateway_port");
  return p || DEFAULT_GATEWAY_PORT;
}

// The gateway serves its page only to same-host requests and hands the page a
// per-session token, so we just point the frame at it and let the page
// bootstrap itself exactly as it does in a normal tab.
async function connect() {
  const port = await gatewayPort();
  const url = `http://127.0.0.1:${port}/`;
  let reachable = false;
  try {
    // A HEAD would be cheaper, but the gateway only implements GET for "/".
    const res = await fetch(url, { method: "GET", cache: "no-store" });
    reachable = res.ok;
  } catch (e) {
    reachable = false;
  }
  if (!reachable) {
    $("frame").style.display = "none";
    $("fallback").style.display = "flex";
    $("fbTitle").textContent = "Cagentic isn’t running";
    $("fbBody").innerHTML =
      'Start it with <code>cagentic --serve</code> (or <code>/gateway on</code> ' +
      "in the terminal), then try again.";
    return;
  }
  $("fallback").style.display = "none";
  $("frame").style.display = "block";
  if ($("frame").src !== url) $("frame").src = url;
}

// ---- page context ----------------------------------------------------------
// "Add page" is the one thing a side panel gives you that a browser tab can't:
// the page you're looking at, handed to the assistant without copy-paste.
async function addPageContext() {
  const btn = $("ctxBtn");
  const label = btn.querySelector("span");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) return;
  btn.disabled = true;
  label.textContent = "Adding…";

  let selection = "";
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => String(window.getSelection ? window.getSelection() : ""),
    });
    selection = (result || "").trim();
  } catch (e) {
    // Restricted pages (chrome://, the Web Store) refuse injection. The URL and
    // title are still worth sending, so carry on without the selection.
    selection = "";
  }

  const parts = [
    "Context from the page I'm looking at:",
    `- Title: ${tab.title || "(untitled)"}`,
    `- URL: ${tab.url}`,
  ];
  if (selection) {
    // Cap it: a full-article selection would swamp the turn's context.
    const clipped = selection.length > 4000 ? selection.slice(0, 4000) + "\n…(truncated)" : selection;
    parts.push("", "Selected text:", clipped);
  }

  const port = await gatewayPort();
  try {
    // postMessage into the frame would need the page to opt in; going straight
    // to the API keeps the frame a plain, unmodified copy of the web UI.
    const res = await fetch(`http://127.0.0.1:${port}/api/context/page`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: parts.join("\n") }),
    });
    if (!res.ok) throw new Error(String(res.status));
    btn.classList.add("done");
    label.textContent = "Added";
    setTimeout(() => {
      btn.classList.remove("done");
      btn.disabled = false;
      label.textContent = "Add page";
    }, 1600);
  } catch (e) {
    label.textContent = "Failed";
    setTimeout(() => {
      btn.disabled = false;
      label.textContent = "Add page";
    }, 1600);
  }
}

$("ctxBtn").addEventListener("click", addPageContext);
$("retry").addEventListener("click", connect);
connect();
