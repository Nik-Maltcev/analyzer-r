const API_ROOT = "https://www.id.meanx.pro";
const MARKETS = {
  id: "IDX",
  stocks: "Saham AS",
  crypto: "Kripto",
  saved: "Tersimpan",
};

const state = {
  market: "id",
  feeds: {},
  saved: new Map(),
};

const signalsElement = document.querySelector("#signals");
const statusElement = document.querySelector("#status");
const updatedElement = document.querySelector("#updated-at");
const marketLabelElement = document.querySelector("#market-label");
const refreshButton = document.querySelector("#refresh-button");
const template = document.querySelector("#signal-template");

function setStatus(message, loading = false) {
  statusElement.hidden = false;
  statusElement.textContent = "";
  if (loading) {
    const loader = document.createElement("span");
    loader.className = "loader";
    loader.setAttribute("aria-hidden", "true");
    statusElement.append(loader);
  }
  statusElement.append(document.createTextNode(message));
}

function cleanTicker(ticker) {
  return String(ticker || "").replace("/USD", "");
}

function recommendationText(signal) {
  const tickerA = cleanTicker(signal.ticker_a);
  const tickerB = cleanTicker(signal.ticker_b);
  if (signal.source === "pair") {
    return signal.direction === "long_a"
      ? `Beli ${tickerA} / Jual ${tickerB}`
      : `Jual ${tickerA} / Beli ${tickerB}`;
  }
  return signal.direction === "long"
    ? `Pertimbangkan beli ${tickerA}`
    : `Pertimbangkan jual ${tickerA}`;
}

function signalTiming(signal) {
  const age = Number(signal.signal_days || 0);
  const review = signal.review_in_days;
  if (review === null || review === undefined) {
    return `Sinyal aktif selama ${age} hari`;
  }
  return `Aktif ${age} hari · periksa lagi ~${review} hari`;
}

function renderSignals(items) {
  signalsElement.replaceChildren();
  if (!items.length) {
    setStatus(
      state.market === "saved"
        ? "Anda belum menyimpan sinyal."
        : "Belum ada sinyal tervalidasi di pasar ini."
    );
    return;
  }

  statusElement.hidden = true;
  for (const signal of items) {
    const node = template.content.cloneNode(true);
    node.querySelector(".market-badge").textContent = MARKETS[signal.market] || signal.market_label;
    node.querySelector(".pair").textContent = signal.pair;
    node.querySelector(".recommendation").textContent = recommendationText(signal);
    node.querySelector(".timing").textContent = signalTiming(signal);
    node.querySelector(".z-score").textContent = signal.primary_metric || `Z ${Number(signal.z_score).toFixed(2)}`;
    node.querySelector(".correlation").textContent = signal.confidence === "high"
      ? "Keyakinan tinggi"
      : `korelasi ${signal.correlation_pct}%`;

    const saveButton = node.querySelector(".save-button");
    const saved = state.saved.has(signal.id);
    saveButton.classList.toggle("is-saved", saved);
    saveButton.textContent = saved ? "\u2605" : "\u2606";
    saveButton.setAttribute("aria-label", saved ? "Hapus dari tersimpan" : "Simpan sinyal");
    saveButton.addEventListener("click", () => toggleSaved(signal));
    signalsElement.append(node);
  }
}

async function persistSaved() {
  await chrome.storage.local.set({ savedSignals: [...state.saved.values()] });
}

async function toggleSaved(signal) {
  if (state.saved.has(signal.id)) {
    state.saved.delete(signal.id);
  } else {
    state.saved.set(signal.id, signal);
  }
  await persistSaved();
  renderCurrent();
}

function renderCurrent() {
  marketLabelElement.textContent = MARKETS[state.market];
  if (state.market === "saved") {
    updatedElement.textContent = `${state.saved.size} tersimpan`;
    renderSignals([...state.saved.values()]);
    return;
  }
  const feed = state.feeds[state.market];
  if (!feed) return;
  updatedElement.textContent = new Intl.DateTimeFormat("id-ID", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(feed.updated_at));
  renderSignals(feed.items || []);
}

async function loadFeed(force = false) {
  marketLabelElement.textContent = MARKETS[state.market];
  if (state.market === "saved") {
    renderCurrent();
    return;
  }
  if (!force && state.feeds[state.market]) {
    renderCurrent();
    return;
  }

  refreshButton.classList.add("is-loading");
  setStatus("Memuat sinyal...", true);
  try {
    const response = await fetch(
      `${API_ROOT}/api/public/extension/feed?market=${state.market}`,
      { cache: "no-store" }
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.feeds[state.market] = await response.json();
    renderCurrent();
  } catch (error) {
    console.error(error);
    setStatus("Tidak dapat memperbarui. Coba lagi sebentar.");
    updatedElement.textContent = "Tidak tersedia";
  } finally {
    refreshButton.classList.remove("is-loading");
  }
}

document.querySelectorAll(".market-tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelector(".market-tab.is-active")?.classList.remove("is-active");
    button.classList.add("is-active");
    state.market = button.dataset.market;
    loadFeed();
  });
});

refreshButton.addEventListener("click", () => loadFeed(true));
document.querySelector("#open-app").addEventListener("click", () => {
  const market = state.market === "saved" ? "id" : state.market;
  chrome.tabs.create({
    url: `${API_ROOT}/app?market=${market}&utm_source=chrome_extension&utm_medium=extension&utm_campaign=id_launch`,
  });
});

chrome.storage.local.get({ savedSignals: [] }).then(({ savedSignals }) => {
  state.saved = new Map(savedSignals.map((signal) => [signal.id, signal]));
  loadFeed();
});
