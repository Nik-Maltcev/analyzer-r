const API_ROOT = "https://www.br.meanx.pro";
const MARKETS = {
  br: "B3",
  stocks: "Ações EUA",
  crypto: "Cripto",
  saved: "Salvos",
};

const state = {
  market: "br",
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

function signalTiming(signal) {
  const age = Number(signal.signal_days || 0);
  const review = signal.review_in_days;
  if (review === null || review === undefined) {
    return `Sinal ativo há ${age} dia${age === 1 ? "" : "s"}`;
  }
  return `Ativo há ${age} dia${age === 1 ? "" : "s"} · revisar em ~${review} dia${review === 1 ? "" : "s"}`;
}

function renderSignals(items) {
  signalsElement.replaceChildren();
  if (!items.length) {
    setStatus(
      state.market === "saved"
        ? "Você ainda não salvou nenhum sinal."
        : "Nenhum sinal validado neste mercado agora."
    );
    return;
  }

  statusElement.hidden = true;
  for (const signal of items) {
    const node = template.content.cloneNode(true);
    node.querySelector(".market-badge").textContent = signal.market_label;
    node.querySelector(".pair").textContent = signal.pair;
    node.querySelector(".recommendation").textContent = signal.recommendation;
    node.querySelector(".timing").textContent = signalTiming(signal);
    node.querySelector(".z-score").textContent = signal.primary_metric || `Z ${Number(signal.z_score).toFixed(2)}`;
    node.querySelector(".correlation").textContent = signal.secondary_metric || `corr. ${signal.correlation_pct}%`;

    const saveButton = node.querySelector(".save-button");
    const saved = state.saved.has(signal.id);
    saveButton.classList.toggle("is-saved", saved);
    saveButton.textContent = saved ? "\u2605" : "\u2606";
    saveButton.setAttribute("aria-label", saved ? "Remover dos salvos" : "Salvar sinal");
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
    updatedElement.textContent = `${state.saved.size} salvo${state.saved.size === 1 ? "" : "s"}`;
    renderSignals([...state.saved.values()]);
    return;
  }
  const feed = state.feeds[state.market];
  if (!feed) return;
  updatedElement.textContent = new Intl.DateTimeFormat("pt-BR", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(feed.updated_at));
  renderSignals(feed.items || []);
}

async function loadFeed(force = false) {
  if (state.market === "saved") {
    renderCurrent();
    return;
  }
  if (!force && state.feeds[state.market]) {
    renderCurrent();
    return;
  }

  refreshButton.classList.add("is-loading");
  setStatus("Buscando sinais...", true);
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
    setStatus("Não foi possível atualizar. Tente novamente em instantes.");
    updatedElement.textContent = "Indisponível";
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
  const market = state.market === "saved" ? "br" : state.market;
  chrome.tabs.create({
    url: `${API_ROOT}/app?market=${market}&utm_source=chrome_extension&utm_medium=extension&utm_campaign=br_launch`,
  });
});

chrome.storage.local.get({ savedSignals: [] }).then(({ savedSignals }) => {
  state.saved = new Map(savedSignals.map((signal) => [signal.id, signal]));
  loadFeed();
});
