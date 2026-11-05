/**
 * ETU-AI Client Application
 * Gère le Setup Wizard initial, le streaming SSE, l'injection HTML de l'IA,
 * la persistance SQLite et le superviseur MAIA-Beacon (port 11343).
 */

let currentConversationId = null;
let activeDownloadPollers = {};
let setupPollingInterval = null;
let currentActiveModel = null;
let availableInstalledModels = [];
let remoteApiModels = [];
let installedNativeModels = [];
let assistantProfiles = [];
let activeProfileId = "default";
let preferredProfileId = "default";
let pendingAvatarData = null;
let profileAvatarFallback = "✦";
let currentContextLimit = null;
let currentModelReady = null;
let currentModelState = "unknown";
let localModelHasResponded = false;
let currentHardwareSnapshot = null;
const streamMarkup = new WeakMap();
const streamFrames = new WeakMap();
const appliedStreamTemplates = new WeakMap();
let activeChatStream = null;
let chatViewVersion = 0;
let appPowerState = "awake";
let currentLaunchMode = "normal";
let powerActionMenu = null;
const notificationStorageKey = "etu-app-notifications";
let appNotifications = [];
try { appNotifications = JSON.parse(localStorage.getItem(notificationStorageKey) || "[]"); } catch (_) { appNotifications = []; }
if (!Array.isArray(appNotifications)) appNotifications = [];
// Les anciennes alertes de réponse sans conversation ne peuvent plus être consultées utilement.
const storedNotificationKeys = new Set();
appNotifications = appNotifications.filter(item => {
  if (!item || !Number.isFinite(item.time) || (item.kind === "response" && !item.conversationId)) return false;
  const key = item.eventKey || `${item.kind}:${item.title}:${item.detail}`;
  if (storedNotificationKeys.has(key)) return false;
  storedNotificationKeys.add(key);
  return true;
}).slice(0, 50);
const notificationEventStorageKey = "etu-notification-events";
let notificationEventKeys = new Set(appNotifications.map(item => item.eventKey).filter(Boolean));
try {
  const keys = JSON.parse(localStorage.getItem(notificationEventStorageKey) || "[]");
  if (Array.isArray(keys)) keys.filter(key => typeof key === "string").slice(-200).forEach(key => notificationEventKeys.add(key));
} catch (_) { /* Les notifications restent utilisables si le cache est invalide. */ }
const seenDownloadStatuses = new Map();
let downloadRefreshInFlight = false;
let applyingAppRoute = false;
let lastAppliedAppRoute = null;

const modelTabRoutes = {
  "tab-catalog": "local",
  "tab-vision": "vision",
  "tab-hf-explorer": "hugging-face",
  "tab-hf-custom": "direct",
};
const modelRouteTabs = Object.fromEntries(Object.entries(modelTabRoutes).map(([tab, route]) => [route, tab]));

function setAppRoute(route, replace = false) {
  requestAnimationFrame(dismissViewedResponseNotifications);
  if (applyingAppRoute || window.location.hash === route) return;
  window.history[replace ? "replaceState" : "pushState"]({}, "", route);
  lastAppliedAppRoute = route;
}

function syncModelsRoute() {
  const activeTab = document.querySelector(".modal-tab-btn.active")?.dataset.tab || "tab-catalog";
  const routeTab = modelTabRoutes[activeTab] || "local";
  const inSettingsModels = !document.getElementById("settings-view")?.hidden
    && document.querySelector('[data-settings-panel="models"]:not([hidden])');
  setAppRoute(inSettingsModels ? `#/settings/models/${routeTab}` : `#/models/${routeTab}`);
}

function restoreAppRoute() {
  if (lastAppliedAppRoute === window.location.hash) return;
  lastAppliedAppRoute = window.location.hash;
  const route = window.location.hash.replace(/^#\/?/, "").split("?")[0].split("/").filter(Boolean);
  applyingAppRoute = true;
  try {
    if (route[0] === "settings") {
      const section = ["general", "models", "profiles", "providers", "data", "wiki", "etu", "installation"].includes(route[1]) ? route[1] : "general";
      closeDownloadsView({ fromRoute: true });
      showSettingsView(section, { fromRoute: true });
      if (section === "models") showModelsTab(modelRouteTabs[route[2]] || "tab-catalog", { fromRoute: true });
    } else if (route[0] === "chat" && route[1]) {
      closeSettingsView({ fromRoute: true });
      closeDownloadsView({ fromRoute: true });
      try { selectConversation(decodeURIComponent(route[1])); }
      catch { window.history.replaceState({}, "", "#/chat"); }
    } else if (route[0] === "downloads") {
      closeSettingsView({ fromRoute: true });
      showDownloadsView({ fromRoute: true });
    } else if (route[0] === "models") {
      closeSettingsView({ fromRoute: true });
      closeDownloadsView({ fromRoute: true });
      openModal("modal-models", { fromRoute: true });
      showModelsTab(modelRouteTabs[route[1]] || "tab-catalog", { fromRoute: true });
    } else {
      closeSettingsView({ fromRoute: true });
      closeDownloadsView({ fromRoute: true });
      const modelDialog = document.getElementById("modal-models");
      if (modelDialog?.classList.contains("active")) closeModal("modal-models", { fromRoute: true });
      if (route[0] !== "chat") window.history.replaceState({}, "", "#/chat");
    }
  } finally {
    applyingAppRoute = false;
  }
}

function initAppUrlRouting() {
  if (!window.location.hash) setAppRoute("#/chat", true);
  window.addEventListener("popstate", restoreAppRoute);
  window.addEventListener("hashchange", restoreAppRoute);
  window.addEventListener("popstate", dismissViewedResponseNotifications);
  window.addEventListener("hashchange", dismissViewedResponseNotifications);
  restoreAppRoute();
}

function resizeChatInput() {
  const input = document.getElementById("chat-input");
  if (!input) return;
  input.style.overflowY = "hidden";
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
  input.style.overflowY = input.value.length && input.scrollHeight > input.clientHeight + 1 ? "auto" : "hidden";
}

function trackChatComposerHeight() {
  const main = document.getElementById("chat-main");
  const composer = document.getElementById("chat-input-container");
  if (!main || !composer) return;
  const update = () => main.style.setProperty("--chat-composer-height", `${Math.ceil(composer.getBoundingClientRect().height)}px`);
  update();
  new ResizeObserver(update).observe(composer);
}

function cancelChatStream() {
  chatViewVersion++;
  if (activeChatStream) {
    const { controller, bubble, requestId, accepted, rawMessage, userBubble } = activeChatStream;
    if (!accepted) {
      document.getElementById("chat-input").value = rawMessage;
      resizeChatInput();
      userBubble.remove();
    }
    fetch(`/api/chat/requests/${requestId}/stop`, { method: "POST" }).catch(() => {});
    controller.abort();
    finishStreamRender(bubble);
    const message = bubble.closest(".message");
    message.classList.remove("streaming");
    setAssistantActivity(message, "stopped", "Génération interrompue");
    if (!bubble.textContent.trim()) bubble.textContent = "Génération interrompue.";
    const timer = streamFrames.get(bubble);
    if (timer) clearTimeout(timer);
    streamFrames.delete(bubble);
    activeChatStream = null;
  }
  document.getElementById("chat-form").dataset.busy = "false";
  document.getElementById("btn-send").disabled = false;
  const attach = document.getElementById("btn-attach");
  if (attach) attach.disabled = false;
  document.getElementById("btn-stop").hidden = true;
}

document.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.theme = localStorage.getItem("etu-theme") || "light";
  const themeButton = document.getElementById("btn-theme");
  const updateThemeButton = () => {
    const light = document.documentElement.dataset.theme === "light";
    themeButton.textContent = light ? "☾" : "☀";
    themeButton.setAttribute("aria-label", light ? "Activer le thème sombre" : "Activer le thème clair");
    themeButton.title = themeButton.getAttribute("aria-label");
  };
  updateThemeButton();
  document.getElementById("btn-theme").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("etu-theme", theme);
    updateThemeButton();
    ETUChatUI.refreshTheme();
    document.querySelectorAll(".authored-preview-frame").forEach(sendAuthoredPreview);
  });
  initSetupWizard();
  initModals();
  initModelSelector();
  initChatForm();
  trackChatComposerHeight();
  ETUChatUI.configure({
    submit: submitGeneratedPrompt,
    renderProgram: (container, html) => renderAuthoredPreview(container, html, true),
    copy: value => navigator.clipboard.writeText(String(value).slice(0, 10000)).catch(() => {}),
    viewChanged: (available, active) => {
      const button = document.getElementById("btn-chat-view");
      button.hidden = !available;
      button.textContent = active ? "Voir la discussion" : "Afficher la vue";
    },
  });
  document.getElementById("btn-chat-view").addEventListener("click", () => { ETUChatUI.toggleView(); scrollToBottom(); });
  document.getElementById("chat-messages").addEventListener("click", (event) => {
    const suggestion = event.target.closest("[data-prompt]");
    if (!suggestion) return;
    document.getElementById("chat-input").value = suggestion.dataset.prompt;
    resizeChatInput();
    document.getElementById("chat-form").requestSubmit();
  });
  initProfiles();
  initSettingsPage();
  initHardwarePreference();
  initAppUtilities();
  initApiSettings();
  loadConversations();
  loadServerStatus();

  const sidebar = document.getElementById("sidebar");
  const sidebarToggle = document.getElementById("btn-open-sidebar");
  const sidebarCollapseToggle = document.getElementById("btn-collapse-sidebar");
  const desktopSidebarMedia = window.matchMedia("(min-width: 701px)");
  const sidebarCollapsedStorageKey = "etu-sidebar-collapsed";
  const syncSidebarCollapseButton = () => {
    const collapsed = sidebar.classList.contains("is-collapsed");
    sidebarCollapseToggle.setAttribute("aria-expanded", String(!collapsed));
    sidebarCollapseToggle.setAttribute("aria-label", collapsed ? "Déployer la barre latérale" : "Rétracter la barre latérale");
    sidebarCollapseToggle.title = collapsed ? "Déployer la barre latérale" : "Rétracter la barre latérale";
  };
  if (desktopSidebarMedia.matches && localStorage.getItem(sidebarCollapsedStorageKey) === "true") {
    sidebar.classList.add("is-collapsed");
  }
  syncSidebarCollapseButton();
  sidebarCollapseToggle.addEventListener("click", () => {
    if (!desktopSidebarMedia.matches) return;
    const collapsed = sidebar.classList.toggle("is-collapsed");
    localStorage.setItem(sidebarCollapsedStorageKey, String(collapsed));
    syncSidebarCollapseButton();
  });
  desktopSidebarMedia.addEventListener("change", (event) => {
    if (!event.matches) sidebar.classList.remove("is-collapsed");
    else if (localStorage.getItem(sidebarCollapsedStorageKey) === "true") sidebar.classList.add("is-collapsed");
    syncSidebarCollapseButton();
  });
  const closeSidebar = () => {
    sidebar.classList.remove("is-open");
    sidebarToggle.setAttribute("aria-expanded", "false");
    document.body.classList.remove("sidebar-open");
  };
  sidebarToggle.addEventListener("click", () => {
    const isOpen = sidebar.classList.toggle("is-open");
    sidebarToggle.setAttribute("aria-expanded", String(isOpen));
    document.body.classList.toggle("sidebar-open", isOpen);
    if (isOpen) document.getElementById("history-search").focus();
  });
  document.getElementById("btn-close-sidebar").addEventListener("click", closeSidebar);
  document.getElementById("sidebar-backdrop").addEventListener("click", closeSidebar);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sidebar.classList.contains("is-open")) {
      closeSidebar();
      sidebarToggle.focus();
    }
  });
  document.getElementById("history-search").addEventListener("input", renderConversationHistory);

  document.getElementById("btn-open-settings")?.addEventListener("click", () => showSettingsView("general"));
  document.getElementById("btn-settings-close")?.addEventListener("click", closeSettingsView);
  document.getElementById("btn-settings-theme")?.addEventListener("click", () => document.getElementById("btn-theme")?.click());
  document.getElementById("btn-settings-repair")?.addEventListener("click", () => document.getElementById("btn-reinstall-app")?.click());
  document.getElementById("btn-settings-uninstall")?.addEventListener("click", () => showToast("La désinstallation sera disponible avec le programme d’installation de bureau.", "info"));
  document.getElementById("btn-settings-dependencies")?.addEventListener("click", () => showToast("La désinstallation des dépendances sera disponible avec le programme d’installation de bureau.", "info"));
  document.getElementById("btn-settings-reinstall-server")?.addEventListener("click", reinstallServerVariant);
  document.getElementById("btn-settings-delete-chats")?.addEventListener("click", async (event) => {
    if (!conversationHistory.length) {
      showToast("Aucun chat à supprimer.", "info");
      return;
    }
    if (!window.confirm("Supprimer définitivement tous les chats enregistrés sur cet ordinateur ?")) return;
    const button = event.currentTarget;
    button.disabled = true;
    cancelChatStream();
    try {
      const response = await fetch("/api/conversations", { method: "DELETE" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      conversationHistory = [];
      renderConversationHistory();
      startNewConversation();
      showSettingsView("data");
      showToast("Tous les chats ont été supprimés.", "success");
    } catch (error) {
      console.error("Erreur suppression des chats :", error);
      showToast("Impossible de supprimer les chats.", "error");
    } finally {
      button.disabled = false;
    }
  });
  document.getElementById("btn-settings-update")?.addEventListener("click", checkEngineUpdates);
  document.querySelectorAll(".settings-nav-item").forEach((button) => {
    button.addEventListener("click", () => showSettingsView(button.dataset.settingsSection));
  });

  const newChatBtn = document.getElementById("btn-new-chat");
  if (newChatBtn) {
    newChatBtn.addEventListener("click", () => {
      closeSettingsView();
      startNewConversation();
      closeSidebar();
      document.getElementById("chat-input").focus();
    });
  }

  const reopenWizardBtn = document.getElementById("btn-reinstall-app");
  if (reopenWizardBtn) {
    reopenWizardBtn.addEventListener("click", () => {
      closeModal("modal-configuration");
      cancelChatStream();
      document.getElementById("app-container").style.display = "none";
      const wz = document.getElementById("wizard-container");
      wz.style.display = "flex";
      showWizardStep(1);
      loadHardwareDiagnosis();
      if (wizardParticles) {
        wizardParticles.resize();
        wizardParticles.setTheme("cyan");
        wizardParticles.addRipple(window.innerWidth / 2, window.innerHeight / 2, 1.5);
      }
    });
  }
  initAppUrlRouting();
});

async function initHardwarePreference() {
  const control = document.getElementById("settings-hardware-preference");
  const status = document.getElementById("settings-hardware-status");
  if (!control || !status) return;
  const choices = [...control.querySelectorAll("[data-hardware-preference]")];
  let hardwareCapabilities = { has_gpu: false, has_npu: false };
  const setSelectedPreference = (preference) => {
    choices.forEach((choice) => {
      const selected = choice.dataset.hardwarePreference === preference;
      choice.setAttribute("aria-pressed", String(selected));
      choice.classList.toggle("is-selected", selected);
    });
  };
  const setAvailableHardware = (data) => {
    hardwareCapabilities = data;
    choices.forEach((choice) => {
      const preference = choice.dataset.hardwarePreference;
      choice.disabled = (preference === "gpu" && !data.has_gpu) || (preference === "npu" && !data.has_npu);
      if (choice.disabled) choice.title = `Aucun ${preference.toUpperCase()} détecté sur cet ordinateur`;
      else choice.removeAttribute("title");
    });
  };
  try {
    const response = await fetch("/api/settings/hardware");
    const data = await response.json();
    if (response.ok) {
      setSelectedPreference(data.preference || "auto");
      setAvailableHardware(data);
    }
  } catch (_) { status.textContent = "Préférence matérielle indisponible."; }
  choices.forEach((choice) => choice.addEventListener("click", async () => {
    const preference = choice.dataset.hardwarePreference;
    if (choice.disabled || choice.getAttribute("aria-pressed") === "true") return;
    choices.forEach((item) => { item.disabled = true; });
    status.textContent = "Redémarrage de MAIA-Beacon…";
    try {
      const response = await fetch("/api/settings/hardware", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ preference }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Impossible d’appliquer ce réglage");
      setSelectedPreference(data.preference || preference);
      setAvailableHardware(data);
      status.textContent = "Préférence appliquée. MAIA-Beacon redémarre avec le matériel choisi.";
    } catch (error) {
      status.textContent = error.message;
      try {
        const response = await fetch("/api/settings/hardware");
        if (response.ok) {
          const data = await response.json();
          setSelectedPreference(data.preference || "auto");
          setAvailableHardware(data);
        }
      } catch (_) {}
    } finally { setAvailableHardware(hardwareCapabilities); }
  }));
}

async function initApiSettings() {
  const open = document.getElementById("settings-nav-providers");
  const form = document.getElementById("api-settings-form");
  if (!open || !form) return;
  const providerSelect = document.getElementById("api-provider-select");
  const modelsContainer = document.getElementById("api-provider-models");
  const modelSearch = document.getElementById("api-provider-model-search");
  const modelHint = document.getElementById("api-model-hint");
  const clearKeyInput = document.getElementById("api-provider-clear-key");
  const clearKeyButton = document.getElementById("api-provider-clear-key-button");
  let savedModelSelection = "";
  let chatgptAuthMethod = "api_key";
  let presets = {};
  let currentConfig = {};
  const settingsMessage = document.getElementById("api-settings-message");
  const setSettingsMessage = (text, state = "info") => {
    settingsMessage.textContent = text;
    settingsMessage.dataset.state = state;
  };
  const providerLabels = { openai: "OpenAI", anthropic: "Anthropic", gemini: "Google Gemini", mistral: "Mistral", openrouter: "OpenRouter", groq: "Groq", together: "Together AI", deepseek: "DeepSeek", xai: "xAI", cerebras: "Cerebras", fireworks: "Fireworks AI", perplexity: "Perplexity", nvidia: "NVIDIA NIM", sambanova: "SambaNova", deepinfra: "DeepInfra", "github-models": "GitHub Models", "openai-compatible": "API compatible OpenAI", custom: "Fournisseur personnalisé" };
  const providerMarks = { openai: "✳", anthropic: "✹", gemini: "✦", mistral: "M", openrouter: "◈", groq: "G", together: "T", deepseek: "D", xai: "X", cerebras: "C", fireworks: "F", perplexity: "P", nvidia: "N", sambanova: "S", deepinfra: "DI", "github-models": "GH", "openai-compatible": "AI", custom: "＋" };
  const providerSvg = new Set(["openai", "anthropic", "gemini", "mistral", "openrouter", "groq", "together", "deepseek", "xai", "cerebras", "fireworks", "perplexity", "nvidia", "sambanova", "deepinfra", "github-models"]);
  const setProviderMark = (element, id) => {
    element.replaceChildren();
    if (providerSvg.has(id)) {
      const image = document.createElement("img");
      image.src = `/static/provider-icons/${id}.svg`;
      image.alt = "";
      image.setAttribute("aria-hidden", "true");
      element.append(image);
    } else {
      element.textContent = providerMarks[id] || "AI";
    }
  };
  const renderProviderList = (selected = providerSelect.value) => {
    const list = document.getElementById("api-provider-list");
    if (!list) return;
    list.replaceChildren();
    Object.entries(presets).filter(([id]) => id !== "chatgpt").forEach(([id, preset]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "api-provider-nav-item";
      button.setAttribute("aria-current", String(id === selected));
      const mark = document.createElement("span"); mark.className = "api-provider-nav-mark"; setProviderMark(mark, id);
      const copy = document.createElement("span"); copy.className = "api-provider-nav-copy"; copy.textContent = id === "custom" ? "Personnalisé" : id === "openai-compatible" ? "Autre API compatible" : (preset.label || providerLabels[id] || id);
      const state = document.createElement("span"); state.className = "api-provider-nav-state";
      if (currentConfig.configured?.[id]?.has_api_key) state.classList.add("is-configured");
      if (currentConfig.active_provider === id) state.classList.add("is-active");
      button.append(mark, copy, state);
      button.addEventListener("click", () => { providerSelect.value = id; providerSelect.dispatchEvent(new Event("change")); });
      list.append(button);
    });
  };
  const setKeyStatus = (hasKey) => {
    const status = document.getElementById("api-key-status");
    status.textContent = hasKey ? "Clé enregistrée" : "Aucune clé";
    status.dataset.hasKey = String(Boolean(hasKey));
    if (clearKeyButton) clearKeyButton.hidden = !hasKey || (providerSelect.value === "openai" && chatgptAuthMethod !== "api_key");
  };
  const setClearKeyState = (value) => {
    if (clearKeyInput) clearKeyInput.checked = Boolean(value);
    if (clearKeyButton) {
      clearKeyButton.textContent = value ? "Clé à supprimer" : "Supprimer la clé enregistrée";
      clearKeyButton.setAttribute("aria-pressed", String(Boolean(value)));
    }
  };
  const renderModelEmptyState = (title, detail = "", state = "") => {
    const empty = document.createElement("div");
    empty.className = `api-model-empty-state ${state}`.trim();
    const icon = state === "is-loading"
      ? '<span class="api-model-loading-spinner" aria-hidden="true"></span>'
      : (window.ETUSVG?.icon("cloudOff", "api-model-empty-icon") || "");
    empty.innerHTML = `${icon}<strong>${escapeHtml(title)}</strong>${detail ? `<span>${escapeHtml(detail)}</span>` : ""}`;
    return empty;
  };
  let loadSequence = 0;
  let activeModelRequest = null;
  const loadProviderModels = async ({ hasSavedKey = false, selected = [] } = {}) => {
    activeModelRequest?.abort();
    const sequence = ++loadSequence;
    const controller = new AbortController();
    activeModelRequest = controller;
    const refreshButton = document.getElementById("api-provider-models-refresh");
    if (refreshButton) {
      refreshButton.disabled = true;
      refreshButton.classList.add("is-loading");
      refreshButton.setAttribute("aria-busy", "true");
    }
    modelHint.textContent = "Chargement des modèles depuis le fournisseur…";
    modelsContainer.replaceChildren(renderModelEmptyState("Recherche des modèles…", "La requête peut prendre quelques secondes.", "is-loading"));
    try {
      const timeoutId = setTimeout(() => controller.abort(), 25000);
      const response = await fetch("/api/settings/provider/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          provider: providerSelect.value,
          base_url: document.getElementById("api-provider-url").value,
          api_key: document.getElementById("api-provider-key").value,
          auth_method: providerSelect.value === "openai" ? chatgptAuthMethod : undefined,
        }),
      }).finally(() => clearTimeout(timeoutId));
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Impossible de charger les modèles.");
      if (sequence !== loadSequence) return;
      modelsContainer.replaceChildren();
      modelSearch.hidden = false;
      modelSearch.value = "";
      result.models.forEach((id) => {
        const label = document.createElement("label"); label.className = "api-provider-model-option";
        const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.name = "api-provider-model-choice"; checkbox.value = id;
        checkbox.checked = selected.includes(id) || (!selected.length && id === savedModelSelection);
        label.classList.toggle("is-selected", checkbox.checked);
        const mark = document.createElement("span"); mark.className = "api-model-provider-mark"; setProviderMark(mark, providerSelect.value);
        const text = document.createElement("span"); text.className = "api-provider-model-name"; text.textContent = id;
        checkbox.addEventListener("change", () => label.classList.toggle("is-selected", checkbox.checked));
        label.append(mark, text, checkbox); modelsContainer.append(label);
      });
      modelHint.textContent = `${result.models.length} modèles reçus. Cochez ceux à afficher dans le sélecteur du chat.`;
    } catch (error) {
      if (sequence !== loadSequence) return;
      const statusMessage = error.message || "Impossible de charger les modèles.";
      const isTimeout = error.name === "AbortError";
      const hasAvailableKey = hasSavedKey || Boolean(document.getElementById("api-provider-key").value.trim());
      if (providerSelect.value === "mistral" && savedModelSelection === "mistral-medium") {
        const replacementId = "mistral-medium-3-5";
        const label = document.createElement("label"); label.className = "api-provider-model-option is-selected";
        const mark = document.createElement("span"); mark.className = "api-model-provider-mark"; setProviderMark(mark, "mistral");
        const name = document.createElement("span"); name.className = "api-provider-model-name"; name.textContent = `${replacementId} · remplacement conseillé`;
        const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.name = "api-provider-model-choice"; checkbox.value = replacementId; checkbox.checked = true;
        checkbox.addEventListener("change", () => label.classList.toggle("is-selected", checkbox.checked));
        label.append(mark, name, checkbox);
        modelsContainer.replaceChildren(label);
        modelSearch.hidden = true;
        modelHint.textContent = `La liste Mistral n’a pas répondu. ${replacementId} remplace le modèle retiré mistral-medium ; enregistrez pour l’activer.`;
        return;
      }
      const conciseMessage = isTimeout ? "La réponse met trop de temps" : providerSelect.value === "openai" && chatgptAuthMethod === "account" && !hasAvailableKey ? "Connectez votre compte ChatGPT" : (/clé API|key|401|403/i.test(statusMessage) ? "Vérifiez la clé API" : (/404|statut/i.test(statusMessage) ? "Aucun modèle disponible" : (hasAvailableKey ? "Modèles indisponibles" : "Saisissez une clé API")));
      const detail = isTimeout ? "Délai dépassé après 25 secondes. Vérifiez la connexion et réessayez." : (providerSelect.value === "openai" && chatgptAuthMethod === "account" && !hasAvailableKey ? "Connectez-vous avec votre compte ChatGPT, puis réessayez." : statusMessage);
      modelsContainer.replaceChildren(renderModelEmptyState(conciseMessage, detail));
      modelSearch.hidden = true;
      modelHint.textContent = hasAvailableKey ? "Utilisez ↻ pour réessayer." : providerSelect.value === "openai" && chatgptAuthMethod === "account" ? "Connectez votre compte pour charger les modèles." : "Saisissez une clé API, puis utilisez ↻ pour charger les modèles.";
    } finally {
      if (activeModelRequest === controller) activeModelRequest = null;
      if (sequence === loadSequence && refreshButton) {
        refreshButton.disabled = false;
        refreshButton.classList.remove("is-loading");
        refreshButton.removeAttribute("aria-busy");
      }
    }
  };
  modelSearch.addEventListener("input", () => {
    const query = modelSearch.value.trim().toLocaleLowerCase();
    let visibleCount = 0;
    modelsContainer.querySelectorAll(".api-provider-model-option").forEach((option) => {
      const matches = option.querySelector(".api-provider-model-name").textContent.toLocaleLowerCase().includes(query);
      option.hidden = !matches;
      if (matches) visibleCount++;
    });
    const total = modelsContainer.querySelectorAll(".api-provider-model-option").length;
    modelHint.textContent = query
      ? `${visibleCount} modèle${visibleCount > 1 ? "s" : ""} correspondant${visibleCount > 1 ? "s" : ""} sur ${total}.`
      : `${total} modèles reçus. Cochez ceux à afficher dans le sélecteur du chat.`;
  });
  const checkedModels = () => [...modelsContainer.querySelectorAll('input[name="api-provider-model-choice"]:checked')].map((input) => input.value);
  document.getElementById("api-provider-models-refresh")?.addEventListener("click", () => {
    loadProviderModels({ hasSavedKey: currentConfig.has_api_key, selected: checkedModels() });
  });
  const renderProviderBrand = (id, config = {}) => {
    const provider = presets[id] || {};
    const labels = {
      openai: "OpenAI", chatgpt: "OpenAI", anthropic: "Anthropic", gemini: "Google Gemini", mistral: "Mistral",
      openrouter: "OpenRouter", groq: "Groq", together: "Together AI", deepseek: "DeepSeek",
      xai: "xAI", cerebras: "Cerebras", fireworks: "Fireworks AI", perplexity: "Perplexity",
      nvidia: "NVIDIA NIM", sambanova: "SambaNova", deepinfra: "DeepInfra", "github-models": "GitHub Models",
      "openai-compatible": "API compatible OpenAI", custom: "Fournisseur personnalisé",
    };
    const marks = { openai: "✳", anthropic: "✹", gemini: "✦", mistral: "M", openrouter: "◈", groq: "G", together: "T", deepseek: "D", xai: "X", cerebras: "C", fireworks: "F", perplexity: "P", nvidia: "N", sambanova: "S", deepinfra: "DI", "github-models": "GH", "openai-compatible": "AI", custom: "＋" };
    const card = document.getElementById("api-provider-brand");
    card.dataset.provider = id;
    setProviderMark(document.getElementById("api-provider-mark"), id);
    document.getElementById("api-provider-brand-name").textContent = provider.label || labels[id] || id;
    document.getElementById("api-provider-brand-detail").textContent = id === "openai" || id === "chatgpt" ? (config.auth_method === "account" ? "Compte ChatGPT" : "Clé API OpenAI") : `${provider.type === "openai" ? "API compatible OpenAI" : "Connexion API native"} · clé stockée localement`;
    document.getElementById("api-provider-brand-state").textContent = config.enabled ? "Actif" : (config.has_api_key ? ((id === "openai" || id === "chatgpt") && config.auth_method === "account" ? "Compte connecté" : "Clé enregistrée") : "À configurer");
    card.classList.toggle("is-active", Boolean(config.enabled));
  };
  const updatePrivacyHint = (config) => {
    const hint = document.getElementById("chat-privacy-hint");
    const active = config?.active_provider;
    const providerLabel = active && config.providers?.[active]?.label;
    if (hint) hint.textContent = active === "openai" || active === "chatgpt" ? (config?.auth_method === "account" ? "Compte ChatGPT utilisé · messages transmis à OpenAI." : "Clé API OpenAI utilisée · facturation API OpenAI.") : providerLabel ? `Messages transmis à ${providerLabel}.` : "Les réponses restent sur cet ordinateur.";
  };
  const applyProviderPreset = (id, preserveValues = false) => {
    const preset = presets[id];
    if (!preset) return;
    const accountProvider = id === "openai" || id === "chatgpt";
    const accountMode = accountProvider && chatgptAuthMethod === "account";
    const nativeProvider = preset.type !== "openai";
    document.getElementById("api-key-label").childNodes[0].textContent = accountProvider ? "Clé API OpenAI " : "Clé API ";
    document.getElementById("chatgpt-auth-method-wrap").hidden = !accountProvider;
    document.getElementById("chatgpt-auth-method").value = chatgptAuthMethod;
    document.getElementById("chatgpt-api-key-note").hidden = !accountProvider || chatgptAuthMethod !== "api_key";
    document.getElementById("chatgpt-account-panel").hidden = !accountMode;
    document.querySelector(".api-key-intro strong").textContent = accountMode ? "Votre compte reste privé" : "Vos clés restent privées";
    document.querySelector(".api-key-intro p").textContent = accountMode
      ? "Les jetons de connexion sont conservés dans le coffre-fort de votre système."
      : "Stockées dans le coffre-fort de votre système. Elles ne sont jamais affichées après l’enregistrement.";
    document.getElementById("api-key-label").hidden = accountMode;
    document.getElementById("api-provider-key").hidden = accountMode;
    if (accountMode && clearKeyButton) clearKeyButton.hidden = true;
    document.querySelector("#api-settings-form > .etu-text").hidden = accountMode;
    document.getElementById("api-provider-url-wrap").hidden = (nativeProvider && !accountProvider) || accountMode;
    document.getElementById("api-provider-url").required = !nativeProvider || (accountProvider && !accountMode);
    if (!preserveValues) {
      document.getElementById("api-provider-url").value = preset.base_url;
    }
  };
  const accountSelect = document.getElementById("chatgpt-account-select");
  const accountStatus = document.getElementById("chatgpt-account-status");
  const connectButton = document.getElementById("chatgpt-connect");
  const addAccountButton = document.getElementById("chatgpt-add-account");
  const disconnectButton = document.getElementById("chatgpt-disconnect");
  let chatgptAccounts = [];
  const loadChatgptStatus = async () => {
    const response = await fetch("/api/settings/provider/chatgpt");
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Impossible de lire les comptes ChatGPT.");
    chatgptAccounts = result.accounts;
    accountSelect.replaceChildren();
    result.accounts.forEach((account) => {
      const option = document.createElement("option");
      option.value = account.client_id;
      option.textContent = `${account.email || "Compte ChatGPT"} · ${account.client_id.slice(-6)}${account.connected ? "" : " (déconnecté)"}`;
      accountSelect.append(option);
    });
    accountSelect.hidden = !result.accounts.length;
    accountSelect.previousElementSibling.hidden = !result.accounts.length;
    if (result.active) accountSelect.value = result.active;
    connectButton.textContent = result.accounts.length ? "Reconnecter ce compte" : "Continuer avec ChatGPT";
    addAccountButton.hidden = !result.accounts.length;
    disconnectButton.hidden = !result.active;
    const statusText = result.flow.status === "pending" ? "Terminez la connexion dans la fenêtre OpenAI…"
      : result.active ? `Connecté : ${result.accounts.find((account) => account.client_id === result.active)?.email || "compte ChatGPT"}`
      : "Aucun compte connecté.";
    if (accountStatus.textContent !== statusText) accountStatus.textContent = statusText;
    return result;
  };
  const startChatgptFlow = async (newAccount = false) => {
    const popup = window.open("about:blank", "_blank");
    connectButton.disabled = true;
    addAccountButton.disabled = true;
    accountStatus.textContent = "Ouverture de la connexion ChatGPT…";
    try {
      const response = await fetch("/api/settings/provider/chatgpt/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: newAccount ? null : accountSelect.value || null }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Connexion impossible.");
      if (!popup) throw new Error("Autorisez les fenêtres supplémentaires, puis réessayez.");
      popup.location.href = result.url;
      accountStatus.textContent = "Terminez la connexion dans la fenêtre OpenAI…";
      for (let attempt = 0; attempt < 300; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const state = await loadChatgptStatus();
        if (state.flow.status === "success") {
          accountStatus.textContent = "Compte ChatGPT connecté.";
          currentConfig = { ...currentConfig, has_api_key: true, auth_method: "account" };
          renderProviderBrand("openai", currentConfig);
          setKeyStatus(true);
          await loadProviderModels({ hasSavedKey: true, selected: checkedModels() });
          return;
        }
        if (state.flow.status === "error") throw new Error(state.flow.message || "Connexion annulée.");
      }
      throw new Error("La connexion a expiré. Réessayez.");
    } catch (error) {
      popup?.close();
      accountStatus.textContent = error.message;
    } finally {
      connectButton.disabled = false;
      addAccountButton.disabled = false;
    }
  };
  connectButton.addEventListener("click", () => startChatgptFlow());
  addAccountButton.addEventListener("click", () => startChatgptFlow(true));
  accountSelect.addEventListener("change", async () => {
    if (!chatgptAccounts.find((account) => account.client_id === accountSelect.value)?.connected) {
      accountStatus.textContent = "Ce compte est déconnecté. Utilisez « Reconnecter ce compte ».";
      return;
    }
    try {
      const response = await fetch("/api/settings/provider/chatgpt/select", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ client_id: accountSelect.value }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Sélection impossible.");
      currentConfig = { ...currentConfig, has_api_key: true, auth_method: "account" };
      renderProviderBrand("openai", currentConfig);
      setKeyStatus(true);
      await loadProviderModels({ hasSavedKey: true, selected: checkedModels() });
    } catch (error) { accountStatus.textContent = error.message; }
  });
  disconnectButton.addEventListener("click", async () => {
    disconnectButton.disabled = true;
    accountStatus.textContent = "Déconnexion…";
    try {
      const response = await fetch("/api/settings/provider/chatgpt/disconnect", { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Déconnexion impossible.");
      await loadChatgptStatus();
      accountStatus.textContent = result.revoked ? "Compte déconnecté." : "Compte déconnecté localement. Vérifiez l'accès dans les réglages ChatGPT.";
      providerSelect.dispatchEvent(new Event("change"));
    } catch (error) { accountStatus.textContent = error.message; }
    finally { disconnectButton.disabled = false; }
  });
  providerSelect.addEventListener("change", async () => {
    applyProviderPreset(providerSelect.value);
    document.getElementById("api-provider-key").value = "";
    setClearKeyState(false);
    try {
      const response = await fetch(`/api/settings/provider?provider=${encodeURIComponent(providerSelect.value)}`);
      if (!response.ok) throw new Error("Impossible de charger les réglages de ce fournisseur.");
      const config = await response.json();
      currentConfig = { ...currentConfig, ...config };
      chatgptAuthMethod = providerSelect.value === "openai" ? (config.auth_method || "api_key") : "account";
      applyProviderPreset(providerSelect.value, true);
      savedModelSelection = config.model || presets[providerSelect.value]?.model || "";
      renderProviderList(providerSelect.value);
      renderProviderBrand(providerSelect.value, config);
      document.getElementById("api-provider-url").value = config.base_url || presets[providerSelect.value]?.base_url || "";
      modelsContainer.replaceChildren();
      modelSearch.hidden = true;
      modelSearch.value = "";
      setKeyStatus(config.has_api_key);
      if (providerSelect.value === "openai") await loadChatgptStatus();
      await loadProviderModels({ hasSavedKey: config.has_api_key, selected: config.models || (config.model ? [config.model] : []) });
    } catch (error) {
      setSettingsMessage(error.message, "error");
    }
  });
  document.getElementById("api-provider-add")?.addEventListener("click", () => {
    providerSelect.value = "custom";
    providerSelect.dispatchEvent(new Event("change"));
  });
  clearKeyButton?.addEventListener("click", async () => {
    const provider = providerSelect.value;
    if (!currentConfig.has_api_key || !window.confirm(`Supprimer immédiatement la clé API enregistrée pour ${providerLabels[provider] || provider} ?`)) return;
    clearKeyButton.disabled = true;
    setSettingsMessage("Suppression de la clé…", "info");
    try {
      const response = await fetch("/api/settings/provider", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          enabled: false,
          base_url: document.getElementById("api-provider-url").value || currentConfig.base_url || presets[provider]?.base_url,
          model: checkedModels()[0] || savedModelSelection || currentConfig.model || presets[provider]?.model,
          api_key: "",
          clear_api_key: true,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Impossible de supprimer la clé enregistrée.");
      currentConfig = result;
      document.getElementById("api-provider-key").value = "";
      setClearKeyState(false);
      setKeyStatus(false);
      renderProviderBrand(provider, result);
      renderProviderList(provider);
      updatePrivacyHint(result);
      setSettingsMessage("Clé API supprimée du coffre-fort.", "success");
      loadServerStatus();
    } catch (error) {
      setSettingsMessage(error.message || "Impossible de supprimer la clé enregistrée.", "error");
    } finally {
      clearKeyButton.disabled = false;
    }
  });
  fetch("/api/settings/provider").then((response) => response.ok ? response.json() : null).then(updatePrivacyHint).catch(() => {});
  const loadProviderSettings = async (inWizard = false) => {
    setSettingsMessage("");
    try {
      const response = await fetch("/api/settings/provider");
      if (!response.ok) throw new Error("Impossible de charger la configuration.");
      const config = await response.json();
      currentConfig = config;
      presets = config.providers || {};
      providerSelect.value = config.provider || "openai-compatible";
      chatgptAuthMethod = providerSelect.value === "openai" ? (config.auth_method || "api_key") : "account";
      if (!providerSelect.value) providerSelect.value = "custom";
      savedModelSelection = config.model || presets[providerSelect.value]?.model || "";
      renderProviderBrand(providerSelect.value, config);
      applyProviderPreset(providerSelect.value, true);
      document.getElementById("api-provider-url").value = config.base_url || presets[providerSelect.value]?.base_url || "https://api.openai.com/v1";
      modelsContainer.replaceChildren();
      modelSearch.hidden = true;
      modelSearch.value = "";
      document.getElementById("api-provider-key").value = "";
      setClearKeyState(false);
      setKeyStatus(config.has_api_key);
      if (providerSelect.value === "openai") await loadChatgptStatus();
      renderProviderList(providerSelect.value);
      if (!inWizard) showSettingsView("providers");
      else providerSelect.focus();
      await loadProviderModels({ hasSavedKey: config.has_api_key, selected: config.models || (config.model ? [config.model] : []) });
    } catch (error) { setSettingsMessage(error.message, "error"); }
  };
  open.addEventListener("click", () => loadProviderSettings());
  window.openWizardProviderSettings = () => loadProviderSettings(true);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setSettingsMessage("Enregistrement…", "info");
    const payload = {
      enabled: true,
      base_url: document.getElementById("api-provider-url").value,
      model: checkedModels()[0] || savedModelSelection,
      models: checkedModels(),
      api_key: document.getElementById("api-provider-key").value,
      clear_api_key: Boolean(clearKeyInput?.checked),
      provider: providerSelect.value,
      auth_method: providerSelect.value === "openai" ? chatgptAuthMethod : undefined,
    };
    try {
      const response = await fetch("/api/settings/provider", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Enregistrement impossible.");
      document.getElementById("api-provider-key").value = "";
      currentConfig = { ...currentConfig, ...result, configured: { ...currentConfig.configured, [providerSelect.value]: { has_api_key: result.has_api_key } } };
      savedModelSelection = result.model || payload.model;
      renderProviderList(providerSelect.value);
      setKeyStatus(result.has_api_key);
      renderProviderBrand(providerSelect.value, result);
      updatePrivacyHint(result);
      setSettingsMessage("Fournisseur activé et configuration enregistrée.", "success");
      loadServerStatus();
    } catch (error) { setSettingsMessage(error.message, "error"); }
  });
  document.getElementById("api-provider-key").addEventListener("input", () => {
    const hasTypedKey = Boolean(document.getElementById("api-provider-key").value.trim());
    modelHint.textContent = hasTypedKey
      ? "Clé saisie. Utilisez ↻ pour charger les modèles."
      : "Utilisez ↻ pour recharger les modèles du fournisseur.";
  });
  document.getElementById("chatgpt-auth-method").addEventListener("change", async (event) => {
    chatgptAuthMethod = event.target.value;
    applyProviderPreset("openai", true);
    renderProviderBrand("openai", { ...currentConfig, auth_method: chatgptAuthMethod });
    document.getElementById("api-provider-key").value = "";
    setClearKeyState(false);
    const accountState = chatgptAuthMethod === "account" ? await loadChatgptStatus().catch(() => null) : null;
    const savedCredential = chatgptAuthMethod === "account" ? Boolean(accountState?.active) : Boolean(currentConfig.has_openai_api_key);
    currentConfig.has_api_key = savedCredential;
    setKeyStatus(savedCredential);
    if (chatgptAuthMethod === "api_key") {
      modelHint.textContent = "Saisissez votre clé API OpenAI puis actualisez la liste des modèles.";
      modelsContainer.replaceChildren(renderModelEmptyState("Clé API OpenAI", "Ajoutez une clé API pour charger les modèles."));
    } else {
      await loadProviderModels({ hasSavedKey: currentConfig.has_api_key, selected: checkedModels() });
    }
  });
}

/* -------------------------------------------------------------------------- */
/* -------------------------------------------------------------------------- */
/* Setup Wizard Logic - Thinking Orbs, Particules & Ondulations Réactives     */
/* -------------------------------------------------------------------------- */
let wizardParticles = null;

class WizardParticlesFX {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext("2d");
    this.particles = [];
    this.ripples = [];
    this.mouse = { x: -1000, y: -1000, isHovering: false };
    this.activeTheme = "cyan";

    // Palettes RGB [R, G, B]
    this.themes = {
      cyan: {
        p1: [0, 242, 254],
        p2: [56, 189, 248],
        p3: [139, 92, 246],
        cssHue1: "#00f2fe",
        cssHue2: "#38bdf8",
        cssGlow: "rgba(0, 242, 254, 0.4)"
      },
      purple: {
        p1: [168, 85, 247],
        p2: [236, 72, 153],
        p3: [99, 102, 241],
        cssHue1: "#a855f7",
        cssHue2: "#ec4899",
        cssGlow: "rgba(168, 85, 247, 0.4)"
      },
      emerald: {
        p1: [16, 185, 129],
        p2: [52, 211, 153],
        p3: [6, 182, 212],
        cssHue1: "#10b981",
        cssHue2: "#34d399",
        cssGlow: "rgba(16, 185, 129, 0.4)"
      },
      amber: {
        p1: [245, 158, 11],
        p2: [251, 191, 36],
        p3: [217, 119, 6],
        cssHue1: "#f59e0b",
        cssHue2: "#fbbf24",
        cssGlow: "rgba(245, 158, 11, 0.4)"
      }
    };

    this.currentColors = {
      p1: [...this.themes.cyan.p1],
      p2: [...this.themes.cyan.p2],
      p3: [...this.themes.cyan.p3],
    };

    this.init();
  }

  init() {
    this.resize();
    window.addEventListener("resize", () => this.resize());

    // Écouteur de mouvement souris
    window.addEventListener("mousemove", (e) => {
      this.mouse.x = e.clientX;
      this.mouse.y = e.clientY;
      this.mouse.isHovering = true;
    });

    window.addEventListener("mouseleave", () => {
      this.mouse.isHovering = false;
      this.mouse.x = -1000;
      this.mouse.y = -1000;
    });

    // Écouteur de clics pour créer une onde de choc (Ripple)
    window.addEventListener("pointerdown", (e) => {
      // Déclencher une ondulation réactive à l'emplacement exact du clic
      this.addRipple(e.clientX, e.clientY, 1.2);
    });

    this.spawnParticles();
    this.animate();
  }

  resize() {
    if (!this.canvas) return;
    this.canvas.width = window.innerWidth;
    this.canvas.height = window.innerHeight;
  }

  spawnParticles() {
    this.particles = [];
    const count = Math.min(80, Math.floor((window.innerWidth * window.innerHeight) / 16000));
    for (let i = 0; i < count; i++) {
      const x = Math.random() * this.canvas.width;
      const y = Math.random() * this.canvas.height;
      this.particles.push({
        x: x,
        y: y,
        baseX: x,
        baseY: y,
        vx: (Math.random() - 0.5) * 0.7,
        vy: (Math.random() - 0.5) * 0.7,
        size: 1.5 + Math.random() * 2.2,
        phase: Math.random() * Math.PI * 2,
        colorIndex: Math.floor(Math.random() * 3),
      });
    }
  }

  addRipple(x, y, intensity = 1.0) {
    this.ripples.push({
      x: x,
      y: y,
      radius: 5,
      maxRadius: Math.max(window.innerWidth, window.innerHeight) * 0.65,
      speed: 8.5,
      waveWidth: 70,
      intensity: intensity,
      opacity: 0.9,
    });
  }

  setTheme(name) {
    if (!this.themes[name] || this.activeTheme === name) return;
    this.activeTheme = name;
    const target = this.themes[name];

    // Mettre à jour les variables CSS pour les Thinking Orbs
    document.documentElement.style.setProperty("--orb-hue-1", target.cssHue1);
    document.documentElement.style.setProperty("--orb-hue-2", target.cssHue2);
    document.documentElement.style.setProperty("--orb-glow-color", target.cssGlow);
  }

  animate() {
    if (!this.ctx || !this.canvas) return;
    if (document.hidden || document.getElementById("wizard-container")?.style.display === "none" ||
        window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setTimeout(() => this.animate(), 1000);
      return;
    }
    requestAnimationFrame(() => this.animate());

    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

    // Interpolation fluide des couleurs
    const target = this.themes[this.activeTheme];
    const lerpSpeed = 0.05;
    for (let k of ["p1", "p2", "p3"]) {
      for (let i = 0; i < 3; i++) {
        this.currentColors[k][i] += (target[k][i] - this.currentColors[k][i]) * lerpSpeed;
      }
    }

    const c1 = `rgb(${Math.round(this.currentColors.p1[0])}, ${Math.round(this.currentColors.p1[1])}, ${Math.round(this.currentColors.p1[2])})`;
    const c2 = `rgb(${Math.round(this.currentColors.p2[0])}, ${Math.round(this.currentColors.p2[1])}, ${Math.round(this.currentColors.p2[2])})`;
    const c3 = `rgb(${Math.round(this.currentColors.p3[0])}, ${Math.round(this.currentColors.p3[1])}, ${Math.round(this.currentColors.p3[2])})`;
    const colors = [c1, c2, c3];

    // Mise à jour des ondulations (ripples)
    for (let i = this.ripples.length - 1; i >= 0; i--) {
      const r = this.ripples[i];
      r.radius += r.speed;
      r.opacity *= 0.975;
      r.intensity *= 0.975;

      // Dessin de l'onde lumineuse
      this.ctx.beginPath();
      this.ctx.arc(r.x, r.y, r.radius, 0, Math.PI * 2);
      this.ctx.strokeStyle = `rgba(${Math.round(this.currentColors.p1[0])}, ${Math.round(this.currentColors.p1[1])}, ${Math.round(this.currentColors.p1[2])}, ${r.opacity * 0.4})`;
      this.ctx.lineWidth = 2;
      this.ctx.stroke();

      if (r.radius > r.maxRadius || r.opacity < 0.01) {
        this.ripples.splice(i, 1);
      }
    }

    // Mise à jour et dessin des particules
    const len = this.particles.length;
    for (let i = 0; i < len; i++) {
      const p = this.particles[i];

      // Mouvement naturel continu
      p.x += p.vx;
      p.y += p.vy;

      // Rebond doux sur les bords
      if (p.x < 0) { p.x = 0; p.vx *= -1; }
      if (p.x > this.canvas.width) { p.x = this.canvas.width; p.vx *= -1; }
      if (p.y < 0) { p.y = 0; p.vy *= -1; }
      if (p.y > this.canvas.height) { p.y = this.canvas.height; p.vy *= -1; }

      // Interaction avec la souris (répulsion élastique)
      if (this.mouse.isHovering) {
        const dx = p.x - this.mouse.x;
        const dy = p.y - this.mouse.y;
        const dist = Math.hypot(dx, dy);
        const maxDist = 130;
        if (dist < maxDist && dist > 0) {
          const force = (1 - dist / maxDist) * 3.5;
          p.x += (dx / dist) * force;
          p.y += (dy / dist) * force;
        }
      }

      // Interaction avec les ondes (ondulations transversales)
      for (const r of this.ripples) {
        const dx = p.x - r.x;
        const dy = p.y - r.y;
        const dist = Math.hypot(dx, dy);
        const diff = dist - r.radius;
        if (Math.abs(diff) < r.waveWidth && dist > 0) {
          const waveFactor = Math.sin((diff / r.waveWidth) * Math.PI) * r.intensity * 5;
          p.x += (dx / dist) * waveFactor;
          p.y += (dy / dist) * waveFactor;
        }
      }

      // Rendu de la particule
      this.ctx.beginPath();
      this.ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      this.ctx.fillStyle = colors[p.colorIndex];
      this.ctx.shadowBlur = 8;
      this.ctx.shadowColor = colors[p.colorIndex];
      this.ctx.fill();

      // Tracé des connexions entre particules proches
      for (let j = i + 1; j < len; j++) {
        const p2 = this.particles[j];
        const dist = Math.hypot(p.x - p2.x, p.y - p2.y);
        const maxConnect = 115;
        if (dist < maxConnect) {
          const alpha = (1 - dist / maxConnect) * 0.28;
          this.ctx.beginPath();
          this.ctx.moveTo(p.x, p.y);
          this.ctx.lineTo(p2.x, p2.y);
          this.ctx.strokeStyle = `rgba(${Math.round(this.currentColors.p2[0])}, ${Math.round(this.currentColors.p2[1])}, ${Math.round(this.currentColors.p2[2])}, ${alpha})`;
          this.ctx.lineWidth = 1;
          this.ctx.stroke();
        }
      }
    }
    this.ctx.shadowBlur = 0;
  }
}

