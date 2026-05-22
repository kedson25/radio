const audio = document.getElementById("audio");
const startButton = document.getElementById("startButton");
const skipButton = document.getElementById("skipButton");
const muteButton = document.getElementById("muteButton");
const liveDot = document.getElementById("liveDot");
const stateLabel = document.getElementById("stateLabel");
const trackTitle = document.getElementById("trackTitle");
const requestedBy = document.getElementById("requestedBy");
const hint = document.getElementById("hint");
const queueList = document.getElementById("queueList");
const queueCount = document.getElementById("queueCount");

let enabled = localStorage.getItem("radioEnabled") === "true";
let currentId = null;
let currentStartedAt = null;
let statusBusy = false;
let seenQueueIds = new Set();
let handoffUntil = 0;

const HANDOFF_GRACE_MS = 15000;
const STREAM_URL = "/stream";

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";

  const whole = Math.floor(seconds);
  const minutes = Math.floor(whole / 60);
  const rest = String(whole % 60).padStart(2, "0");
  return `${minutes}:${rest}`;
}

async function playWhenAllowed() {
  if (!enabled) return;
  ensureStream();

  try {
    await audio.play();
    startButton.textContent = "Radio ligada";
    hint.textContent = "Radio conectada. As proximas musicas entram automaticamente.";
  } catch {
    hint.textContent = "O navegador bloqueou o autoplay. Clique em ligar radio novamente.";
  }
}

function ensureStream(forceReload = false) {
  const currentUrl = audio.getAttribute("src") || "";
  if (forceReload || currentUrl !== STREAM_URL) {
    audio.src = STREAM_URL;
    audio.load();
  }
}

function setIdle() {
  liveDot.classList.remove("live");
  stateLabel.textContent = "Aguardando novas musicas";
  trackTitle.textContent = "Nada tocando agora";
  requestedBy.textContent = "";
  skipButton.disabled = true;
}

function setHandoff() {
  liveDot.classList.add("live");
  stateLabel.textContent = "Preparando proxima musica";
  skipButton.disabled = true;
}

function setTrack(data) {
  handoffUntil = 0;
  liveDot.classList.add("live");
  stateLabel.textContent = data.remaining == null ? "Ao vivo" : `Ao vivo - ${formatTime(data.remaining)} restantes`;
  trackTitle.textContent = data.title || "Musica sem titulo";
  requestedBy.textContent = data.requested_by ? `Pedido por: ${data.requested_by}` : "";
  skipButton.disabled = false;
}

async function updateStatus() {
  if (statusBusy) return;
  statusBusy = true;

  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const data = await response.json();

    if (!data.playing) {
      if (Date.now() < handoffUntil) {
        setHandoff();
        return;
      }

      handoffUntil = 0;
      setIdle();
      if (currentId) {
        currentId = null;
        currentStartedAt = null;
      }
      return;
    }

    setTrack(data);

    if (data.id !== currentId || data.started_at !== currentStartedAt) {
      currentId = data.id;
      currentStartedAt = data.started_at;
      await playWhenAllowed();
    }
  } catch {
    liveDot.classList.remove("live");
    stateLabel.textContent = "Servidor indisponivel";
  } finally {
    statusBusy = false;
  }
}

function statusLabel(status) {
  if (status === "playing") return "tocando";
  if (status === "played") return "tocada";
  if (status === "error") return "erro";
  return "na fila";
}

function renderQueue(items) {
  queueCount.textContent = `${items.length} ${items.length === 1 ? "musica" : "musicas"}`;
  queueList.innerHTML = "";

  const newItems = items.filter((item) => !seenQueueIds.has(item.id));
  if (seenQueueIds.size && newItems.length) {
    const last = newItems[newItems.length - 1];
    hint.textContent = `Novo pedido recebido: ${last.title || "Musica sem titulo"}`;
  }
  seenQueueIds = new Set(items.map((item) => item.id));

  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "Nenhuma musica recebida ainda.";
    queueList.appendChild(empty);
    return;
  }

  for (const item of items) {
    const row = document.createElement("article");
    row.className = `queue-item ${item.status}`;

    const title = document.createElement("strong");
    title.textContent = item.title || "Musica sem titulo";

    const meta = document.createElement("span");
    const by = item.requested_by ? `Pedido por ${item.requested_by}` : "Sem nome";
    meta.textContent = item.timestamp ? `${by} - ${item.timestamp}` : by;

    const badge = document.createElement("small");
    badge.textContent = statusLabel(item.status);

    row.append(title, meta, badge);
    queueList.appendChild(row);
  }
}

async function updateQueue() {
  try {
    const response = await fetch("/api/queue", { cache: "no-store" });
    const data = await response.json();
    renderQueue(data.items || []);
  } catch {
    queueList.innerHTML = '<p class="empty">Nao foi possivel carregar a fila.</p>';
  }
}

audio.addEventListener("loadedmetadata", () => {
  playWhenAllowed();
});

audio.addEventListener("canplay", () => {
  playWhenAllowed();
});

startButton.addEventListener("click", async () => {
  enabled = true;
  localStorage.setItem("radioEnabled", "true");
  audio.muted = false;
  await updateStatus();
  await playWhenAllowed();
});

muteButton.addEventListener("click", () => {
  audio.muted = !audio.muted;
  muteButton.textContent = audio.muted ? "Som mutado" : "Som ligado";
});

skipButton.addEventListener("click", async () => {
  skipButton.disabled = true;
  hint.textContent = "Pulando musica...";

  try {
    await fetch("/api/skip", { method: "POST" });
    currentId = null;
    currentStartedAt = null;
    await updateStatus();
    await updateQueue();
  } catch {
    hint.textContent = "Nao consegui pular agora. Confira se o servidor esta rodando.";
  }
});

audio.addEventListener("ended", async () => {
  if (!enabled) return;

  ensureStream(true);
  await playWhenAllowed();
  setTimeout(updateStatus, 500);
});

setInterval(updateStatus, 1000);
setInterval(updateQueue, 1000);
updateStatus();
updateQueue();
