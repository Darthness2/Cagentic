// Cagentic Bridge popup — shows whether the local bridge is reachable and
// lets the user change the port the extension talks to.

const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");
const portInput = document.getElementById("port");

async function loadPort() {
  const { port } = await chrome.storage.local.get("port");
  portInput.value = port || 8765;
  return portInput.value;
}

async function refreshStatus() {
  const port = await loadPort();
  dot.className = "dot";
  statusText.textContent = "checking…";
  try {
    const res = await fetch(`http://127.0.0.1:${port}/ping`, { method: "GET" });
    if (res.ok) {
      dot.className = "dot on";
      statusText.textContent = `connected on :${port}`;
      return;
    }
  } catch (e) {
    /* fall through */
  }
  dot.className = "dot off";
  statusText.textContent = "Cagentic not reachable";
}

document.getElementById("save").addEventListener("click", async () => {
  const port = parseInt(portInput.value, 10) || 8765;
  await chrome.storage.local.set({ port });
  await refreshStatus();
});

refreshStatus();