async function initSetupWizard() {
  const wizardContainer = document.getElementById("wizard-container");
  const appContainer = document.getElementById("app-container");

  // Initialisation du canvas de particules et d'ondulations
  // Les effets de particules étaient coûteux sur les machines visées par l'IA locale.
  // L'orbe CSS conserve un retour visuel sans boucle de rendu permanente.

  // Boutons de navigation du Wizard
  document.getElementById("btn-wizard-to-step2").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    showWizardStep(2);
    try {
      await loadWizardModelSelection();
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
    if (wizardParticles) {
      wizardParticles.setTheme("cyan");
      wizardParticles.addRipple(window.innerWidth / 2, window.innerHeight / 2, 1.5);
    }
  });

  document.getElementById("btn-wizard-back-step1").addEventListener("click", () => {
    showWizardStep(1);
    if (wizardParticles) {
      wizardParticles.setTheme("emerald");
    }
  });

  document.getElementById("btn-wizard-start-install").addEventListener("click", () => {
    startWizardInstallation();
  });

  document.getElementById("btn-wizard-open-during-install").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await fetch("/api/setup/defer", { method: "POST" });
      if (!response.ok) throw new Error("Impossible d'ouvrir ETU-AI pendant la préparation.");
      openMainApp();
    } catch (error) {
      button.disabled = false;
      showToast(error.message || "Impossible d'ouvrir ETU-AI.", "error");
    }
  });

  document.getElementById("btn-wizard-cancel-install").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Annulation…";
    try {
      const response = await fetch("/api/setup/cancel", { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Impossible d'annuler l'installation.");
      button.textContent = "Annulation en cours…";
    } catch (error) {
      button.disabled = false;
      button.textContent = "Annuler l'installation";
      showToast(error.message || "Impossible d'annuler l'installation.", "error");
    }
  });

  const deferSetup = async () => {
    try {
      const response = await fetch("/api/setup/defer", { method: "POST" });
      if (!response.ok) throw new Error("Impossible d'enregistrer ce choix.");
      openMainApp();
    } catch (error) {
      showToast(error.message || "Impossible de reporter la configuration.", "error");
    }
  };
  document.getElementById("btn-wizard-skip-step1").addEventListener("click", deferSetup);
  document.getElementById("btn-wizard-skip-step2").addEventListener("click", deferSetup);

  let wizardProviderReturnStep = 1;
  const providerMount = document.getElementById("settings-provider-mount");
  const wizardProviderMount = document.getElementById("wizard-provider-mount");
  const moveProviderPanel = (target) => {
    const providerLayout = providerMount?.querySelector(":scope > .settings-provider-layout")
      || wizardProviderMount?.querySelector(":scope > .settings-provider-layout");
    if (providerLayout && target) target.append(providerLayout);
  };
  const openProviderSetup = () => {
    const activeStep = document.querySelector(".wizard-step.active")?.id.match(/wizard-step-(\d+)/)?.[1];
    wizardProviderReturnStep = Number(activeStep) || 1;
    moveProviderPanel(wizardProviderMount);
    showWizardStep(4);
    window.openWizardProviderSettings?.();
  };
  document.getElementById("btn-wizard-provider-step1").addEventListener("click", openProviderSetup);
  document.getElementById("btn-wizard-provider-step2").addEventListener("click", openProviderSetup);
  document.getElementById("btn-wizard-provider-back").addEventListener("click", () => {
    moveProviderPanel(providerMount);
    showWizardStep(wizardProviderReturnStep);
  });
  document.getElementById("btn-wizard-provider-open-app").addEventListener("click", async () => {
    try {
      const response = await fetch("/api/setup/defer", { method: "POST" });
      if (!response.ok) throw new Error("Impossible d'enregistrer l'état de l'installation.");
      moveProviderPanel(providerMount);
      openMainApp();
    } catch (error) {
      showToast(error.message || "Impossible d'ouvrir ETU-AI.", "error");
    }
  });

  document.getElementById("btn-wizard-finish").addEventListener("click", () => {
    openMainApp();
  });

  // Micro-interactions sur les éléments de fonctionnalités
  document.querySelectorAll(".feat-box").forEach((box) => {
    box.addEventListener("mouseenter", () => {
      const mood = box.dataset.mood || "cyan";
      if (wizardParticles) wizardParticles.setTheme(mood);
    });
  });

  try {
    const resp = await fetch("/api/setup/status");
    const statusData = await resp.json();

    // =========================================================================
    // RÈGLE FONDAMENTALE : LE SETUP NE SE FAIT PAS À CHAQUE DÉMARRAGE !
    // Si setup_completed est vrai, on ouvre directement l'application de chat !
    // =========================================================================
    if (statusData.completed || statusData.deferred) {
      openMainApp();
      return;
    }

    if (statusData.status === "running") {
      // Installation en cours en arrière-plan -> Affiche l'étape 3
      wizardContainer.style.display = "flex";
      appContainer.style.display = "none";
      showWizardStep(3);
      startPollingSetupProgress();
      if (wizardParticles) wizardParticles.setTheme("purple");
      return;
    }

    // Premier démarrage absolu -> Étape 1
    wizardContainer.style.display = "flex";
    appContainer.style.display = "none";
    showWizardStep(1);
    loadHardwareDiagnosis();
  } catch (err) {
    console.warn("Impossible de vérifier l'état du setup :", err);
    openMainApp();
  }
}

function openMainApp() {
  document.getElementById("wizard-container").style.display = "none";
  document.getElementById("app-container").style.display = "flex";
  loadServerStatus();
  loadConversations();
}

function showWizardStep(stepNum) {
  document.querySelectorAll(".wizard-step").forEach((el) => el.classList.remove("active"));
  const target = document.getElementById(`wizard-step-${stepNum}`);
  if (target) target.classList.add("active");
  if (target) {
    const content = target.querySelector(".wizard-content");
    if (content) content.scrollTop = 0;
    const heading = target.querySelector("h3");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  }
  const label = document.getElementById("wizard-step-label");
  if (label) label.textContent = { 1: "Votre ordinateur", 2: "Modèles", 3: "Préparation", 4: "Fournisseur IA" }[stepNum] || "Installation";
  document.getElementById("wizard-container").scrollTop = 0;
}

function updateWizardNpuNotice(npus = []) {
  const notices = [
    { banner: document.getElementById("wizard-npu-hardware-notice"), title: document.getElementById("npu-hardware-title"), detail: document.getElementById("npu-hardware-text") },
    { banner: document.getElementById("wizard-npu-banner"), title: document.getElementById("npu-banner-title"), detail: document.getElementById("npu-banner-text") },
  ].filter((notice) => notice.banner);
  if (!notices.length) return;
  const detected = Array.isArray(npus) ? npus : [];
  const intelNpu = detected.some((item) => /intel/i.test(`${item.name || ""} ${item.manufacturer || ""}`));
  if (!detected.length) {
    notices.forEach(({ banner }) => { banner.hidden = true; });
    return;
  }
  const titleText = `Puce NPU détectée : ${detected[0].name || "NPU"}`;
  const detailText = intelNpu
      ? "OpenVINO peut exploiter ce NPU avec certains modèles GGUF texte. Le contexte initial est limité à 1 024 tokens et l’accélération sera contrôlée au démarrage. Choisissez une version OpenVINO compatible dans le menu."
      : "Cette puce NPU est détectée, mais le moteur OpenVINO proposé par ETU-AI cible les NPU Intel. L’installation ne pourra pas confirmer son accélération.";
  notices.forEach(({ banner, title, detail }) => {
    if (title) title.textContent = titleText;
    if (detail) detail.textContent = detailText;
    banner.hidden = false;
  });
}

async function loadHardwareDiagnosis() {
  const continueButton = document.getElementById("btn-wizard-to-step2");
  continueButton.disabled = true;
  continueButton.setAttribute("aria-busy", "true");
  try {
    const resp = await fetch("/api/status");
    if (!resp.ok) throw new Error("Diagnostic indisponible");
    const data = await resp.json();

    const hwCpu = document.getElementById("hw-cpu");
    const hwRam = document.getElementById("hw-ram");
    const hwGpu = document.getElementById("hw-gpu");
    const hwNpu = document.getElementById("hw-npu");
    const hwBadge = document.getElementById("hw-backend-badge");
    const moodText = document.getElementById("wizard-mood-text");

    const cpuName = data.cpu_processor || `Processeur (${data.cpu_cores || 'x'} cœurs)`;
    if (hwCpu) hwCpu.textContent = cpuName;

    if (hwRam) {
      if (data.ram_total_gib) {
        const freeStr = data.ram_available_gib ? ` (${data.ram_available_gib} Go dispo)` : "";
        hwRam.textContent = `${data.ram_total_gib} Go RAM${freeStr}`;
        hwRam.style.color = data.ram_total_gib >= 8 ? "var(--success)" : "var(--warning)";
      } else {
        hwRam.textContent = "8.0 Go RAM détectés";
      }
    }

    if (hwGpu) {
      if (data.gpus && data.gpus.length > 0) {
        const gpuNames = data.gpus.map((g) => g.name || g.type.toUpperCase()).join(", ");
        hwGpu.textContent = gpuNames;
        hwGpu.style.color = "var(--success)";
      } else {
        hwGpu.textContent = "Aucun GPU dédié détecté (Mode CPU)";
        hwGpu.style.color = "var(--text-muted)";
      }
    }

    if (hwNpu) {
      if (data.npus && data.npus.length > 0) {
        const npuNames = data.npus.map((n) => n.name).join(", ");
        hwNpu.textContent = npuNames;
        hwNpu.style.color = "var(--accent-color)";
        if (moodText) moodText.textContent = data.installed_backend === "npu"
          ? "NPU Intel détecté ; moteur OpenVINO installé"
          : "Puce NPU détectée ; backend à confirmer";
        if (wizardParticles) wizardParticles.setTheme("cyan");
      } else {
        hwNpu.textContent = "Aucun NPU détecté";
        hwNpu.style.color = "var(--text-muted)";
        if (moodText) moodText.textContent = "Diagnostic matériel complété";
        if (wizardParticles) wizardParticles.setTheme("emerald");
      }
    }
    updateWizardNpuNotice(data.npus || []);

    const recommendedBackend = String(data.backend || "cpu").toLowerCase();
    wizardRecommendedBackend = recommendedBackend.toUpperCase();
    const backend = data.installed_backend === "npu"
      ? "NPU OpenVINO"
      : recommendedBackend.toUpperCase();
    if (hwBadge) {
      const isCpu = backend === "CPU";
      hwBadge.textContent = isCpu ? "Mode CPU" : backend === "NPU OpenVINO" ? backend : `GPU ${backend}`;
      hwBadge.className = `etu-badge ${isCpu ? "etu-badge-warning" : "etu-badge-success"}`;
    }
  } catch (err) {
    console.error("Erreur diagnostic matériel :", err);
    const badge = document.getElementById("hw-backend-badge");
    if (badge) badge.textContent = "À vérifier";
    ["hw-cpu", "hw-ram", "hw-gpu", "hw-npu"].forEach((id) => {
      const value = document.getElementById(id);
      if (value) value.textContent = "Indisponible";
    });
    updateWizardNpuNotice([]);
    const backendHint = document.getElementById("wizard-backend-hint");
    if (backendHint) backendHint.textContent = "La détection du matériel a échoué. Vous pouvez réessayer plus tard dans les paramètres.";
    wizardRecommendedBackend = "CPU";
  } finally {
    await loadWizardServerVariants();
    continueButton.disabled = false;
    continueButton.removeAttribute("aria-busy");
  }
}

let wizardSearchDebounceTimer = null;
let wizardHfSearchBound = false;
let allLoadedWizardModels = [];
let activeWizardCategory = "all";
const wizardSelectedModels = new Map();
let wizardSelectionInitialized = false;
let wizardModelRequestId = 0;
let wizardModelController = null;

async function loadWizardModelSelection(searchQuery = "") {
  const requestId = ++wizardModelRequestId;
  wizardModelController?.abort();
  const controller = new AbortController();
  wizardModelController = controller;
  const container = document.getElementById("wizard-model-selection-list");
  const counterEl = document.getElementById("selected-models-count");
  const moodText = document.getElementById("wizard-mood-text");
  const filterCompatEl = document.getElementById("wizard-filter-compatible");
  const onlyCompatible = filterCompatEl ? filterCompatEl.checked : true;

  // Lier les écouteurs de la barre d'outils HF et des catégories une seule fois
  if (!wizardHfSearchBound) {
    wizardHfSearchBound = true;
    const searchInput = document.getElementById("wizard-hf-search-input");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        clearTimeout(wizardSearchDebounceTimer);
        wizardSearchDebounceTimer = setTimeout(() => {
          loadWizardModelSelection(e.target.value.trim());
        }, 350);
      });
    }

    if (filterCompatEl) {
      filterCompatEl.addEventListener("change", () => {
        const query = searchInput ? searchInput.value.trim() : "";
        loadWizardModelSelection(query);
      });
    }

    // Gestion des clics sur les catégories
    document.querySelectorAll(".category-pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        document.querySelectorAll(".category-pill").forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
        activeWizardCategory = pill.dataset.cat || "all";
        renderWizardModelCards(allLoadedWizardModels, activeWizardCategory);
      });
    });

    const btnAutoSelect = document.getElementById("btn-wizard-autoselect");
    if (btnAutoSelect) {
      btnAutoSelect.addEventListener("click", async () => {
        try {
          btnAutoSelect.disabled = true;
          btnAutoSelect.textContent = "Recherche...";
          const resp = await fetch("/api/models/auto-select?count=1");
          const data = await resp.json();
          const recs = data.recommended_models || [];
          if (recs.length > 0) {
            wizardSelectedModels.clear();
            const chosen = allLoadedWizardModels.find((m) => m.filename === recs[0].filename) || recs[0];
            wizardSelectedModels.set(chosen.filename, { repo: chosen.repo, filename: chosen.filename, npu_compatible: isModelNpuCandidate(chosen) });
            renderWizardModelCards(allLoadedWizardModels, activeWizardCategory);
            const primaryName = data.primary_model?.name || recs[0].name;
            showToast(`IA optimale sélectionnée pour ce PC : <strong>${escapeHtml(primaryName)}</strong>`, "success", 4000);
            if (moodText) moodText.textContent = `Recommandation automatique : ${primaryName}`;
          }
        } catch (e) {
          showToast(`Erreur auto-sélection : ${e}`, "warning");
        } finally {
          btnAutoSelect.disabled = false;
          btnAutoSelect.textContent = "Choisir pour moi";
        }
      });
    }
  }

  container.setAttribute("aria-busy", "true");
  container.innerHTML = '<div class="hf-loading" role="status"><span class="etu-spinner" aria-hidden="true"></span><span>Chargement des modèles…</span></div>';
  document.getElementById("btn-wizard-start-install").disabled = true;

  try {
    let modelsList = [];
    let hardwareData = {};

    if (searchQuery) {
      // Recherche en direct via l'API Hugging Face sans credential
      const resp = await fetch(
        `/api/models/huggingface/search?q=${encodeURIComponent(searchQuery)}&only_compatible=${onlyCompatible}&limit=60`,
        { signal: controller.signal }
      );
      if (!resp.ok) throw new Error("La recherche est indisponible. Réessayez dans un instant.");
      const data = await resp.json();
      modelsList = data.models || [];
      hardwareData = data.hardware || {};
    } else {
      // Catalogue global enrichi de toutes les IA compatibles
      const resp = await fetch("/api/models/catalog", { signal: controller.signal });
      if (!resp.ok) throw new Error("Le catalogue est indisponible. Réessayez dans un instant.");
      const data = await resp.json();
      modelsList = data.catalog || [];
      hardwareData = data.hardware || {};
    }

    if (requestId !== wizardModelRequestId) return;
    allLoadedWizardModels = modelsList;

    updateWizardNpuNotice(hardwareData?.npus || []);

    // Mise à jour des compteurs de catégories
    updateCategoryCounts(modelsList);

    // Rendu des cartes selon la catégorie active
    renderWizardModelCards(modelsList, activeWizardCategory);

  } catch (err) {
    if (requestId !== wizardModelRequestId || err.name === "AbortError") return;
    container.innerHTML = `<div class="wizard-catalog-error" role="status"><p class="etu-text">${escapeHtml(err.message || "Le catalogue n’a pas pu être chargé.")}</p></div>`;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "etu-btn etu-btn-secondary";
    retry.textContent = "Réessayer";
    retry.addEventListener("click", () => loadWizardModelSelection(searchQuery));
    container.querySelector(".wizard-catalog-error").append(retry);
  } finally {
    if (requestId === wizardModelRequestId) {
      container.removeAttribute("aria-busy");
      document.getElementById("btn-wizard-start-install").disabled = wizardSelectedModels.size === 0;
    }
  }
}

function updateCategoryCounts(models) {
  const counts = {
    all: models.length,
    npu: 0,
    code: 0,
    reasoning: 0,
    "7b": 0,
    installed: 0,
  };

  models.forEach((m) => {
    const text = `${m.name} ${m.repo} ${m.description} ${(m.tags || []).join(" ")}`.toLowerCase();
    const isNpu = isModelNpuCandidate(m);
    const isCode = text.includes("code") || text.includes("coder") || text.includes("starcoder") || text.includes("sql");
    const isReasoning = text.includes("reasoning") || text.includes("deepseek") || text.includes("phi") || text.includes("math") || text.includes("hermes");
    const is7b = text.includes("7b") || text.includes("8b") || (m.size_gib && m.size_gib >= 4.0);

    if (isNpu) counts.npu++;
    if (isCode) counts.code++;
    if (isReasoning) counts.reasoning++;
    if (is7b) counts["7b"]++;
    if (m.is_installed) counts.installed++;
  });

  for (const [key, val] of Object.entries(counts)) {
    const el = document.getElementById(`cat-count-${key}`);
    if (el) el.textContent = val;
  }
}

function isModelNpuCandidate(model) {
  return Boolean(model?.compatibility?.npu_candidate || model?.npu_candidate || model?.npu_compatible);
}

function renderWizardModelCards(models, categoryFilter = "all") {
  const container = document.getElementById("wizard-model-selection-list");
  const counterEl = document.getElementById("selected-models-count");
  const moodText = document.getElementById("wizard-mood-text");
  if (!container) return;

  container.innerHTML = "";
  if (!wizardSelectionInitialized && models.length) {
    const initial = models.find((m) => m.is_installed) || models[0];
    wizardSelectedModels.set(initial.filename, { repo: initial.repo, filename: initial.filename, npu_compatible: isModelNpuCandidate(initial) });
    wizardSelectionInitialized = true;
  }

  const filtered = models.filter((m) => {
    if (categoryFilter === "all") return true;
    if (categoryFilter === "installed") return m.is_installed;
    const text = `${m.name} ${m.repo} ${m.description} ${(m.tags || []).join(" ")}`.toLowerCase();
    if (categoryFilter === "npu") return isModelNpuCandidate(m);
    if (categoryFilter === "code") return text.includes("code") || text.includes("coder") || text.includes("starcoder") || text.includes("sql");
    if (categoryFilter === "reasoning") return text.includes("reasoning") || text.includes("deepseek") || text.includes("phi") || text.includes("math") || text.includes("hermes");
    if (categoryFilter === "7b") return text.includes("7b") || text.includes("8b") || (m.size_gib && m.size_gib >= 4.0);
    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div style="grid-column: 1 / -1; padding: 24px; text-align: center; color: var(--text-muted);">
        <p>Aucun modèle trouvé dans cette catégorie.</p>
      </div>
    `;
    return;
  }

  const updateCounter = () => {
    const selectedCount = wizardSelectedModels.size;
    if (counterEl) counterEl.textContent = selectedCount;
    document.getElementById("btn-wizard-start-install").disabled = selectedCount === 0 || container.getAttribute("aria-busy") === "true";
    if (moodText) {
      moodText.textContent = `${selectedCount} modèle(s) sélectionné(s) pour votre machine`;
    }
  };

  filtered.forEach((item, index) => {
    const npuCandidate = isModelNpuCandidate(item);
    const { card, select, actions, installed, quants } = createHfModelCard(item, { index: `wizard-${index}` });
    if ([...select.options].some((option) => option.value === item.filename)) select.value = item.filename;
    const selectedVariant = quants.find((quant) => wizardSelectedModels.get(quant.filename)?.repo === item.repo);
    if (selectedVariant) select.value = selectedVariant.filename;
    const isSelected = Boolean(selectedVariant);
    card.classList.toggle("is-selected", isSelected);
    card.dataset.repo = item.repo;
    card.dataset.filename = item.filename;
    card.dataset.npu = npuCandidate ? "true" : "false";
    const quantification = card.querySelector(".hf-model-quantification label");
    if (quantification) quantification.textContent = "Fichier à installer";
    const checkboxId = `wizard-model-${index}`;
    const checkboxLabel = document.createElement("label");
    checkboxLabel.className = "hf-model-select-control";
    checkboxLabel.htmlFor = checkboxId;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = checkboxId;
    checkbox.checked = isSelected;
    checkbox.setAttribute("aria-label", `Sélectionner ${item.name}`);
    checkboxLabel.append(checkbox, document.createTextNode("Sélectionner"));
    actions.prepend(checkboxLabel);

    const setSelected = (selected) => {
      checkbox.checked = selected;
      card.classList.toggle("is-selected", selected);
      if (selected) {
        wizardSelectedModels.set(select.value, {
          repo: item.repo,
          filename: select.value,
          npu_compatible: npuCandidate,
          quant: item.quant || quants.find((quant) => quant.filename === select.value)?.quant,
        });
      } else {
        for (const [filename, model] of wizardSelectedModels) {
          if (model.repo === item.repo && (model.filename === item.filename || quants.some((quant) => quant.filename === filename))) {
            wizardSelectedModels.delete(filename);
          }
        }
      }
      updateCounter();
    };

    checkbox.addEventListener("change", () => setSelected(checkbox.checked));
    select.addEventListener("change", () => {
      const installedBadge = card.querySelector(".hf-installed-badge");
      if (installedBadge) installedBadge.hidden = !installed.has(select.value);
      if (!checkbox.checked) return;
      for (const [filename, model] of wizardSelectedModels) {
        if (model.repo === item.repo && (model.filename === item.filename || quants.some((quant) => quant.filename === filename))) {
          wizardSelectedModels.delete(filename);
        }
      }
      setSelected(true);
    });
    card.addEventListener("click", (event) => {
      if (event.target.closest("a, button, select, label, input")) return;
      setSelected(!checkbox.checked);
    });
    const installedBadge = card.querySelector(".hf-installed-badge");
    if (installedBadge && installed.size) installedBadge.hidden = false;

    container.appendChild(card);
  });

  updateCounter();
}

async function startWizardInstallation() {
  if (wizardSelectedModels.size === 0) {
    alert("Veuillez sélectionner au moins un modèle pour démarrer l'installation.");
    return;
  }

  const selectedModels = [...wizardSelectedModels.values()];

  const startButton = document.getElementById("btn-wizard-start-install");
  if (startButton.disabled) return;
  startButton.disabled = true;
  startButton.setAttribute("aria-busy", "true");
  showWizardStep(3);

  // Ambiance Thinking Orb Installation
  if (wizardParticles) {
    wizardParticles.setTheme("purple");
    wizardParticles.addRipple(window.innerWidth / 2, window.innerHeight / 2, 2.0);
  }

  try {
    const resp = await fetch("/api/setup/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        selected_models: selectedModels,
        selected_variant: getWizardBackendMenu()?.value === "auto" ? null : getWizardBackendMenu()?.value,
      }),
    });
    const data = await resp.json();
    if (data.started || data.status === "running") {
      startPollingSetupProgress();
    } else {
      throw new Error(data.detail || "L'installation n'a pas pu démarrer.");
    }
  } catch (err) {
    showWizardStep(2);
    startButton.disabled = false;
    startButton.removeAttribute("aria-busy");
    showToast("Impossible de démarrer l'installation : " + err.message, "error");
  }
}

function startPollingSetupProgress() {
  if (setupPollingInterval) clearInterval(setupPollingInterval);
  let pollingInFlight = false;

  setupPollingInterval = setInterval(async () => {
    if (pollingInFlight) return;
    pollingInFlight = true;
    try {
      const resp = await fetch("/api/setup/status");
      const data = await resp.json();

      const bar = document.getElementById("wizard-progress-bar");
      const percentEl = document.getElementById("wizard-progress-percent");
      const statusText = document.getElementById("wizard-progress-status");
      const progressTrack = document.querySelector("#wizard-step-3 .wizard-progress-track");
      const installVisual = document.querySelector("#wizard-step-3 .wizard-install-visual");
      const installSub = document.getElementById("install-subtitle");
      const moodText = document.getElementById("wizard-mood-text");

      const progress = Math.min(100, Math.max(0, Number(data.progress_percent) || 0));
      if (bar) bar.style.width = `${progress}%`;
      if (percentEl) percentEl.textContent = `${Math.round(progress)} %`;
      if (progressTrack) {
        progressTrack.setAttribute("aria-valuenow", String(Math.round(progress)));
        progressTrack.dataset.state = data.status;
      }
      if (installVisual) installVisual.dataset.state = data.status === "running" ? "running" : data.status;

      // Réactivité de la Thinking Orb selon l'étape
      if (data.current_step === 0 && wizardParticles) {
        wizardParticles.setTheme("cyan");
        if (installSub) installSub.textContent = "Compilation et calibrage du moteur d'inférence haute performance...";
      } else if (data.current_step === 1 && wizardParticles) {
        wizardParticles.setTheme("purple");
        if (installSub) installSub.textContent = "Configuration de l'environnement superviseur MAIA-Beacon...";
      } else if (data.current_step === 2 && wizardParticles) {
        wizardParticles.setTheme("purple");
        if (installSub) installSub.textContent = "Téléchargement & validation d'intégrité des poids de modèles...";
      } else if (data.current_step === 3 && wizardParticles) {
        wizardParticles.setTheme("emerald");
        if (installSub) installSub.textContent = "Vérification en direct du service local sur le port 11343...";
      }

      // Mise à jour du stepper réactif
      data.steps.forEach((step, idx) => {
        const item = document.getElementById(`step-item-${idx}`);
        if (!item) return;

        item.classList.remove("running", "completed", "cancelled", "error");
        item.removeAttribute("aria-current");
        const icon = item.querySelector(".step-icon");
        const stateLabel = item.querySelector(".step-state");
        if (stateLabel) stateLabel.textContent = { running: "En cours", completed: "Terminé", cancelled: "Annulé", error: "Erreur", pending: "À venir" }[step.status] || "À venir";

        if (step.status === "running") {
          item.classList.add("running");
          item.setAttribute("aria-current", "step");
          if (icon) icon.innerHTML = '<span class="etu-spinner" style="border-width:2px;width:12px;height:12px;"></span>';
          if (statusText) statusText.textContent = step.title;
          if (moodText) moodText.textContent = `En cours : ${step.title}`;
        } else if (step.status === "completed") {
          item.classList.add("completed");
          if (icon) icon.textContent = "✓";
        } else if (step.status === "cancelled") {
          item.classList.add("cancelled");
          if (icon) icon.textContent = "–";
        } else if (step.status === "error") {
          item.classList.add("error");
          if (icon) icon.textContent = "!";
        } else {
          if (icon) icon.textContent = `${idx + 1}`;
        }
      });

      // Streaming des logs
      const logsBox = document.getElementById("wizard-logs-box");
      if (logsBox && data.logs) {
        logsBox.innerHTML = data.logs.map((l) => `<div class="terminal-line">${escapeHtml(l)}</div>`).join("");
        logsBox.scrollTop = logsBox.scrollHeight;
      }

      // Si terminé avec succès
      if (data.status === "completed" || (data.completed && data.beacon_online)) {
        clearInterval(setupPollingInterval);
        document.getElementById("btn-wizard-open-during-install").style.display = "none";
        document.getElementById("btn-wizard-cancel-install").style.display = "none";

        const titleEl = document.getElementById("install-title");
        if (titleEl) {
          titleEl.textContent = "Installation terminée avec succès !";
          titleEl.style.color = "var(--success)";
        }
        if (statusText) {
          statusText.textContent = "Serveur MAIA-Beacon actif sur le port 11343 !";
          statusText.style.color = "var(--success)";
        }
        if (installSub) {
          installSub.textContent = "Votre assistant ETU-AI est configuré, optimisé et prêt à l'emploi.";
        }
        if (moodText) {
          moodText.textContent = "Prêt pour les études & la recherche !";
        }

        const finishBtn = document.getElementById("btn-wizard-finish");
        if (finishBtn) {
          finishBtn.style.display = "inline-flex";
          finishBtn.textContent = "Ouvrir ETU-AI";
        }
        window.setTimeout(openMainApp, 1200);
      } else if (data.status === "error") {
        clearInterval(setupPollingInterval);
        if (wizardParticles) wizardParticles.setTheme("amber");

        const titleEl = document.getElementById("install-title");
        if (titleEl) {
          titleEl.textContent = "Une erreur est survenue pendant l'installation";
          titleEl.style.color = "var(--danger)";
        }
        if (statusText) {
          statusText.textContent = data.error_message || "Erreur critique";
          statusText.style.color = "var(--danger)";
        }
        if (moodText) moodText.textContent = "Erreur d'installation";
        const finishBtn = document.getElementById("btn-wizard-finish");
        if (finishBtn) {
          finishBtn.textContent = "Continuer sans moteur local";
          finishBtn.style.display = "inline-flex";
        }
        const openBtn = document.getElementById("btn-wizard-open-during-install");
        const cancelBtn = document.getElementById("btn-wizard-cancel-install");
        if (openBtn) openBtn.style.display = "none";
        if (cancelBtn) cancelBtn.style.display = "none";
      } else if (data.status === "cancelled") {
        clearInterval(setupPollingInterval);
        if (installSub) installSub.textContent = "L’installation a été arrêtée. Vos modèles locaux restent disponibles.";
        if (statusText) statusText.textContent = "Installation annulée";
        if (moodText) moodText.textContent = "Configuration reportée";
        const titleEl = document.getElementById("install-title");
        if (titleEl) titleEl.textContent = "Installation arrêtée";
        const finishBtn = document.getElementById("btn-wizard-finish");
        if (finishBtn) {
          finishBtn.textContent = "Ouvrir ETU-AI";
          finishBtn.style.display = "inline-flex";
        }
        const openBtn = document.getElementById("btn-wizard-open-during-install");
        const cancelBtn = document.getElementById("btn-wizard-cancel-install");
        if (openBtn) openBtn.style.display = "none";
        if (cancelBtn) cancelBtn.style.display = "none";
      }
    } catch (err) {
      console.warn("Erreur de suivi setup :", err);
    } finally {
      pollingInFlight = false;
    }
  }, 800);
}

/* -------------------------------------------------------------------------- */
/* Modals Setup                                                               */
/* -------------------------------------------------------------------------- */
function initModals() {
  document.querySelectorAll(".modal-close").forEach((btn) => {
    btn.addEventListener("click", () => {
      closeModal(btn.dataset.target);
    });
  });

  document.querySelectorAll(".modal-overlay").forEach((modal) => {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeModal(modal.id);
    });
  });

  // Gestion des onglets de la modale Modèles
  document.querySelectorAll(".modal-tab-btn").forEach((tabBtn) => {
    tabBtn.addEventListener("click", () => {
      const targetTabId = tabBtn.dataset.tab;
      showModelsTab(targetTabId);
      if (targetTabId === "tab-vision") {
        const selected = document.getElementById("vision-model-select").value;
        const item = modelCatalogItems.find(model => model.filename === selected);
        if (item && document.getElementById("mmproj-panel").dataset.model !== selected) openProjectorManager(item);
      }
    });
    tabBtn.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll(".modal-tab-btn")];
      const next = tabs[(tabs.indexOf(tabBtn) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      next.click();
      next.focus();
    });
  });
  document.getElementById("vision-model-select").addEventListener("change", (event) => {
    const item = modelCatalogItems.find(model => model.filename === event.target.value);
    if (item) openProjectorManager(item);
  });

  // Recherche Hugging Face dans la modale
  const hfSearchInput = document.getElementById("modal-hf-search-input");
  const btnHfSearch = document.getElementById("btn-modal-hf-search");
  const filterHfCompat = document.getElementById("modal-hf-filter-compatible");
  const catalogSearchInput = document.getElementById("model-catalog-search");
  catalogSearchInput?.addEventListener("input", applyModelCatalogSearch);
  document.getElementById("model-catalog-sort")?.addEventListener("change", sortLocalModelCards);
  document.addEventListener("click", event => {
    document.querySelectorAll(".local-model-menu[open]").forEach(menu => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") document.querySelectorAll(".local-model-menu[open]").forEach(menu => {
      menu.open = false;
      menu.querySelector("summary").focus();
    });
  });

  if (btnHfSearch) {
    btnHfSearch.addEventListener("click", () => {
      const q = hfSearchInput ? hfSearchInput.value.trim() : "";
      const onlyComp = filterHfCompat ? filterHfCompat.checked : true;
      loadHfExplorerResults(q, onlyComp);
    });
  }

  if (hfSearchInput) {
    hfSearchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const q = hfSearchInput.value.trim();
        const onlyComp = filterHfCompat ? filterHfCompat.checked : true;
        loadHfExplorerResults(q, onlyComp);
      }
    });
  }

  if (filterHfCompat) {
    filterHfCompat.addEventListener("change", () => {
      const q = hfSearchInput ? hfSearchInput.value.trim() : "";
      loadHfExplorerResults(q, filterHfCompat.checked);
    });
  }
  ["modal-hf-filter-size", "modal-hf-filter-trusted", "modal-hf-filter-format", "modal-hf-filter-precision"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", () => loadHfExplorerResults(hfSearchInput.value.trim(), filterHfCompat.checked));
  });
  document.querySelectorAll("[data-hf-hardware]").forEach((button) => {
    const name = button.dataset.hfHardware;
    if (name) button.querySelector(".hf-filter-icon").innerHTML = window.ETUSVG?.icon(name) || "";
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-hf-hardware]").forEach((choice) => {
        const active = choice === button;
        choice.classList.toggle("active", active);
        choice.setAttribute("aria-pressed", String(active));
      });
      loadHfExplorerResults(hfSearchInput.value.trim(), filterHfCompat.checked);
    });
  });
  document.getElementById("hf-page-prev")?.addEventListener("click", () => loadHfExplorerResults(hfExplorerState.query, hfExplorerState.onlyCompatible, hfExplorerState.page - 1));
  document.getElementById("hf-page-next")?.addEventListener("click", () => loadHfExplorerResults(hfExplorerState.query, hfExplorerState.onlyCompatible, hfExplorerState.page + 1));
  document.getElementById("hf-page-numbers")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-hf-page]");
    if (button) loadHfExplorerResults(hfExplorerState.query, hfExplorerState.onlyCompatible, Number(button.dataset.hfPage));
  });

  // Bouton Auto-Sélection dans la modale
  const btnModalAuto = document.getElementById("btn-modal-autoselect");
  if (btnModalAuto) {
    btnModalAuto.addEventListener("click", async () => {
      try {
        btnModalAuto.disabled = true;
        btnModalAuto.textContent = "⚡ Détection...";
        const resp = await fetch("/api/models/auto-select?count=1");
        const data = await resp.json();
        const primary = data.primary_model;
        if (primary) {
          showToast(`⚡ Modèle optimal détecté : <strong>${escapeHtml(primary.name)}</strong>`, "info", 3500);
          if (primary.is_installed) {
            await switchActiveModel(primary.filename, primary.name);
            closeModal("modal-models");
          } else {
            if (confirm(`Le modèle le plus optimisé pour votre configuration est "${primary.name}" (${primary.size_gib} Go).\nVoulez-vous lancer son téléchargement maintenant ?`)) {
              await triggerDownload(primary.repo, primary.filename);
            }
          }
        }
      } catch (err) {
        showToast("Erreur auto-sélection : " + err, "warning");
      } finally {
        btnModalAuto.disabled = false;
        btnModalAuto.textContent = "⚡ Auto-Sélection Config PC";
      }
    });
  }

  // Formulaire téléchargement direct HF
  const formCustomDl = document.getElementById("form-custom-download");
  if (formCustomDl) {
    formCustomDl.addEventListener("submit", async (e) => {
      e.preventDefault();
      const repo = document.getElementById("hf-repo").value.trim();
      const filename = document.getElementById("hf-filename").value.trim();
      if (!repo || !filename) return;
      const button = formCustomDl.querySelector('button[type="submit"]');
      if (button?.disabled) return;
      await triggerDownload(repo, filename, button, true);
    });
  }

  const ggufDropZone = document.getElementById("gguf-drop-zone");
  const ggufFileInput = document.getElementById("gguf-local-file");
  const ggufDropStatus = document.getElementById("gguf-drop-status");
  if (ggufDropZone && ggufFileInput && ggufDropStatus) {
    const importGguf = async (file) => {
      if (!file) return;
      if (!/\.gguf$/i.test(file.name)) {
        ggufDropStatus.textContent = "Choisissez un fichier .gguf.";
        ggufDropStatus.dataset.type = "error";
        return;
      }
      if (file.size > 64 * 1024 ** 3) {
        ggufDropStatus.textContent = "64 Go maximum par modèle importé.";
        ggufDropStatus.dataset.type = "error";
        return;
      }
      const filenameInput = document.getElementById("hf-filename");
      filenameInput.value = file.name;
      ggufDropZone.setAttribute("aria-busy", "true");
      ggufDropStatus.textContent = `Import de ${file.name} en cours…`;
      ggufDropStatus.dataset.type = "info";
      try {
        const params = new URLSearchParams({ filename: file.name });
        const response = await fetch(`/api/models/import?${params}`, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Import impossible.");
        ggufDropStatus.textContent = `${file.name} ajouté à la bibliothèque locale.`;
        ggufDropStatus.dataset.type = "success";
        await loadModelCatalog();
      } catch (error) {
        ggufDropStatus.textContent = error.message;
        ggufDropStatus.dataset.type = "error";
      } finally {
        ggufDropZone.removeAttribute("aria-busy");
        ggufFileInput.value = "";
      }
    };
    ggufDropZone.addEventListener("click", (event) => {
      if (event.target !== ggufFileInput) ggufFileInput.click();
    });
    ggufDropZone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        ggufFileInput.click();
      }
    });
    ggufFileInput.addEventListener("change", () => importGguf(ggufFileInput.files?.[0]));
    ggufDropZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      ggufDropZone.classList.add("drag-over");
    });
    ggufDropZone.addEventListener("dragleave", (event) => {
      if (!ggufDropZone.contains(event.relatedTarget)) ggufDropZone.classList.remove("drag-over");
    });
    ggufDropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      ggufDropZone.classList.remove("drag-over");
      importGguf(event.dataTransfer?.files?.[0]);
    });
  }
}

const hfExplorerState = { key: "", query: "", onlyCompatible: true, page: 1, models: [], complete: false, requestId: 0, controller: null };

function createHfModelCard(item, { requestId = "wizard", index = 0, nativeFormat = false } = {}) {
  const icon = (name) => window.ETUSVG?.icon(name) || "";
  const compatibility = item.compatibility || {};
  const badgeText = String(compatibility.badge || "Compatible").replace(/^[^A-Za-zÀ-ÿ0-9]+/, "");
  const badgeIcon = compatibility.has_gpu_acceleration ? "gpu" : nativeFormat || compatibility.npu_candidate ? "npu" : "cpu";
  let badgesHtml = `<span class="etu-badge etu-badge-${compatibility.color || "info"} hf-card-badge">${icon(badgeIcon)}${escapeHtml(badgeText)}</span>`;
  if (compatibility.npu_candidate) {
    badgesHtml += ` <span class="etu-badge etu-badge-cyan hf-card-badge" title="Accélération NPU à confirmer avec ce modèle">${icon("npu")} Candidat NPU</span>`;
  }
  badgesHtml += ` <span class="etu-badge etu-badge-success hf-card-badge hf-installed-badge" hidden>${icon("check")} Installé</span>`;

  const quants = item.quantizations || [{ filename: item.filename, size_gib: item.size_gib, quant: item.quant }];
  const selectOptions = quants.map((quant) => {
    const variantLabel = item.format ? `${quant.quant} · ${quant.filename}` : quant.quant || "GGUF";
    return `<option value="${escapeHtml(quant.filename)}">${escapeHtml(variantLabel)} (~${Number(quant.size_gib || 0).toLocaleString("fr-FR")} Go)</option>`;
  }).join("");
  const quantId = `hf-quant-${requestId}-${index}`;
  const modelPage = `https://huggingface.co/${String(item.repo).split("/").map(encodeURIComponent).join("/")}`;
  const parameters = item.params && !/^inconnu$/i.test(item.params) ? `<span class="hf-parameter-badge">${escapeHtml(item.params)}</span>` : "";
  const description = item.description && !item.description.startsWith("Modèle GGUF haute performance disponible sur Hugging Face") ? `<p class="hf-model-desc">${escapeHtml(item.description)}</p>` : "";
  const requiredRam = Number(compatibility.required_ram_gib || item.required_ram_gib);
  const memoryLabel = requiredRam > 0 ? `~${requiredRam.toLocaleString("fr-FR")} Go` : "Non renseignée";
  const card = document.createElement("article");
  card.className = "hf-model-card";
  card.innerHTML = `
    <header class="hf-model-top">
      <div class="hf-model-identity">
        <div class="hf-model-title-row">
          <h4 class="hf-model-title">${escapeHtml(item.name)}</h4>
          ${parameters}
        </div>
        <span class="hf-model-author">${escapeHtml(item.repo)}</span>
      </div>
      ${item.repo === "local" ? '<span class="hf-model-author">Fichier local</span>' : `<a class="hf-model-page" href="${escapeHtml(modelPage)}" target="_blank" rel="noopener noreferrer" aria-label="Voir ${escapeHtml(item.name)} sur Hugging Face">${icon("externalLink")}<span>Page du modèle</span></a>`}
    </header>
    <div class="hf-model-badges">${badgesHtml}</div>
    ${description}
    <div class="hf-model-metrics">
      <div class="hf-model-metric"><span>${icon("download")}Téléchargements</span><strong>${Number(item.downloads || 0).toLocaleString("fr-FR")}</strong></div>
      <div class="hf-model-metric"><span>${icon("heart")}Soutiens</span><strong>${Number(item.likes || 0).toLocaleString("fr-FR")}</strong></div>
      <div class="hf-model-metric"><span>${icon("memory")}Mémoire estimée</span><strong>${memoryLabel}</strong></div>
    </div>
    ${compatibility.recommendation ? `<p class="hf-model-recommendation">${icon("hint")}<span>${escapeHtml(compatibility.recommendation)}</span></p>` : ""}
    <div class="hf-model-bottom">
      <div class="hf-model-quantification">
        <label for="${quantId}">Quantification</label>
        <select id="${quantId}" class="hf-quant-select">${selectOptions}</select>
      </div>
      <div class="hf-model-actions"></div>
    </div>`;
  return {
    card,
    icon,
    quants,
    select: card.querySelector(".hf-quant-select"),
    actions: card.querySelector(".hf-model-actions"),
    installed: new Set(item.installed_filenames || (item.is_installed ? [item.filename] : [])),
  };
}

function validateHfExplorerResponse(data, requestedFormat) {
  if (requestedFormat === "gguf") return;
  if (data.model_format !== requestedFormat) {
    throw new Error("Le serveur ne reconnaît pas le filtre de format. Redémarrez ETU-AI pour charger la mise à jour.");
  }
  const formats = requestedFormat === "npu" ? ["openvino", "onnx"] : [requestedFormat];
  if ((data.models || []).some((item) => !formats.includes(item.format)
      || (item.format === "openvino" && !/\.xml$/i.test(item.filename || ""))
      || (item.format === "onnx" && !/\.onnx$/i.test(item.filename || "")))) {
    throw new Error("La recherche a renvoyé un modèle dans un autre format. Relancez la recherche après avoir redémarré ETU-AI.");
  }
}

async function loadHfExplorerResults(searchQuery = "", onlyCompatible = true, page = 1) {
  const container = document.getElementById("hf-search-results");
  const countLabel = document.getElementById("hf-search-count");
  if (!container) return;
  const parameterRange = document.getElementById("modal-hf-filter-size").value;
  const trustedOnly = document.getElementById("modal-hf-filter-trusted").checked;
  const hardwareFilter = document.querySelector("[data-hf-hardware].active")?.dataset.hfHardware || "";
  const modelFormat = document.getElementById("modal-hf-filter-format")?.value || "auto";
  const nativeFormat = ["openvino", "onnx"].includes(modelFormat) || (modelFormat === "auto" && hardwareFilter === "npu");
  const precisionFilter = document.getElementById("modal-hf-filter-precision");
  if (precisionFilter) precisionFilter.disabled = !nativeFormat;
  const precision = nativeFormat ? precisionFilter?.value || "" : "";
  const formatNotice = document.getElementById("hf-native-format-notice");
  if (formatNotice) formatNotice.hidden = !nativeFormat;
  const key = JSON.stringify([searchQuery, onlyCompatible, parameterRange, trustedOnly, hardwareFilter, modelFormat, precision]);
  if (hfExplorerState.key !== key) {
    Object.assign(hfExplorerState, { key, models: [], complete: false });
  }
  hfExplorerState.query = searchQuery;
  hfExplorerState.onlyCompatible = onlyCompatible;
  page = Math.max(1, page);
  const requestId = ++hfExplorerState.requestId;
  hfExplorerState.controller?.abort();
  const controller = new AbortController();
  hfExplorerState.controller = controller;
  const needed = Math.min(page * 10 + 1, 61);
  const needsFetch = !hfExplorerState.complete && hfExplorerState.models.length < needed;
  if (needsFetch) {
    container.innerHTML = '<div class="hf-loading" role="status"><span class="hf-loading-orb" aria-hidden="true"></span><span>Recherche des modèles…</span></div>';
    if (countLabel) countLabel.textContent = "Chargement…";
    const pagination = document.getElementById("hf-search-pagination");
    pagination.hidden = true;
    document.getElementById("hf-page-numbers")?.replaceChildren();
    document.getElementById("hf-page-prev").disabled = true;
    document.getElementById("hf-page-next").disabled = true;
  }

  try {
    if (needsFetch) {
      const params = new URLSearchParams({ q: searchQuery, only_compatible: String(onlyCompatible), limit: String(Math.min(needed, 51)), trusted_only: String(trustedOnly), parameter_range: parameterRange, hardware_filter: hardwareFilter, explorer: "true" });
      params.set("model_format", modelFormat);
      params.set("precision", precision);
      const resp = await fetch(`/api/models/huggingface/search?${params}`, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(45000)]) });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Recherche indisponible");
      if (requestId !== hfExplorerState.requestId) return;
      validateHfExplorerResponse(data, modelFormat === "auto" ? (hardwareFilter === "npu" ? "npu" : "gguf") : modelFormat);
      hfExplorerState.models = data.models || [];
      hfExplorerState.complete = hfExplorerState.models.length < needed;
    }
    if (requestId !== hfExplorerState.requestId) return;
    hfExplorerState.page = page;
    const models = hfExplorerState.models.slice((page - 1) * 10, page * 10);
    const hasNext = hfExplorerState.models.length > page * 10 || !hfExplorerState.complete;
    const pagination = document.getElementById("hf-search-pagination");
    const lastPage = Math.max(1, Math.ceil(hfExplorerState.models.length / 10));
    pagination.hidden = hfExplorerState.complete && lastPage <= 1;
    document.getElementById("hf-page-prev").disabled = page === 1;
    document.getElementById("hf-page-next").disabled = !hasNext;
    const pageNumbers = document.getElementById("hf-page-numbers");
    pageNumbers.replaceChildren();
    const visiblePages = new Set();
    if (page <= 4) {
      for (let value = 1; value <= (hfExplorerState.complete ? Math.min(4, lastPage) : 4); value++) visiblePages.add(value);
    } else {
      for (let value = Math.max(1, page - 1); value <= (hfExplorerState.complete ? Math.min(lastPage, page + 2) : page + 2); value++) visiblePages.add(value);
    }
    if (hfExplorerState.complete && lastPage > 4) visiblePages.add(lastPage);
    const orderedPages = [...visiblePages].sort((a, b) => a - b);
    let previousPage = 0;
    orderedPages.forEach((value) => {
      if (previousPage && value - previousPage > 1) {
        const ellipsis = document.createElement("span");
        ellipsis.className = "hf-page-ellipsis";
        ellipsis.textContent = "…";
        ellipsis.setAttribute("aria-hidden", "true");
        pageNumbers.append(ellipsis);
      }
      const pageButton = document.createElement("button");
      pageButton.type = "button";
      pageButton.className = "hf-page-number";
      pageButton.dataset.hfPage = String(value);
      pageButton.textContent = String(value);
      pageButton.setAttribute("aria-label", `Page ${value}`);
      if (value === page) {
        pageButton.classList.add("active");
        pageButton.setAttribute("aria-current", "page");
      }
      pageNumbers.append(pageButton);
      previousPage = value;
    });
    if (!hfExplorerState.complete) {
      const ellipsis = document.createElement("span");
      ellipsis.className = "hf-page-ellipsis";
      ellipsis.textContent = "…";
      ellipsis.setAttribute("aria-hidden", "true");
      pageNumbers.append(ellipsis);
    }
    if (countLabel) countLabel.textContent = `${models.length} modèle${models.length > 1 ? "s" : ""} sur cette page`;
    container.replaceChildren();

    if (!models.length) {
      if (nativeFormat && onlyCompatible) {
        container.innerHTML = '<p class="etu-text">Les exports OpenVINO et ONNX ne sont pas encore exécutables dans Beacon. Décochez « Afficher uniquement les IA compatibles avec ma config » pour les explorer.</p>';
        return;
      }
      container.innerHTML = `<p class="etu-text">Aucun modèle trouvé pour « ${escapeHtml(searchQuery)} » avec ces filtres.</p>`;
      return;
    }

    models.forEach((item, index) => {
      const { card, icon, quants, select, actions, installed } = createHfModelCard(item, { requestId, index, nativeFormat });

      if (item.can_install === false) {
        card.querySelector(".hf-model-quantification").textContent = `Format : ${item.format === "openvino" ? "OpenVINO IR" : "ONNX"} · dépôt complet requis`;
        card.querySelector(".hf-model-actions").innerHTML = '<span class="etu-text">Installation dans Beacon à venir</span>';
        container.appendChild(card);
        return;
      }

      if (installed.size) select.value = [...installed][0];
      if (["openvino", "onnx"].includes(item.format)) {
        card.querySelector(".hf-model-quantification label").textContent = "Quantification / variante publiée";
        const renderNativeActions = () => {
          const variant = quants.find(quant => quant.filename === select.value);
          const isInstalled = installed.has(select.value);
          card.querySelector(".hf-installed-badge").hidden = !isInstalled;
          actions.replaceChildren();
          const button = document.createElement("button");
          button.type = "button";
          button.className = "etu-btn etu-btn-primary";
          button.textContent = isInstalled ? "Fichiers installés" : "Installer cette variante";
          button.disabled = isInstalled;
          button.addEventListener("click", () => {
            const filename = select.value;
            select.disabled = true;
            void triggerDownload(item.repo, filename, button, false, item.format, task => {
              select.disabled = false;
              if (task.status === "completed") {
                installed.add(filename);
                item.installed_filenames = [...installed];
                item.is_installed = true;
              }
              renderNativeActions();
            });
          });
          actions.append(button);
          if (isInstalled && variant) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "etu-btn etu-btn-secondary";
            remove.textContent = "Désinstaller";
            remove.addEventListener("click", async () => {
              if (!window.confirm(`Désinstaller ${item.name} (${variant.quant}) ?`)) return;
              remove.disabled = true;
              try {
                const response = await fetch(`/api/models/native/${encodeURIComponent(variant.id)}`, { method: "DELETE" });
                const result = await response.json();
                if (!response.ok) throw new Error(result.detail || "Désinstallation impossible");
                installed.delete(variant.filename);
                item.installed_filenames = [...installed];
                renderNativeActions();
                void loadModelCatalog();
              } catch (error) {
                remove.disabled = false;
                showToast(error.message, "warning");
              }
            });
            actions.append(remove);
          }
        };
        select.addEventListener("change", renderNativeActions);
        renderNativeActions();
        container.appendChild(card);
        return;
      }
      const renderActions = () => {
        const filename = select.value;
        const isInstalled = installed.has(filename);
        card.querySelector(".hf-installed-badge").hidden = !isInstalled;
        actions.replaceChildren();
        const primary = document.createElement("button");
        primary.type = "button";
        primary.className = `etu-btn ${isInstalled ? "etu-btn-secondary" : "etu-btn-primary"}`;
        primary.innerHTML = `${icon(isInstalled ? "check" : "download")}<span>${isInstalled ? "Activer" : "Télécharger"}</span>`;
        primary.addEventListener("click", () => isInstalled ? activateModel(filename, item.name) : triggerDownload(item.repo, filename, primary));
        actions.append(primary);
        if (isInstalled) {
          const uninstall = document.createElement("button");
          uninstall.type = "button";
          uninstall.className = "etu-btn etu-btn-secondary";
          uninstall.textContent = "Désinstaller";
          uninstall.addEventListener("click", async () => {
            if (!window.confirm(`Désinstaller « ${filename} » de cet ordinateur ?`)) return;
            uninstall.disabled = true;
            try {
              const response = await fetch(`/api/models/${encodeURIComponent(filename)}`, { method: "DELETE" });
              const result = await response.json().catch(() => ({}));
              if (!response.ok) throw new Error(result.detail || "La désinstallation a échoué.");
              installed.delete(filename);
              item.installed_filenames = [...installed];
              item.is_installed = installed.size > 0;
              renderActions();
              showToast("Modèle désinstallé.", "success");
              loadModelCatalog();
              loadServerStatus();
            } catch (error) {
              showToast(error.message || "La désinstallation a échoué.", "warning");
              uninstall.disabled = false;
            }
          });
          actions.append(uninstall);
        }
      };
      select.addEventListener("change", renderActions);
      renderActions();

      container.appendChild(card);
    });
  } catch (err) {
    if (requestId !== hfExplorerState.requestId) return;
    container.replaceChildren();
    const error = document.createElement("p");
    error.className = "etu-text etu-badge-warning";
    error.textContent = err.name === "TimeoutError"
      ? "La recherche Hugging Face prend trop de temps. Réessayez avec un terme plus précis."
      : `Erreur lors de la recherche Hugging Face : ${err.message || err}`;
    container.append(error);
    document.getElementById("hf-search-pagination").hidden = true;
    if (countLabel) countLabel.textContent = "";
  }
}

function openModal(id, { fromRoute = false } = {}) {
  if (id === "modal-models") restoreModelCatalogModal();
  const el = document.getElementById(id);
  document.getElementById("sidebar")?.classList.remove("is-open");
  document.getElementById("btn-open-sidebar")?.setAttribute("aria-expanded", "false");
  document.body.classList.remove("sidebar-open");
  const component = window.ETUComponents?.getDialog(id);
  if (component) component.open();
  else if (el) el.classList.add("active");
  if (id === "modal-models") loadModelCatalog();
  if (id === "modal-models" && !el?.classList.contains("settings-model-catalog") && !fromRoute) syncModelsRoute();
}

function showSettingsView(section = "general", { fromRoute = false } = {}) {
  const app = document.getElementById("app-container");
  const view = document.getElementById("settings-view");
  if (!app || !view) return;
  closeDownloadsView({ fromRoute: true });
  document.getElementById("notification-center").hidden = true;
  view.hidden = false;
  app.classList.add("settings-open");
  document.getElementById("sidebar")?.classList.remove("is-open");
  document.getElementById("btn-open-sidebar")?.setAttribute("aria-expanded", "false");
  document.body.classList.remove("sidebar-open");
  const labels = { general: "Général", models: "Modèles", profiles: "Profils d’IA", providers: "Fournisseurs IA et clés API", data: "Mes données", wiki: "Wiki et mémoire", etu: "ETU", installation: "Installation" };
  const selected = labels[section] ? section : "general";
  powerActionMenu?.close();
  document.getElementById("btn-refresh-library").hidden = selected !== "models";
  if (selected === "models") mountModelCatalogInSettings();
  else restoreModelCatalogModal();
  if (selected === "profiles") loadProfiles().then(renderProfileManager).catch(() => {});
  document.getElementById("settings-section-title").textContent = labels[selected];
  document.querySelectorAll(".settings-nav-item").forEach((button) => {
    const active = button.dataset.settingsSection === selected;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".settings-panel").forEach((panel) => {
    panel.hidden = panel.dataset.settingsPanel !== selected;
  });
  if (selected === "etu") window.refreshEtuRemoteConnection?.();
  if (selected === "wiki") window.ETUWiki?.refresh();
  if (selected === "installation") loadServerVariants();
  if (selected === "providers") requestAnimationFrame(updateProviderSidebarHeight);
  if (!fromRoute) {
    if (selected === "models") syncModelsRoute();
    else setAppRoute(`#/settings/${selected}`);
  }
}

function closeSettingsView({ fromRoute = false, syncRoute = true } = {}) {
  powerActionMenu?.close();
  restoreModelCatalogModal();
  const app = document.getElementById("app-container");
  const view = document.getElementById("settings-view");
  if (view) view.hidden = true;
  app?.classList.remove("settings-open");
  if (syncRoute && !fromRoute && document.getElementById("downloads-view")?.hidden !== false) setAppRoute("#/chat");
}

function showDownloadsView({ fromRoute = false } = {}) {
  closeSettingsView({ fromRoute: true, syncRoute: false });
  const app = document.getElementById("app-container");
  const view = document.getElementById("downloads-view");
  if (!app || !view) return;
  document.getElementById("notification-center").hidden = true;
  document.getElementById("btn-open-notifications")?.setAttribute("aria-expanded", "false");
  view.hidden = false;
  app.classList.add("downloads-open");
  document.getElementById("sidebar")?.classList.remove("is-open");
  document.getElementById("btn-open-sidebar")?.setAttribute("aria-expanded", "false");
  document.body.classList.remove("sidebar-open");
  requestAnimationFrame(drawDownloadSpeedChart);
  refreshDownloadManager();
  if (!fromRoute) setAppRoute("#/downloads");
}

function closeDownloadsView({ fromRoute = false, syncRoute = true } = {}) {
  const view = document.getElementById("downloads-view");
  if (view) view.hidden = true;
  document.getElementById("app-container")?.classList.remove("downloads-open");
  if (syncRoute && !fromRoute && document.getElementById("settings-view")?.hidden !== false) setAppRoute("#/chat");
}

let serverVariantPoller = null;
let serverVariantMenu = null;
let wizardBackendMenu = null;
let wizardRecommendedBackend = "CPU";
function getWizardBackendMenu() {
  if (!wizardBackendMenu) {
    wizardBackendMenu = ETUComponents.listMenu(document.getElementById("wizard-backend-menu"), {
      onChange: () => updateWizardBackendHint(),
      placement: "up",
    });
  }
  return wizardBackendMenu;
}
function updateWizardBackendHint() {
  const hint = document.getElementById("wizard-backend-hint");
  if (!hint) return;
  const selected = getWizardBackendMenu()?.selectedItem;
  hint.textContent = selected?.id === "auto"
    ? "Versions indisponibles pour le moment. ETU-AI choisira un moteur compatible."
    : selected ? `La version ${selected.label} sera installée.` : "Aucune version compatible disponible.";
}
function getServerVariantMenu() {
  if (!serverVariantMenu) serverVariantMenu = ETUComponents.listMenu(document.getElementById("settings-server-menu"));
  return serverVariantMenu;
}
function serverVariantPresentation(variant) {
  const match = variant.id.match(/^llama-server-(.+?)-(windows|macos|linux)-(x64|arm64)-(.+)\.(?:zip|tar\.gz)$/);
  const tag = variant.tag || match?.[1] || "Version inconnue";
  const platform = variant.platform || match?.[2] || "";
  const arch = variant.arch || match?.[3] || "";
  const backend = variant.variant || match?.[4] || "";
  const compatible = variant.compatible !== false;
  const isCuda = backend.startsWith("cuda");
  const isVulkan = backend.startsWith("vulkan");
  const isOpenVino = backend.includes("openvino");
  const isMetal = backend === "metal";
  const cudaVersion = backend.match(/^cuda([0-9.]+)/)?.[1];
  const title = backend === "cpu" ? "CPU" : [
    isCuda ? `NVIDIA CUDA${cudaVersion ? ` ${cudaVersion}` : ""}` : null,
    isVulkan ? "Vulkan" : null,
    isMetal ? "Metal" : null,
    isOpenVino ? "OpenVINO" : null,
  ].filter(Boolean).join(" + ") || backend.toUpperCase();
  const badges = [
    backend === "cpu" ? "cpu" : null,
    isCuda || isVulkan || isMetal ? "gpu" : null,
    isOpenVino ? "npu" : null,
    isCuda ? "nvidia" : null,
    isVulkan ? "vulkan" : null,
  ].filter(Boolean);
  const platformNames = { windows: "Windows", macos: "macOS", linux: "Linux" };
  return {
    id: variant.id,
    variant: backend,
    label: `${tag} · ${title}`,
    optionLabel: title,
    group: tag,
    subgroup: `${platformNames[platform] || platform} · ${arch}`,
    platform,
    badges,
    description: compatible ? "" : "Non compatible avec cet ordinateur",
    disabled: !compatible,
  };
}
function presentServerVariants(variants) {
  const unique = [...new Map((variants || []).map((variant) => [variant.id, variant])).values()];
  const versions = [...new Set(unique.map((variant) => serverVariantPresentation(variant).group))];
  unique.sort((a, b) => {
    const first = serverVariantPresentation(a);
    const second = serverVariantPresentation(b);
    return versions.indexOf(first.group) - versions.indexOf(second.group)
      || Number(first.disabled) - Number(second.disabled)
      || first.subgroup.localeCompare(second.subgroup, "fr");
  });
  return unique.map(serverVariantPresentation);
}
function latestServerVariants(variants) {
  const items = presentServerVariants(variants);
  const latest = items[0]?.group;
  return latest ? items.filter((item) => item.group === latest) : items;
}
async function loadWizardServerVariants() {
  const menu = getWizardBackendMenu();
  const hint = document.getElementById("wizard-backend-hint");
  if (hint) hint.textContent = "Chargement des versions du moteur…";
  try {
    const response = await fetch("/api/maintenance/server");
    if (!response.ok) throw new Error("Catalogue indisponible");
    const data = await response.json();
    const items = latestServerVariants(data.variants);
    if (!items.some((item) => !item.disabled)) throw new Error(data.catalog_error || "Aucune version compatible");
    const previous = items.some((item) => item.id === menu.value && !item.disabled) ? menu.value : null;
    const recommended = items.find((item) => !item.disabled && (item.variant === wizardRecommendedBackend.toLowerCase() || item.variant.startsWith(wizardRecommendedBackend.toLowerCase())));
    menu.setItems(items, previous || data.selected_variant || recommended?.id);
    updateWizardBackendHint();
  } catch (error) {
    menu.setItems([{ id: "auto", label: "Choix automatique", optionLabel: "Choix automatique", description: "Le catalogue des versions est momentanément indisponible.", platform: "cpu", badges: ["cpu"] }], "auto");
    updateWizardBackendHint();
  }
}
async function loadServerVariants() {
  const menu = getServerVariantMenu();
  const status = document.getElementById("settings-server-status");
  const showOlderToggle = document.getElementById("settings-server-show-older");
  if (showOlderToggle && !showOlderToggle.dataset.bound) {
    showOlderToggle.addEventListener("change", loadServerVariants);
    showOlderToggle.dataset.bound = "true";
  }
  status.dataset.loading = "true";
  status.setAttribute("aria-busy", "true");
  if (!status.textContent.trim()) status.textContent = "Chargement des versions du moteur…";
  try {
    const previousSelection = menu.value;
    const response = await fetch("/api/maintenance/server");
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}));
      throw new Error(failure.detail || `Liste des versions indisponible (${response.status})`);
    }
    const data = await response.json();
    const allItems = presentServerVariants(data.variants);
    const visibleItems = showOlderToggle?.checked ? allItems : latestServerVariants(data.variants);
    menu.setItems(visibleItems, previousSelection || data.selected_variant);
    const latestTag = allItems[0]?.group;
    const installedTag = data.installed_variant ? data.release_tag : null;
    const hiddenOlderInstalled = !showOlderToggle?.checked && installedTag && latestTag && installedTag !== latestTag;
    status.textContent = data.status === "running" ? (data.message || "Réinstallation en cours…") : data.status === "error" ? data.message : data.catalog_error || `Version installée : ${data.installed_variant || "inconnue"} · ${data.release_tag}${hiddenOlderInstalled ? ` · ${installedTag} est une ancienne version masquée` : ""}`;
    status.dataset.loading = data.status === "running" ? "true" : "false";
    status.setAttribute("aria-busy", data.status === "running" ? "true" : "false");
    document.getElementById("btn-settings-reinstall-server").disabled = data.status === "running" || !menu.value;
    if (data.status === "running") {
      clearTimeout(serverVariantPoller);
      serverVariantPoller = setTimeout(loadServerVariants, 1500);
    } else if (serverVariantPoller) {
      clearTimeout(serverVariantPoller);
      serverVariantPoller = null;
      if (data.status === "completed") loadServerStatus();
    }
  } catch (error) {
    status.textContent = error.message;
    status.dataset.loading = "false";
    status.setAttribute("aria-busy", "false");
    menu.setItems([]);
    document.getElementById("btn-settings-reinstall-server").disabled = true;
  }
}

async function reinstallServerVariant() {
  const menu = getServerVariantMenu();
  const button = document.getElementById("btn-settings-reinstall-server");
  if (!menu.value || !window.confirm(`Réinstaller ${menu.selectedItem.label} ? Le moteur local redémarrera.`)) return;
  button.disabled = true;
  button.classList.add("is-loading");
  button.setAttribute("aria-busy", "true");
  button.querySelector("span:last-child").textContent = "Préparation…";
  const status = document.getElementById("settings-server-status");
  status.textContent = "Préparation de la réinstallation…";
  status.dataset.loading = "true";
  status.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/maintenance/server/reinstall", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ variant: menu.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Réinstallation impossible");
    loadServerVariants();
    showDownloadsView();
  } catch (error) {
    status.textContent = error.message;
    status.dataset.loading = "false";
    status.setAttribute("aria-busy", "false");
    button.classList.remove("is-loading");
    button.removeAttribute("aria-busy");
    button.querySelector("span:last-child").textContent = "Réinstaller cette version";
    button.disabled = false;
  }
}

function mountModelCatalogInSettings() {
  const catalog = document.getElementById("modal-models");
  const mount = document.getElementById("settings-models-mount");
  if (!catalog || !mount) return;
  mount.append(catalog);
  catalog.classList.add("settings-model-catalog");
  catalog.classList.remove("active");
  loadModelCatalog();
}

function restoreModelCatalogModal() {
  const catalog = document.getElementById("modal-models");
  if (!catalog || !catalog.classList.contains("settings-model-catalog")) return;
  document.body.append(catalog);
  catalog.classList.remove("settings-model-catalog", "active");
}

function updateProviderSidebarHeight() {
  const sidebar = document.querySelector("#settings-provider-mount .api-provider-sidebar");
  if (!sidebar || !sidebar.getClientRects().length || window.innerWidth <= 700) return;
  sidebar.style.setProperty("--provider-sidebar-height", `${Math.max(240, window.innerHeight - Math.max(16, sidebar.getBoundingClientRect().top) - 24)}px`);
}

function initSettingsPage() {
  const modal = document.getElementById("modal-api-settings");
  const mount = document.getElementById("settings-provider-mount");
  const providerLayout = modal?.querySelector(".modal-body");
  if (providerLayout && mount) {
    providerLayout.classList.add("settings-provider-layout");
    mount.append(providerLayout);
    modal.remove();
  }
  document.querySelector(".settings-content")?.addEventListener("scroll", updateProviderSidebarHeight, { passive: true });
  window.addEventListener("resize", updateProviderSidebarHeight);
  document.getElementById("btn-refresh-library")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const data = await loadModelCatalog({ throwOnError: true });
      const installed = new Set(data.installed || []);
      const installedNative = new Set((data.native_models || []).map(model => model.id));
      for (const item of hfExplorerState.models) {
        item.installed_filenames = (item.quantizations || [])
          .filter(quant => item.format ? installedNative.has(quant.id) : installed.has(quant.filename))
          .map(quant => quant.filename);
        item.is_installed = item.installed_filenames.length > 0;
      }
      if (document.getElementById("tab-hf-explorer").classList.contains("active")) {
        await loadHfExplorerResults(hfExplorerState.query, hfExplorerState.onlyCompatible, hfExplorerState.page);
      }
      showToast(`Bibliothèque actualisée · ${installed.size} modèle${installed.size > 1 ? "s" : ""} ${installed.size > 1 ? "locaux" : "local"}.`, "success");
    } catch (error) { showToast(escapeHtml(error.message), "warning"); }
    finally { button.disabled = false; button.removeAttribute("aria-busy"); }
  });
}

function closeModal(id, { fromRoute = false } = {}) {
  const el = document.getElementById(id);
  const component = window.ETUComponents?.getDialog(id);
  if (component) component.close();
  else if (el) el.classList.remove("active");
  if (id === "modal-models" && document.getElementById("settings-view")?.hidden === false
      && document.getElementById("settings-section-title")?.textContent === "Modèles") {
    mountModelCatalogInSettings();
  }
  if (id === "modal-models" && !fromRoute && document.getElementById("settings-view")?.hidden !== false) setAppRoute("#/chat");
}

/* -------------------------------------------------------------------------- */
/* Toast Notifications                                                        */
/* -------------------------------------------------------------------------- */
function isConversationVisible(conversationId) {
  return Boolean(conversationId && conversationId === currentConversationId
    && !document.hidden && document.hasFocus()
    && ["#/chat", `#/chat/${encodeURIComponent(conversationId)}`].includes(window.location.hash)
    && document.getElementById("chat-messages")?.getClientRects().length
    && !document.querySelector('.modal-overlay.active'));
}

function dismissViewedResponseNotifications() {
  const remaining = appNotifications.filter(item => item.kind !== "response" || !isConversationVisible(item.conversationId));
  if (remaining.length !== appNotifications.length) {
    appNotifications = remaining;
    persistAppNotifications();
  }
  document.querySelectorAll('.toast-response').forEach(toast => {
    if (isConversationVisible(toast.dataset.conversationId)) toast.remove();
  });
}

function addAppNotification(title, detail, kind = "info", { eventKey, conversationId } = {}) {
  if (kind === "response" && isConversationVisible(conversationId)) return;
  if (kind === "power" && !document.hidden && document.hasFocus()) return;
  if (eventKey && notificationEventKeys.has(eventKey)) return;
  if (eventKey) {
    notificationEventKeys.add(eventKey);
    const keys = [...notificationEventKeys].slice(-200);
    notificationEventKeys = new Set(keys);
    localStorage.setItem(notificationEventStorageKey, JSON.stringify(keys));
  }
  const centerOpen = document.getElementById("notification-center")?.hidden === false && !document.hidden && document.hasFocus();
  appNotifications.unshift({ id: crypto.randomUUID(), title, detail, kind, time: Date.now(), read: centerOpen, eventKey, conversationId });
  appNotifications = appNotifications.slice(0, 50);
  localStorage.setItem(notificationStorageKey, JSON.stringify(appNotifications));
  renderAppNotifications();
  if (!centerOpen) showToast(detail, kind, 6500, title, conversationId);
}

function notificationKind(kind) {
  return ["success", "warning", "error", "download", "power", "response"].includes(kind) ? kind : "info";
}

function notificationIcon(kind) {
  const safeKind = notificationKind(kind);
  if (safeKind === "success" || safeKind === "response") return "check";
  if (safeKind === "warning" || safeKind === "error") return "alert";
  if (safeKind === "download") return "download";
  if (safeKind === "power") return "power";
  return "bell";
}

function persistAppNotifications() {
  localStorage.setItem(notificationStorageKey, JSON.stringify(appNotifications));
  renderAppNotifications();
}

function renderAppNotifications() {
  const list = document.getElementById("notification-list");
  const count = document.getElementById("notification-count");
  if (!list || !count) return;
  const clearButton = document.getElementById("btn-clear-notifications");
  if (clearButton) clearButton.disabled = appNotifications.length === 0;
  const unread = appNotifications.filter(item => !item.read).length;
  count.hidden = unread === 0;
  count.textContent = unread > 9 ? "9+" : String(unread);
  list.replaceChildren();
  if (!appNotifications.length) {
    list.innerHTML = '<p class="utility-empty">Aucune notification pour le moment.</p>';
    return;
  }
  for (const item of appNotifications) {
    const row = document.createElement("article");
    const kind = notificationKind(item.kind);
    row.className = `notification-item notification-${kind}${item.read ? "" : " is-unread"}`;
    const icon = document.createElement("span");
    icon.className = "notification-item-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = window.ETUSVG?.icon(notificationIcon(kind)) || "";
    const copy = document.createElement("div");
    copy.className = "notification-item-copy";
    const heading = document.createElement("strong");
    heading.textContent = item.title;
    const detail = document.createElement("p");
    detail.textContent = item.detail;
    const time = document.createElement("time");
    time.dateTime = new Date(item.time).toISOString();
    time.textContent = new Date(item.time).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
    copy.append(heading, detail, time);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "notification-item-remove";
    remove.setAttribute("aria-label", `Supprimer la notification : ${item.title}`);
    remove.title = "Supprimer cette notification";
    remove.innerHTML = '<span aria-hidden="true">×</span>';
    remove.addEventListener("click", () => {
      appNotifications = appNotifications.filter(candidate => candidate.id !== item.id);
      persistAppNotifications();
    });
    row.append(icon, copy, remove);
    list.append(row);
  }
}

function formatDownloadBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes === 0) return "0 o";
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} Ko`;
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} Go`;
  return `${(bytes / 1024 ** 2).toFixed(1)} Mo`;
}

const downloadTimeEstimates = new Map();

function downloadRemainingTime(task, now = Date.now()) {
  if (task.status !== "downloading") {
    downloadTimeEstimates.delete(task.task_id);
    return "";
  }
  const total = Number(task.total_bytes);
  const received = Math.max(0, Number(task.downloaded_bytes) || 0);
  const previous = downloadTimeEstimates.get(task.task_id);
  const elapsed = previous ? (now - previous.at) / 1000 : 0;
  const moved = previous && received > previous.bytes;
  const reported = Math.max(0, Number(task.speed_mbps) || 0) * 1024 ** 2;
  const measured = elapsed > 0 && moved ? (received - previous.bytes) / elapsed : 0;
  const sample = measured || reported;
  const speed = previous?.speed && sample ? previous.speed * .7 + sample * .3 : sample || previous?.speed || 0;
  const lastProgressAt = !previous || moved ? now : previous.lastProgressAt;
  downloadTimeEstimates.set(task.task_id, { bytes: received, at: now, speed, lastProgressAt });
  if (!Number.isFinite(total) || total <= 0) return "Temps restant : calcul en cours…";
  if (received >= total) return "Finalisation…";
  if (!Number.isFinite(speed) || speed <= 0 || now - lastProgressAt >= 15000) return "Temps restant : en attente du débit…";
  const seconds = Math.max(1, Math.ceil((total - received) / speed));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const duration = hours ? `${hours} h ${minutes} min` : minutes ? `${minutes} min ${seconds % 60} s` : `${seconds} s`;
  return `Temps restant estimé : ~${duration}`;
}

const downloadSpeedHistory = [];
const previousDownloadedBytes = new Map();
let engineUpdateActivity = null;
let dismissedDownloadEntries = new Set();
try { dismissedDownloadEntries = new Set(JSON.parse(localStorage.getItem("etu-dismissed-downloads") || "[]")); } catch (_) {}
try { engineUpdateActivity = JSON.parse(localStorage.getItem("etu-engine-update") || "null"); } catch (_) {}
if (engineUpdateActivity?.status === "checking") engineUpdateActivity = null;

function downloadBrand(task = {}) {
  const source = [task.repo, task.filename, task.local_filename].filter(Boolean).join(" ").toLowerCase();
  const brands = [
    [/qwen|qwq|qvq/, "Qwen", "qwen-color", "#7360ee"],
    [/mistral|ministral|mixtral|codestral|magistral/, "Mistral", "download-mistral-color", "#f49b39"],
    [/deepseek/, "DeepSeek", "download-deepseek-color", "#4b83f5"],
    [/llama|meta-/, "Llama", "meta-color", "#478bfa"],
    [/gemma|gemini/, "Gemma", "gemma-color", "#65a1ed"],
    [/phi[-_ .0-9]|microsoft/, "Microsoft", null, "#52a8d8"],
    [/claude/, "Claude", "download-claude-color", "#d79976"],
    [/gpt|openai/, "OpenAI", "download-openai", "#4ab497"],
  ];
  for (const [pattern, label, icon, color] of brands) {
    if (pattern.test(source)) return { label, logo: icon ? `/static/provider-icons/${icon}.svg${label === "OpenAI" ? "?v=logo-2" : ""}` : "/static/platform-icons/windows.svg", color };
  }
  if (task.kind === "engine" || task.kind === "update") return { label: "ETU-AI", logo: "/static/logo_etu_ai.svg", color: "#5585df" };
  return source ? { label: "Modèle GGUF", logo: "/static/provider-icons/huggingface-color.svg", color: "#e9bd52" }
    : { label: "ETU-AI", logo: "/static/logo_etu_ai.svg", color: "#5585df" };
}

function createDownloadHeading(titleText, task) {
  const brand = downloadBrand(task);
  const heading = document.createElement("div");
  heading.className = "download-manager-title";
  const tile = document.createElement("span");
  tile.className = "download-manager-brand";
  tile.style.setProperty("--download-brand-color", brand.color);
  if (task?.kind === "update") {
    tile.classList.add("download-manager-brand--update");
    tile.innerHTML = window.ETUSVG?.icon("update") || "";
  } else {
    const logo = document.createElement("img");
    logo.src = brand.logo;
    logo.alt = brand.label;
    tile.append(logo);
  }
  const title = document.createElement("strong");
  title.textContent = titleText;
  heading.append(tile, title);
  return heading;
}

function seedDownloadHistory(now = Date.now()) {
  if (downloadSpeedHistory.length) return;
  for (let i = 59; i >= 0; i--) downloadSpeedHistory.push({ network: 0, disk: 0, at: now - i * 1000 });
}
let downloadPeakSpeed = 0;
let lastDownloadSampleAt = 0;
let downloadChart = null;

function getDownloadChart() {
  const container = document.getElementById("download-speed-chart");
  if (!container || !window.echarts) return null;
  if (!downloadChart) downloadChart = window.echarts.init(container, null, { renderer: "canvas" });
  return downloadChart;
}

function clearDownloadDashboard() {
  const dashboard = document.getElementById("download-dashboard");
  const wasActive = dashboard?.dataset.idle !== "true";
  if (!dashboard) return;
  dashboard.hidden = false;
  dashboard.classList.add("is-idle");
  dashboard.dataset.idle = "true";
  const metrics = dashboard.querySelector(".download-metrics");
  if (metrics) metrics.hidden = true;
  const panel = document.querySelector(".download-chart-panel");
  panel?.removeAttribute("data-banner-title");
  panel?.removeAttribute("data-banner-repo");
  panel?.style.removeProperty("--download-banner-image");
  downloadSpeedHistory.length = 0;
  seedDownloadHistory();
  resolveDownloadBanner();
  previousDownloadedBytes.clear();
  downloadPeakSpeed = 0;
  lastDownloadSampleAt = 0;
  const activeCount = document.getElementById("download-active-count");
  if (activeCount) activeCount.textContent = "";
  if (wasActive) {
    downloadChart?.clear();
  }
  if ((wasActive || !downloadChart) && !document.getElementById("downloads-view")?.hidden) requestAnimationFrame(drawDownloadSpeedChart);
}

function resolveDownloadBanner(task = {}) {
  const dashboard = document.getElementById("download-dashboard");
  const brand = downloadBrand(task);
  dashboard?.style.setProperty("--download-brand-color", brand.color);
  const logo = document.getElementById("download-banner-logo");
  if (logo && logo.getAttribute("src") !== brand.logo) logo.src = brand.logo;
}

function recordDownloadSample(tasks, serverInstall = null) {
  const downloading = tasks.filter(task => task.status === "downloading");
  if (!downloading.length && serverInstall?.status !== "running") {
    clearDownloadDashboard();
    return;
  }
  const dashboard = document.getElementById("download-dashboard");
  dashboard.hidden = false;
  dashboard.classList.remove("is-idle");
  dashboard.dataset.idle = "false";
  const metrics = dashboard.querySelector(".download-metrics");
  if (metrics) metrics.hidden = false;
  if (!document.getElementById("downloads-view")?.hidden) resolveDownloadBanner(downloading[0] || { kind: "engine" });
  const now = Date.now();
  const elapsedSeconds = lastDownloadSampleAt ? Math.max((now - lastDownloadSampleAt) / 1000, 0.1) : 1;
  const active = tasks.filter(task => ["queued", "downloading"].includes(task.status));
  const serverDownloading = serverInstall?.status === "running" && serverInstall?.phase === "downloading";
  const network = downloading.reduce((sum, task) => sum + (Number(task.speed_mbps) || 0), 0)
    + (serverDownloading ? Number(serverInstall.speed_mbps) || 0 : 0);
  const total = active.reduce((sum, task) => sum + (Number(task.downloaded_bytes) || 0), 0)
    + (serverDownloading ? Number(serverInstall.downloaded_bytes) || 0 : 0);
  let diskBytes = 0;
  const currentIds = new Set();
  for (const task of tasks) {
    const id = task.task_id;
    currentIds.add(id);
    const bytes = Number(task.downloaded_bytes) || 0;
    const previous = previousDownloadedBytes.get(id);
    if (previous !== undefined && bytes > previous) diskBytes += bytes - previous;
    previousDownloadedBytes.set(id, bytes);
  }
  if (serverDownloading) {
    const bytes = Number(serverInstall.downloaded_bytes) || 0;
    const previous = previousDownloadedBytes.get("server-reinstall");
    if (previous !== undefined && bytes > previous) diskBytes += bytes - previous;
    previousDownloadedBytes.set("server-reinstall", bytes);
    currentIds.add("server-reinstall");
  } else {
    previousDownloadedBytes.delete("server-reinstall");
  }
  for (const id of previousDownloadedBytes.keys()) if (!currentIds.has(id)) previousDownloadedBytes.delete(id);
  const disk = diskBytes / elapsedSeconds / 1024 ** 2;
  downloadPeakSpeed = Math.max(downloadPeakSpeed, network);
  seedDownloadHistory(now - 1000);
  downloadSpeedHistory.push({ network, disk, at: now });
  if (downloadSpeedHistory.length > 60) downloadSpeedHistory.splice(0, downloadSpeedHistory.length - 60);
  lastDownloadSampleAt = now;
  const setText = (id, value) => { const element = document.getElementById(id); if (element) element.textContent = value; };
  setText("download-current-speed", downloading.length || serverDownloading ? `${network.toFixed(1)} Mo/s` : "—");
  setText("download-peak-speed", `${downloadPeakSpeed.toFixed(1)} Mo/s`);
  setText("download-total-size", formatDownloadBytes(total));
  setText("download-disk-speed", downloading.length || serverDownloading ? `${disk.toFixed(1)} Mo/s` : "—");
  const activeTransfers = downloading.length + Number(serverDownloading);
  setText("download-active-count", `${activeTransfers || 1} transfert${activeTransfers > 1 ? "s" : ""} en cours`);
  drawDownloadSpeedChart();
}

function drawDownloadSpeedChart() {
  const container = document.getElementById("download-speed-chart");
  if (!container || document.getElementById("download-dashboard")?.hidden || document.getElementById("downloads-view")?.hidden || !container.clientWidth) return;
  const chart = getDownloadChart();
  if (!chart) return;
  chart.resize({ width: container.clientWidth, height: container.clientHeight });
  const styles = getComputedStyle(document.documentElement);
  const accent = document.getElementById("download-dashboard")?.style.getPropertyValue("--download-brand-color") || styles.getPropertyValue("--accent-color").trim() || "#58a6ff";
  const success = styles.getPropertyValue("--success").trim() || accent;
  const muted = styles.getPropertyValue("--text-muted").trim() || "#8b949e";
  const border = styles.getPropertyValue("--border-color").trim() || "#30363d";
  const text = styles.getPropertyValue("--text-main").trim() || "#e6edf3";
  seedDownloadHistory();
  const samples = downloadSpeedHistory.slice(-60);
  chart.setOption({
    animation: false,
    color: [accent, success],
    grid: { top: 16, right: 54, bottom: 8, left: 12 },
    tooltip: {
      trigger: "axis",
      formatter: params => params.map(item => item.marker + item.seriesName + " : " + Number(item.value).toFixed(2) + " Mo/s").join("<br>"),
      backgroundColor: styles.getPropertyValue("--bg-primary").trim() || "#161b22",
      borderColor: border,
      textStyle: { color: text },
      valueFormatter: value => `${Number(value).toFixed(2)} Mo/s`,
    },
    xAxis: {
      type: "category",
      data: samples.map(sample => new Date(sample.at).toLocaleTimeString([], { minute: "2-digit", second: "2-digit" })),
      boundaryGap: true,
      axisTick: { show: false },
      axisLine: { show: false },
      axisLabel: { show: false },
      axisPointer: { label: { show: false } },
    },
    yAxis: {
      type: "value",
      min: 0,
      position: "right",
      axisLabel: { color: muted, fontSize: 10, formatter: value => `${value} Mo/s` },
      splitLine: { lineStyle: { color: border, opacity: .72 } },
    },
    series: [
      { name: "Réseau", type: "bar", data: samples.map(sample => sample.network), barMaxWidth: 12, itemStyle: { color: accent, borderRadius: [3, 3, 0, 0], opacity: .82 } },
      { name: "Disque", type: "line", data: samples.map(sample => sample.disk), smooth: .25, showSymbol: false, connectNulls: true, lineStyle: { width: 2, color: success }, areaStyle: { color: success, opacity: .08 } },
    ],
  }, true);
  container.setAttribute("aria-label", `Débit réseau actuel ${samples.at(-1)?.network.toFixed(1) || "0.0"} Mo/s, écriture disque ${samples.at(-1)?.disk.toFixed(1) || "0.0"} Mo/s`);
}

function updateDownloadNavigation(tasks = [], serverInstall = null) {
  const button = document.getElementById("btn-open-downloads");
  if (!button) return;
  const downloading = tasks.filter(task => task.status === "downloading");
  const queued = tasks.filter(task => task.status === "queued").length;
  const serverRunning = serverInstall?.status === "running";
  const running = downloading.length + Number(serverRunning);
  const serverHasDownloadSize = serverRunning && serverInstall?.phase === "downloading" && Number(serverInstall.total_bytes) > 0;
  const indeterminate = Boolean((serverRunning && !serverHasDownloadSize) || downloading.some(task => !(Number(task.total_bytes) > 0)));
  const total = downloading.reduce((sum, task) => sum + Math.max(0, Number(task.total_bytes) || 0), 0)
    + (serverHasDownloadSize ? Number(serverInstall.total_bytes) || 0 : 0);
  const received = downloading.reduce((sum, task) => sum + Math.max(0, Number(task.downloaded_bytes) || 0), 0)
    + (serverHasDownloadSize ? Number(serverInstall.downloaded_bytes) || 0 : 0);
  const percentage = total ? Math.min(100, Math.round(received / total * 100)) : 0;
  button.classList.toggle("is-downloading", running > 0);
  button.classList.toggle("is-download-indeterminate", running > 0 && indeterminate);
  button.classList.toggle("has-queued-downloads", running === 0 && queued > 0);
  button.style.setProperty("--download-nav-progress", `${percentage}%`);
  const details = [];
  if (running) details.push(`${running} en cours${indeterminate ? "" : ` · ${percentage} %`}`);
  if (queued) details.push(`${queued} en attente`);
  const label = `Téléchargements${details.length ? " · " + details.join(" · ") : ""}`;
  button.setAttribute("aria-label", label);
  button.title = label;
  const count = document.getElementById("download-nav-count");
  if (count) {
    count.hidden = running + queued === 0;
    count.textContent = String(running + queued);
  }
}

function renderDownloadManager(tasks, serverInstall = null) {
  updateDownloadNavigation(tasks, serverInstall);
  const list = document.getElementById("download-manager-list");
  const summary = document.getElementById("download-summary");
  if (!list || !summary) return;
  const active = tasks.filter(task => ["queued", "downloading"].includes(task.status));
  const serverIsActive = serverInstall?.status === "running";
  if (serverIsActive) {
    const previousSize = dismissedDownloadEntries.size;
    for (const key of dismissedDownloadEntries) if (key.startsWith("server:")) dismissedDownloadEntries.delete(key);
    if (dismissedDownloadEntries.size !== previousSize) localStorage.setItem("etu-dismissed-downloads", JSON.stringify([...dismissedDownloadEntries]));
  }
  const activeCount = active.length + Number(serverIsActive);
  summary.textContent = activeCount
    ? `${activeCount} transfert${activeCount > 1 ? "s" : ""} à traiter`
    : "";
  list.replaceChildren();
  const groups = {};
  for (const [key, label, empty] of [["running", "En cours", "Aucun téléchargement en cours."], ["queued", "En attente", "Aucun téléchargement en attente."], ["done", "Fait", "Aucun téléchargement terminé."]]) {
    const section = document.createElement("section");
    section.className = "download-task-group";
    section.dataset.downloadGroup = key;
    const heading = document.createElement("div");
    heading.className = "download-group-heading";
    const title = document.createElement("h3");
    title.textContent = label;
    heading.append(title);
    const entries = document.createElement("div");
    entries.className = "download-group-entries";
    section.append(heading, entries);
    list.append(section);
    groups[key] = { section, heading, title, entries, label, empty };
  }
  const serverHistoryKey = "server:" + JSON.stringify([serverInstall?.release_tag, serverInstall?.variant, serverInstall?.message]);
  const showServerInstall = ["running", "completed", "error"].includes(serverInstall?.status)
    && (serverIsActive || !dismissedDownloadEntries.has(serverHistoryKey));
  if (showServerInstall) {
    const row = document.createElement("article");
    row.className = "download-manager-item download-manager-item--server";
    const heading = createDownloadHeading("Installation / mise à jour du moteur", { kind: "engine" });
    const status = document.createElement("span");
    const serverLabels = { running: "En cours", completed: "Terminé", error: "Erreur" };
    status.textContent = serverInstall.status === "running" && serverInstall.phase === "downloading" && Number(serverInstall.total_bytes) > 0
      ? `Téléchargement · ${Math.round(Number(serverInstall.percentage) || 0)} %`
      : serverInstall.status === "running" && serverInstall.phase === "installing" ? "Vérification et installation" : serverLabels[serverInstall.status];
    const progress = document.createElement("progress");
    progress.max = 100;
    if (serverInstall.status === "completed") progress.value = 100;
    else if (serverInstall.status === "error") progress.value = 0;
    else if (serverInstall.phase === "downloading" && Number(serverInstall.total_bytes) > 0) {
      progress.value = Math.max(0, Math.min(100, Number(serverInstall.percentage) || 0));
    } else progress.removeAttribute("value");
    const details = document.createElement("small");
    const version = serverInstall.release_tag ? `Version ${serverInstall.release_tag}` : "Moteur llama.cpp";
    const downloadStats = serverInstall.phase === "downloading"
      ? ` · ${formatDownloadBytes(serverInstall.downloaded_bytes)} / ${serverInstall.total_bytes ? formatDownloadBytes(serverInstall.total_bytes) : "taille inconnue"} · ${(Number(serverInstall.speed_mbps) || 0).toFixed(2)} Mo/s`
      : "";
    details.textContent = `${version} · ${serverInstall.message || "Préparation…"}${downloadStats}`;
    row.append(heading, status, progress, details);
    groups[serverIsActive ? "running" : "done"].entries.append(row);
  }
  if (engineUpdateActivity) renderEngineUpdateActivity(groups[engineUpdateActivity.status === "checking" ? "running" : engineUpdateActivity.status === "available" ? "queued" : "done"].entries);
  for (const task of [...tasks].reverse()) {
    const group = task.status === "downloading" ? "running" : task.status === "queued" ? "queued" : "done";
    if (group === "done" && dismissedDownloadEntries.has(task.task_id)) continue;
    const row = document.createElement("article");
    row.className = "download-manager-item";
    row.dataset.status = task.status;
    const title = createDownloadHeading(task.filename || task.local_filename, task);
    row.style.setProperty("--download-brand-color", downloadBrand(task).color);
    const status = document.createElement("span");
    const labels = { queued: "En attente", downloading: "Téléchargement", completed: "Terminé", cancelled: "Annulé", error: "Erreur" };
    status.textContent = task.status === "downloading" && !task.total_bytes && !task.downloaded_bytes
      ? "Connexion à Hugging Face…"
      : `${labels[task.status] || task.status} · ${Math.round(Number(task.percentage) || 0)} %`;
    const progress = document.createElement("progress");
    progress.max = 100;
    progress.value = Math.max(0, Math.min(100, Number(task.percentage) || 0));
    progress.setAttribute("aria-label", `Progression du téléchargement de ${task.filename || task.local_filename}`);
    if (task.status === "downloading" && !Number(task.total_bytes)) progress.removeAttribute("value");
    progress.hidden = task.status === "completed";
    const stats = document.createElement("small");
    const speedDetail = task.status === "downloading" ? ` · ${(Number(task.speed_mbps) || 0).toFixed(2)} Mo/s` : "";
    const remainingTime = downloadRemainingTime(task);
    stats.textContent = `${formatDownloadBytes(task.downloaded_bytes)} / ${task.total_bytes ? formatDownloadBytes(task.total_bytes) : "taille inconnue"}${speedDetail}${remainingTime ? ` · ${remainingTime}` : ""}${task.error_message ? ` · ${task.error_message}` : ""}`;
    row.append(title, status, progress, stats);
    if (["queued", "downloading"].includes(task.status)) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "etu-btn etu-btn-secondary";
      cancel.textContent = "Annuler";
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        await fetch(`/api/models/tasks/${encodeURIComponent(task.task_id)}/cancel`, { method: "POST" });
      });
      row.append(cancel);
    }
    groups[group].entries.append(row);
  }
  for (const [key, group] of Object.entries(groups)) {
    const count = group.entries.children.length;
    if (!count) {
      group.section.remove();
      continue;
    }
    group.title.textContent = `${group.label} (${count})`;
    if (key === "done") {
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "download-clear-history";
      clear.textContent = "Effacer l’historique";
      clear.addEventListener("click", () => {
        tasks.filter(task => !["downloading", "queued"].includes(task.status)).forEach(task => dismissedDownloadEntries.add(task.task_id));
        if (showServerInstall && !serverIsActive) dismissedDownloadEntries.add(serverHistoryKey);
        if (engineUpdateActivity && !["checking", "available"].includes(engineUpdateActivity.status)) {
          engineUpdateActivity = null;
          localStorage.removeItem("etu-engine-update");
        }
        localStorage.setItem("etu-dismissed-downloads", JSON.stringify([...dismissedDownloadEntries]));
        renderDownloadManager(tasks, serverInstall);
      });
      group.heading.append(clear);
    }
  }
  if (!list.children.length) {
    list.innerHTML = `<div class="download-manager-empty" role="img" aria-label="Aucun téléchargement en cours"><span aria-hidden="true">${window.ETUSVG?.icon("download") || ""}</span></div>`;
  }
}

function renderEngineUpdateActivity(list) {
  const activity = engineUpdateActivity;
  const row = document.createElement("article");
  row.className = "download-manager-item download-manager-item--update";
  const heading = createDownloadHeading("Mises à jour du moteur", { kind: "update" });
  const status = document.createElement("span");
  status.textContent = { checking: "Recherche en cours", available: "Mise à jour disponible", current: "Moteur à jour", error: "Recherche impossible" }[activity.status] || "";
  const progress = document.createElement("progress");
  progress.max = 100;
  if (activity.status !== "checking") progress.value = activity.status === "current" ? 100 : 0;
  const detail = document.createElement("small");
  detail.textContent = activity.message;
  row.append(heading, status);
  if (activity.status === "checking") row.append(progress);
  row.append(detail);
  if (activity.status === "available") {
    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "etu-btn etu-btn-primary";
    choose.textContent = "Choisir la version à installer";
    choose.addEventListener("click", () => showSettingsView("installation"));
    row.append(choose);
  }
  list.append(row);
}

async function checkEngineUpdates() {
  if (engineUpdateActivity?.status === "checking") return;
  engineUpdateActivity = { status: "checking", message: "Recherche des versions compatibles du moteur sur GitHub…" };
  showDownloadsView();
  const buttons = ["btn-settings-update", "btn-downloads-check-updates"].map(id => document.getElementById(id)).filter(Boolean);
  buttons.forEach(button => { button.disabled = true; });
  try {
    const response = await fetch("/api/maintenance/server?refresh=true", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || data.catalog_error) throw new Error(data.detail || data.catalog_error || "Les versions sont indisponibles.");
    const versionParts = tag => /^v?\d+(?:\.\d+){1,3}$/i.test(tag || "") ? tag.replace(/^v/i, "").split(".").map(Number) : null;
    const compare = (a, b) => {
      for (let i = 0; i < Math.max(a.length, b.length); i++) {
        const delta = (a[i] || 0) - (b[i] || 0);
        if (delta) return delta;
      }
      return 0;
    };
    const installed = versionParts(data.release_tag);
    if (!installed) throw new Error("La version installée ne permet pas une comparaison automatique. Consultez les versions dans Installation.");
    const newer = data.variants.filter(item => item.compatible && versionParts(item.tag)
      && (!data.installed_variant || item.variant === data.installed_variant)
      && compare(versionParts(item.tag), installed) > 0)
      .sort((a, b) => compare(versionParts(b.tag), versionParts(a.tag)));
    engineUpdateActivity = newer.length
      ? { status: "available", message: "Version " + newer[0].tag + " disponible · version configurée " + data.release_tag }
      : { status: "current", message: "Version " + data.release_tag + " · aucune version compatible plus récente trouvée." };
  } catch (error) {
    engineUpdateActivity = { status: "error", message: error.message };
  } finally {
    localStorage.setItem("etu-engine-update", JSON.stringify(engineUpdateActivity));
    buttons.forEach(button => { button.disabled = false; });
    refreshDownloadManager();
  }
}

async function refreshDownloadManager() {
  if (appPowerState !== "awake") { clearDownloadDashboard(); updateDownloadNavigation(); return; }
  if (downloadRefreshInFlight) return;
  downloadRefreshInFlight = true;
  try {
    const response = await fetch("/api/models/tasks", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const tasks = (await response.json()).tasks || [];
    let serverInstall = null;
    try {
      const serverResponse = await fetch("/api/maintenance/server/progress", { cache: "no-store" });
      if (serverResponse.ok) serverInstall = await serverResponse.json();
    } catch (_) { /* Le suivi des téléchargements de modèles reste disponible. */ }
    recordDownloadSample(tasks, serverInstall);
    for (const task of tasks) {
      const previous = seenDownloadStatuses.get(task.task_id);
      if (previous && previous !== "completed" && task.status === "completed") {
        addAppNotification("Modèle téléchargé", `${task.local_filename || task.filename} est prêt à être utilisé.`, "download", { eventKey: `download:${task.task_id}` });
      }
      seenDownloadStatuses.set(task.task_id, task.status);
    }
    renderDownloadManager(tasks, serverInstall);
  } catch (_) {
    updateDownloadNavigation();
    const summary = document.getElementById("download-summary");
    const list = document.getElementById("download-manager-list");
    if (summary) summary.textContent = "Suivi temporairement indisponible";
    if (list) list.innerHTML = '<p class="download-empty">Le gestionnaire se reconnectera au serveur.</p>';
  } finally {
    downloadRefreshInFlight = false;
  }
}

async function refreshPowerState() {
  if (appPowerState === "shutting_down") return;
  try {
    const response = await fetch("/api/power/state");
    if (!response.ok) return;
    const state = await response.json();
    appPowerState = state.state;
    const text = document.getElementById("settings-power-status");
    if (text) text.textContent = state.error || (state.state === "sleeping" ? "ETU-AI est en veille." : state.state === "entering_sleep" ? "Mise en veille après les réponses en cours…" : "");
    applyLaunchMode(state.mode);
  } catch (_) { /* Le redémarrage interrompt temporairement la connexion. */ }
}

function applyLaunchMode(mode) {
  if (!["normal", "local", "server"].includes(mode)) return;
  currentLaunchMode = mode;
  document.querySelectorAll("[data-restart-mode]").forEach(button => {
    const active = button.dataset.restartMode === mode;
    button.classList.toggle("is-current", active);
    if (active) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });
}

async function wakeApp() {
  const response = await fetch("/api/power/wake", { method: "POST" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Réveil impossible");
  appPowerState = "awake";
  if (data.woke) addAppNotification("ETU-AI réveillé", "Le modèle peut recevoir de nouvelles demandes.", "power");
  refreshPowerState();
}

function showPowerTransition(action) {
  const overlay = document.getElementById("power-transition-overlay");
  if (!overlay) return;
  const labels = { restart: "Redémarrage d’ETU-AI…", sleep: "Mise en veille d’ETU-AI…", shutdown: "Arrêt d’ETU-AI…" };
  const icons = { restart: "restart", sleep: "sleep", shutdown: "power" };
  const label = document.getElementById("power-transition-label");
  if (label) label.textContent = labels[action] || labels.restart;
  const fallback = overlay.querySelector(".power-transition-fallback");
  if (fallback) {
    fallback.dataset.svgIcon = icons[action] || "restart";
    fallback.innerHTML = window.ETUSVG?.icon(fallback.dataset.svgIcon) || "";
  }
  overlay.dataset.action = action;
  overlay.hidden = false;
  overlay.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => overlay.classList.add("is-visible"));
}

function hidePowerTransition() {
  const overlay = document.getElementById("power-transition-overlay");
  if (!overlay) return;
  overlay.classList.remove("is-visible");
  overlay.setAttribute("aria-hidden", "true");
  overlay.hidden = true;
}

async function restartInMode(mode) {
  const modeLabels = { normal: "normal", local: "local", server: "serveur" };
  const modeLabel = modeLabels[mode] || mode;
  if (!window.confirm(`Redémarrer ETU-AI en mode ${modeLabel} ?`)) return false;
  const status = document.getElementById("settings-launch-mode-status");
  status.textContent = `Redémarrage en mode ${mode}…`;
  const response = await fetch("/api/power/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Redémarrage impossible");
  appPowerState = "shutting_down";
  showPowerTransition("restart");
  const deadline = Date.now() + 90000;
  const waitForRestart = async () => {
    if (Date.now() > deadline) { hidePowerTransition(); status.textContent = "Le redémarrage prend trop de temps. Rechargez la page après avoir relancé ETU-AI."; return; }
    try {
      const ready = await fetch("/api/launch-mode", { cache: "no-store" });
      if (ready.ok) {
        const current = await ready.json();
        if (current.pid !== data.old_pid && current.mode === mode) { location.reload(); return; }
      }
    } catch (_) { /* L'ancien serveur vient de s'arrêter. */ }
    setTimeout(waitForRestart, 1000);
  };
  setTimeout(waitForRestart, 1000);
  return true;
}

function initAppUtilities() {
  persistAppNotifications();
  window.addEventListener("focus", dismissViewedResponseNotifications);
  document.addEventListener("visibilitychange", dismissViewedResponseNotifications);
  document.querySelectorAll("[data-svg-icon]").forEach(holder => { holder.innerHTML = window.ETUSVG?.icon(holder.dataset.svgIcon) || ""; });
  const togglePanel = id => {
    const panel = document.getElementById(id);
    panel.hidden = !panel.hidden;
    if (id === "notification-center") closeDownloadsView();
    if (id === "notification-center" && !document.getElementById(id).hidden) {
      appNotifications.forEach(item => { item.read = true; });
      localStorage.setItem(notificationStorageKey, JSON.stringify(appNotifications));
      renderAppNotifications();
    }
    document.getElementById("btn-open-notifications").setAttribute("aria-expanded", String(!document.getElementById("notification-center").hidden));
  };
  document.getElementById("btn-open-downloads")?.addEventListener("click", showDownloadsView);
  document.getElementById("btn-downloads-back")?.addEventListener("click", closeDownloadsView);
  document.getElementById("btn-downloads-check-updates")?.addEventListener("click", checkEngineUpdates);
  document.getElementById("btn-open-notifications")?.addEventListener("click", () => togglePanel("notification-center"));
  document.getElementById("btn-clear-notifications")?.addEventListener("click", () => {
    appNotifications = [];
    persistAppNotifications();
  });
  document.querySelectorAll("[data-close-panel]").forEach(button => button.addEventListener("click", () => { document.getElementById(button.dataset.closePanel).hidden = true; document.getElementById("btn-open-notifications").setAttribute("aria-expanded", "false"); }));
  const powerButton = document.getElementById("btn-settings-power");
  const powerMenu = document.getElementById("settings-power-menu");
  powerActionMenu = window.ETUComponents.actionMenu(powerButton, powerMenu);
  document.querySelectorAll("[data-power-action]").forEach(button => button.addEventListener("click", async () => {
    powerActionMenu.close(true);
    try {
      const action = button.dataset.powerAction;
      if (action === "sleep") {
        const response = await fetch("/api/power/sleep", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail);
        appPowerState = data.state;
        showPowerTransition("sleep");
        refreshPowerState();
      } else if (action === "restart") {
        await restartInMode(currentLaunchMode);
      } else if (action === "shutdown") {
        if (!window.confirm("Éteindre ETU-AI ? L’application ne sera plus accessible jusqu’à son prochain démarrage.")) return;
        const response = await fetch("/api/power/shutdown", { method: "POST" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail);
        appPowerState = "shutting_down";
        showPowerTransition("shutdown");
        document.getElementById("settings-power-status").textContent = "ETU-AI s’arrête. Fermez cet onglet après l’arrêt.";
      }
    } catch (error) { showToast(escapeHtml(error.message), "warning"); refreshPowerState(); }
  }));
  document.querySelectorAll("[data-restart-mode]").forEach(button => button.addEventListener("click", async () => {
    try { await restartInMode(button.dataset.restartMode); }
    catch (error) { document.getElementById("settings-launch-mode-status").textContent = error.message; }
  }));
  fetch("/api/launch-mode").then(response => response.ok ? response.json() : null).then(state => { if (state) applyLaunchMode(state.mode); }).catch(() => {});
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      powerActionMenu.close();
      closeDownloadsView();
      document.getElementById("notification-center").hidden = true;
      document.getElementById("btn-open-notifications").setAttribute("aria-expanded", "false");
    }
  });
  renderAppNotifications();
  renderDownloadManager([]);
  window.addEventListener("resize", drawDownloadSpeedChart);
  refreshPowerState();
  refreshDownloadManager();
  setInterval(refreshPowerState, 3000);
  setInterval(refreshDownloadManager, 1000);
}

function appendSafeToastContent(container, message) {
  const template = document.createElement("template");
  template.innerHTML = String(message ?? "");
  const append = (parent, node) => {
    if (node.nodeType === Node.TEXT_NODE) { parent.append(document.createTextNode(node.textContent)); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.tagName === "BR") { parent.append(document.createElement("br")); return; }
    const target = node.tagName === "STRONG" || node.tagName === "EM" ? document.createElement(node.tagName.toLowerCase()) : parent;
    if (target !== parent) parent.append(target);
    [...node.childNodes].forEach(child => append(target, child));
  };
  [...template.content.childNodes].forEach(node => append(container, node));
}

function showToast(message, type = "info", duration = 4500, title = "", conversationId = "") {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  if (conversationId) toast.dataset.conversationId = conversationId;
  const safeType = notificationKind(type);
  toast.className = `toast-message toast-${safeType}`;
  toast.setAttribute("role", "status");
  toast.setAttribute("aria-live", "polite");
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = window.ETUSVG?.icon(notificationIcon(safeType)) || "";
  const copy = document.createElement("div");
  copy.className = "toast-copy";
  const heading = document.createElement("strong");
  heading.textContent = title || ({success:"Terminé",warning:"Attention",error:"Erreur",download:"Téléchargement terminé",power:"ETU-AI réveillé",response:"Réponse terminée"}[safeType] || "Information");
  const detail = document.createElement("div");
  detail.className = "toast-detail";
  appendSafeToastContent(detail, message);
  copy.append(heading, detail);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Fermer la notification");
  close.textContent = "×";
  const progress = document.createElement("span");
  progress.className = "toast-duration";
  progress.setAttribute("aria-hidden", "true");
  progress.style.animationDuration = `${Math.max(1000, duration)}ms`;
  toast.append(icon, copy, close, progress);
  container.appendChild(toast);

  let remaining = duration;
  let timerStarted = Date.now();
  let timer = setTimeout(dismiss, remaining);
  function dismiss() {
    clearTimeout(timer);
    toast.classList.add("toast-out");
    setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 300);
  }
  close.addEventListener("click", dismiss);
  toast.addEventListener("mouseenter", () => {
    clearTimeout(timer);
    remaining = Math.max(0, remaining - (Date.now() - timerStarted));
    progress.style.animationPlayState = "paused";
  });
  toast.addEventListener("mouseleave", () => {
    timerStarted = Date.now();
    progress.style.animationPlayState = "running";
    timer = setTimeout(dismiss, remaining);
  });
}

/* -------------------------------------------------------------------------- */
/* Sélecteur de Modèle & Menu Déroulant (Chat Header & Toolbar)               */
/* -------------------------------------------------------------------------- */
function initModelSelector() {
  const btnHeaderSelector = document.getElementById("btn-model-selector");
  const menu = document.getElementById("model-dropdown-menu");
  const btnOpenCatalog = document.getElementById("btn-dropdown-open-models");

  function toggleDropdown(e) {
    if (e) e.stopPropagation();
    if (!menu) return;
    const isOpen = menu.classList.contains("active");
    if (isOpen) {
      menu.classList.remove("active");
      hideModelStatsPopover();
      if (btnHeaderSelector) {
        btnHeaderSelector.classList.remove("open");
        btnHeaderSelector.setAttribute("aria-expanded", "false");
      }
    } else {
      menu.classList.add("active");
      if (btnHeaderSelector) {
        btnHeaderSelector.classList.add("open");
        btnHeaderSelector.setAttribute("aria-expanded", "true");
      }
    }
  }

  if (btnHeaderSelector) {
    btnHeaderSelector.addEventListener("click", toggleDropdown);
  }

  if (btnOpenCatalog) {
    btnOpenCatalog.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu) menu.classList.remove("active");
      hideModelStatsPopover();
      if (btnHeaderSelector) {
        btnHeaderSelector.classList.remove("open");
        btnHeaderSelector.setAttribute("aria-expanded", "false");
      }
      openModal("modal-models");
    });
  }

  // Fermer le dropdown lors d'un clic extérieur
  document.addEventListener("click", (e) => {
    const wrapper = document.getElementById("model-selector-wrapper");
    if (wrapper && !wrapper.contains(e.target)) {
      if (menu) menu.classList.remove("active");
      hideModelStatsPopover();
      if (btnHeaderSelector) {
        btnHeaderSelector.classList.remove("open");
        btnHeaderSelector.setAttribute("aria-expanded", "false");
      }
    }
  });

  // Fermer avec la touche Échap
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (menu) menu.classList.remove("active");
      if (btnHeaderSelector) btnHeaderSelector.classList.remove("open");
      if (btnHeaderSelector) btnHeaderSelector.setAttribute("aria-expanded", "false");
    }
  });
  document.getElementById("model-dropdown-search")?.addEventListener("input", filterModelDropdownList);
}

let modelStatsPopover = null;
let modelStatsHideTimer = null;

function hideModelStatsPopover() {
  clearTimeout(modelStatsHideTimer);
  if (modelStatsPopover) modelStatsPopover.hidden = true;
}

function showModelStatsPopover(itemEl) {
  clearTimeout(modelStatsHideTimer);
  if (!modelStatsPopover) {
    modelStatsPopover = document.createElement("aside");
    modelStatsPopover.className = "model-stats-popover";
    modelStatsPopover.setAttribute("role", "tooltip");
    modelStatsPopover.addEventListener("mouseenter", () => clearTimeout(modelStatsHideTimer));
    modelStatsPopover.addEventListener("mouseleave", hideModelStatsPopover);
    document.body.appendChild(modelStatsPopover);
  }
  const metrics = itemEl._modelPerformance || { loading: true };
  const value = (number, digits, unit = "s") => number == null ? "—" : `${(number / (unit === "s" ? 1000 : 1)).toFixed(digits)} ${unit}`;
  const stats = metrics.loading ? null : [
    ["Chargement", value(metrics.load, 1)],
    ["TTFT", value(metrics.ttft, 2)],
    ["Vitesse", value(metrics.speed, 1, "t/s")],
  ];
  modelStatsPopover.innerHTML = `<div class="model-stats-grid">${(stats || [["Chargement", "…"], ["TTFT", "…"], ["Vitesse", "…"]]).map(([label, result]) => `<div><span>${label}</span><strong>${result}</strong></div>`).join("")}</div>`;
  modelStatsPopover.hidden = false;
  const rect = itemEl.getBoundingClientRect();
  const popoverWidth = Math.min(260, window.innerWidth - 24);
  const gap = 10;
  const placeRight = rect.right + gap + popoverWidth <= window.innerWidth - 12 || rect.left < popoverWidth + gap + 12;
  const left = placeRight
    ? Math.min(window.innerWidth - popoverWidth - 12, rect.right + gap)
    : Math.max(12, rect.left - popoverWidth - gap);
  modelStatsPopover.style.left = `${left}px`;
  modelStatsPopover.style.top = `${Math.max(12, Math.min(rect.top + rect.height / 2 - modelStatsPopover.offsetHeight / 2, window.innerHeight - modelStatsPopover.offsetHeight - 12))}px`;
}

function scheduleHideModelStatsPopover() {
  clearTimeout(modelStatsHideTimer);
  modelStatsHideTimer = setTimeout(hideModelStatsPopover, 140);
}

function updateModelUI(activeModelName, installedModels, remoteModels = null) {
  if (activeModelName) {
    currentActiveModel = activeModelName;
  }
  if (installedModels && Array.isArray(installedModels)) {
    availableInstalledModels = installedModels;
  }
  if (remoteModels && Array.isArray(remoteModels)) remoteApiModels = remoteModels;
  const selectableModels = [...availableInstalledModels, ...remoteApiModels];
  const providerLogoIds = new Set(["openai", "anthropic", "gemini", "mistral", "openrouter", "groq", "together", "deepseek", "xai", "cerebras", "fireworks", "perplexity", "nvidia", "sambanova", "deepinfra", "github-models"]);
  const displayedModel = getActiveProfile().model || currentActiveModel;

  const activeNameEl = document.getElementById("active-model-name");
  const listEl = document.getElementById("model-dropdown-list");

  // Déterminer le modèle d'affichage et ses métadonnées
  let currentDisplay = displayedModel || "Ministral 3B";

  const matched = displayedModel && selectableModels.find(
    (m) =>
      (m.id && m.id === displayedModel) ||
      (m.filename && m.filename.toLowerCase().includes((displayedModel || "").toLowerCase())) ||
      (m.name && m.name.toLowerCase().includes((displayedModel || "").toLowerCase()))
  );

  if (matched) {
    currentDisplay = matched.source === "api" ? `${matched.name} · ${matched.provider_label}` : (matched.name || matched.filename.replace(".gguf", ""));
  } else if (displayedModel) {
    currentDisplay = displayedModel.replace(/\.gguf$/i, "").replace(/[-_]/g, " ");
  }

  if (activeNameEl) activeNameEl.textContent = currentDisplay;

  // Remplir le menu déroulant
  if (listEl) {
    listEl.innerHTML = "";
    if (selectableModels.length === 0 && installedNativeModels.length === 0) {
      listEl.innerHTML = '<div class="model-dropdown-empty">Aucun modèle installé. Utilisez « Gérer les modèles » ci-dessous.</div>';
    } else {
      selectableModels.forEach((item) => {
        const itemStem = item.source === "api" ? item.id : (item.filename ? item.filename.replace(/\.gguf$/i, "") : item.id);
        const isActive =
          displayedModel &&
          (displayedModel.toLowerCase() === itemStem.toLowerCase() ||
            (item.filename && item.filename.toLowerCase() === displayedModel.toLowerCase()));

        const itemEl = document.createElement("button");
        itemEl.type = "button";
        itemEl.className = `model-dropdown-item ${isActive ? "active" : ""}`;
        itemEl.setAttribute("role", "option");
        itemEl.setAttribute("aria-selected", String(Boolean(isActive)));
        itemEl.tabIndex = 0;
        itemEl.dataset.modelName = item.name || itemStem;
        itemEl.dataset.search = [item.name, itemStem, item.filename, item.id, item.model, item.provider_label, item.quant]
          .filter(Boolean).join(" ").toLocaleLowerCase();
        
        let badgesHtml = "";
        if (item.source === "api") {
          const providerId = providerLogoIds.has(item.provider) ? item.provider : "";
          const providerName = item.provider_label || item.provider || "Fournisseur";
          const colorProviderIcons = { mistral: "download-mistral-color", openai: "download-openai", anthropic: "download-claude-color" };
          const providerIcon = colorProviderIcons[providerId] || providerId;
          const providerImage = providerId ? `<img src="/static/provider-icons/${escapeHtml(providerIcon)}.svg" alt="" aria-hidden="true">` : "";
          badgesHtml = `<span class="model-provider-logo" data-provider="${escapeHtml(providerId)}" title="${escapeHtml(providerName)}" aria-label="Fournisseur : ${escapeHtml(providerName)}">${providerImage}<span>${escapeHtml(providerName.slice(0, 2).toUpperCase())}</span></span>`;
        } else {
          const compatibility = item.compatibility || {};
          const supported = [];
          if (compatibility.has_gpu_acceleration) supported.push({ id: "gpu", label: "GPU" });
          if (compatibility.npu_supported || item.npu_supported || compatibility.has_npu_acceleration) {
            supported.push({ id: "npu", label: "NPU OpenVINO validé" });
          } else if (isModelNpuCandidate(item)) {
            supported.push({ id: "npu", label: "NPU candidat, non validé" });
          }
          if (!supported.length) supported.push({ id: "cpu", label: "CPU" });
          badgesHtml = supported.map(({ id, label }) =>
            `<span class="model-hardware-badge ${id}" title="${label}" aria-label="${label}">${window.ETUSVG?.icon(id) || ""}</span>`
          ).join("");
        }

        const metaLabel = getModelParameterCount(item) || "";
        itemEl.setAttribute("aria-label", `${item.name || itemStem}${metaLabel ? ` · ${metaLabel}` : ""}. Clic droit pour ouvrir la fiche du modèle.`);

        itemEl.innerHTML = `
          <div class="model-dropdown-item-left">
            <span class="model-item-check">${isActive ? "✓" : ""}</span>
            <div class="model-item-details">
              <span class="model-item-title">${escapeHtml(item.name || itemStem)}</span>
              <span class="model-item-meta">${escapeHtml(metaLabel)}</span>
            </div>
          </div>
          <div class="model-dropdown-item-right">
            ${badgesHtml}
          </div>
        `;

        itemEl.addEventListener("click", () => {
          hideModelStatsPopover();
          const menu = document.getElementById("model-dropdown-menu");
          const btnHeaderSelector = document.getElementById("btn-model-selector");
          if (menu) menu.classList.remove("active");
          if (btnHeaderSelector) {
            btnHeaderSelector.classList.remove("open");
            btnHeaderSelector.setAttribute("aria-expanded", "false");
          }
          const displayName = item.source === "api" ? `${item.name} · ${item.provider_label}` : (item.name || itemStem);
          switchActiveModel(item.source === "api" ? item.model : (item.filename || itemStem), displayName, item.source || "local", item.provider);
        });
        itemEl.addEventListener("contextmenu", (event) => {
          event.preventDefault();
          document.getElementById("model-dropdown-menu")?.classList.remove("active");
          const selector = document.getElementById("btn-model-selector");
          selector?.classList.remove("open");
          selector?.setAttribute("aria-expanded", "false");
          void openModelIdentity(item, itemStem);
        });

        listEl.appendChild(itemEl);
        itemEl._modelPerformance = { loading: true };
        itemEl.addEventListener("mouseenter", () => showModelStatsPopover(itemEl));
        itemEl.addEventListener("mouseleave", scheduleHideModelStatsPopover);
        itemEl.addEventListener("focus", () => showModelStatsPopover(itemEl));
        itemEl.addEventListener("blur", scheduleHideModelStatsPopover);
        const query = new URLSearchParams({ model_id: String(item.id || itemStem), model_name: String(item.name || itemStem), limit: "100" });
        fetch(`/api/models/performance?${query}`).then((response) => response.ok ? response.json() : null).then((data) => {
          if (!data) { itemEl._modelPerformance = { loading: false, load: null, ttft: null }; return; }
          const measurements = Array.isArray(data.measurements) ? data.measurements : [];
          const median = (values) => {
            const sorted = values.map(Number).filter((value) => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
            if (!sorted.length) return null;
            const middle = Math.floor(sorted.length / 2);
            return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
          };
          const load = median(measurements.filter((entry) => entry.load_ms != null).map((entry) => entry.load_ms));
          const ttft = median(measurements.filter((entry) => entry.ttft_ms != null).map((entry) => entry.ttft_ms));
          const rates = measurements.map((entry) => Number(entry.tokens_per_second)).filter((rate) => Number.isFinite(rate) && rate > 0);
          const speed = rates.length ? rates.reduce((sum, rate) => sum + rate, 0) / rates.length : null;
          itemEl._modelPerformance = { loading: false, load, ttft, speed };
          if (modelStatsPopover && !modelStatsPopover.hidden && itemEl.matches(":hover, :focus")) showModelStatsPopover(itemEl);
        }).catch(() => { itemEl._modelPerformance = { loading: false, load: null, ttft: null }; });
      });
    }
    installedNativeModels.forEach((item) => {
      const itemEl = document.createElement("button");
      const displayName = item.repo?.split("/").pop() || item.filename || "Modèle NPU";
      itemEl.type = "button";
      itemEl.className = "model-dropdown-item model-dropdown-item-unavailable";
      itemEl.disabled = true;
      itemEl.title = "Ce format est installé, mais le moteur Beacon actuel ne permet pas encore de l’exécuter.";
      itemEl.setAttribute("role", "option");
      itemEl.setAttribute("aria-selected", "false");
      itemEl.dataset.modelName = displayName;
      itemEl.dataset.search = [displayName, item.repo, item.filename, item.format, item.quant, "npu"].filter(Boolean).join(" ").toLocaleLowerCase();
      itemEl.innerHTML = `
        <div class="model-dropdown-item-left">
          <span class="model-item-check" aria-hidden="true">${window.ETUSVG?.icon("npu") || ""}</span>
          <div class="model-item-details">
            <span class="model-item-title">${escapeHtml(displayName)}</span>
            <span class="model-item-meta">${escapeHtml(`${item.quant || "Précision non indiquée"} · ${String(item.format || "modèle natif").toUpperCase()} · moteur Beacon non disponible`)}</span>
          </div>
        </div>
        <span class="model-native-unavailable-label">Non exécutable</span>`;
      listEl.appendChild(itemEl);
    });
    filterModelDropdownList();
  }
}

function getModelParameterCount(item) {
  const candidates = [item?.name, item?.model, item?.filename, item?.id].filter(Boolean);
  for (const candidate of candidates) {
    const match = String(candidate).match(/\b(\d+(?:[.,]\d+)?\s?B)\b/i);
    if (match) return match[1].replace(/\s+/g, "");
  }
  return "";
}

function renderModelHardware(snapshot, compatibility, isRemote = false) {
  const hardware = snapshot || {};
  const hasGpu = Array.isArray(hardware.gpus) && hardware.gpus.length > 0;
  const hasNpu = Array.isArray(hardware.npus) && hardware.npus.length > 0;
  const rows = [
    ["cpu", "Processeur", hardware.cpu ? "Détecté" : "Non détecté", isRemote ? "Non utilisé (API)" : "Pris en charge"],
    ["gpu", "Carte graphique", hasGpu ? "Détectée" : "Non détectée", isRemote ? "Non utilisé (API)" : (compatibility?.has_gpu_acceleration ? "Pris en charge" : (hasGpu ? "Non pris en charge ici" : ""))],
    ["npu", "Puce IA", hasNpu ? "Détectée" : "Non détectée", isRemote ? "Non utilisé (API)" : (compatibility?.has_npu_acceleration ? "Pris en charge" : (hasNpu ? "Non pris en charge ici" : ""))],
  ];
  return rows.map(([icon, label, value, support]) => `
    <div class="model-hardware-item">
      <span class="model-hardware-icon">${window.ETUSVG?.icon(icon, "") || ""}</span>
      <span><small>${label}</small><strong>${escapeHtml(value || "Non détecté")}</strong>${support ? `<em>${escapeHtml(support)}</em>` : ""}</span>
    </div>
  `).join("");
}

async function openModelIdentity(item, itemStem) {
  const dialog = document.getElementById("modal-model-identity");
  const title = document.getElementById("model-identity-title");
  const summary = document.getElementById("model-identity-summary");
  const hardware = document.getElementById("model-identity-hardware");
  const performance = document.getElementById("model-identity-performance");
  const fit = document.getElementById("model-identity-fit");
  const uninstall = document.getElementById("btn-model-identity-uninstall");
  const hardwareSection = document.getElementById("model-identity-hardware-section");
  const displayName = item.name || itemStem;
  if (!dialog || !title || !summary || !hardware || !performance || !fit || !uninstall) return;

  title.textContent = displayName;
  const description = String(item.description || "").split(/(?<=[.!?])\s/)[0].slice(0, 180);
  const isRemote = item.source === "api";
  const source = isRemote ? "API" : "Modèle local";
  if (hardwareSection) hardwareSection.hidden = isRemote;
  summary.innerHTML = `
    ${description ? `<p>${escapeHtml(description)}</p>` : ""}
    <div class="model-identity-tags">
      <span>${source}</span>
    </div>
  `;
  let compatibility = item.compatibility || null;
  if (!isRemote && !compatibility) {
    try {
      const catalogResponse = await fetch("/api/models/catalog");
      if (catalogResponse.ok) {
        const catalogData = await catalogResponse.json();
        const catalogItem = (catalogData.catalog || []).find((candidate) => candidate.filename === item.filename || candidate.id === item.id);
        compatibility = catalogItem?.compatibility || null;
      }
    } catch (error) {
      // Keep the identity sheet usable when compatibility data is unavailable.
    }
  }
  fit.textContent = isRemote
    ? "Ce modèle fonctionne en ligne ; le matériel de cet ordinateur n’est pas utilisé."
    : compatibility?.status_tier === "incompatible" || compatibility?.is_compatible === false
      ? "Ce modèle risque d’être trop lourd pour cet ordinateur."
      : compatibility?.status_tier === "tight" && Number(currentHardwareSnapshot?.ram_available_gib) > 0 && Number(compatibility.required_ram_gib) > Number(currentHardwareSnapshot.ram_available_gib)
        ? "Libérez un peu de mémoire avant de lancer ce modèle."
      : compatibility?.status_tier === "tight"
        ? "Ce modèle devrait fonctionner, avec peu de marge."
        : compatibility?.is_compatible
          ? "Ce modèle est adapté à cet ordinateur."
          : "Compatibilité avec cet ordinateur non évaluée.";
  hardware.innerHTML = renderModelHardware(currentHardwareSnapshot, compatibility, isRemote);
  const canUninstall = !isRemote && item.is_installed === true && Boolean(item.filename);
  uninstall.hidden = !canUninstall;
  uninstall.onclick = canUninstall ? async () => {
    if (!window.confirm(`Désinstaller « ${displayName} » de cet ordinateur ?`)) return;
    uninstall.disabled = true;
    try {
      const response = await fetch(`/api/models/${encodeURIComponent(item.filename)}`, { method: "DELETE" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "La désinstallation a échoué.");
      closeModal("modal-model-identity");
      showToast("Modèle désinstallé.", "success");
      await loadServerStatus();
    } catch (error) {
      showToast(error.message || "La désinstallation a échoué.", "warning");
    } finally {
      uninstall.disabled = false;
    }
  } : null;
  performance.innerHTML = '<p class="model-identity-loading">Chargement des mesures locales…</p>';
  openModal("modal-model-identity");

  try {
    const query = new URLSearchParams({ model_id: String(item.id || itemStem), model_name: String(displayName), limit: "100" });
    const response = await fetch(`/api/models/performance?${query}`);
    if (!response.ok) throw new Error("Lecture des mesures impossible");
    const data = await response.json();
    const measurements = Array.isArray(data.measurements) ? data.measurements : [];
    const median = (values) => {
      const sorted = values.map(Number).filter((value) => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
      if (!sorted.length) return null;
      const middle = Math.floor(sorted.length / 2);
      return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
    };
    const validRates = measurements.map((entry) => Number(entry.tokens_per_second)).filter((rate) => Number.isFinite(rate) && rate > 0);
    const average = validRates.length ? validRates.reduce((sum, rate) => sum + rate, 0) / validRates.length : null;
    const medianTtft = median(measurements.filter((entry) => entry.ttft_ms != null).map((entry) => entry.ttft_ms));
    const medianLoad = median(measurements.filter((entry) => entry.load_ms != null).map((entry) => entry.load_ms));
    const hasMetrics = medianTtft !== null || medianLoad !== null || average !== null;
    const summaryStats = `<div class="model-performance-summary">
      <div class="model-performance-stat"><span>Temps de chargement</span><strong>${medianLoad === null ? "—" : `${(medianLoad / 1000).toFixed(1)} s`}</strong><small>Médiane · ${measurements.filter((entry) => entry.load_ms != null).length} activations</small></div>
      <div class="model-performance-stat"><span>TTFT · première réponse</span><strong>${medianTtft === null ? "—" : `${(medianTtft / 1000).toFixed(2)} s`}</strong><small>Médiane · ${measurements.filter((entry) => entry.ttft_ms != null).length} réponses</small></div>
      <div class="model-performance-stat"><span>Vitesse de génération</span><strong>${average === null ? "—" : `${average.toFixed(1)} tokens/s`}</strong><small>Moyenne · ${validRates.length} réponses</small></div>
    </div>`;
    performance.innerHTML = hasMetrics
      ? summaryStats
      : `<p class="model-identity-empty">${window.ETUSVG?.icon("clock", "model-empty-clock") || ""}<span>Pas encore de mesure pour ce modèle.</span></p>`;
  } catch (error) {
    performance.innerHTML = '<p class="model-identity-empty">Aucune mesure disponible pour le moment.</p>';
  }
}

function filterModelDropdownList() {
  const list = document.getElementById("model-dropdown-list");
  const search = document.getElementById("model-dropdown-search");
  if (!list || !search) return;
  const query = search.value.trim().toLocaleLowerCase();
  const items = [...list.querySelectorAll(".model-dropdown-item")];
  let visibleCount = 0;
  items.forEach((item) => {
    const matches = !query || item.dataset.search.includes(query);
    item.hidden = !matches;
    if (matches) visibleCount++;
  });
  let empty = list.querySelector(".model-dropdown-no-results");
  if (!empty) {
    empty = document.createElement("div");
    empty.className = "model-dropdown-no-results";
    empty.textContent = "Aucun modèle ne correspond à cette recherche.";
    list.appendChild(empty);
  }
  empty.hidden = !query || visibleCount > 0 || items.length === 0;
}

async function switchActiveModel(modelFilename, displayName = null, source = "local", provider = null) {
  const cleanStem = source === "api" ? modelFilename : modelFilename.replace(/\.gguf$/i, "");
  const friendlyName = displayName || cleanStem;

  const indicator = document.getElementById("model-status-indicator");
  if (indicator) indicator.classList.add("loading");

  showToast(`Sélection de <strong>${escapeHtml(friendlyName)}</strong>…`, "info", 2000);

  try {
    const resp = await fetch("/api/models/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: cleanStem, source, provider }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Activation refusée");

    if (data.success) {
      currentActiveModel = data.active_model_id || data.active_model || cleanStem;
      if (source === "local") localModelHasResponded = false;
      updateModelUI(currentActiveModel, availableInstalledModels);
      if (source === "local") {
        currentModelReady = false;
        currentModelState = "selected";
        showToast(`<strong>${escapeHtml(friendlyName)}</strong> sélectionné. Il sera chargé à votre prochain message.`, "success", 4000);
      } else {
        loadServerStatus();
        showToast(`<strong>${escapeHtml(friendlyName)}</strong> sélectionné pour la prochaine demande.`, "success", 3500);
      }
    } else {
      showToast(`⚠️ Avertissement : ${data.error || "Impossible de basculer"}`, "warning", 4000);
    }
  } catch (err) {
    showToast(`❌ Erreur de basculement : ${err}`, "warning", 4000);
  } finally {
    if (indicator) indicator.classList.remove("loading");
  }
}

/* -------------------------------------------------------------------------- */
/* Statut Serveur & Détection                                                 */
/* -------------------------------------------------------------------------- */
async function loadServerStatus() {
  try {
    const resp = await fetch("/api/status");
    const data = await resp.json();
    const backendText = document.getElementById("backend-text");
    const pairingText = document.getElementById("pairing-text");
    const beaconText = document.getElementById("beacon-text");
    const beaconDot = document.getElementById("beacon-dot");
    const modelIndicator = document.getElementById("model-status-indicator");
    currentContextLimit = Number(data.active_context) || null;
    currentHardwareSnapshot = {
      cpu: data.cpu_processor || (data.cpu_cores ? `${data.cpu_cores} cœurs` : ""),
      gpus: (data.gpus || []).map((device) => device.name || device.type?.toUpperCase()).filter(Boolean),
      npus: (data.npus || []).map((device) => device.name || device.type?.toUpperCase()).filter(Boolean),
      backend: data.active_engine || data.backend || "",
      ram_total_gib: data.ram_total_gib || null,
      ram_available_gib: data.ram_available_gib || null,
    };
    currentModelReady = Boolean(data.active_remote_model) || Boolean(data.model_ready);
    currentModelState = data.active_remote_model ? "running" : (data.model_state || "unknown");
    if (modelIndicator) {
      modelIndicator.style.background = data.active_remote_model ? "var(--accent-color)" : (data.model_ready ? "var(--success)" : "var(--warning)");
      modelIndicator.title = data.active_remote_model ? "Modèle API sélectionné" : (data.model_ready ? "Modèle prêt" : (data.model_state === "sleeping" ? "Modèle en veille" : "Modèle non prêt"));
    }

    if (backendText) backendText.textContent = data.active_engine
      ? `Moteur actif : ${String(data.active_engine).toUpperCase()}`
      : `Moteur conseillé : ${(data.backend || "CPU").toUpperCase()}`;

    if (beaconText && beaconDot) {
      if (data.model_ready) {
        beaconText.textContent = "Modèle local : Prêt";
        beaconDot.style.background = "var(--success)";
      } else if (data.model_state === "sleeping") {
        beaconText.textContent = "Modèle local : En veille";
        beaconDot.style.background = "var(--accent-color)";
      } else if (data.beacon_online) {
        beaconText.textContent = "Modèle local : Chargement";
        beaconDot.style.background = "var(--warning)";
      } else {
        beaconText.textContent = "Beacon : Inactif";
        beaconDot.style.background = "var(--warning)";
      }
    }

    if (pairingText) {
      pairingText.textContent = "Etu2 : à venir";
      pairingText.style.color = "var(--text-muted)";
    }

    // Mise à jour du sélecteur de modèle
    if (data.installed_models) {
      availableInstalledModels = data.installed_models;
    }
    if (data.remote_models) remoteApiModels = data.remote_models;
    currentActiveModel = data.active_remote_model?.id || data.active_model || null;
    updateModelUI(currentActiveModel, availableInstalledModels, remoteApiModels);
  } catch (err) {
    console.warn("Serveur de status non joignable :", err);
  }
}

/* -------------------------------------------------------------------------- */
/* Gestion des Conversations                                                  */
/* -------------------------------------------------------------------------- */
let conversationHistory = [];

function renderConversationHistory() {
  const listEl = document.getElementById("conv-list");
  if (!listEl) return;
  const search = document.getElementById("history-search").value.trim().toLocaleLowerCase("fr");
  const matches = conversationHistory.filter(conv => (conv.title || "").toLocaleLowerCase("fr").includes(search));
  document.getElementById("history-count").textContent = String(conversationHistory.length);
  listEl.replaceChildren();
  if (!matches.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = search ? "Aucune discussion trouvée" : "Vos discussions apparaîtront ici";
    listEl.appendChild(empty);
    return;
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  let previousGroup = "";
  for (const conv of matches) {
    const updated = new Date(conv.updated_at);
    const age = Number.isNaN(updated.getTime()) ? Infinity : (today - updated) / 86400000;
    const group = age <= 0 ? "Aujourd’hui" : age < 7 ? "7 derniers jours" : "Plus ancien";
    if (group !== previousGroup) {
      const heading = document.createElement("div");
      heading.className = "history-group";
      heading.textContent = group;
      listEl.appendChild(heading);
      previousGroup = group;
    }
    const item = document.createElement("div");
    item.className = "conv-item" + (conv.id === currentConversationId ? " active" : "");
    const title = document.createElement("button");
    title.type = "button";
    title.className = "conv-title";
    title.textContent = conv.title || "Nouvelle discussion";
    title.title = title.textContent;
    title.addEventListener("click", () => {
      selectConversation(conv.id);
      document.getElementById("btn-close-sidebar").click();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "delete-btn";
    remove.setAttribute("aria-label", `Supprimer ${title.textContent}`);
    remove.title = "Supprimer cette discussion";
    remove.textContent = "×";
    remove.addEventListener("click", () => deleteConversation(conv.id));
    item.append(title, remove);
    listEl.appendChild(item);
  }
}

async function loadConversations() {
  try {
    const resp = await fetch("/api/conversations");
    const data = await resp.json();
    conversationHistory = data.conversations || [];
    renderConversationHistory();
  } catch (err) {
    console.error("Erreur chargement conversations :", err);
  }
}

async function selectConversation(convId) {
  closeSettingsView({ syncRoute: false });
  closeDownloadsView({ fromRoute: true });
  setAppRoute(`#/chat/${encodeURIComponent(convId)}`);
  cancelChatStream();
  const viewVersion = chatViewVersion;
  currentConversationId = convId;
  loadConversations();
  try {
    if (!assistantProfiles.length) await loadProfiles();
    const resp = await fetch(`/api/conversations/${convId}/messages`);
    const data = await resp.json();
    if (viewVersion !== chatViewVersion || currentConversationId !== convId) return;
    const profileId = data.conversation.profile_id || "default";
    setDisplayedProfile(profileId);
    const container = document.getElementById("chat-messages");
    if (!container) return;
    ETUChatUI.reset();
    container.innerHTML = "";

    data.messages.forEach((msg) => {
      renderMessage(msg.role, msg.content, msg.attachments || [], msg.id, msg.tool_calls || []);
    });
    window.ETUWiki?.restoreEvents(convId, container);
    dismissViewedResponseNotifications();
  } catch (err) {
    console.error("Erreur affichage messages :", err);
  }
}

function startNewConversation() {
  closeSettingsView({ syncRoute: false });
  closeDownloadsView({ fromRoute: true });
  setAppRoute("#/chat");
  cancelChatStream();
  document.getElementById("chat-input").value = "";
  pendingChatFiles = [];
  renderAttachments(document.getElementById("chat-attachments"), [], true);
  document.getElementById("attachment-error").textContent = "";
  ETUChatUI.reset();
  currentConversationId = null;
  setDisplayedProfile(preferredProfileId);
  const container = document.getElementById("chat-messages");
  if (container) {
    container.innerHTML = '<div class="chat-welcome"><div class="welcome-orb" aria-hidden="true"><img src="/static/logo_etu_ai.svg" alt=""></div><h2>Comment puis-je vous aider ?</h2><p>Un assistant local pour apprendre, coder et explorer vos idées.</p><div class="welcome-suggestions"><button type="button" data-prompt="Explique-moi la récursivité avec un exemple simple.">Comprendre un concept</button><button type="button" data-prompt="Prépare un quiz interactif sur SQL.">Créer un quiz</button><button type="button" data-prompt="Aide-moi à relire mon devoir.">Relire un devoir</button></div></div>';
  }
  resizeChatInput();
  loadConversations();
}

async function deleteConversation(convId) {
  if (!confirm("Voulez-vous supprimer cette discussion ?")) return;
  await fetch(`/api/conversations/${convId}`, { method: "DELETE" });
  if (currentConversationId === convId) {
    startNewConversation();
  } else {
    loadConversations();
  }
}

/* -------------------------------------------------------------------------- */
/* Chat & Streaming SSE avec Generative UI                                    */
/* -------------------------------------------------------------------------- */
let pendingChatFiles = [];
let pendingEditedMessage = null;
let webSearchEnabled = localStorage.getItem("etu-web-search-enabled") === "true";
let webSearchAvailable = false;

function updateWebSearchUI() {
  const searchButton = document.getElementById("btn-web-search");
  const chip = document.getElementById("web-search-chip");
  const consent = document.getElementById("web-search-consent");
  const status = document.getElementById("web-search-status");
  const consentStatus = document.getElementById("web-search-consent-status");
  if (searchButton) {
    searchButton.disabled = !webSearchAvailable;
    searchButton.setAttribute("aria-checked", String(webSearchEnabled));
  }
  if (chip) {
    chip.hidden = !webSearchEnabled;
    chip.querySelector(".web-search-chip-remove")?.setAttribute("aria-label", "Désactiver la recherche Web");
  }
  if (consent) {
    consent.checked = webSearchEnabled;
    consent.disabled = !webSearchAvailable;
  }
  document.getElementById("btn-attach")?.classList.toggle("search-enabled", webSearchEnabled);
  if (status) status.textContent = !webSearchAvailable ? "DDGS indisponible" : webSearchEnabled ? "Activée" : "Désactivée dans Mes données";
  if (consentStatus) consentStatus.textContent = !webSearchAvailable
    ? "Installez requirements-app.txt et redémarrez ETU-AI pour rendre DDGS disponible."
    : webSearchEnabled ? "Le modèle peut appeler la recherche web quand il le juge utile." : "La recherche web est désactivée.";
}

function setWebSearchEnabled(enabled) {
  webSearchEnabled = Boolean(enabled && webSearchAvailable);
  localStorage.setItem("etu-web-search-enabled", String(webSearchEnabled));
  updateWebSearchUI();
}

function setChatAttachMenu(open, trigger = document.getElementById("btn-attach")) {
  const menu = document.getElementById("attach-dropdown-menu");
  const home = document.getElementById("attach-menu-home");
  const bottomTrigger = document.getElementById("btn-attach");
  if (!menu || !home) return;
  if (open && trigger?.classList.contains("message-edit-attach")) trigger.closest(".attach-menu-wrapper")?.append(menu);
  else home.append(menu);
  menu.hidden = !open;
  bottomTrigger?.setAttribute("aria-expanded", String(open && trigger === bottomTrigger));
  document.querySelectorAll(".message-edit-attach").forEach((button) => button.setAttribute("aria-expanded", String(open && trigger === button)));
}

function renderAttachments(container, attachments, removable = false) {
  container.replaceChildren();
  attachments.forEach((file, index) => {
    const chip = document.createElement("span");
    chip.className = "attachment-chip";
    if ((file.kind === "image" && file.data) || (file instanceof File && file.type.startsWith("image/"))) {
      const image = document.createElement("img");
      if (file instanceof File) {
        const previewUrl = URL.createObjectURL(file);
        image.onload = image.onerror = () => URL.revokeObjectURL(previewUrl);
        image.src = previewUrl;
      } else {
        image.src = `data:image/jpeg;base64,${file.data}`;
      }
      image.alt = file.name;
      chip.appendChild(image);
    }
    const label = document.createElement("span");
    label.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} Ko`;
    chip.appendChild(label);
    if (file.warning) chip.title = file.warning;
    if (removable) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Retirer ${file.name}`);
      remove.addEventListener("click", () => {
        if (document.getElementById("chat-form").dataset.busy === "true") return;
        pendingChatFiles.splice(index, 1);
        renderAttachments(container, pendingChatFiles, true);
      });
      chip.appendChild(remove);
    }
    container.appendChild(chip);
  });
}

async function addChatFiles(files) {
  if (document.getElementById("chat-form").dataset.busy === "true") return;
  const error = document.getElementById("attachment-error");
  const additions = Array.from(files);
  const total = [...pendingChatFiles, ...additions];
  error.textContent = "";
  if (total.length > 6 || additions.some((file) => !file.size || file.size > 10 * 1024 * 1024) || total.reduce((size, file) => size + file.size, 0) > 20 * 1024 * 1024) {
    error.textContent = "6 fichiers maximum, 10 Mo par fichier et 20 Mo au total. Les fichiers vides ne sont pas acceptés.";
    return;
  }
  pendingChatFiles.push(...additions);
  renderAttachments(document.getElementById("chat-attachments"), pendingChatFiles, true);
}

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, data: String(reader.result).split(",", 2)[1] });
    reader.onerror = () => reject(new Error(`Impossible de lire ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function initChatForm() {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  if (!form || !input) return;
  input.addEventListener("input", resizeChatInput);
  resizeChatInput();
  document.getElementById("btn-stop").addEventListener("click", cancelChatStream);
  const picker = document.getElementById("chat-file-input");
  const attachButton = document.getElementById("btn-attach");
  const attachMenu = document.getElementById("attach-dropdown-menu");
  const searchButton = document.getElementById("btn-web-search");
  attachButton.addEventListener("click", () => setChatAttachMenu(attachMenu.hidden, attachButton));
  document.getElementById("btn-attach-file").addEventListener("click", () => {
    setChatAttachMenu(false);
    picker.click();
  });
  searchButton.addEventListener("click", () => {
    if (!webSearchAvailable) return;
    setWebSearchEnabled(!webSearchEnabled);
    setChatAttachMenu(false);
  });
  document.querySelector(".web-search-chip-remove")?.addEventListener("click", (event) => {
    event.stopPropagation();
    setWebSearchEnabled(false);
  });
  document.getElementById("web-search-consent")?.addEventListener("change", (event) => setWebSearchEnabled(event.currentTarget.checked));
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".attach-menu-wrapper")) {
      setChatAttachMenu(false);
    }
  });
  fetch("/api/search/status").then((response) => response.ok ? response.json() : null).then((status) => {
    webSearchAvailable = Boolean(status?.configured);
    if (!webSearchAvailable) setWebSearchEnabled(false);
    searchButton.title = webSearchAvailable ? "Autoriser ou désactiver la recherche web" : "DDGS doit être installé pour utiliser la recherche web";
    updateWebSearchUI();
  }).catch(() => { webSearchAvailable = false; updateWebSearchUI(); });
  updateWebSearchUI();
  picker.addEventListener("change", () => {
    addChatFiles(picker.files);
    picker.value = "";
  });
  const dropZone = document.getElementById("chat-input-container");
  dropZone.addEventListener("dragover", (event) => {
    if (event.dataTransfer.types.includes("Files")) event.preventDefault();
  });
  dropZone.addEventListener("drop", (event) => {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    addChatFiles(event.dataTransfer.files);
  });
  input.addEventListener("paste", (event) => {
    if (event.clipboardData.files.length) {
      event.preventDefault();
      addChatFiles(event.clipboardData.files);
    }
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.dispatchEvent(new Event("submit"));
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const rawMessage = input.value.trim();
    if (!rawMessage && !pendingChatFiles.length) return;
    if (form.dataset.busy === "true") return;
    form.dataset.busy = "true";
    const editContext = pendingEditedMessage;
    pendingEditedMessage = null;
    document.getElementById("btn-send").disabled = true;
    document.getElementById("btn-attach").disabled = true;
    const submittedFiles = editContext ? [...(editContext.attachments || [])] : [...pendingChatFiles];
    let accepted = false;
    document.getElementById("attachment-error").textContent = "";
    input.value = "";
    resizeChatInput();

    // Commande slash pour changer de modèle : /model ou /models
    if (!editContext && !submittedFiles.length && (rawMessage.startsWith("/model") || rawMessage.startsWith("/switch"))) {
      handleModelSlashCommand(rawMessage);
      form.dataset.busy = "false";
      document.getElementById("btn-send").disabled = false;
      document.getElementById("btn-attach").disabled = false;
      return;
    }

    // 1. Afficher le message utilisateur
    document.querySelector("#chat-messages .chat-welcome")?.remove();
    const userBubble = editContext?.userBubble || renderMessage("user", rawMessage, submittedFiles);

    // 2. Créer la bulle assistant pour le streaming
    const assistantBubble = renderMessage("assistant", "");
    assistantBubble.classList.add("streaming");
    const bubbleContent = assistantBubble.querySelector(".message-bubble");
    const controller = new AbortController();
    const requestId = crypto.randomUUID();
    bubbleContent.dataset.requestId = requestId;
    const requestConversationId = currentConversationId;
    const requestViewVersion = chatViewVersion;
    activeChatStream = { controller, bubble: bubbleContent, requestId, accepted: false, rawMessage, userBubble };
    document.getElementById("btn-stop").hidden = false;
    const profileModel = getActiveProfile().model || currentActiveModel;
    const localModelMayNeedWarmup = !String(profileModel || "").startsWith("api:") && (
      !localModelHasResponded || currentModelReady !== true || ["sleeping", "loading", "offline", "unknown"].includes(currentModelState)
    );
    setAssistantActivity(
      assistantBubble,
      localModelMayNeedWarmup ? "model-loading" : "thinking",
      localModelMayNeedWarmup ? "Réveil du modèle local…" : "Réflexion en cours",
    );

    // 3. Appel au flux SSE avec le modèle sélectionné
    try {
      const encodedAttachments = await Promise.all(submittedFiles.map(fileBase64));
      if (appPowerState !== "awake") await wakeApp();
      bubbleContent.dataset.generationStarted = String(performance.now());
      bubbleContent.dataset.modelId = String(profileModel || "");
      bubbleContent.dataset.modelName = document.getElementById("active-model-name")?.textContent || profileModel || "";
      bubbleContent.dataset.modelSource = String(profileModel || "").startsWith("api:") ? "api" : "local";
      bubbleContent.dataset.hardwareProfile = JSON.stringify(currentHardwareSnapshot || {});
      const resp = await fetch("/api/chat/stream", {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: requestConversationId,
          message: rawMessage,
          model: profileModel,
          profile_id: activeProfileId,
          attachments: encodedAttachments,
          web_search: webSearchEnabled,
          request_id: requestId,
          edit_message_id: editContext?.messageId || null,
        }),
      });

      if (controller.signal.aborted || requestViewVersion !== chatViewVersion) return;

      if (!resp.ok) {
        const failure = await resp.json();
        throw new Error(typeof failure.detail === "string" ? failure.detail : "Fichiers ou message invalides");
      }
      accepted = true;
      updateWebSearchUI();
      if (activeChatStream?.controller === controller) activeChatStream.accepted = true;
      if (editContext && submittedFiles.length) {
        let messageFiles = userBubble.querySelector(".message-attachments");
        if (!messageFiles) {
          messageFiles = document.createElement("div");
          messageFiles.className = "attachment-list message-attachments";
          userBubble.querySelector(".message-bubble")?.append(messageFiles);
        }
        const additions = document.createElement("div");
        renderAttachments(additions, submittedFiles);
        messageFiles.append(...additions.children);
      }
      pendingChatFiles = [];
      renderAttachments(document.getElementById("chat-attachments"), pendingChatFiles, true);
      const warnings = JSON.parse(resp.headers.get("X-Attachment-Warnings") || "[]");
      if (warnings.length) document.getElementById("attachment-error").textContent = warnings.join(" ");

      const convHeader = resp.headers.get("X-Conversation-Id");
      bubbleContent.dataset.conversationId = convHeader || requestConversationId || currentConversationId || "";
      if (convHeader && !currentConversationId) {
        currentConversationId = convHeader;
        setAppRoute(`#/chat/${encodeURIComponent(convHeader)}`);
        loadConversations();
      }
      window.ETUWiki?.watchConversation(bubbleContent.dataset.conversationId);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (controller.signal.aborted || requestViewVersion !== chatViewVersion) return;
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n\r?\n/);
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.trim()) continue;
          const dataMatch = line.match(/^data:\s*(.*)$/m);
          if (dataMatch) {
            try {
              const eventData = JSON.parse(dataMatch[1]);
              handleStreamEvent(eventData, bubbleContent);
            } catch (err) {
              console.error("Erreur parsing SSE JSON :", err, line);
            }
          }
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        const dataMatch = buffer.match(/^data:\s*(.*)$/m);
        if (dataMatch) handleStreamEvent(JSON.parse(dataMatch[1]), bubbleContent);
      }
      if (bubbleContent.dataset.generationCompleted !== "true" && bubbleContent.dataset.streamError !== "true" && !(streamMarkup.get(bubbleContent) || "").trim()) {
        handleStreamEvent({ event: "error", message: "La connexion s’est interrompue avant la réponse du modèle. Réessayez." }, bubbleContent);
      }
      finishStreamRender(bubbleContent);
      if (assistantBubble.classList.contains("streaming")) {
        assistantBubble.classList.remove("streaming");
        finalizeAssistantBubble(bubbleContent);
      }
      setAssistantActivity(assistantBubble, "done", "");
    } catch (err) {
      if (controller.signal.aborted || requestViewVersion !== chatViewVersion) return;
      if (!accepted) {
        input.value = rawMessage;
        resizeChatInput();
        if (!editContext) userBubble.remove();
        document.getElementById("attachment-error").textContent = err.message;
      }
      finishStreamRender(bubbleContent);
      setAssistantActivity(assistantBubble, "error", "Erreur de réponse");
      assistantBubble.classList.remove("streaming");
      const error = document.createElement("p");
      error.className = "etu-text etu-badge-warning";
      error.textContent = `Erreur de connexion au serveur : ${err}`;
      bubbleContent.appendChild(error);
    } finally {
      bubbleContent.dataset.generationMs = String(Math.round(performance.now() - Number(bubbleContent.dataset.generationStarted)));
      updateAssistantTelemetry(bubbleContent, true);
      recordModelPerformance(bubbleContent);
      if (activeChatStream?.controller === controller) {
        activeChatStream = null;
        form.dataset.busy = "false";
        document.getElementById("btn-send").disabled = false;
        document.getElementById("btn-attach").disabled = false;
        document.getElementById("btn-stop").hidden = true;
        document.querySelectorAll(".message-edit-button").forEach(button => { button.disabled = false; });
      }
    }
  });
}

function handleModelSlashCommand(commandStr) {
  renderMessage("user", commandStr);
  const parts = commandStr.trim().split(/\s+/);

  if (parts.length === 1 || parts[1] === "list") {
    // Affiche la liste des modèles installés avec boutons d'activation directe
    let modelsListHtml = "";
    if (availableInstalledModels.length === 0) {
      modelsListHtml = "<p class='etu-text'>Aucun modèle n'est actuellement installé.</p>";
    } else {
      modelsListHtml = `<div class="model-switch-card">`;
      availableInstalledModels.forEach((m) => {
        const stem = m.filename ? m.filename.replace(/\.gguf$/i, "") : m.id;
        const isActive = currentActiveModel && currentActiveModel.toLowerCase() === stem.toLowerCase();
        const npuTag = isModelNpuCandidate(m) ? " ⚡ NPU" : "";
        const sizeTag = m.local_size_gib ? ` (${m.local_size_gib} Go)` : "";
        modelsListHtml += `
          <button type="button" class="model-switch-btn ${isActive ? "active" : ""}" data-model="${escapeHtml(m.filename || stem)}" data-name="${escapeHtml(m.name || stem)}">
            <span><strong>${escapeHtml(m.name || stem)}</strong>${sizeTag}${npuTag}</span>
            <span class="etu-badge ${isActive ? "etu-badge-success" : "etu-badge-info"}">${isActive ? "✓ Actif" : "Activer"}</span>
          </button>
        `;
      });
      modelsListHtml += `</div>`;
    }

    const cardHtml = `
      <div class="etu-card">
        <div class="etu-card-header">
          <span class="etu-badge etu-badge-info">Gestionnaire Rapide</span>
          <h4>Sélection du Modèle de Chat</h4>
        </div>
        <div class="etu-card-body">
          <p class="etu-text">Cliquez sur un modèle ci-dessous pour l'activer instantanément :</p>
          ${modelsListHtml}
        </div>
      </div>
    `;
    const msgEl = renderMessage("assistant", cardHtml);
    const modelControls = msgEl.querySelector(".authored-inline")?.shadowRoot || msgEl;
    modelControls.querySelectorAll(".model-switch-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mFile = btn.dataset.model;
        const mName = btn.dataset.name;
        switchActiveModel(mFile, mName);
      });
    });
  } else {
    // Recherche par mot clé : ex /model qwen
    const query = parts.slice(1).join(" ").toLowerCase();
    const matched = availableInstalledModels.find(
      (m) =>
        (m.name && m.name.toLowerCase().includes(query)) ||
        (m.filename && m.filename.toLowerCase().includes(query))
    );

    if (matched) {
      switchActiveModel(matched.filename, matched.name);
      renderMessage(
        "assistant",
        `<p class="etu-text">Basculement en cours vers le modèle <strong>${escapeHtml(matched.name || matched.filename)}</strong>.</p>`
      );
    } else {
      renderMessage(
        "assistant",
        `<p class="etu-text etu-badge-warning">Aucun modèle installé ne correspond à « ${escapeHtml(query)} ». Utilisez <code>/model</code> pour voir les modèles disponibles.</p>`
      );
    }
  }
}

function handleStreamEvent(data, bubbleEl) {
  if (bubbleEl.dataset.renderFinalized === "true") return;
  if (data.event === "user_message" && data.message_id) {
    const userMessage = bubbleEl.closest(".message")?.previousElementSibling;
    if (userMessage?.classList.contains("user")) {
      userMessage.dataset.messageId = data.message_id;
      addUserMessageActions(userMessage);
    }
  } else if (data.event === "wiki") {
    window.ETUWiki?.chatEvent(data, bubbleEl.closest(".message"));
  } else if (data.event === "token" && data.html) {
    if (!bubbleEl.dataset.firstTokenMs) bubbleEl.dataset.firstTokenMs = String(Math.round(performance.now() - Number(bubbleEl.dataset.generationStarted)));
    if (!String(currentActiveModel || "").startsWith("api:")) localModelHasResponded = true;
    const html = (streamMarkup.get(bubbleEl) || "") + data.html;
    streamMarkup.set(bubbleEl, html);
    setAssistantActivity(bubbleEl.closest(".message"), "writing", "Rédaction en cours");
    if (!streamFrames.has(bubbleEl)) {
      streamFrames.set(bubbleEl, setTimeout(() => {
        streamFrames.delete(bubbleEl);
        renderStreamMarkup(bubbleEl);
      }, 55));
    }
  } else if (data.event === "tool_start" && data.html) {
    if (data.tool) addMessageToolBadges(bubbleEl.closest(".message"), [data.tool]);
    setAssistantActivity(bubbleEl.closest(".message"), "tool", "Consultation en cours");
  } else if (data.event === "tool_result") {
    setAssistantActivity(bubbleEl.closest(".message"), "thinking", "Analyse du résultat");
  } else if (data.event === "metrics") {
    if (data.prompt_tokens !== null && data.prompt_tokens !== undefined && Number.isFinite(Number(data.prompt_tokens))) bubbleEl.dataset.promptTokens = String(data.prompt_tokens);
    if (data.completion_tokens !== null && data.completion_tokens !== undefined && Number.isFinite(Number(data.completion_tokens))) bubbleEl.dataset.completionTokens = String(data.completion_tokens);
    if (data.total_tokens !== null && data.total_tokens !== undefined && Number.isFinite(Number(data.total_tokens))) bubbleEl.dataset.totalTokens = String(data.total_tokens);
    if (data.generation_tps !== null && data.generation_tps !== undefined && Number.isFinite(Number(data.generation_tps))) bubbleEl.dataset.generationTps = String(data.generation_tps);
    updateAssistantTelemetry(bubbleEl, true);
  } else if (data.event === "done") {
    if (bubbleEl.dataset.generationCompleted !== "true" && bubbleEl.dataset.conversationId) {
      addAppNotification("Réponse terminée", "Le modèle a fini sa réponse.", "response", {
        conversationId: bubbleEl.dataset.conversationId,
        eventKey: `response:${bubbleEl.dataset.requestId}`,
      });
    }
    bubbleEl.dataset.generationCompleted = "true";
    finishStreamRender(bubbleEl);
    bubbleEl.closest(".message")?.classList.remove("streaming");
    setAssistantActivity(bubbleEl.closest(".message"), "done", "");
    finalizeAssistantBubble(bubbleEl);
    updateAssistantTelemetry(bubbleEl, true);
  } else if (data.event === "status") {
    const state = data.state === "loading_model" ? "model-loading" : "thinking";
    const activity = bubbleEl.closest(".message")?.querySelector(".assistant-activity");
    // Le serveur signale d'abord qu'il a reçu la requête : garder l'animation de
    // réveil jusqu'au premier token si la page a détecté un modèle froid.
    if (state === "model-loading" || activity?.dataset.state !== "model-loading") {
      setAssistantActivity(bubbleEl.closest(".message"), state, data.message || (state === "model-loading" ? "Réveil du modèle local…" : "Réflexion en cours"));
    }
  } else if (data.event === "error") {
    ETUChatUI.showConversation();
    finishStreamRender(bubbleEl);
    streamMarkup.delete(bubbleEl);
    bubbleEl.dataset.streamError = "true";
    bubbleEl.innerHTML = `<p class="etu-text etu-badge-warning">${escapeHtml(data.message || "La requête a échoué.")}</p>`;
  }
}

function setAssistantActivity(message, state, label) {
  if (!message) return;
  let activity = message.querySelector(".assistant-activity");
  if (state === "done") {
    activity?.remove();
    return;
  }
  if (!activity) {
    activity = document.createElement("div");
    activity.className = "assistant-activity";
    activity.setAttribute("role", "status");
    activity.innerHTML = '<span class="activity-orb" aria-hidden="true"></span><span class="activity-label"></span>';
    message.querySelector(".message-bubble").before(activity);
  }
  activity.dataset.state = state;
  activity.querySelector(".activity-label").textContent = label;
}

function renderStreamMarkup(bubble) {
  const raw = streamMarkup.get(bubble) || "";
  if (renderAuthoredPreview(bubble, raw, false)) {
    // The iframe receives the model's actual markup as it arrives.
  } else {
    let live = bubble.querySelector(".stream-live-text");
    if (!live) {
      live = document.createElement("div");
      live.className = "stream-live-text";
      bubble.appendChild(live);
    }
    live.replaceChildren(renderAssistantFragment(raw));
  }
  scrollToBottom();
}

function applyCompletedStreamTemplates(bubble, raw) {
  const matches = [];
  let start = 0;
  let depth = 0;
  for (const tag of raw.matchAll(/<\/?template\b[^>]*>/gi)) {
    if (!tag[0].startsWith("</")) {
      if (depth++ === 0) start = tag.index;
    } else if (depth > 0 && --depth === 0) {
      const markup = raw.slice(start, tag.index + tag[0].length);
      if (/^<template\b[^>]*\bfor=["'][\w/-]{1,100}["']/i.test(markup)) matches.push(markup);
    }
  }
  const applied = appliedStreamTemplates.get(bubble) || 0;
  for (const markup of matches.slice(applied)) {
    const parsed = document.createElement("template");
    parsed.innerHTML = markup;
    const template = parsed.content.querySelector("template[for]");
    ETUChatUI.patch(template.getAttribute("for"), template.innerHTML);
  }
  appliedStreamTemplates.set(bubble, matches.length);
}

function authoredDocument(raw) {
  const fence = /```(?:html|htm)\s*\n/i.exec(raw);
  if (fence) {
    const start = fence.index + fence[0].length;
    const end = raw.indexOf("```", start);
    return {
      html: raw.slice(start, end < 0 ? undefined : end), source: raw, complete: end >= 0,
      before: raw.slice(0, fence.index), after: end < 0 ? "" : raw.slice(end + 3),
    };
  }
  if (/^\s*(?:<!doctype|<html\b|<style\b|<template\b|<form\b|<section\b|<article\b|<main\b)/i.test(raw) ||
      /^\s*<div\b[^>]*\b(?:style|class|data-update-marker)=/i.test(raw) ||
      (/^\s*</.test(raw) && /<(?:form|style|script|template|svg)\b/i.test(raw))) {
    return { html: raw, source: raw, complete: true };
  }
  return null;
}

function renderAuthoredPreview(bubble, raw, complete) {
  const authored = authoredDocument(raw);
  if (!authored) return false;
  let content = bubble.querySelector(":scope > .authored-response-content");
  if (!content) {
    bubble.replaceChildren();
    const before = document.createElement("div");
    before.className = "authored-text-before";
    content = document.createElement("div");
    content.className = "authored-response-content";
    const after = document.createElement("div");
    after.className = "authored-text-after";
    bubble.append(before, content, after);
  }
  for (const position of ["before", "after"]) {
    const text = bubble.querySelector(`:scope > .authored-text-${position}`);
    text.replaceChildren(renderAssistantFragment(authored[position] || ""));
    text.hidden = !(authored[position] || "").trim();
  }
  // HTML/CSS et formulaires sont des éléments natifs de la conversation.
  // Seuls les programmes JavaScript ont besoin d'une origine isolée.
  let hasScript = false;
  if (/<script\b/i.test(authored.html)) {
    const inspection = document.createElement("template");
    inspection.innerHTML = authored.html;
    // Les scripts dans les templates appartiennent à la zone mise à jour,
    // pas à cette réponse : ils sont traités quand le patch est appliqué.
    hasScript = !!inspection.content.querySelector("script");
  }
  if (!hasScript || !complete) {
    const hasContent = ETUChatUI.render(content, authored.html, complete);
    if (complete && hasContent) ETUChatUI.showConversation();
    if (complete) applyCompletedStreamTemplates(bubble, authored.html);
    showAuthoredSource(bubble, authored.html);
    return true;
  }
  let frame = content.querySelector(".authored-preview-frame");
  if (complete) ETUChatUI.showConversation();
  if (!frame) {
    content.replaceChildren();
    frame = document.createElement("iframe");
    frame.className = "authored-preview-frame";
    frame.title = "Interface créée par l’IA";
    // allow-forms autorise l'événement submit ; la CSP interdit toute navigation.
    frame.setAttribute("sandbox", "allow-scripts allow-forms");
    frame.src = "/static/preview.html?v=chat-ui-6";
    frame.addEventListener("load", () => sendAuthoredPreview(frame));
    content.appendChild(frame);
  }
  frame.previewHtml = authored.html;
  frame.previewComplete = complete;
  sendAuthoredPreview(frame);
  applyCompletedStreamTemplates(bubble, authored.html);
  if (complete) {
    showAuthoredSource(bubble, authored.html);
  }
  return true;
}

function showAuthoredSource(bubble, html) {
    let details = bubble.querySelector(":scope > .authored-preview-source");
    if (!details) {
      details = document.createElement("details");
      details.className = "authored-preview-source";
      details.innerHTML = '<summary>Voir le code de l’interface</summary><pre><code></code></pre>';
      bubble.appendChild(details);
    }
    const code = details.querySelector("code");
    code.className = "language-html";
    code.textContent = html;
    highlightCode(details);
}

function sendAuthoredPreview(frame) {
  const theme = getComputedStyle(document.documentElement);
  frame.contentWindow?.postMessage({ type: "etu-preview-update", html: frame.previewHtml || "", complete: frame.previewComplete === true,
    colorScheme: document.documentElement.dataset.theme || "dark",
    theme: Object.fromEntries(["--text-main", "--text-muted", "--bg-primary", "--bg-secondary", "--bg-tertiary", "--border-color", "--accent-color"].map(name => [name, theme.getPropertyValue(name)])) }, "*");
  for (const patch of frame.previewPatches || [])
    frame.contentWindow?.postMessage({ type: "etu-preview-patch", ...patch }, "*");
}

window.addEventListener("message", async (event) => {
  const frame = [...document.querySelectorAll(".authored-preview-frame")]
    .find((candidate) => candidate.contentWindow === event.source);
  if (!frame || !event.data || typeof event.data !== "object") return;
  if (event.data.type === "etu-preview-ready") {
    sendAuthoredPreview(frame);
  } else if (event.data.type === "etu-preview-error") {
    let feedback = frame.parentElement.querySelector(".authored-preview-error");
    if (!feedback) {
      feedback = document.createElement("p");
      feedback.className = "authored-preview-error";
      feedback.setAttribute("role", "status");
      frame.after(feedback);
    }
    feedback.textContent = `Le code généré rencontre une erreur : ${String(event.data.message || "erreur inconnue").slice(0, 300)}`;
    let retry = frame.parentElement.querySelector(".authored-preview-repair");
    if (!retry) {
      retry = document.createElement("button");
      retry.className = "authored-preview-repair";
      retry.type = "button";
      retry.textContent = "Demander une correction";
      retry.addEventListener("click", () => {
        if (document.getElementById("btn-send").disabled) return;
        document.getElementById("chat-input").value = `Corrige l’interface de ta dernière réponse. ${feedback.textContent}. Renvoie une version complète et fonctionnelle dans un seul bloc html, avec sa propre logique JavaScript.`;
        document.getElementById("chat-form").requestSubmit();
      });
      feedback.after(retry);
    }
  } else if (event.data.type === "etu-preview-height") {
    const height = Number(event.data.height);
    if (Number.isFinite(height)) frame.style.height = `${Math.max(40, Math.min(4000, height))}px`;
  } else if (event.data.type === "etu-preview-copy" && typeof event.data.value === "string") {
    if (!navigator.userActivation.isActive) return;
    const value = event.data.value.slice(0, 500);
    try { await navigator.clipboard.writeText(value); }
    catch { /* Clipboard may require browser permission. */ }
  } else if (event.data.type === "etu-preview-action" && typeof event.data.value === "string") {
    if (!navigator.userActivation.isActive) return;
    submitGeneratedPrompt(event.data.value.slice(0, 10000));
  } else if (event.data.type === "etu-preview-form" && typeof event.data.description === "string" && Array.isArray(event.data.fields)) {
    if (!navigator.userActivation.isActive || event.data.fields.length > 100) return;
    const fields = event.data.fields.filter(field => Array.isArray(field) && field.length === 2 && typeof field[0] === "string" && ["string", "boolean"].includes(typeof field[1]));
    submitGeneratedPrompt(`[formulaire] ${event.data.description.slice(0, 500)}\n${JSON.stringify(fields).slice(0, 12000)}`);
  }
});

function submitGeneratedPrompt(value) {
  const form = document.getElementById("chat-form");
  if (form.dataset.busy === "true" || !value) return false;
  const input = document.getElementById("chat-input");
  const draft = input.value;
  input.value = String(value).slice(0, 14000);
  form.requestSubmit();
  input.value = draft;
  resizeChatInput();
  return true;
}

function finishStreamRender(bubble) {
  const frame = streamFrames.get(bubble);
  if (frame) clearTimeout(frame);
  streamFrames.delete(bubble);
  if (streamMarkup.has(bubble)) renderStreamMarkup(bubble);
}

function finalizeAssistantBubble(bubble) {
  if (bubble.dataset.streamError === "true" || bubble.dataset.renderFinalized === "true") return;
  const raw = streamMarkup.get(bubble) || "";
  if (!renderAuthoredPreview(bubble, raw, true)) {
    ETUChatUI.showConversation();
    bubble.replaceChildren(renderAssistantFragment(raw));
    applyPartialUpdates(bubble);
  }
  streamMarkup.delete(bubble);
  bubble.dataset.copyText = raw;
  bubble.dataset.renderFinalized = "true";
}

function updateAssistantTelemetry(bubble, completed = false) {
  const message = bubble.closest(".message");
  const panel = message?.querySelector(".assistant-telemetry-panel");
  if (!panel) return;

  if (!bubble.dataset.generationStarted) {
    const list = panel.querySelector(".assistant-telemetry-values");
    list.replaceChildren();
    for (const label of ["Temps total", "TTFT", "Tokens générés", "Vitesse", "Contexte utilisé"]) {
      const row = document.createElement("div");
      row.className = "assistant-telemetry-row";
      const key = document.createElement("dt");
      key.textContent = label;
      const val = document.createElement("dd");
      val.textContent = "Non enregistrée pour cette réponse";
      row.append(key, val);
      list.appendChild(row);
    }
    panel.querySelector(".assistant-context-progress").hidden = true;
    return;
  }

  const elapsedMs = Number(bubble.dataset.generationMs || (completed ? 0 : performance.now() - Number(bubble.dataset.generationStarted)));
  const firstTokenMs = Number(bubble.dataset.firstTokenMs || 0);
  const exactTokens = Number(bubble.dataset.completionTokens);
  const hasExactTokens = bubble.dataset.completionTokens !== undefined && Number.isFinite(exactTokens);
  const outputText = bubble.innerText || bubble.textContent || "";
  const estimatedTokens = outputText.trim() ? Math.max(1, Math.round(outputText.trim().length / 4)) : null;
  const generatedTokens = hasExactTokens ? exactTokens : estimatedTokens;
  const generationMs = Math.max(0, elapsedMs - firstTokenMs);
  const serverTokensPerSecond = Number(bubble.dataset.generationTps);
  const hasServerTokensPerSecond = bubble.dataset.generationTps !== undefined && Number.isFinite(serverTokensPerSecond) && serverTokensPerSecond > 0;
  const tokensPerSecond = hasServerTokensPerSecond ? serverTokensPerSecond : (generationMs > 0 && generatedTokens !== null && generatedTokens > 0 ? generatedTokens / (generationMs / 1000) : null);
  const promptTokens = Number(bubble.dataset.promptTokens);
  const hasPromptTokens = Number.isFinite(promptTokens) && promptTokens >= 0;

  const values = [
    ["Temps total", completed ? `${(elapsedMs / 1000).toFixed(2)} s` : "En cours…"],
    ["TTFT", firstTokenMs ? `${(firstTokenMs / 1000).toFixed(2)} s` : "En attente du premier token"],
    ["Tokens générés", completed ? (generatedTokens === null ? "Indisponible" : `${hasExactTokens ? "" : "≈ "}${generatedTokens}`) : "En cours…"],
    ["Vitesse", tokensPerSecond ? `${tokensPerSecond.toFixed(1)} tokens/s${hasServerTokensPerSecond ? "" : " · estimée"}` : "—"],
    ["Contexte utilisé", hasPromptTokens ? `${promptTokens.toLocaleString("fr-FR")} / ${currentContextLimit ? currentContextLimit.toLocaleString("fr-FR") : "—"} tokens` : "Indisponible pour cette réponse"],
  ];

  const list = panel.querySelector(".assistant-telemetry-values");
  list.replaceChildren();
  for (const [label, value] of values) {
    const row = document.createElement("div");
    row.className = "assistant-telemetry-row";
    const key = document.createElement("dt");
    key.textContent = label;
    const val = document.createElement("dd");
    val.textContent = value;
    row.append(key, val);
    list.appendChild(row);
  }

  const progress = panel.querySelector(".assistant-context-progress");
  const ratio = hasPromptTokens && currentContextLimit ? Math.min(100, Math.round(promptTokens / currentContextLimit * 100)) : 0;
  progress.hidden = !hasPromptTokens || !currentContextLimit;
  progress.value = ratio;
  progress.setAttribute("aria-label", `Contexte utilisé : ${ratio} %`);
}

function recordModelPerformance(bubble) {
  if (!bubble || bubble.dataset.generationCompleted !== "true" || bubble.dataset.performanceRecordSent === "true") return;
  const modelId = bubble.dataset.modelId;
  const modelName = bubble.dataset.modelName;
  if (!modelId || !modelName) return;
  bubble.dataset.performanceRecordSent = "true";

  const totalMs = Math.max(0, Number(bubble.dataset.generationMs) || 0);
  const ttftValue = Number(bubble.dataset.firstTokenMs);
  const ttftMs = Number.isFinite(ttftValue) && ttftValue > 0 ? Math.round(ttftValue) : null;
  const exactOutputTokens = Number(bubble.dataset.completionTokens);
  const hasExactOutputTokens = bubble.dataset.completionTokens !== undefined && Number.isFinite(exactOutputTokens);
  const outputText = bubble.innerText || bubble.textContent || "";
  const outputTokens = hasExactOutputTokens
    ? Math.max(0, Math.round(exactOutputTokens))
    : (outputText.trim() ? Math.max(1, Math.round(outputText.trim().length / 4)) : null);
  const promptValue = Number(bubble.dataset.promptTokens);
  const promptTokens = Number.isFinite(promptValue) && promptValue >= 0 ? Math.round(promptValue) : null;
  const serverSpeed = Number(bubble.dataset.generationTps);
  const hasServerSpeed = bubble.dataset.generationTps !== undefined && Number.isFinite(serverSpeed) && serverSpeed > 0;
  const generationMs = Math.max(0, totalMs - (ttftMs || 0));
  const speed = hasServerSpeed
    ? serverSpeed
    : (outputTokens && generationMs > 0 ? outputTokens / (generationMs / 1000) : null);

  let hardware = currentHardwareSnapshot || {};
  try {
    hardware = JSON.parse(bubble.dataset.hardwareProfile || "{}") || hardware;
  } catch (_) {}

  fetch("/api/models/performance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model_id: modelId,
      model_name: modelName,
      source: bubble.dataset.modelSource || "local",
      total_ms: Math.round(totalMs),
      ttft_ms: ttftMs,
      prompt_tokens: promptTokens,
      output_tokens: outputTokens,
      tokens_per_second: speed,
      measurement: hasServerSpeed ? "server" : "estimated",
      hardware,
    }),
  }).then((response) => {
    if (!response.ok) console.warn("La mesure de performance du modèle n’a pas été enregistrée.");
  }).catch((error) => console.warn("La mesure de performance du modèle n’a pas été enregistrée.", error));
}

function addAssistantActions(message, bubble) {
  const actions = document.createElement("div");
  actions.className = "assistant-message-actions";
  const attach = document.createElement("button");
  attach.type = "button";
  attach.className = "assistant-action-button assistant-attach-button";
  attach.setAttribute("aria-label", "Ajouter un fichier ou un outil au prochain message");
  attach.title = "Ajouter un fichier ou activer la recherche web";
  attach.innerHTML = `${window.ETUSVG?.icon("plus", "assistant-action-icon") || ""}<span>Ajouter</span>`;
  attach.addEventListener("click", () => {
    document.getElementById("chat-input")?.focus();
    const trigger = document.getElementById("btn-attach");
    if (trigger && document.getElementById("attach-dropdown-menu")?.hidden) trigger.click();
  });
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "assistant-action-button assistant-copy-button";
  copy.setAttribute("aria-label", "Copier la réponse");
  copy.innerHTML = `${window.ETUSVG?.icon("copy", "assistant-action-icon") || ""}<span>Copier</span>`;
  copy.addEventListener("click", async () => {
    const visibleText = (bubble.innerText || bubble.textContent || "").trim();
    const text = visibleText || (bubble.dataset.copyText || "").trim();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      copy.innerHTML = `${window.ETUSVG?.icon("check", "assistant-action-icon") || ""}<span>Copié</span>`;
      showToast("Réponse copiée", "success", 1600);
      setTimeout(() => { copy.innerHTML = `${window.ETUSVG?.icon("copy", "assistant-action-icon") || ""}<span>Copier</span>`; }, 1400);
    } catch {
      showToast("Impossible d’accéder au presse-papiers", "warning", 2500);
    }
  });

  const details = document.createElement("details");
  details.className = "assistant-telemetry";
  const summary = document.createElement("summary");
  summary.innerHTML = `${window.ETUSVG?.icon("telemetry", "assistant-action-icon") || ""}<span>Télémétrie</span>`;
  const panel = document.createElement("div");
  panel.className = "assistant-telemetry-panel";
  const list = document.createElement("dl");
  list.className = "assistant-telemetry-values";
  const progress = document.createElement("progress");
  progress.className = "assistant-context-progress";
  progress.max = 100;
  progress.value = 0;
  progress.hidden = true;
  panel.append(list, progress);
  details.append(summary, panel);
  actions.append(attach, copy, details);
  message.appendChild(actions);
  updateAssistantTelemetry(bubble, true);
}

function attachInteractiveButtons(container) {
  container.querySelectorAll("button[data-action],button[data-chat-action]").forEach((btn) => {
    if (!btn.dataset.bound) {
      btn.dataset.bound = "true";
      btn.addEventListener("click", () => {
        const action = btn.dataset.chatAction ?? btn.dataset.action;
        if (btn.closest("form") && btn.type === "submit") return;
        submitGeneratedPrompt(action);
      });
    }
  });
  container.querySelectorAll("form").forEach((form) => {
    if (form.dataset.bound) return;
    form.dataset.bound = "true";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!event.isTrusted) return;
      const prompt = ETUChatUI.formPrompt(form, event.submitter);
      submitGeneratedPrompt(`[formulaire] ${prompt.description}\n${JSON.stringify(prompt.fields)}`);
    });
  });
}

function renderMessage(role, contentHtml, attachments = [], messageId = "", toolCalls = []) {
  const container = document.getElementById("chat-messages");
  const msgDiv = document.createElement("div");
  msgDiv.className = `message ${role}`;
  if (messageId) msgDiv.dataset.messageId = messageId;

  const isUser = role === "user";
  const profile = getActiveProfile();
  msgDiv.innerHTML = `
    <div class="avatar ${isUser ? "user-avatar" : "ai-avatar"}">${isUser ? "VOUS" : avatarMarkup(profile.avatar)}</div>
    <div class="message-bubble"></div>
  `;
  const bubble = msgDiv.querySelector(".message-bubble");
  container.appendChild(msgDiv);
  if (isUser) {
    const content = document.createElement("div");
    content.className = "message-user-content";
    content.textContent = contentHtml;
    bubble.appendChild(content);
  }
  else {
    if (!renderAuthoredPreview(bubble, contentHtml, true)) {
      if (contentHtml.trim()) ETUChatUI.showConversation();
      bubble.appendChild(renderAssistantFragment(contentHtml));
    }
  }
  if (attachments.length) {
    const files = document.createElement("div");
    files.className = "attachment-list message-attachments";
    renderAttachments(files, attachments);
    bubble.appendChild(files);
  }
  if (!isUser) {
    addMessageToolBadges(msgDiv, toolCalls.map((call) => call?.function?.name || call?.name).filter(Boolean));
    addAssistantActions(msgDiv, bubble);
    applyPartialUpdates(bubble);
  } else if (messageId) {
    addUserMessageActions(msgDiv);
  }
  attachInteractiveButtons(msgDiv);
  scrollToBottom();
  return msgDiv;
}

function addMessageToolBadge(message, key, label, icon = "hub") {
  if (!message || !key || !label) return;
  let strip = message.querySelector(":scope > .message-tools");
  if (!strip) {
    strip = document.createElement("div");
    strip.className = "message-tools";
    strip.setAttribute("aria-label", "Outils utilisés pour cette réponse");
    message.insertBefore(strip, message.querySelector(":scope > .message-bubble"));
  }
  let badge = [...strip.children].find((item) => item.dataset.toolKey === key);
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "message-tool-badge";
    badge.dataset.toolKey = key;
    badge.tabIndex = 0;
    strip.append(badge);
  }
  badge.dataset.tooltip = label;
  badge.setAttribute("aria-label", label);
  badge.innerHTML = window.ETUSVG?.icon(icon, "message-tool-icon") || "";
}

function removeMessageToolBadge(message, key) {
  const strip = message?.querySelector(":scope > .message-tools");
  strip?.querySelector(`[data-tool-key="${CSS.escape(key)}"]`)?.remove();
  if (strip && !strip.children.length) strip.remove();
}

function addMessageToolBadges(message, toolNames = []) {
  const labels = {
    search_web: ["Recherche Web", "globe"],
    wiki_read: ["Lecture du Wiki", "book"],
    wiki_search: ["Recherche dans le Wiki", "book"],
    wiki_save: ["Écriture dans le Wiki", "book"],
    wiki_update: ["Mise à jour du Wiki", "book"],
  };
  for (const name of toolNames) {
    const [label, icon] = labels[name] || [name.replace(/^wiki_/, "Wiki · ").replaceAll("_", " "), "hub"];
    addMessageToolBadge(message, name, label, icon);
  }
}

window.ETUAIAddToolBadge = addMessageToolBadge;
window.ETUAIRemoveToolBadge = removeMessageToolBadge;

function addUserMessageActions(messageEl) {
  if (!messageEl?.dataset.messageId || messageEl.querySelector(".message-action-row")) return;
  const bubble = messageEl.querySelector(".message-bubble");
  if (!bubble) return;
  const actions = document.createElement("div");
  actions.className = "message-action-row";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "message-edit-button";
  edit.disabled = document.getElementById("chat-form")?.dataset.busy === "true";
  edit.setAttribute("aria-label", "Modifier ce message");
  edit.title = "Modifier ce message";
  edit.innerHTML = `${window.ETUSVG?.icon("edit", "message-edit-icon") || ""}<span>Modifier</span>`;
  edit.addEventListener("click", () => beginUserMessageEdit(messageEl));
  actions.append(edit);
  messageEl.append(actions);
}

function beginUserMessageEdit(messageEl) {
  if (!currentConversationId || document.getElementById("chat-form")?.dataset.busy === "true") return;
  const bubble = messageEl.querySelector(".message-bubble");
  const content = bubble?.querySelector(".message-user-content");
  const actions = messageEl.querySelector(".message-action-row");
  if (!bubble || !content || !actions || messageEl.dataset.editing === "true") return;
  messageEl.dataset.editing = "true";
  const bubbleRect = bubble.getBoundingClientRect();
  const bubbleHeight = Math.ceil(bubbleRect.height);
  const bubbleWidth = Math.ceil(bubbleRect.width);
  bubble.style.height = `${bubbleHeight}px`;
  bubble.style.width = `${bubbleWidth}px`;
  bubble.classList.add("message-bubble-editing");

  const textarea = document.createElement("textarea");
  textarea.className = "message-edit-input";
  textarea.value = content.textContent;
  textarea.setAttribute("aria-label", "Modifier le message envoyé");
  textarea.rows = 1;
  const controls = document.createElement("div");
  controls.className = "message-edit-controls";
  const error = document.createElement("span");
  error.className = "message-edit-error";
  error.setAttribute("role", "status");
  const editAttach = document.createElement("button");
  editAttach.type = "button";
  editAttach.className = "message-edit-attach";
  editAttach.setAttribute("aria-haspopup", "menu");
  editAttach.setAttribute("aria-expanded", "false");
  editAttach.setAttribute("aria-label", "Ajouter un fichier ou activer la recherche Web");
  editAttach.title = "Ajouter un fichier ou activer la recherche Web";
  editAttach.innerHTML = window.ETUSVG?.icon("plus", "message-edit-icon") || "+";
  editAttach.addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = document.getElementById("attach-dropdown-menu");
    setChatAttachMenu(menu.hidden || menu.parentElement !== editAttach.closest(".attach-menu-wrapper"), editAttach);
  });
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "message-edit-cancel";
  cancel.innerHTML = `${window.ETUSVG?.icon("close", "message-edit-icon") || ""}<span>Annuler</span>`;
  const save = document.createElement("button");
  save.type = "button";
  save.className = "message-edit-save";
  save.innerHTML = `${window.ETUSVG?.icon("check", "message-edit-icon") || ""}<span>Enregistrer et renvoyer</span>`;
  const restore = () => {
    setChatAttachMenu(false);
    textarea.replaceWith(content);
    controls.replaceWith(actions);
    bubble.style.removeProperty("height");
    bubble.style.removeProperty("width");
    bubble.classList.remove("message-bubble-editing");
    delete messageEl.dataset.editing;
  };
  cancel.addEventListener("click", restore);
  save.addEventListener("click", async () => {
    const editedContent = textarea.value.trim();
    if (!editedContent) {
      error.textContent = "Le message ne peut pas être vide.";
      textarea.focus();
      return;
    }
    save.disabled = cancel.disabled = true;
    error.textContent = "Enregistrement…";
    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(currentConversationId)}/messages/${encodeURIComponent(messageEl.dataset.messageId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: editedContent }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Modification impossible.");

      content.textContent = editedContent;
      restore();
      let next = messageEl.nextElementSibling;
      while (next) {
        const following = next.nextElementSibling;
        next.remove();
        next = following;
      }
      pendingEditedMessage = { messageId: messageEl.dataset.messageId, userBubble: messageEl, attachments: [...pendingChatFiles] };
      const input = document.getElementById("chat-input");
      input.value = editedContent;
      resizeChatInput();
      document.getElementById("chat-form").requestSubmit();
    } catch (problem) {
      error.textContent = problem.message;
      save.disabled = cancel.disabled = false;
    }
  });
  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      restore();
    } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      save.click();
    }
  });
  controls.classList.add("message-edit-controls");
  controls.setAttribute("aria-label", "Options de modification du message");
  controls.style.setProperty("--message-edit-width", `${bubbleWidth}px`);
  controls.append(editAttach, error, cancel, save);
  const editMenuWrapper = document.createElement("div");
  editMenuWrapper.className = "attach-menu-wrapper edit-attach-menu-wrapper";
  editAttach.parentElement?.removeChild(editAttach);
  editMenuWrapper.append(editAttach);
  controls.prepend(editMenuWrapper);
  const resizeEditBubble = () => {
    const maxHeight = Math.max(160, Math.floor(window.innerHeight * 0.45));
    textarea.style.height = "auto";
    const nextHeight = Math.min(Math.max(textarea.scrollHeight + 20, bubbleHeight), maxHeight);
    bubble.style.height = `${nextHeight}px`;
    textarea.style.height = "100%";
    textarea.style.overflowY = textarea.scrollHeight + 20 > maxHeight ? "auto" : "hidden";
  };
  textarea.addEventListener("input", resizeEditBubble);
  content.replaceWith(textarea);
  actions.replaceWith(controls);
  textarea.focus();
  resizeEditBubble();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

function formatAssistantMarkup(raw) {
  let text = String(raw || "");
  // Certains petits modèles enveloppent du Markdown entier dans un seul <p>.
  if (/^\s*<p\b[^>]*>/i.test(text) && /(?:```|\n#{2,5}\s)/.test(text)) {
    text = text.replace(/^\s*<p\b[^>]*>/i, "").replace(/<\/p>\s*$/i, "");
  }
  const renderer = new marked.Renderer();
  renderer.html = ({ text: html, block }) => block ? formatEmbeddedMarkdown(html) : html;
  return marked.parse(text, { gfm: true, breaks: true, renderer });
}

function formatEmbeddedMarkdown(html) {
  // Les modèles mélangent parfois des listes HTML avec des liens ou du gras MD.
  // Traiter uniquement leurs textes, jamais les attributs ni le contenu du code.
  const source = document.createElement("template");
  source.innerHTML = html;
  const textRenderer = new marked.Renderer();
  // Les chevrons décodés depuis &lt; restent du texte, pas de nouvelles balises.
  textRenderer.html = ({ text }) => escapeHtml(text);
  function format(parent) {
    for (const node of [...parent.childNodes]) {
      if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
        const inline = document.createElement("template");
        inline.innerHTML = marked.parseInline(node.textContent, { gfm: true, breaks: true, renderer: textRenderer });
        node.replaceWith(inline.content);
      } else if (node.nodeType === Node.ELEMENT_NODE &&
                 !["PRE", "CODE", "SCRIPT", "STYLE", "TEXTAREA", "TEMPLATE"].includes(node.tagName)) {
        if (["P", "DIV"].includes(node.tagName) &&
            [...node.childNodes].every(child => child.nodeType === Node.TEXT_NODE) &&
            /(?:^|\n)\s*(?:#{1,6}\s|`{3,}|~{3,}|[-*+]\s|\d+[.)]\s|>\s)/.test(node.textContent)) {
          const block = document.createElement("template");
          block.innerHTML = marked.parse(node.textContent, { gfm: true, breaks: true, renderer: textRenderer });
          node.replaceWith(block.content);
        } else format(node);
      }
    }
  }
  format(source.content);
  return source.innerHTML;
}

function highlightCode(root) {
  for (const code of root.querySelectorAll("pre > code")) {
    const language = code.className.match(/(?:^|\s)language-([\w+#-]+)/)?.[1]?.toLowerCase();
    const source = code.textContent;
    code.classList.add("hljs");
    if (source.length > 30000) continue;
    if (language && !hljs.getLanguage(language)) continue;
    const result = language
      ? hljs.highlight(source, { language, ignoreIllegals: true })
      : hljs.highlightAuto(source, ["javascript", "typescript", "python", "sql", "json", "css", "xml", "bash"]);
    code.innerHTML = result.value;
  }
}

function renderAssistantFragment(raw) {
  const fragment = safeFragment(formatAssistantMarkup(raw));
  highlightCode(fragment);
  return fragment;
}

function safeFragment(html) {
  const allowed = new Set(["P", "DIV", "SPAN", "STRONG", "EM", "B", "I", "BR", "HR", "S", "DEL", "A", "UL", "OL", "LI", "H1", "H2", "H3", "H4", "H5", "H6", "PRE", "CODE", "BLOCKQUOTE", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "BUTTON", "FORM", "LABEL", "INPUT", "TEXTAREA", "SELECT", "OPTION", "TEMPLATE"]);
  const source = document.createElement("template");
  source.innerHTML = String(html).replace(/<\?(marker|start)\s+name=["']?([\w-]{1,80})["']?\s*\??>/gi, "<!--etu-$1:$2-->")
    .replace(/<\?end\s*\??>/gi, "<!--etu-end-->");
  function clean(parent) {
    for (const node of [...parent.childNodes]) {
      if (node.nodeType === Node.TEXT_NODE) continue;
      if (node.nodeType === Node.COMMENT_NODE) {
        if (!/^etu-(?:marker|start):[\w-]{1,80}$|^etu-end$/.test(node.data)) node.remove();
        continue;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) { node.remove(); continue; }
      if (!allowed.has(node.tagName)) { node.remove(); continue; }
      for (const attr of [...node.attributes]) {
        const name = attr.name.toLowerCase();
        const value = attr.value;
        const valid = (name === "class" && /^[\w\s+#-]{0,120}$/.test(value)) ||
          (name === "href" && node.tagName === "A" && safeMarkdownLink(value)) ||
          (name === "start" && node.tagName === "OL" && /^\d{1,9}$/.test(value)) ||
          (["data-action", "data-chat-action"].includes(name) && node.tagName === "BUTTON" && value.length <= 500) ||
          (["data-model", "data-name"].includes(name) && node.tagName === "BUTTON" && value.length <= 200) ||
          (name === "data-description" && node.tagName === "FORM" && value.length <= 500) ||
          (name === "data-update-marker" && /^[\w-]{1,80}$/.test(value)) ||
          (name === "for" && node.tagName === "TEMPLATE" && /^[\w-]{1,80}$/.test(value)) ||
          (name === "data-color" && /^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$/.test(value)) ||
          (name === "type" && ["button", "submit", "text", "checkbox", "radio", "number"].includes(value)) ||
          (name === "name" && /^[\w-]{1,80}$/.test(value)) ||
          (["value", "placeholder"].includes(name) && value.length <= 500) ||
          (["checked", "selected", "required", "disabled"].includes(name));
        if (!valid) node.removeAttribute(attr.name);
      }
      if (node.tagName === "A" && node.hasAttribute("href")) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
      clean(node.tagName === "TEMPLATE" ? node.content : node);
    }
  }
  clean(source.content);
  return source.content;
}

function safeMarkdownLink(value) {
  try { return ["http:", "https:", "mailto:"].includes(new URL(value, window.location.href).protocol); }
  catch { return false; }
}

function applyPartialUpdates(bubble) {
  bubble.querySelectorAll("template[for]").forEach((template) => {
    const marker = template.getAttribute("for");
    const chat = document.getElementById("chat-messages");
    const target = [...chat.querySelectorAll("[data-update-marker]")]
      .find((element) => element.dataset.updateMarker === marker);
    const content = template.content.cloneNode(true);
    if (target) {
      target.replaceChildren(content);
      template.remove();
    } else {
      const walker = document.createTreeWalker(chat, NodeFilter.SHOW_COMMENT);
      let start = null;
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node.data === `etu-marker:${marker}`) {
          const slot = document.createElement("span");
          slot.dataset.updateMarker = marker;
          node.replaceWith(slot);
          slot.replaceChildren(content);
          template.remove();
          break;
        }
        if (node.data === `etu-start:${marker}`) start = node;
        if (node.data === "etu-end" && start && start.parentNode === node.parentNode) {
          const range = document.createRange();
          range.setStartAfter(start);
          range.setEndBefore(node);
          range.deleteContents();
          node.parentNode.insertBefore(content, node);
          template.remove();
          break;
        }
      }
    }
  });
  const chat = document.getElementById("chat-messages");
  attachInteractiveButtons(chat);
}

function getActiveProfile() {
  return assistantProfiles.find((profile) => profile.id === activeProfileId) ||
    { id: "default", name: "ETU-AI", avatar: "✦", instructions: "" };
}

function profileOrigin(profile) {
  return profile.id === "default" ? "Intégré à ETU-AI" : "Créé sur cet appareil";
}

function profileModelLabel(model) {
  if (!model) return "Modèle actif par défaut";
  const item = [...availableInstalledModels, ...remoteApiModels]
    .find((candidate) => (candidate.source === "api" ? candidate.id : candidate.filename || candidate.id) === model);
  return item ? (item.name || item.filename || item.id) : model;
}

let profileManagerPage = 1;
const PROFILE_MANAGER_PAGE_SIZE = 10;

function renderProfileManager() {
  const list = document.getElementById("settings-profile-list");
  if (!list) return;
  const search = document.getElementById("profile-search-local").value.trim().toLocaleLowerCase();
  const matching = assistantProfiles.filter((profile) =>
    [profile.name, profileOrigin(profile), profileModelLabel(profile.model)]
      .some((value) => value.toLocaleLowerCase().includes(search)));
  const pageCount = Math.max(1, Math.ceil(matching.length / PROFILE_MANAGER_PAGE_SIZE));
  profileManagerPage = Math.min(profileManagerPage, pageCount);
  const start = (profileManagerPage - 1) * PROFILE_MANAGER_PAGE_SIZE;
  list.replaceChildren();
  matching.slice(start, start + PROFILE_MANAGER_PAGE_SIZE).forEach((profile) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "profile-manager-card";
    card.innerHTML = `
      <span class="profile-manager-avatar" aria-hidden="true">${avatarMarkup(profile.avatar)}</span>
      <span class="profile-manager-copy">
        <span class="profile-manager-name">${escapeHtml(profile.name)}</span>
        <span class="profile-manager-origin">${escapeHtml(profileOrigin(profile))}</span>
        <span class="profile-manager-model">${escapeHtml(profileModelLabel(profile.model))}</span>
      </span>
      ${profile.id === activeProfileId ? '<span class="profile-manager-active">Actif</span>' : ""}
      <span class="profile-manager-edit" aria-hidden="true">Modifier →</span>`;
    card.addEventListener("click", () => openProfileDialog(profile));
    list.append(card);
  });
  const createCard = document.createElement("button");
  createCard.type = "button";
  createCard.className = "profile-manager-create-card";
  createCard.innerHTML = '<span aria-hidden="true">＋</span><strong>Créer un profil</strong><span>Ajouter un nouvel assistant</span>';
  createCard.addEventListener("click", () => openProfileDialog());
  list.append(createCard);
  const pagination = document.getElementById("settings-profile-pagination");
  if (pagination) {
    pagination.replaceChildren();
    pagination.hidden = pageCount <= 1;
    if (pageCount > 1) {
      const summary = document.createElement("span");
      summary.className = "profile-manager-page-summary";
      summary.textContent = `${matching.length} profils · page ${profileManagerPage} sur ${pageCount}`;
      const previous = document.createElement("button");
      previous.type = "button";
      previous.className = "etu-btn etu-btn-secondary";
      previous.textContent = "Précédent";
      previous.disabled = profileManagerPage === 1;
      previous.addEventListener("click", () => { profileManagerPage--; renderProfileManager(); });
      const next = document.createElement("button");
      next.type = "button";
      next.className = "etu-btn etu-btn-secondary";
      next.textContent = "Suivant";
      next.disabled = profileManagerPage === pageCount;
      next.addEventListener("click", () => { profileManagerPage++; renderProfileManager(); });
      pagination.append(summary, previous, next);
    }
  }
  document.getElementById("settings-profile-empty").hidden = matching.length > 0;
}

function populateProfileModels(selectedModel = "") {
  const select = document.getElementById("profile-model");
  select.replaceChildren(new Option("Modèle actif par défaut", ""));
  const seen = new Set();
  for (const item of [...availableInstalledModels, ...remoteApiModels]) {
    const value = item.source === "api" ? item.id : (item.filename || item.id);
    if (!value || seen.has(value)) continue;
    seen.add(value);
    const label = item.source === "api" ? `${item.name || value} · ${item.provider_label || "API"}` : (item.name || value);
    select.add(new Option(label, value));
  }
  if (selectedModel && !seen.has(selectedModel)) select.add(new Option(`${selectedModel} (indisponible)`, selectedModel));
  select.value = selectedModel || "";
  window.refreshWorkshopModelChoices?.();
}

function avatarMarkup(avatar) {
  return /^data:image\/webp;base64,[A-Za-z0-9+/=]+$/.test(avatar || "")
    ? `<img src="${avatar}" alt="" class="profile-avatar-image">`
    : escapeHtml(avatar || "✦");
}

function updateProfilePreview(avatar) {
  document.getElementById("profile-image-preview").innerHTML = avatarMarkup(avatar);
}

function setDisplayedProfile(profileId) {
  activeProfileId = assistantProfiles.some((profile) => profile.id === profileId) ? profileId : "default";
  const activeProfile = getActiveProfile();
  const trigger = document.getElementById("profile-select");
  const avatar = document.getElementById("profile-select-avatar");
  const name = document.getElementById("profile-select-name");
  if (trigger && avatar && name) {
    avatar.innerHTML = avatarMarkup(activeProfile.avatar);
    name.textContent = activeProfile.name;
    trigger.setAttribute("aria-label", "Profil actif : " + activeProfile.name + ". Choisir un profil");
    trigger.title = "Profil actif : " + activeProfile.name;
  }
  document.querySelectorAll("#profile-options [role='option']").forEach((option) => {
    const selected = option.dataset.profileId === activeProfileId;
    option.setAttribute("aria-selected", String(selected));
    option.classList.toggle("is-selected", selected);
  });
  document.querySelectorAll("#chat-messages .assistant .ai-avatar").forEach((avatar) => {
    avatar.innerHTML = avatarMarkup(getActiveProfile().avatar);
  });
  const modelButton = document.getElementById("btn-model-selector");
  if (modelButton) {
    modelButton.disabled = Boolean(activeProfile.model);
    modelButton.title = activeProfile.model ? "Modèle défini dans ce profil. Modifiez le profil pour en changer." : "Choisir un modèle";
  }
  updateModelUI(null, availableInstalledModels, remoteApiModels);
  renderProfileManager();
}

async function loadProfiles() {
  const response = await fetch("/api/profiles");
  if (!response.ok) throw new Error("Impossible de charger les profils");
  const data = await response.json();
  assistantProfiles = data.profiles;
  preferredProfileId = data.active_profile_id;
  if (!currentConversationId) activeProfileId = preferredProfileId;
  const options = document.getElementById("profile-options");
  options.replaceChildren();
  assistantProfiles.forEach((profile) => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "profile-picker-option";
    option.setAttribute("role", "option");
    option.dataset.profileId = profile.id;
    option.innerHTML = '<span class="profile-picker-avatar" aria-hidden="true">' + avatarMarkup(profile.avatar) + '</span><span class="profile-picker-option-name"></span><svg class="profile-picker-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"></path></svg>';
    option.querySelector(".profile-picker-option-name").textContent = profile.name;
    options.appendChild(option);
  });
  setDisplayedProfile(activeProfileId);
}

function editProfile(profile = null) {
  profileAvatarFallback = profile?.avatar || "✦";
  pendingAvatarData = null;
  document.getElementById("profile-id").value = profile?.id || "";
  document.getElementById("profile-name").value = profile?.name || "";
  document.getElementById("profile-image-file").value = "";
  updateProfilePreview(profileAvatarFallback);
  document.getElementById("profile-instructions").value = profile?.instructions || "";
  populateProfileModels(profile?.model || "");
  document.getElementById("profile-form-title").textContent = profile ? `Modifier ${profile.name}` : "Créer un profil";
  document.getElementById("profile-delete").hidden = !profile || profile.id === "default";
  document.getElementById("profile-form-error").textContent = "";
  window.refreshWorkshopPublication?.(profile);
}

function openProfileDialog(profile = null) {
  editProfile(profile);
  openModal("modal-profiles");
}

function initProfiles() {
  ETUComponents.dialog(document.getElementById("modal-profiles"));
  loadProfiles().catch((error) => console.warn(error));
  const picker = document.getElementById("profile-picker");
  const trigger = document.getElementById("profile-select");
  const menu = document.getElementById("profile-menu");
  const positionPickerMenu = () => {
    if (menu.hidden) return;
    const rect = trigger.getBoundingClientRect();
    const inset = 8;
    const width = Math.max(1, Math.min(window.innerWidth - inset * 2, Math.max(rect.width, 220)));
    const left = Math.max(inset, Math.min(rect.left, window.innerWidth - width - inset));
    const preferredHeight = Math.min(280, window.innerHeight * .45);
    const below = Math.max(0, window.innerHeight - rect.bottom - inset - 6);
    const above = Math.max(0, rect.top - inset - 6);
    const placeAbove = below < preferredHeight && above > below;
    const available = placeAbove ? above : below;
    menu.style.left = `${left}px`;
    menu.style.width = `${width}px`;
    menu.style.maxHeight = `${Math.max(1, Math.min(preferredHeight, available))}px`;
    menu.style.top = placeAbove ? "auto" : `${rect.bottom + 6}px`;
    menu.style.bottom = placeAbove ? `${window.innerHeight - rect.top + 6}px` : "auto";
    menu.dataset.placement = placeAbove ? "top" : "bottom";
  };
  const setPickerOpen = (open, focusFirst = false) => {
    if (open) document.body.append(menu);
    menu.hidden = !open;
    trigger.setAttribute("aria-expanded", String(open));
    if (open) positionPickerMenu();
    else picker.append(menu);
    if (focusFirst && open) menu.querySelector("[role='option']")?.focus();
  };
  menu.addEventListener("click", (event) => {
    const option = event.target.closest("[role='option']");
    if (option) activateProfile(option.dataset.profileId);
  });
  trigger.addEventListener("click", () => setPickerOpen(menu.hidden));
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      setPickerOpen(false);
      event.preventDefault();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setPickerOpen(true, true);
    }
  });
  menu.addEventListener("keydown", (event) => {
    const options = [...menu.querySelectorAll("[role='option']")];
    if (!options.length) return;
    const index = options.indexOf(document.activeElement);
    let nextIndex = index;
    if (event.key === "ArrowDown") nextIndex = (index + 1) % options.length;
    else if (event.key === "ArrowUp") nextIndex = (index - 1 + options.length) % options.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = options.length - 1;
    else if (event.key === "Escape") {
      setPickerOpen(false);
      trigger.focus();
      event.preventDefault();
      return;
    } else return;
    event.preventDefault();
    options[nextIndex]?.focus();
  });
  document.addEventListener("click", (event) => {
    if (!picker.contains(event.target) && !menu.contains(event.target)) setPickerOpen(false);
  });
  document.getElementById("profile-manage").addEventListener("click", () => {
    setPickerOpen(false);
    showSettingsView("profiles");
  });
  window.addEventListener("resize", positionPickerMenu);
  document.addEventListener("scroll", positionPickerMenu, true);
  document.getElementById("btn-create-profile").addEventListener("click", () => openProfileDialog());
  document.getElementById("profile-search-local").addEventListener("input", () => { profileManagerPage = 1; renderProfileManager(); });
  document.querySelectorAll("[data-profile-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      const selected = tab.dataset.profileTab;
      document.querySelectorAll("[data-profile-tab]").forEach((button) => {
        const active = button.dataset.profileTab === selected;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", String(active));
      });
      document.getElementById("profile-panel-local").hidden = selected !== "local";
      document.getElementById("profile-panel-workshop").hidden = selected !== "workshop";
    });
  });
  document.getElementById("profile-image-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const error = document.getElementById("profile-form-error");
    try {
      if (!file.type.startsWith("image/") || file.size > 5 * 1024 * 1024) throw new Error("Choisissez une image de moins de 5 Mo.");
      const bitmap = await createImageBitmap(file);
      const canvas = document.createElement("canvas");
      canvas.width = 128;
      canvas.height = 128;
      const context = canvas.getContext("2d");
      const side = Math.min(bitmap.width, bitmap.height);
      context.drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, 128, 128);
      bitmap.close();
      const dataUrl = canvas.toDataURL("image/webp", 0.78);
      if (dataUrl.length > 95000) throw new Error("Image trop volumineuse après réduction.");
      pendingAvatarData = dataUrl;
      updateProfilePreview(dataUrl);
      error.textContent = "";
    } catch (problem) {
      error.textContent = problem.message;
    }
  });
  async function activateProfile(profileId) {
    try {
      const response = await fetch("/api/profiles/" + encodeURIComponent(profileId) + "/activate", { method: "POST" });
      if (!response.ok) throw new Error("Activation impossible");
      preferredProfileId = profileId;
      setDisplayedProfile(profileId);
      setPickerOpen(false);
      startNewConversation();
    } catch (error) {
      setDisplayedProfile(activeProfileId);
      showToast(escapeHtml(error.message), "warning");
    }
  }
  document.getElementById("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = document.getElementById("profile-id").value;
    const body = {
      name: document.getElementById("profile-name").value.trim(),
      avatar: pendingAvatarData || profileAvatarFallback,
      instructions: document.getElementById("profile-instructions").value.trim(),
      model: document.getElementById("profile-model").value || null,
    };
    try {
      const response = await fetch(id ? `/api/profiles/${encodeURIComponent(id)}` : "/api/profiles", {
        method: id ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error((await response.json()).detail || "Enregistrement impossible");
      const profile = await response.json();
      await loadProfiles();
      editProfile(profile);
      document.getElementById("profile-search-local").value = "";
      document.getElementById("profile-tab-local").click();
      renderProfileManager();
      closeModal("modal-profiles");
      showToast("Profil enregistré", "success");
    } catch (error) {
      document.getElementById("profile-form-error").textContent = error.message;
    }
  });
  document.getElementById("profile-delete").addEventListener("click", async () => {
    const id = document.getElementById("profile-id").value;
    if (!id || id === "default" || !confirm("Supprimer ce profil ? Ses conversations utiliseront ETU-AI.")) return;
    const response = await fetch(`/api/profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!response.ok) {
      document.getElementById("profile-form-error").textContent = "Suppression impossible";
      return;
    }
    if (activeProfileId === id) {
      activeProfileId = "default";
      preferredProfileId = "default";
      startNewConversation();
    }
    await loadProfiles();
    editProfile(getActiveProfile());
    closeModal("modal-profiles");
  });
}

function scrollToBottom() {
  const container = document.getElementById("chat-messages");
  if (container) container.scrollTop = container.classList.contains("generated-view") ? 0 : container.scrollHeight;
}

/* -------------------------------------------------------------------------- */
/* Catalogue & Téléchargement Hugging Face                                    */
/* -------------------------------------------------------------------------- */
let modelCatalogItems = [];
let modelRuntimeHardware = {};

function isTextOnlyModel(model) {
  return Boolean(model.mmproj?.text_only || /(?:^|[-_.])text[-_.]instruct(?:[-_.]|$)/i.test(model.filename || ""));
}

function applyModelCatalogSearch() {
  const input = document.getElementById("model-catalog-search");
  const query = (input?.value || "").trim().normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase();
  const cards = [...document.querySelectorAll("[data-catalog-search]")];
  let visibleCount = 0;
  const visibleBySection = new Map();
  cards.forEach((card) => {
    const matches = !query || card.dataset.catalogSearch.includes(query);
    card.hidden = !matches;
    if (matches) {
      visibleCount += 1;
      const section = card.closest(".catalog-section");
      if (section) visibleBySection.set(section, (visibleBySection.get(section) || 0) + 1);
    }
  });
  document.querySelectorAll(".catalog-section").forEach((section) => {
    const count = visibleBySection.get(section) || 0;
    section.hidden = Boolean(query) && count === 0;
  });
  const countLabel = document.getElementById("model-catalog-search-count");
  if (countLabel) countLabel.textContent = query ? `${visibleCount} modèle${visibleCount > 1 ? "s" : ""} trouvé${visibleCount > 1 ? "s" : ""}` : "";
}

function sortLocalModelCards() {
  const list = document.getElementById("catalog-local-list");
  const sort = document.getElementById("model-catalog-sort")?.value || "name";
  if (!list) return;
  const direction = sort.endsWith("desc") ? -1 : 1;
  const field = sort.startsWith("disk") ? "disk" : sort.startsWith("ram") ? "ram" : sort.startsWith("params") ? "params" : "name";
  const cards = [...list.querySelectorAll(":scope > .catalog-item")];
  cards.sort((a, b) => {
    if (field === "name") return a.dataset.name.localeCompare(b.dataset.name, "fr", { sensitivity: "base" });
    return (Number(a.dataset[field]) - Number(b.dataset[field])) * direction || a.dataset.name.localeCompare(b.dataset.name, "fr", { sensitivity: "base" });
  });
  cards.forEach(card => list.append(card));
}

function renderModelRuntimeIcons(item) {
  const icons = [{ name: "CPU", shortName: "CPU", markup: window.ETUSVG?.icon("cpu", "") || "" }];
  const compatibility = item.compatibility || {};
  const backend = String(modelRuntimeHardware.recommended_backend || "cpu").toLowerCase();
  if (compatibility.has_gpu_acceleration) {
    if (backend.startsWith("cuda")) icons.push({ name: "NVIDIA CUDA", shortName: "GPU", markup: '<img src="/static/provider-icons/nvidia.svg" alt="">' });
    else if (backend.startsWith("vulkan")) icons.push({ name: "GPU Vulkan", shortName: "GPU", markup: window.ETUSVG?.icon("gpu", "") || "" });
    else if (backend === "metal") icons.push({ name: "GPU Metal", shortName: "GPU", markup: '<img src="/static/platform-icons/macos.svg" alt="">' });
    else icons.push({ name: "GPU", shortName: "GPU", markup: window.ETUSVG?.icon("gpu", "") || "" });
  }
  if (modelRuntimeHardware.npus?.length && (compatibility.npu_supported || item.npu_supported || compatibility.has_npu_acceleration)) {
    icons.push({ name: "NPU Intel OpenVINO validé pour ce modèle", shortName: "NPU", markup: window.ETUSVG?.icon("npu", "") || "" });
  } else if (modelRuntimeHardware.npus?.length && (compatibility.npu_candidate || item.npu_candidate)) {
    icons.push({ name: "Candidat NPU · modèle non validé avec ce moteur", shortName: "NPU ?", markup: window.ETUSVG?.icon("npu", "") || "" });
  }
  return icons.map(({ name, shortName, markup }) => `<span class="local-model-runtime-icon" role="img" title="${escapeHtml(name)}" aria-label="${escapeHtml(name)}">${markup}<small>${escapeHtml(shortName)}</small></span>`).join("");
}

function showModelsTab(targetTabId, { fromRoute = false } = {}) {
  document.querySelectorAll(".modal-tab-btn").forEach(button => {
    const active = button.dataset.tab === targetTabId;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".modal-tab-content").forEach(content => content.classList.toggle("active", content.id === targetTabId));
  document.querySelector("#modal-models .modal-body").scrollTop = 0;
  if (targetTabId === "tab-hf-explorer") {
    const resultsEl = document.getElementById("hf-search-results");
    if (resultsEl && (!resultsEl.children.length || resultsEl.textContent.includes("Prêt"))) loadHfExplorerResults();
  }
  if (!fromRoute && (document.getElementById("modal-models")?.classList.contains("active")
      || (!document.getElementById("settings-view")?.hidden && document.querySelector('[data-settings-panel="models"]:not([hidden])')))) {
    syncModelsRoute();
  }
}

async function loadModelCatalog({ throwOnError = false } = {}) {
  try {
    const resp = await fetch("/api/models/catalog", { cache: "no-store" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "La bibliothèque locale ne peut pas être actualisée.");
    const localList = document.getElementById("catalog-local-list");
    if (!localList) return;
    modelRuntimeHardware = data.hardware || {};
    installedNativeModels = Array.isArray(data.native_models) ? data.native_models : [];
    localList.replaceChildren();

    const installed = data.catalog.filter((m) => m.is_installed);
    modelCatalogItems = data.catalog;
    const visionSelect = document.getElementById("vision-model-select");
    const previousSelection = visionSelect.value;
    visionSelect.replaceChildren();
    const visionModels = installed.filter(model => !isTextOnlyModel(model));
    if (!visionModels.length) {
      visionSelect.add(new Option("Aucun modèle vision installé", ""));
      visionSelect.disabled = true;
      document.getElementById("mmproj-panel").hidden = true;
    } else {
      visionSelect.disabled = false;
      visionModels.forEach(model => visionSelect.add(new Option(model.name, model.filename)));
      visionSelect.value = visionModels.some(model => model.filename === previousSelection) ? previousSelection : visionModels[0].filename;
      if (document.getElementById("tab-vision").classList.contains("active") && document.getElementById("mmproj-panel").dataset.model !== visionSelect.value) {
        openProjectorManager(visionModels.find(model => model.filename === visionSelect.value));
      }
    }
    availableInstalledModels = installed;
    remoteApiModels = Array.isArray(data.remote_models) ? data.remote_models : [];
    updateModelUI(currentActiveModel, availableInstalledModels);

    installed.forEach((item) => {
      const card = document.createElement("div");
      card.className = "catalog-item is-installed local-model-row";
      card.dataset.name = item.name || item.filename;
      card.dataset.disk = Number(item.local_size_gib || item.size_gib || 0);
      card.dataset.ram = Number(item.required_ram_gib || item.compatibility?.required_ram_gib || 0);
      const parameterCount = String(item.name || item.filename).match(/(?:^|[^\d])(\d+(?:\.\d+)?)\s*[bB](?:\b|[-_])/);
      card.dataset.params = parameterCount ? Number(parameterCount[1]) : 0;
      card.dataset.catalogSearch = `${item.name} ${item.filename} ${item.description} ${item.category || ""} ${item.repo || ""}`.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase();
      const brand = downloadBrand(item);
      const displayName = (item.name || item.filename).replace(/\s*\(Modèle Local Détecté\)/i, "");
      const quant = item.filename.match(/(?:^|[-_.])(IQ\d[\w]*|Q\d[\w]*|BF16|F16|F32)(?=[-.]|$)/i)?.[1] || item.quant || "GGUF";
      const formatSize = value => Number(value) > 0 ? `${Number(value).toLocaleString("fr-FR", { maximumFractionDigits: 2 })} Go` : "—";

      card.innerHTML = `
        <div class="local-model-identity">
          <span class="local-model-logo"><img src="${escapeHtml(brand.logo)}" alt="" loading="lazy"></span>
          <div class="local-model-copy">
            <strong title="${escapeHtml(item.name)}">${escapeHtml(displayName)}</strong>
            <span class="local-model-filename" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</span>
            <p title="${escapeHtml(item.description || "")}">${escapeHtml(item.description || "")}</p>
            <div class="local-model-tags">
              ${parameterCount ? `<span>${escapeHtml(parameterCount[1])} B paramètres</span>` : ""}
              ${isModelNpuCandidate(item) ? `<span class="local-model-npu-tag" title="Candidat NPU ; backend et accélération à confirmer">${window.ETUSVG?.icon("npu", "") || ""} NPU</span>` : ""}
              <span class="local-model-fit" title="Estimation de la charge sur cet ordinateur">${escapeHtml(item.compatibility?.badge || "Compatibilité inconnue")}</span>
            </div>
            <div class="local-model-runtime" aria-label="Matériel et accélérateurs">${renderModelRuntimeIcons(item)}</div>
          </div>
        </div>
        <div class="local-model-stat"><span class="local-model-mobile-label">Disque</span>${formatSize(card.dataset.disk)}</div>
        <div class="local-model-stat"><span class="local-model-mobile-label">RAM estimée</span>${formatSize(card.dataset.ram)}</div>
        <div class="local-model-quant"><span>${escapeHtml(quant.toUpperCase())}</span></div>
        <details class="local-model-menu">
          <summary aria-label="Actions pour ${escapeHtml(displayName)}" title="Actions du modèle"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg></summary>
          <div class="local-model-menu-panel">
            <button class="etu-btn etu-btn-secondary btn-local-model-details" type="button">Voir la fiche technique</button>
            <button class="btn-uninstall-local" type="button" data-filename="${escapeHtml(item.filename)}">Désinstaller</button>
          </div>
        </details>
      `;

      const uninstallButton = card.querySelector(".btn-uninstall-local");
      card.querySelector(".btn-local-model-details").addEventListener("click", () => {
        card.querySelector(".local-model-menu").open = false;
        void openModelIdentity(item, item.filename.replace(/\.gguf$/i, ""));
      });
      card.querySelector(".local-model-menu").addEventListener("toggle", event => {
        if (event.currentTarget.open) localList.querySelectorAll(".local-model-menu[open]").forEach(menu => {
          if (menu !== event.currentTarget) menu.open = false;
        });
      });
      uninstallButton.addEventListener("click", async () => {
        if (!window.confirm(`Désinstaller « ${item.filename} » de cet ordinateur ?`)) return;
        uninstallButton.disabled = true;
        try {
          const response = await fetch(`/api/models/${encodeURIComponent(item.filename)}`, { method: "DELETE" });
          const result = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(result.detail || "La désinstallation a échoué.");
          showToast("Modèle désinstallé.", "success");
          await loadModelCatalog();
          await loadServerStatus();
        } catch (error) {
          showToast(error.message || "La désinstallation a échoué.", "warning");
          uninstallButton.disabled = false;
        }
      });

      localList.appendChild(card);
      if (item.is_installed) {
        const projectorButton = document.createElement("button");
        projectorButton.type = "button";
        projectorButton.className = "etu-btn etu-btn-secondary catalog-vision-btn";
        const info = item.mmproj || {};
        const textOnly = isTextOnlyModel(item);
        projectorButton.textContent = info.installed ? "Gérer le projecteur vision" : "Ajouter un projecteur vision";
        if (!textOnly) {
          projectorButton.addEventListener("click", () => openProjectorManager(item));
          card.querySelector(".local-model-menu-panel").prepend(projectorButton);
        }
      }
    });
    for (const item of data.native_models || []) {
      const card = document.createElement("div");
      card.className = "catalog-item is-installed local-model-row";
      card.dataset.name = item.repo;
      card.dataset.disk = item.size_gib;
      card.dataset.ram = "0";
      card.dataset.params = "0";
      card.dataset.catalogSearch = `${item.repo} ${item.format} ${item.quant}`.toLowerCase();
      card.innerHTML = `<div class="local-model-identity"><div class="local-model-copy">
        <strong>${escapeHtml(item.repo.split("/").pop())}</strong>
        <span class="local-model-filename">${escapeHtml(item.filename)}</span>
        <p>${escapeHtml(item.format.toUpperCase())} · fichiers installés · moteur d’exécution à intégrer</p>
        </div></div><div class="local-model-stat">${Number(item.size_gib).toLocaleString("fr-FR")} Go</div>
        <div class="local-model-stat">—</div><div class="local-model-quant">${escapeHtml(item.quant)}</div>
        <button type="button" class="etu-btn etu-btn-secondary">Désinstaller</button>`;
      card.querySelector("button").addEventListener("click", async event => {
        if (!window.confirm(`Désinstaller ${item.repo} (${item.quant}) ?`)) return;
        event.currentTarget.disabled = true;
        try {
          const response = await fetch(`/api/models/native/${encodeURIComponent(item.id)}`, { method: "DELETE" });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || "Désinstallation impossible");
          await loadModelCatalog();
        } catch (error) {
          card.querySelector("button").disabled = false;
          showToast(error.message, "warning");
        }
      });
      localList.appendChild(card);
    }
    const localEmpty = document.getElementById("catalog-local-empty");
    if (localEmpty) localEmpty.hidden = installed.length > 0 || (data.native_models || []).length > 0;
    sortLocalModelCards();
    applyModelCatalogSearch();
    return data;
  } catch (err) {
    if (throwOnError) throw err;
    console.error("Erreur chargement catalogue :", err);
  }
}

async function openProjectorManager(item) {
  const panel = document.getElementById("mmproj-panel");
  showModelsTab("tab-vision");
  document.getElementById("vision-model-select").value = item.filename;
  panel.hidden = false;
  panel.dataset.model = item.filename;
  panel.innerHTML = `<h4>Projecteur pour ${escapeHtml(item.name || item.filename)}</h4>
    <p class="mmproj-association"></p>
    <div class="mmproj-import-box">
      <strong>Importer depuis cet ordinateur</strong>
      <p>Fichier GGUF nommé mmproj ou projector, correspondant exactement à ce modèle. 8 Go maximum.</p>
      <input class="mmproj-local" type="file" accept=".gguf" hidden>
      <button type="button" class="etu-btn etu-btn-primary mmproj-pick">Choisir un fichier .gguf</button>
    </div>
    <details class="mmproj-online" open>
      <summary>Rechercher sur Hugging Face</summary>
      <label>Dépôt exact du modèle <input class="mmproj-repo" type="text" placeholder="auteur/modèle-GGUF"></label>
      <button type="button" class="etu-btn etu-btn-secondary mmproj-search">Rechercher les projecteurs correspondants</button>
      <div class="mmproj-candidates"></div>
    </details>
    <div class="mmproj-actions">
      <button type="button" class="etu-btn etu-btn-primary mmproj-apply">Charger / recharger le modèle</button>
      <button type="button" class="etu-btn etu-btn-secondary mmproj-remove">Retirer l'association</button>
    </div>
    <p class="mmproj-status" role="status"></p>`;
  const model = item.filename;
  const repoInput = panel.querySelector(".mmproj-repo");
  repoInput.value = item.mmproj?.repo || (item.repo !== "local" ? item.repo : "") || "";
  const status = panel.querySelector(".mmproj-status");
  const setStatus = (message, type = "info") => {
    status.textContent = message;
    status.dataset.type = type;
  };
  const showAssociation = (data) => {
    const association = data.association;
    panel.dataset.association = association?.local_filename || "";
    panel.querySelector(".mmproj-association").textContent = association
      ? `${association.filename} · ${data.installed ? "installé" : "fichier à télécharger"}${association.manual ? " · association manuelle" : ""}`
      : "Aucun projecteur associé.";
    panel.querySelector(".mmproj-pick").disabled = Boolean(data.text_only);
    panel.querySelector(".mmproj-remove").disabled = !association;
    const apply = panel.querySelector(".mmproj-apply");
    apply.disabled = Boolean(association && !data.installed);
    apply.textContent = association ? "Charger le modèle avec ce projecteur" : "Charger le modèle sans projecteur";
  };
  showAssociation(item.mmproj || {});
  const search = async () => {
    const searchButton = panel.querySelector(".mmproj-search");
    if (searchButton.disabled) return;
    searchButton.disabled = true;
    searchButton.textContent = "Recherche en cours…";
    panel.setAttribute("aria-busy", "true");
    setStatus("Vérification du dépôt et de la variante du modèle…");
    const params = new URLSearchParams({ model });
    if (repoInput.value.trim()) params.set("repo", repoInput.value.trim());
    try {
      const response = await fetch(`/api/models/mmproj?${params}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Recherche impossible");
      if (panel.dataset.model !== model) return;
      showAssociation(data);
      setStatus(data.message);
      const list = panel.querySelector(".mmproj-candidates");
      list.replaceChildren();
      data.candidates.forEach((candidate, index) => {
        const row = document.createElement("div");
        row.className = "mmproj-candidate";
        const label = document.createElement("span");
        label.textContent = `${candidate.filename} · ${candidate.size_gib} Go${index === 0 ? " · conseillé" : ""}`;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "etu-btn etu-btn-secondary";
        button.textContent = candidate.is_installed ? "Désinstaller" : "Télécharger et associer";
        button.addEventListener("click", async () => {
          button.disabled = true;
          if (candidate.is_installed) {
            button.textContent = "Désinstallation…";
            setStatus(`Désinstallation de ${candidate.filename}…`);
            try {
              const response = await fetch(`/api/models/mmproj/${encodeURIComponent(candidate.local_filename)}`, { method: "DELETE" });
              const result = await response.json();
              if (!response.ok) throw new Error(result.detail || "Désinstallation impossible");
              candidate.is_installed = false;
              button.disabled = false;
              button.textContent = "Télécharger et associer";
              if (panel.dataset.model === model && panel.dataset.association === candidate.local_filename) {
                showAssociation({ association: null });
              }
              setStatus("Projecteur désinstallé.", "success");
              loadModelCatalog();
            } catch (error) {
              button.disabled = false;
              button.textContent = "Réessayer la désinstallation";
              setStatus(error.message, "error");
            }
            return;
          }
          button.textContent = candidate.is_installed ? "Association en cours…" : "Démarrage du téléchargement…";
          setStatus(candidate.is_installed ? "Association du projecteur…" : "Démarrage du téléchargement du projecteur…");
          let downloading = false;
          try {
            const result = await fetch("/api/models/mmproj/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model, repo: candidate.repo, filename: candidate.filename }) });
            const saved = await result.json();
            if (!result.ok) throw new Error(saved.detail || "Association impossible");
            if (saved.task_id) {
              downloading = true;
              button.textContent = "Téléchargement en cours…";
              pollDownloadProgress(saved.task_id, candidate.filename, () => {
                if (panel.dataset.model === model && panel.dataset.association === saved.association.local_filename) {
                  showAssociation({ ...saved, installed: true });
                  setStatus("Projecteur téléchargé et associé. Rechargez le modèle pour l'utiliser.", "success");
                }
              }, task => {
                if (!button.isConnected) return;
                if (task.status === "completed") {
                  candidate.is_installed = true;
                  button.textContent = "Projecteur installé";
                } else if (["error", "cancelled"].includes(task.status)) {
                  button.disabled = false;
                  button.textContent = "Réessayer le téléchargement";
                  if (panel.dataset.model === model) setStatus(task.error_message || "Téléchargement annulé.", task.status === "error" ? "error" : "info");
                } else {
                  button.textContent = task.total_bytes ? `Téléchargement en cours · ${Math.round(task.percentage || 0)} %` : task.downloaded_bytes ? "Téléchargement en cours…" : "Connexion à Hugging Face…";
                }
              });
            } else {
              button.textContent = "Projecteur associé";
            }
            if (panel.dataset.model === model) {
              showAssociation(saved);
              setStatus(saved.installed ? "Projecteur associé. Rechargez le modèle pour l'utiliser." : "Téléchargement lancé. Attendez sa fin, puis rechargez le modèle.", saved.installed ? "success" : "info");
            }
            loadModelCatalog();
          } catch (error) {
            button.textContent = "Réessayer le téléchargement";
            setStatus(error.message, "error");
          } finally {
            if (!downloading) button.disabled = false;
          }
        });
        row.append(label, button);
        list.appendChild(row);
      });
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      if (panel.dataset.model === model) {
        searchButton.disabled = false;
        searchButton.textContent = "Rechercher les projecteurs correspondants";
        panel.removeAttribute("aria-busy");
      }
    }
  };
  panel.querySelector(".mmproj-search").addEventListener("click", search);
  panel.querySelector(".mmproj-pick").addEventListener("click", () => panel.querySelector(".mmproj-local").click());
  panel.querySelector(".mmproj-local").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const pick = panel.querySelector(".mmproj-pick");
    pick.disabled = true;
    setStatus(`Import de ${file.name} en cours… Gardez cette fenêtre ouverte.`);
    try {
      if (file.size > 8 * 1024 ** 3) throw new Error("8 Go maximum par projecteur.");
      if (!/\.gguf$/i.test(file.name) || !/(mmproj|projector)/i.test(file.name)) throw new Error("Choisissez un fichier .gguf dont le nom contient mmproj ou projector.");
      const params = new URLSearchParams({ model, filename: file.name });
      const response = await fetch(`/api/models/mmproj/import?${params}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Import impossible");
      if (panel.dataset.model === model) {
        showAssociation(data);
        setStatus("Projecteur importé et associé. Chargez le modèle pour vérifier la vision.", "success");
      }
      loadModelCatalog();
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      pick.disabled = false;
      event.target.value = "";
    }
  });
  panel.querySelector(".mmproj-remove").addEventListener("click", async () => {
    const response = await fetch(`/api/models/mmproj?${new URLSearchParams({ model })}`, { method: "DELETE" });
    if (!response.ok) { setStatus("Suppression de l'association impossible.", "error"); return; }
    showAssociation({ association: null });
    setStatus("Association retirée. Rechargez le modèle pour appliquer le changement.", "success");
    loadModelCatalog();
  });
  panel.querySelector(".mmproj-apply").addEventListener("click", () => switchActiveModel(model, item.name || model));
  search();
}

async function triggerDownload(repo, filename, button = null, reusable = false, modelFormat = "", onTerminal = null) {
  const initialLabel = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Démarrage du téléchargement…";
  }
  try {
    const resp = await fetch(modelFormat ? "/api/models/native/download" : "/api/models/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, filename, ...(modelFormat ? { model_format: modelFormat } : {}) }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Téléchargement refusé");
    if (data.task_id) {
      if (button) button.textContent = "Téléchargement en cours…";
      pollDownloadProgress(data.task_id, filename, null, task => {
        if (["completed", "error", "cancelled"].includes(task.status)) onTerminal?.(task);
        if (!button?.isConnected) return;
        if (["error", "cancelled"].includes(task.status)) {
          button.disabled = false;
          button.textContent = "Réessayer le téléchargement";
        } else {
          button.textContent = task.status === "completed" ? "Installé" : `Téléchargement en cours · ${Math.round(task.percentage || 0)} %`;
          if (task.status === "completed" && reusable) {
            button.disabled = false;
            button.textContent = initialLabel;
          }
        }
      });
      showToast(`Téléchargement de ${escapeHtml(filename)} lancé. Retrouvez sa progression dans Téléchargements.`, "info");
    } else {
      throw new Error("Le serveur n'a pas confirmé le démarrage du téléchargement.");
    }
  } catch (err) {
    onTerminal?.({ status: "error" });
    if (button) {
      button.disabled = false;
      button.textContent = "Réessayer le téléchargement";
    }
    showToast("Erreur lors du démarrage du téléchargement : " + err, "warning");
  }
}

function pollDownloadProgress(taskId, filename, onComplete = null, onUpdate = null) {
  if (!seenDownloadStatuses.has(taskId)) seenDownloadStatuses.set(taskId, "queued");
  refreshDownloadManager();
  const container = document.getElementById("active-tasks-container");
  if (!container) return;
  const taskBox = document.createElement("div");
  taskBox.className = "etu-card";
  taskBox.id = `task-${taskId}`;
  taskBox.innerHTML = `
    <div style="display: flex; justify-content: space-between;">
      <span style="font-weight: 600;">Téléchargement : ${escapeHtml(filename)}</span>
      <span class="task-percent">0%</span>
    </div>
    <div class="progress-bar-container">
      <div class="progress-bar-fill" style="width: 0%;"></div>
    </div>
    <div style="display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-muted); margin-top: 4px;">
      <span class="task-speed">0 Mo/s</span>
      <span class="task-status">Démarrage...</span>
    </div>
    <button type="button" class="task-cancel etu-btn etu-btn-secondary">Annuler</button>
  `;
  container.prepend(taskBox);
  const cancelButton = taskBox.querySelector(".task-cancel");
  cancelButton.addEventListener("click", async () => {
    const response = await fetch(`/api/models/tasks/${taskId}/cancel`, { method: "POST" });
    if (response.ok) cancelButton.disabled = true;
  });

  let polling = false;
  const poller = setInterval(async () => {
    if (polling) return;
    polling = true;
    try {
      const resp = await fetch(`/api/models/tasks/${taskId}`);
      if (!resp.ok) {
        if (resp.status === 404) {
          clearInterval(poller);
          onUpdate?.({ status: "error", error_message: "Le suivi du téléchargement a été interrompu. Relancez-le pour reprendre." });
        }
        throw new Error("Suivi temporairement indisponible");
      }
      const t = await resp.json();
      onUpdate?.(t);

      const percentEl = taskBox.querySelector(".task-percent");
      const fillEl = taskBox.querySelector(".progress-bar-fill");
      const speedEl = taskBox.querySelector(".task-speed");
      const statusEl = taskBox.querySelector(".task-status");

      if (percentEl) percentEl.textContent = `${t.percentage}%`;
      if (fillEl) fillEl.style.width = `${t.percentage}%`;
      if (speedEl) speedEl.textContent = `${t.speed_mbps} Mo/s`;
      if (statusEl) statusEl.textContent = t.status;

      if (t.status === "completed") {
        clearInterval(poller);
        if (seenDownloadStatuses.get(taskId) !== "completed") {
          seenDownloadStatuses.set(taskId, "completed");
          addAppNotification("Modèle téléchargé", `${filename} est prêt à être utilisé.`, "download", { eventKey: `download:${taskId}` });
        }
        if (onComplete) onComplete();
        cancelButton.remove();
        if (statusEl) {
          statusEl.textContent = "Téléchargé avec succès !";
          statusEl.style.color = "var(--success)";
        }
        loadModelCatalog();
        loadServerStatus();
      } else if (t.status === "error") {
        clearInterval(poller);
        cancelButton.remove();
        if (statusEl) {
          statusEl.textContent = `Erreur: ${t.error_message}`;
          statusEl.style.color = "var(--danger)";
        }
        showToast(`❌ Erreur téléchargement ${escapeHtml(filename)}: ${escapeHtml(t.error_message)}`, "warning");
      } else if (t.status === "cancelled") {
        clearInterval(poller);
        cancelButton.remove();
        if (statusEl) statusEl.textContent = "Annulé — relancez pour reprendre";
      }
    } catch (e) {
      const statusEl = taskBox.querySelector(".task-status");
      if (statusEl) statusEl.textContent = "Reconnexion au suivi du téléchargement…";
    } finally {
      polling = false;
    }
  }, 1000);
}

async function activateModel(filename, displayName = null) {
  closeModal("modal-models");
  await switchActiveModel(filename, displayName);
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
