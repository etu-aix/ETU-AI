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
let assistantProfiles = [];
let activeProfileId = "default";
let preferredProfileId = "default";
let pendingAvatarData = null;
const streamMarkup = new WeakMap();
const streamFrames = new WeakMap();
const appliedStreamTemplates = new WeakMap();

document.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.theme = localStorage.getItem("etu-theme") || "dark";
  document.getElementById("btn-theme").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("etu-theme", theme);
  });
  initSetupWizard();
  initModals();
  initModelSelector();
  initChatForm();
  document.getElementById("chat-messages").addEventListener("click", (event) => {
    const suggestion = event.target.closest("[data-prompt]");
    if (!suggestion) return;
    document.getElementById("chat-input").value = suggestion.dataset.prompt;
    document.getElementById("chat-form").requestSubmit();
  });
  initProfiles();
  initApiSettings();
  loadConversations();
  loadServerStatus();

  const newChatBtn = document.getElementById("btn-new-chat");
  if (newChatBtn) {
    newChatBtn.addEventListener("click", () => {
      startNewConversation();
    });
  }

  const reopenWizardBtn = document.getElementById("btn-reopen-wizard");
  if (reopenWizardBtn) {
    reopenWizardBtn.addEventListener("click", () => {
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
});

async function initApiSettings() {
  const open = document.getElementById("btn-open-api-settings");
  const form = document.getElementById("api-settings-form");
  if (!open || !form) return;
  const providerSelect = document.getElementById("api-provider-select");
  let presets = {};
  const setKeyStatus = (hasKey) => {
    const status = document.getElementById("api-key-status");
    status.textContent = hasKey ? "Clé enregistrée" : "Aucune clé";
    status.dataset.hasKey = String(Boolean(hasKey));
  };
  const renderProviderBrand = (id, config = {}) => {
    const provider = presets[id] || {};
    const labels = {
      openai: "OpenAI", anthropic: "Anthropic", gemini: "Google Gemini", mistral: "Mistral",
      openrouter: "OpenRouter", groq: "Groq", together: "Together AI", deepseek: "DeepSeek",
      xai: "xAI", cerebras: "Cerebras", fireworks: "Fireworks AI", perplexity: "Perplexity",
      nvidia: "NVIDIA NIM", sambanova: "SambaNova", deepinfra: "DeepInfra", "github-models": "GitHub Models",
      "openai-compatible": "API compatible OpenAI", custom: "Fournisseur personnalisé",
    };
    const marks = { openai: "✳", anthropic: "✹", gemini: "✦", mistral: "M", openrouter: "◈", groq: "G", together: "T", deepseek: "D", xai: "X", cerebras: "C", fireworks: "F", perplexity: "P", nvidia: "N", sambanova: "S", deepinfra: "DI", "github-models": "GH", "openai-compatible": "AI", custom: "＋" };
    const card = document.getElementById("api-provider-brand");
    card.dataset.provider = id;
    document.getElementById("api-provider-mark").textContent = marks[id] || "AI";
    document.getElementById("api-provider-brand-name").textContent = provider.label || labels[id] || id;
    document.getElementById("api-provider-brand-detail").textContent = `${provider.type === "openai" ? "API compatible OpenAI" : "Connexion API native"} · clé stockée localement`;
    document.getElementById("api-provider-brand-state").textContent = config.enabled ? "Actif" : (config.has_api_key ? "Clé enregistrée" : "À configurer");
    card.classList.toggle("is-active", Boolean(config.enabled));
  };
  const updatePrivacyHint = (config) => {
    const hint = document.getElementById("chat-privacy-hint");
    const active = config?.active_provider;
    const providerLabel = active && config.providers?.[active]?.label;
    if (hint) hint.textContent = providerLabel ? `Messages transmis à ${providerLabel}.` : "Les réponses restent sur cet ordinateur.";
  };
  const applyProviderPreset = (id, preserveValues = false) => {
    const preset = presets[id];
    if (!preset) return;
    const nativeProvider = preset.type !== "openai";
    document.getElementById("api-provider-url-wrap").hidden = nativeProvider;
    document.getElementById("api-provider-url").required = !nativeProvider;
    if (!preserveValues) {
      document.getElementById("api-provider-url").value = preset.base_url;
      document.getElementById("api-provider-model").value = preset.model || "";
    }
  };
  providerSelect.addEventListener("change", async () => {
    applyProviderPreset(providerSelect.value);
    document.getElementById("api-provider-key").value = "";
    document.getElementById("api-provider-clear-key").checked = false;
    try {
      const response = await fetch(`/api/settings/provider?provider=${encodeURIComponent(providerSelect.value)}`);
      if (!response.ok) throw new Error("Impossible de charger les réglages de ce fournisseur.");
      const config = await response.json();
      renderProviderBrand(providerSelect.value, config);
      document.getElementById("api-provider-url").value = config.base_url || presets[providerSelect.value]?.base_url || "";
      document.getElementById("api-provider-model").value = config.model || presets[providerSelect.value]?.model || "";
      document.getElementById("api-provider-enabled").checked = Boolean(config.enabled);
      setKeyStatus(config.has_api_key);
    } catch (error) {
      document.getElementById("api-settings-message").textContent = error.message;
    }
  });
  fetch("/api/settings/provider").then((response) => response.ok ? response.json() : null).then(updatePrivacyHint).catch(() => {});
  open.addEventListener("click", async () => {
    const message = document.getElementById("api-settings-message");
    message.textContent = "";
    try {
      const response = await fetch("/api/settings/provider");
      if (!response.ok) throw new Error("Impossible de charger la configuration.");
      const config = await response.json();
      presets = config.providers || {};
      providerSelect.value = config.provider || "openai-compatible";
      if (!providerSelect.value) providerSelect.value = "custom";
      renderProviderBrand(providerSelect.value, config);
      applyProviderPreset(providerSelect.value, true);
      document.getElementById("api-provider-enabled").checked = Boolean(config.enabled);
      document.getElementById("api-provider-url").value = config.base_url || presets[providerSelect.value]?.base_url || "https://api.openai.com/v1";
      document.getElementById("api-provider-model").value = config.model || presets[providerSelect.value]?.model || "";
      document.getElementById("api-provider-key").value = "";
      document.getElementById("api-provider-clear-key").checked = false;
      setKeyStatus(config.has_api_key);
      openModal("modal-api-settings");
    } catch (error) { message.textContent = error.message; }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = document.getElementById("api-settings-message");
    message.textContent = "Enregistrement…";
    const payload = {
      enabled: document.getElementById("api-provider-enabled").checked,
      base_url: document.getElementById("api-provider-url").value,
      model: document.getElementById("api-provider-model").value,
      api_key: document.getElementById("api-provider-key").value,
      clear_api_key: document.getElementById("api-provider-clear-key").checked,
      provider: providerSelect.value,
    };
    try {
      const response = await fetch("/api/settings/provider", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Enregistrement impossible.");
      document.getElementById("api-provider-key").value = "";
      setKeyStatus(result.has_api_key);
      renderProviderBrand(providerSelect.value, result);
      updatePrivacyHint(result);
      message.textContent = result.enabled ? "Fournisseur activé et configuration enregistrée." : "Configuration enregistrée.";
      loadServerStatus();
    } catch (error) { message.textContent = error.message; }
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
  document.getElementById("btn-wizard-to-step2").addEventListener("click", () => {
    showWizardStep(2);
    loadWizardModelSelection();
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

  document.getElementById("btn-wizard-finish").addEventListener("click", () => {
    wizardContainer.style.display = "none";
    appContainer.style.display = "flex";
    loadServerStatus();
    loadConversations();
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
    if (statusData.completed) {
      wizardContainer.style.display = "none";
      appContainer.style.display = "flex";
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
    wizardContainer.style.display = "flex";
    appContainer.style.display = "none";
    showWizardStep(1);
  }
}

function showWizardStep(stepNum) {
  document.querySelectorAll(".wizard-step").forEach((el) => el.classList.remove("active"));
  const target = document.getElementById(`wizard-step-${stepNum}`);
  if (target) target.classList.add("active");
  document.getElementById("wizard-container").scrollTop = 0;
}

async function loadHardwareDiagnosis() {
  try {
    const resp = await fetch("/api/status");
    const data = await resp.json();

    const hwCpu = document.getElementById("hw-cpu");
    const hwRam = document.getElementById("hw-ram");
    const hwGpu = document.getElementById("hw-gpu");
    const hwNpu = document.getElementById("hw-npu");
    const hwRec = document.getElementById("hw-rec-backend");
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
        hwNpu.textContent = `⚡ ${npuNames}`;
        hwNpu.style.color = "#38bdf8";
        if (moodText) moodText.textContent = "Puce NPU détectée ; backend à confirmer";
        if (wizardParticles) wizardParticles.setTheme("cyan");
      } else {
        hwNpu.textContent = "Aucun NPU détecté";
        hwNpu.style.color = "var(--text-muted)";
        if (moodText) moodText.textContent = "Diagnostic matériel complété";
        if (wizardParticles) wizardParticles.setTheme("emerald");
      }
    }

    const backend = (data.backend || "cpu").toUpperCase();
    if (hwRec) hwRec.textContent = `Accélération : ${backend}`;
    if (hwBadge) {
      hwBadge.textContent = backend === "CPU" ? "Mode CPU" : `GPU ${backend}`;
      hwBadge.className = `etu-badge ${backend === "CPU" ? "etu-badge-warning" : "etu-badge-success"}`;
    }
  } catch (err) {
    console.error("Erreur diagnostic matériel :", err);
  }
}

let wizardSearchDebounceTimer = null;
let wizardHfSearchBound = false;
let allLoadedWizardModels = [];
let activeWizardCategory = "all";
const wizardSelectedModels = new Map();
let wizardSelectionInitialized = false;

async function loadWizardModelSelection(searchQuery = "") {
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
            wizardSelectedModels.set(chosen.filename, { repo: chosen.repo, filename: chosen.filename, npu_compatible: !!chosen.npu_compatible });
            renderWizardModelCards(allLoadedWizardModels, activeWizardCategory);
            const primaryName = data.primary_model?.name || recs[0].name;
            showToast(`⚡ IA optimale sélectionnée pour ce PC : <strong>${escapeHtml(primaryName)}</strong>`, "success", 4000);
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

  container.innerHTML = "<p class='etu-text'>Analyse de compatibilité et recherche sur Hugging Face...</p>";

  try {
    let modelsList = [];
    let hardwareData = {};

    if (searchQuery) {
      // Recherche en direct via l'API Hugging Face sans credential
      const resp = await fetch(
        `/api/models/huggingface/search?q=${encodeURIComponent(searchQuery)}&only_compatible=${onlyCompatible}&limit=60`
      );
      const data = await resp.json();
      modelsList = data.models || [];
      hardwareData = data.hardware || {};
    } else {
      // Catalogue global enrichi de toutes les IA compatibles
      const resp = await fetch("/api/models/catalog");
      const data = await resp.json();
      modelsList = data.catalog || [];
      hardwareData = data.hardware || {};
    }

    allLoadedWizardModels = modelsList;

    // Affichage bannière NPU si détecté
    const npuBanner = document.getElementById("wizard-npu-banner");
    if (npuBanner) {
      if (hardwareData && hardwareData.has_npu && hardwareData.npus && hardwareData.npus.length > 0) {
        const npuName = hardwareData.npus[0].name;
        const npuTitle = document.getElementById("npu-banner-title");
        const npuText = document.getElementById("npu-banner-text");
        if (npuTitle) npuTitle.textContent = `Puce NPU Détectée : ${npuName}`;
        if (npuText) npuText.textContent = `Votre processeur intègre l'unité neurale ${npuName}. Sa présence ne garantit pas que le moteur utilisera ce NPU.`;
        npuBanner.style.display = "flex";
      } else {
        npuBanner.style.display = "none";
      }
    }

    // Mise à jour des compteurs de catégories
    updateCategoryCounts(modelsList);

    // Rendu des cartes selon la catégorie active
    renderWizardModelCards(modelsList, activeWizardCategory);

  } catch (err) {
    container.innerHTML = `<p class='etu-text etu-badge-warning'>Erreur chargement catalogue : ${err}</p>`;
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
    const isNpu = m.npu_compatible || (m.size_gib && m.size_gib <= 3.8);
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

function renderWizardModelCards(models, categoryFilter = "all") {
  const container = document.getElementById("wizard-model-selection-list");
  const counterEl = document.getElementById("selected-models-count");
  const moodText = document.getElementById("wizard-mood-text");
  if (!container) return;

  container.innerHTML = "";
  if (!wizardSelectionInitialized && models.length) {
    const initial = models.find((m) => m.is_installed) || models[0];
    wizardSelectedModels.set(initial.filename, { repo: initial.repo, filename: initial.filename, npu_compatible: !!initial.npu_compatible });
    wizardSelectionInitialized = true;
  }

  const filtered = models.filter((m) => {
    if (categoryFilter === "all") return true;
    if (categoryFilter === "installed") return m.is_installed;
    const text = `${m.name} ${m.repo} ${m.description} ${(m.tags || []).join(" ")}`.toLowerCase();
    if (categoryFilter === "npu") return m.npu_compatible || (m.size_gib && m.size_gib <= 3.8);
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
    if (moodText) {
      moodText.textContent = `${selectedCount} modèle(s) sélectionné(s) pour votre machine`;
    }
  };

  filtered.forEach((item) => {
    const isSelected = wizardSelectedModels.has(item.filename);
    const card = document.createElement("div");
    card.className = `model-select-card ${isSelected ? "selected" : ""} ${item.is_installed ? "is-installed" : ""} ${item.npu_compatible ? "is-npu-card" : ""}`;
    card.dataset.repo = item.repo;
    card.dataset.filename = item.filename;
    card.dataset.npu = item.npu_compatible ? "true" : "false";

    let badgesHtml = `<span class="etu-badge etu-badge-${item.compatibility?.color || 'info'}">${item.compatibility?.badge || 'Compatible'}</span>`;
    if (item.npu_compatible) {
      badgesHtml += ` <span class="etu-badge etu-badge-cyan">⚡ Modèle léger</span>`;
    }
    if (item.is_installed) {
      const sizeInfo = item.local_size_gib ? `${item.local_size_gib} Go` : `${item.size_gib} Go`;
      badgesHtml += ` <span class="etu-badge etu-badge-success">✓ Déjà présent (${sizeInfo})</span>`;
    }
    if (item.is_custom_local) {
      badgesHtml += ` <span class="etu-badge etu-badge-purple">📁 Modèle Local</span>`;
    }
    if (item.is_huggingface_live) {
      badgesHtml += ` <span class="etu-badge etu-badge-info">🤗 Hugging Face</span>`;
    } else if (item.file_verified === false && !item.is_installed) {
      badgesHtml += ` <span class="etu-badge etu-badge-warning">Fichier à vérifier</span>`;
    }

    const installedNotice = item.is_installed
      ? `<div class="model-installed-flag">✓ Déjà détecté sur votre disque (aucun téléchargement requis)</div>`
      : "";

    const ramRequired = item.compatibility?.required_ram_gib || item.required_ram_gib || (item.size_gib ? Math.round(item.size_gib * 1.3) : 6.0);

    card.innerHTML = `
      <input type="checkbox" id="chk-${escapeHtml(item.id || item.filename)}" ${isSelected ? "checked" : ""}>
      <div class="model-card-details">
        <div class="model-card-top">
          <span class="model-card-title">${escapeHtml(item.name)}</span>
          <div style="display: flex; gap: 6px; flex-wrap: wrap;">
            ${badgesHtml}
          </div>
        </div>
        <p class="model-card-desc">${escapeHtml(item.description || item.repo)}</p>
        <div class="model-card-meta">
          <span>💾 Taille : ${item.size_gib} Go</span>
          <span>🧠 RAM requise : ~${ramRequired} Go</span>
          <span>🏷️ Format : ${item.quant || 'Q4_K_M'}</span>
          ${item.downloads ? `<span>⬇️ ${item.downloads.toLocaleString()} dl</span>` : ""}
        </div>
        ${installedNotice}
      </div>
    `;

    // Réactivité au survol
    card.addEventListener("mouseenter", () => {
      if (item.npu_compatible && wizardParticles) {
        wizardParticles.setTheme("cyan");
      } else if (ramRequired >= 8 && wizardParticles) {
        wizardParticles.setTheme("purple");
      }
    });

    // Clic sur la carte
    card.addEventListener("click", (e) => {
      const chk = card.querySelector("input[type='checkbox']");
      if (e.target !== chk) {
        chk.checked = !chk.checked;
      }
      if (chk.checked) {
        card.classList.add("selected");
        wizardSelectedModels.set(item.filename, { repo: item.repo, filename: item.filename, npu_compatible: !!item.npu_compatible });
      } else {
        card.classList.remove("selected");
        wizardSelectedModels.delete(item.filename);
      }
      updateCounter();

      if (wizardParticles) {
        const rect = card.getBoundingClientRect();
        const rippleX = e.clientX || (rect.left + rect.width / 2);
        const rippleY = e.clientY || (rect.top + rect.height / 2);
        wizardParticles.addRipple(rippleX, rippleY, 1.3);
      }
    });

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
      body: JSON.stringify({ selected_models: selectedModels }),
    });
    const data = await resp.json();
    if (data.started || data.status === "running") {
      startPollingSetupProgress();
    }
  } catch (err) {
    alert("Impossible de démarrer l'installation : " + err);
  }
}

function startPollingSetupProgress() {
  if (setupPollingInterval) clearInterval(setupPollingInterval);

  setupPollingInterval = setInterval(async () => {
    try {
      const resp = await fetch("/api/setup/status");
      const data = await resp.json();

      const bar = document.getElementById("wizard-progress-bar");
      const percentEl = document.getElementById("wizard-progress-percent");
      const statusText = document.getElementById("wizard-progress-status");
      const masterOrb = document.getElementById("wizard-master-orb");
      const installSub = document.getElementById("install-subtitle");
      const moodText = document.getElementById("wizard-mood-text");

      if (bar) bar.style.width = `${data.progress_percent}%`;
      if (percentEl) percentEl.textContent = `${data.progress_percent}%`;

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

        item.classList.remove("running", "completed");
        const icon = item.querySelector(".step-icon");

        if (step.status === "running") {
          item.classList.add("running");
          if (icon) icon.innerHTML = '<span class="etu-spinner" style="border-width:2px;width:12px;height:12px;"></span>';
          if (statusText) statusText.textContent = step.title;
          if (moodText) moodText.textContent = `En cours : ${step.title}`;
        } else if (step.status === "completed") {
          item.classList.add("completed");
          if (icon) icon.textContent = "✓";
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

        if (wizardParticles) {
          wizardParticles.setTheme("emerald");
          // Multiples ondes de choc festives
          wizardParticles.addRipple(window.innerWidth / 2, window.innerHeight * 0.4, 2.5);
          setTimeout(() => wizardParticles.addRipple(window.innerWidth * 0.35, window.innerHeight * 0.45, 1.8), 250);
          setTimeout(() => wizardParticles.addRipple(window.innerWidth * 0.65, window.innerHeight * 0.45, 1.8), 500);
        }

        const titleEl = document.getElementById("install-title");
        if (titleEl) {
          titleEl.textContent = "Installation terminée avec succès !";
          titleEl.style.color = "#34d399";
        }
        if (statusText) {
          statusText.textContent = "Serveur MAIA-Beacon actif sur le port 11343 !";
          statusText.style.color = "#34d399";
        }
        if (installSub) {
          installSub.textContent = "Votre assistant ETU-AI est configuré, optimisé et prêt à l'emploi.";
        }
        if (moodText) {
          moodText.textContent = "Prêt pour les études & la recherche !";
        }

        const finishBtn = document.getElementById("btn-wizard-finish");
        if (finishBtn) finishBtn.style.display = "inline-flex";
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
      }
    } catch (err) {
      console.warn("Erreur de suivi setup :", err);
    }
  }, 800);
}

/* -------------------------------------------------------------------------- */
/* Modals Setup                                                               */
/* -------------------------------------------------------------------------- */
function initModals() {
  const openModelsBtn = document.getElementById("btn-open-models");
  if (openModelsBtn) {
    openModelsBtn.addEventListener("click", () => {
      openModal("modal-models");
      loadModelCatalog();
    });
  }

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
      document.querySelectorAll(".modal-tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".modal-tab-content").forEach((c) => c.classList.remove("active"));
      tabBtn.classList.add("active");
      const targetContent = document.getElementById(targetTabId);
      if (targetContent) targetContent.classList.add("active");

      if (targetTabId === "tab-hf-explorer") {
        const resultsEl = document.getElementById("hf-search-results");
        if (resultsEl && (!resultsEl.children.length || resultsEl.textContent.includes("Prêt"))) {
          loadHfExplorerResults();
        }
      }
    });
  });

  // Recherche Hugging Face dans la modale
  const hfSearchInput = document.getElementById("modal-hf-search-input");
  const btnHfSearch = document.getElementById("btn-modal-hf-search");
  const filterHfCompat = document.getElementById("modal-hf-filter-compatible");

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
      await triggerDownload(repo, filename);
    });
  }
}

async function loadHfExplorerResults(searchQuery = "", onlyCompatible = true) {
  const container = document.getElementById("hf-search-results");
  const countLabel = document.getElementById("hf-search-count");
  if (!container) return;

  container.innerHTML = "<p class='etu-text'>Interrogation en direct de l'API Hugging Face...</p>";
  if (countLabel) countLabel.textContent = "Recherche en cours...";

  try {
    const url = searchQuery
      ? `/api/models/huggingface/search?q=${encodeURIComponent(searchQuery)}&only_compatible=${onlyCompatible}&limit=25`
      : `/api/models/huggingface/compatible?limit=25`;
    const resp = await fetch(url);
    const data = await resp.json();
    const models = data.models || [];
    container.innerHTML = "";

    if (countLabel) {
      countLabel.textContent = `${models.length} modèle(s) trouvé(s)`;
    }

    if (models.length === 0) {
      container.innerHTML = `
        <div style="padding: 24px; text-align: center; color: var(--text-muted);">
          <p>Aucun modèle trouvé sur Hugging Face pour « ${escapeHtml(searchQuery)} ».</p>
        </div>
      `;
      return;
    }

    models.forEach((item) => {
      const card = document.createElement("div");
      card.className = "hf-model-card";

      let badgesHtml = `<span class="etu-badge etu-badge-${item.compatibility?.color || 'info'}">${item.compatibility?.badge || 'Compatible'}</span>`;
      if (item.npu_compatible) {
        badgesHtml += ` <span class="etu-badge etu-badge-cyan">⚡ NPU</span>`;
      }
      if (item.is_installed) {
        badgesHtml += ` <span class="etu-badge etu-badge-success">✓ Installé</span>`;
      }

      // Construction du sélecteur de quantification
      const quants = item.quantizations || [{ filename: item.filename, size_gib: item.size_gib, quant: item.quant }];
      let selectOptions = "";
      quants.forEach((q) => {
        selectOptions += `<option value="${escapeHtml(q.filename)}" data-size="${q.size_gib}">${escapeHtml(q.quant || 'GGUF')} (~${q.size_gib} Go)</option>`;
      });

      card.innerHTML = `
        <div class="hf-model-top">
          <div>
            <div class="hf-model-title-row">
              <span class="hf-model-title">${escapeHtml(item.name)}</span>
              <span class="etu-badge etu-badge-info" style="font-size: 0.72rem;">${escapeHtml(item.params || 'IA')}</span>
              ${badgesHtml}
            </div>
            <span class="hf-model-author">🤗 ${escapeHtml(item.repo)}</span>
          </div>
        </div>
        <div class="hf-model-meta-row">
          <span class="hf-meta-item">⬇️ ${(item.downloads || 0).toLocaleString()} dl</span>
          <span class="hf-meta-item">❤️ ${item.likes || 0}</span>
          <span class="hf-meta-item">🧠 RAM requise : ~${item.compatibility?.required_ram_gib || item.required_ram_gib || '6'} Go</span>
          <span class="hf-meta-item">💡 ${escapeHtml(item.compatibility?.recommendation || '')}</span>
        </div>
        <p class="hf-model-desc">${escapeHtml(item.description)}</p>
        <div class="hf-model-bottom">
          <div style="display: flex; align-items: center; gap: 8px;">
            <label style="font-size: 0.8rem; color: var(--text-muted);">Quantification :</label>
            <select class="hf-quant-select">
              ${selectOptions}
            </select>
          </div>
          <div>
            ${
              item.is_installed
                ? `<button class="etu-btn etu-btn-secondary btn-select-hf" data-filename="${escapeHtml(item.filename)}" data-name="${escapeHtml(item.name)}">Activer</button>`
                : `<button class="etu-btn etu-btn-primary btn-dl-hf" data-repo="${escapeHtml(item.repo)}">Télécharger GGUF</button>`
            }
          </div>
        </div>
      `;

      const dlBtn = card.querySelector(".btn-dl-hf");
      if (dlBtn) {
        dlBtn.addEventListener("click", () => {
          const selectEl = card.querySelector(".hf-quant-select");
          const chosenFile = selectEl ? selectEl.value : item.filename;
          triggerDownload(item.repo, chosenFile);
        });
      }

      const selectBtn = card.querySelector(".btn-select-hf");
      if (selectBtn) {
        selectBtn.addEventListener("click", () => {
          activateModel(item.filename, item.name);
        });
      }

      container.appendChild(card);
    });
  } catch (err) {
    container.innerHTML = `<p class='etu-text etu-badge-warning'>Erreur lors de la recherche Hugging Face : ${err}</p>`;
  }
}

function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add("active");
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove("active");
}

/* -------------------------------------------------------------------------- */
/* Toast Notifications                                                        */
/* -------------------------------------------------------------------------- */
function showToast(message, type = "info", duration = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast-message toast-${type}`;
  toast.innerHTML = `<span>${message}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add("toast-out");
    setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 300);
  }, duration);
}

/* -------------------------------------------------------------------------- */
/* Sélecteur de Modèle & Menu Déroulant (Chat Header & Toolbar)               */
/* -------------------------------------------------------------------------- */
function initModelSelector() {
  const btnHeaderSelector = document.getElementById("btn-model-selector");
  const btnQuickSelector = document.getElementById("btn-quick-model-select");
  const menu = document.getElementById("model-dropdown-menu");
  const btnOpenCatalog = document.getElementById("btn-dropdown-open-models");

  function toggleDropdown(e) {
    if (e) e.stopPropagation();
    if (!menu) return;
    const isOpen = menu.classList.contains("active");
    if (isOpen) {
      menu.classList.remove("active");
      if (btnHeaderSelector) btnHeaderSelector.classList.remove("open");
    } else {
      menu.classList.add("active");
      if (btnHeaderSelector) btnHeaderSelector.classList.add("open");
    }
  }

  if (btnHeaderSelector) {
    btnHeaderSelector.addEventListener("click", toggleDropdown);
  }

  if (btnQuickSelector) {
    btnQuickSelector.addEventListener("click", (e) => {
      // Toggle dropdown and scroll to top/focus if needed
      toggleDropdown(e);
      if (btnHeaderSelector) {
        btnHeaderSelector.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });
  }

  if (btnOpenCatalog) {
    btnOpenCatalog.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu) menu.classList.remove("active");
      if (btnHeaderSelector) btnHeaderSelector.classList.remove("open");
      openModal("modal-models");
      loadModelCatalog();
    });
  }

  // Fermer le dropdown lors d'un clic extérieur
  document.addEventListener("click", (e) => {
    const wrapper = document.getElementById("model-selector-wrapper");
    if (wrapper && !wrapper.contains(e.target) && e.target !== btnQuickSelector && !btnQuickSelector?.contains(e.target)) {
      if (menu) menu.classList.remove("active");
      if (btnHeaderSelector) btnHeaderSelector.classList.remove("open");
    }
  });

  // Fermer avec la touche Échap
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (menu) menu.classList.remove("active");
      if (btnHeaderSelector) btnHeaderSelector.classList.remove("open");
    }
  });
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

  const activeNameEl = document.getElementById("active-model-name");
  const quickNameEl = document.getElementById("quick-model-name");
  const badgeIconEl = document.getElementById("model-badge-icon");
  const quickIconEl = document.getElementById("quick-model-icon");
  const countBadgeEl = document.getElementById("model-count-badge");
  const listEl = document.getElementById("model-dropdown-list");

  // Déterminer le modèle d'affichage et ses métadonnées
  let currentDisplay = currentActiveModel || "Ministral 3B";

  const matched = currentActiveModel && selectableModels.find(
    (m) =>
      (m.id && m.id === currentActiveModel) ||
      (m.filename && m.filename.toLowerCase().includes((currentActiveModel || "").toLowerCase())) ||
      (m.name && m.name.toLowerCase().includes((currentActiveModel || "").toLowerCase()))
  );

  if (matched) {
    currentDisplay = matched.source === "api" ? `${matched.name} · ${matched.provider_label}` : (matched.name || matched.filename.replace(".gguf", ""));
  } else if (currentActiveModel) {
    currentDisplay = currentActiveModel.replace(/\.gguf$/i, "").replace(/[-_]/g, " ");
  }

  if (activeNameEl) activeNameEl.textContent = currentDisplay;
  if (quickNameEl) quickNameEl.textContent = currentDisplay;

  const icon = "🧠";
  if (badgeIconEl) badgeIconEl.textContent = icon;
  if (quickIconEl) quickIconEl.textContent = icon;

  if (countBadgeEl) {
    countBadgeEl.textContent = `${selectableModels.length} disponible${selectableModels.length > 1 ? "s" : ""}`;
  }

  // Remplir le menu déroulant
  if (listEl) {
    listEl.innerHTML = "";
    if (selectableModels.length === 0) {
      listEl.innerHTML = `
        <div style="padding: 12px; text-align: center; color: var(--text-muted); font-size: 0.82rem;">
          Aucun modèle installé.<br>Cliquez sur "Gérer les modèles" ci-dessous.
        </div>
      `;
    } else {
      selectableModels.forEach((item) => {
        const itemStem = item.source === "api" ? item.id : (item.filename ? item.filename.replace(/\.gguf$/i, "") : item.id);
        const isActive =
          currentActiveModel &&
          (currentActiveModel.toLowerCase() === itemStem.toLowerCase() ||
            (item.filename && item.filename.toLowerCase() === currentActiveModel.toLowerCase()));

        const itemEl = document.createElement("div");
        itemEl.className = `model-dropdown-item ${isActive ? "active" : ""}`;
        
        let badgesHtml = "";
        if (item.source === "api") {
          badgesHtml += `<span class="model-source-badge api">API · ${escapeHtml(item.provider_label)}</span>`;
        } else if (item.npu_compatible) {
          badgesHtml += `<span class="etu-badge etu-badge-cyan" style="font-size: 0.68rem; padding: 1px 5px;">⚡ Léger</span>`;
        } else if (item.compatibility?.has_gpu_acceleration) {
          badgesHtml += `<span class="etu-badge etu-badge-success" style="font-size: 0.68rem; padding: 1px 5px;">GPU possible</span>`;
        }

        const sizeLabel = item.local_size_gib ? `${item.local_size_gib} Go` : (item.size_gib ? `${item.size_gib} Go` : "");
        const metaLabel = item.source === "api" ? `Modèle distant · ${item.model}` : `${sizeLabel ? sizeLabel + " • " : ""}${item.quant || "GGUF"}`;

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
          const menu = document.getElementById("model-dropdown-menu");
          const btnHeaderSelector = document.getElementById("btn-model-selector");
          if (menu) menu.classList.remove("active");
          if (btnHeaderSelector) btnHeaderSelector.classList.remove("open");
          const displayName = item.source === "api" ? `${item.name} · ${item.provider_label}` : (item.name || itemStem);
          switchActiveModel(item.source === "api" ? item.model : (item.filename || itemStem), displayName, item.source || "local", item.provider);
        });

        listEl.appendChild(itemEl);
      });
    }
  }
}

async function switchActiveModel(modelFilename, displayName = null, source = "local", provider = null) {
  const cleanStem = source === "api" ? modelFilename : modelFilename.replace(/\.gguf$/i, "");
  const friendlyName = displayName || cleanStem;

  const indicator = document.getElementById("model-status-indicator");
  if (indicator) indicator.classList.add("loading");

  showToast(`🔄 Activation du modèle <strong>${escapeHtml(friendlyName)}</strong>...`, "info", 2500);

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
      updateModelUI(currentActiveModel, availableInstalledModels);
      showToast(`✅ Modèle <strong>${escapeHtml(friendlyName)}</strong> activé et prêt !`, "success", 3500);
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
async function loadConversations() {
  try {
    const resp = await fetch("/api/conversations");
    const data = await resp.json();
    const listEl = document.getElementById("conv-list");
    if (!listEl) return;
    listEl.innerHTML = "";

    data.conversations.forEach((conv) => {
      const item = document.createElement("div");
      item.className = "conv-item" + (conv.id === currentConversationId ? " active" : "");
      item.innerHTML = `
        <span class="conv-title">${escapeHtml(conv.title)}</span>
        <span class="delete-btn" title="Supprimer">&times;</span>
      `;
      item.querySelector(".conv-title").addEventListener("click", () => {
        selectConversation(conv.id);
      });
      item.querySelector(".delete-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        deleteConversation(conv.id);
      });
      listEl.appendChild(item);
    });
  } catch (err) {
    console.error("Erreur chargement conversations :", err);
  }
}

async function selectConversation(convId) {
  currentConversationId = convId;
  loadConversations();
  try {
    if (!assistantProfiles.length) await loadProfiles();
    const resp = await fetch(`/api/conversations/${convId}/messages`);
    const data = await resp.json();
    const profileId = data.conversation.profile_id || "default";
    setDisplayedProfile(profileId);
    const container = document.getElementById("chat-messages");
    if (!container) return;
    container.innerHTML = "";

    data.messages.forEach((msg) => {
      renderMessage(msg.role, msg.content);
    });
  } catch (err) {
    console.error("Erreur affichage messages :", err);
  }
}

function startNewConversation() {
  currentConversationId = null;
  setDisplayedProfile(preferredProfileId);
  const container = document.getElementById("chat-messages");
  if (container) {
    container.innerHTML = '<div class="chat-welcome"><div class="welcome-orb" aria-hidden="true">✦</div><h2>Comment puis-je vous aider ?</h2><p>Un assistant local pour apprendre, coder et explorer vos idées.</p><div class="welcome-suggestions"><button type="button" data-prompt="Explique-moi la récursivité avec un exemple simple.">Comprendre un concept</button><button type="button" data-prompt="Prépare un quiz interactif sur SQL.">Créer un quiz</button><button type="button" data-prompt="Aide-moi à relire mon devoir.">Relire un devoir</button></div></div>';
  }
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
function initChatForm() {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  if (!form || !input) return;

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.dispatchEvent(new Event("submit"));
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const rawMessage = input.value.trim();
    if (!rawMessage) return;
    if (form.dataset.busy === "true") return;
    form.dataset.busy = "true";
    document.getElementById("btn-send").disabled = true;
    input.value = "";

    // Commande slash pour changer de modèle : /model ou /models
    if (rawMessage.startsWith("/model") || rawMessage.startsWith("/switch")) {
      handleModelSlashCommand(rawMessage);
      form.dataset.busy = "false";
      document.getElementById("btn-send").disabled = false;
      return;
    }

    // 1. Afficher le message utilisateur
    document.querySelector("#chat-messages .chat-welcome")?.remove();
    renderMessage("user", rawMessage);

    // 2. Créer la bulle assistant pour le streaming
    const assistantBubble = renderMessage("assistant", "");
    assistantBubble.classList.add("streaming");
    const bubbleContent = assistantBubble.querySelector(".message-bubble");
    setAssistantActivity(assistantBubble, "thinking", "Réflexion en cours");

    // 3. Appel au flux SSE avec le modèle sélectionné
    try {
      const resp = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: currentConversationId,
          message: rawMessage,
          model: currentActiveModel,
          profile_id: activeProfileId,
        }),
      });

      if (!resp.ok) {
        const failure = await resp.json();
        throw new Error(failure.detail || "Réponse du serveur indisponible");
      }

      const convHeader = resp.headers.get("X-Conversation-Id");
      if (convHeader && !currentConversationId) {
        currentConversationId = convHeader;
        loadConversations();
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
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
      finishStreamRender(bubbleContent);
      if (assistantBubble.classList.contains("streaming")) {
        assistantBubble.classList.remove("streaming");
        finalizeAssistantBubble(bubbleContent);
      }
      setAssistantActivity(assistantBubble, "done", "");
    } catch (err) {
      finishStreamRender(bubbleContent);
      setAssistantActivity(assistantBubble, "error", "Erreur de réponse");
      assistantBubble.classList.remove("streaming");
      const error = document.createElement("p");
      error.className = "etu-text etu-badge-warning";
      error.textContent = `Erreur de connexion au serveur : ${err}`;
      bubbleContent.appendChild(error);
    } finally {
      form.dataset.busy = "false";
      document.getElementById("btn-send").disabled = false;
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
        const npuTag = m.npu_compatible ? " ⚡ NPU" : "";
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
    msgEl.querySelectorAll(".model-switch-btn").forEach((btn) => {
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
  if (data.event === "token" && data.html) {
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
    setAssistantActivity(bubbleEl.closest(".message"), "tool", "Consultation en cours");
  } else if (data.event === "tool_result") {
    setAssistantActivity(bubbleEl.closest(".message"), "thinking", "Analyse du résultat");
  } else if (data.event === "done") {
    finishStreamRender(bubbleEl);
    bubbleEl.closest(".message")?.classList.remove("streaming");
    setAssistantActivity(bubbleEl.closest(".message"), "done", "");
    finalizeAssistantBubble(bubbleEl);
  } else if (data.event === "status") {
    setAssistantActivity(bubbleEl.closest(".message"), "thinking", data.message || "Réflexion en cours");
  } else if (data.event === "error") {
    finishStreamRender(bubbleEl);
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
    const visible = raw.replace(/<template\b[\s\S]*?(?:<\/template>|$)/gi, "")
      .replace(/<\?(?:marker|start|end)\b[^>]*>?/gi, "")
      .replace(/<[^>]*>?/g, "")
      .replace(/^\s*```(?:html|markdown)?\s*/i, "");
    live.textContent = visible;
    applyCompletedStreamTemplates(bubble, raw);
  }
  scrollToBottom();
}

function applyCompletedStreamTemplates(bubble, raw) {
  const matches = raw.match(/<template\s+for=["'][\w-]{1,80}["']\s*>[\s\S]*?<\/template>/gi) || [];
  const applied = appliedStreamTemplates.get(bubble) || 0;
  for (const markup of matches.slice(applied)) {
    bubble.appendChild(safeFragment(markup));
    applyPartialUpdates(bubble);
  }
  appliedStreamTemplates.set(bubble, matches.length);
}

function authoredDocument(raw) {
  const fence = /```(?:html|htm)\s*\n/i.exec(raw);
  if (fence) {
    const start = fence.index + fence[0].length;
    const end = raw.indexOf("```", start);
    return { html: raw.slice(start, end < 0 ? undefined : end), source: raw, complete: end >= 0 };
  }
  if (/^\s*(?:<!doctype|<html\b|<style\b)/i.test(raw) ||
      /^\s*<(?:div|section|main|article)\b[^>]*\b(?:style|class)=/i.test(raw)) {
    return { html: raw, source: raw, complete: true };
  }
  return null;
}

function renderAuthoredPreview(bubble, raw, complete) {
  const authored = authoredDocument(raw);
  if (!authored) return false;
  let frame = bubble.querySelector(".authored-preview-frame");
  if (!frame) {
    bubble.replaceChildren();
    frame = document.createElement("iframe");
    frame.className = "authored-preview-frame";
    frame.title = "Interface créée par l’IA";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.src = "/static/preview.html?v=interactive-programs-1";
    frame.addEventListener("load", () => sendAuthoredPreview(frame));
    bubble.appendChild(frame);
  }
  frame.previewHtml = authored.html;
  frame.previewComplete = complete;
  sendAuthoredPreview(frame);
  if (complete) {
    let details = bubble.querySelector(".authored-preview-source");
    if (!details) {
      details = document.createElement("details");
      details.className = "authored-preview-source";
      details.innerHTML = '<summary>Voir le code de l’interface</summary><pre><code></code></pre>';
      bubble.appendChild(details);
    }
    details.querySelector("code").textContent = authored.html;
  }
  return true;
}

function sendAuthoredPreview(frame) {
  frame.contentWindow?.postMessage({ type: "etu-preview-update", html: frame.previewHtml || "", complete: frame.previewComplete === true }, "*");
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
    if (Number.isFinite(height)) frame.style.height = `${Math.max(100, Math.min(680, height))}px`;
  } else if (event.data.type === "etu-preview-copy" && typeof event.data.value === "string") {
    if (!navigator.userActivation.isActive) return;
    const value = event.data.value.slice(0, 500);
    try { await navigator.clipboard.writeText(value); }
    catch { /* Clipboard may require browser permission. */ }
  } else if (event.data.type === "etu-preview-action" && typeof event.data.value === "string") {
    if (!navigator.userActivation.isActive) return;
    const input = document.getElementById("chat-input");
    input.value = event.data.value.slice(0, 500);
    document.getElementById("chat-form").requestSubmit();
  }
});

function finishStreamRender(bubble) {
  const frame = streamFrames.get(bubble);
  if (frame) clearTimeout(frame);
  streamFrames.delete(bubble);
  if (streamMarkup.has(bubble)) renderStreamMarkup(bubble);
}

function finalizeAssistantBubble(bubble) {
  const raw = streamMarkup.get(bubble) || "";
  if (!renderAuthoredPreview(bubble, raw, true)) {
    bubble.replaceChildren(safeFragment(formatAssistantMarkup(raw)));
    applyPartialUpdates(bubble);
  }
  streamMarkup.delete(bubble);
  appliedStreamTemplates.delete(bubble);
}

function attachInteractiveButtons(container) {
  container.querySelectorAll(".etu-btn[data-action]").forEach((btn) => {
    if (!btn.dataset.bound) {
      btn.dataset.bound = "true";
      btn.addEventListener("click", () => {
        const action = btn.dataset.action;
        const input = document.getElementById("chat-input");
        input.value = action;
        document.getElementById("chat-form").requestSubmit();
      });
    }
  });
  container.querySelectorAll("form[data-description]").forEach((form) => {
    if (form.dataset.bound) return;
    form.dataset.bound = "true";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const fields = [...new FormData(form)].map(([key, value]) => `${key}: ${value}`).join("\n");
      const input = document.getElementById("chat-input");
      input.value = `${form.dataset.description}\n${fields}`.trim();
      document.getElementById("chat-form").requestSubmit();
    });
  });
}

function renderMessage(role, contentHtml) {
  const container = document.getElementById("chat-messages");
  const msgDiv = document.createElement("div");
  msgDiv.className = `message ${role}`;

  const isUser = role === "user";
  const profile = getActiveProfile();
  msgDiv.innerHTML = `
    <div class="avatar ${isUser ? "user-avatar" : "ai-avatar"}">${isUser ? "VOUS" : avatarMarkup(profile.avatar)}</div>
    <div class="message-bubble"></div>
  `;
  const bubble = msgDiv.querySelector(".message-bubble");
  if (isUser) bubble.textContent = contentHtml;
  else {
    if (!renderAuthoredPreview(bubble, contentHtml, true))
      bubble.appendChild(safeFragment(formatAssistantMarkup(contentHtml)));
  }
  container.appendChild(msgDiv);
  if (!isUser) applyPartialUpdates(bubble);
  attachInteractiveButtons(msgDiv);
  scrollToBottom();
  return msgDiv;
}

function formatAssistantMarkup(raw) {
  let text = String(raw || "");
  // Certains petits modèles enveloppent du Markdown entier dans un seul <p>.
  if (/^\s*<p\b[^>]*>/i.test(text) && /(?:```|\n#{2,5}\s)/.test(text)) {
    text = text.replace(/^\s*<p\b[^>]*>/i, "").replace(/<\/p>\s*$/i, "");
  }
  if (/^\s*<(?:p|div|span|ul|ol|table|pre|blockquote|button|form|template|\?marker|\?start)/i.test(text)) return text;
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const result = [];
  let inCode = false;
  let inList = false;
  let paragraph = [];
  const inline = (value) => escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  const flush = () => {
    if (paragraph.length) result.push(`<p>${inline(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      flush();
      if (inList) { result.push("</ul>"); inList = false; }
      result.push(inCode ? "</code></pre>" : '<pre class="etu-code"><code>');
      inCode = !inCode;
    } else if (inCode) {
      result.push(`${escapeHtml(line)}\n`);
    } else if (/^\s*#{1,5}\s+/.test(line)) {
      flush();
      if (inList) { result.push("</ul>"); inList = false; }
      const match = line.match(/^\s*(#{1,5})\s+(.*)$/);
      result.push(`<h${Math.min(match[1].length + 1, 5)}>${inline(match[2])}</h${Math.min(match[1].length + 1, 5)}>`);
    } else if (/^\s*[-*]\s+/.test(line)) {
      flush();
      if (!inList) { result.push("<ul>"); inList = true; }
      result.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`);
    } else if (!line.trim()) {
      flush();
      if (inList) { result.push("</ul>"); inList = false; }
    } else if (!/^\s*---+\s*$/.test(line)) {
      paragraph.push(line.trim());
    }
  }
  flush();
  if (inList) result.push("</ul>");
  if (inCode) result.push("</code></pre>");
  return result.join("");
}

function safeFragment(html) {
  const allowed = new Set(["P", "DIV", "SPAN", "STRONG", "EM", "B", "I", "BR", "UL", "OL", "LI", "H2", "H3", "H4", "H5", "PRE", "CODE", "BLOCKQUOTE", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "BUTTON", "FORM", "LABEL", "INPUT", "TEXTAREA", "SELECT", "OPTION", "TEMPLATE"]);
  const source = document.createElement("template");
  source.innerHTML = String(html).replace(/<\?(marker|start)\s+name=["']?([\w-]{1,80})["']?\s*\?>/gi, "<!--etu-$1:$2-->")
    .replace(/<\?end\s*\?>/gi, "<!--etu-end-->");
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
        const valid = (name === "class" && /^[\w\s-]{0,120}$/.test(value)) ||
          (name === "data-action" && node.tagName === "BUTTON" && value.length <= 500) ||
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
      clean(node.tagName === "TEMPLATE" ? node.content : node);
    }
  }
  clean(source.content);
  const walker = document.createTreeWalker(source.content, NodeFilter.SHOW_TEXT);
  const markdownNodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.parentElement?.closest("pre, code, textarea")) continue;
    if (/\*\*[^*]+\*\*|`[^`]+`/.test(node.textContent)) markdownNodes.push(node);
  }
  markdownNodes.forEach((node) => {
    const parts = node.textContent.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
    node.replaceWith(...parts.map((part) => {
      if (part.startsWith("**") && part.endsWith("**")) {
        const strong = document.createElement("strong");
        strong.textContent = part.slice(2, -2);
        return strong;
      }
      if (part.startsWith("`") && part.endsWith("`")) {
        const code = document.createElement("code");
        code.textContent = part.slice(1, -1);
        return code;
      }
      return document.createTextNode(part);
    }));
  });
  return source.content;
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
  const select = document.getElementById("profile-select");
  if (select) select.value = activeProfileId;
  document.querySelectorAll("#chat-messages .assistant .ai-avatar").forEach((avatar) => {
    avatar.innerHTML = avatarMarkup(getActiveProfile().avatar);
  });
}

async function loadProfiles() {
  const response = await fetch("/api/profiles");
  if (!response.ok) throw new Error("Impossible de charger les profils");
  const data = await response.json();
  assistantProfiles = data.profiles;
  preferredProfileId = data.active_profile_id;
  if (!currentConversationId) activeProfileId = preferredProfileId;
  const select = document.getElementById("profile-select");
  select.replaceChildren();
  assistantProfiles.forEach((profile) => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = `${profile.avatar.startsWith("data:image/") ? "◉" : profile.avatar} ${profile.name}`;
    select.appendChild(option);
  });
  setDisplayedProfile(activeProfileId);
  const list = document.getElementById("profile-list");
  list.replaceChildren();
  assistantProfiles.forEach((profile) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "profile-item" + (profile.id === activeProfileId ? " active" : "");
    button.innerHTML = `<span class="profile-mini-image">${avatarMarkup(profile.avatar)}</span><span>${escapeHtml(profile.name)}</span>`;
    button.addEventListener("click", () => editProfile(profile));
    list.appendChild(button);
  });
}

function editProfile(profile = null) {
  pendingAvatarData = profile?.avatar?.startsWith("data:image/webp;base64,") ? profile.avatar : null;
  document.getElementById("profile-id").value = profile?.id || "";
  document.getElementById("profile-name").value = profile?.name || "";
  document.getElementById("profile-avatar").value = pendingAvatarData ? "" : (profile?.avatar || "✦");
  document.getElementById("profile-image-file").value = "";
  updateProfilePreview(profile?.avatar || "✦");
  document.getElementById("profile-instructions").value = profile?.instructions || "";
  document.getElementById("profile-form-title").textContent = profile ? `Modifier ${profile.name}` : "Créer un profil";
  document.getElementById("profile-delete").hidden = !profile || profile.id === "default";
  document.getElementById("profile-form-error").textContent = "";
}

function initProfiles() {
  loadProfiles().catch((error) => console.warn(error));
  document.getElementById("btn-open-profiles").addEventListener("click", () => {
    editProfile(getActiveProfile());
    openModal("modal-profiles");
  });
  document.getElementById("profile-new").addEventListener("click", () => editProfile());
  document.getElementById("profile-avatar").addEventListener("input", (event) => {
    pendingAvatarData = null;
    updateProfilePreview(event.target.value || "✦");
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
      document.getElementById("profile-avatar").value = "";
      updateProfilePreview(dataUrl);
      error.textContent = "";
    } catch (problem) {
      error.textContent = problem.message;
    }
  });
  document.getElementById("profile-select").addEventListener("change", async (event) => {
    const profileId = event.target.value;
    try {
      const response = await fetch(`/api/profiles/${encodeURIComponent(profileId)}/activate`, { method: "POST" });
      if (!response.ok) throw new Error("Activation impossible");
      preferredProfileId = profileId;
      setDisplayedProfile(profileId);
      startNewConversation();
    } catch (error) {
      event.target.value = activeProfileId;
      showToast(escapeHtml(error.message), "warning");
    }
  });
  document.getElementById("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = document.getElementById("profile-id").value;
    const body = {
      name: document.getElementById("profile-name").value.trim(),
      avatar: pendingAvatarData || document.getElementById("profile-avatar").value.trim() || "✦",
      instructions: document.getElementById("profile-instructions").value.trim(),
      model: null,
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
  });
}

function scrollToBottom() {
  const container = document.getElementById("chat-messages");
  if (container) container.scrollTop = container.scrollHeight;
}

/* -------------------------------------------------------------------------- */
/* Catalogue & Téléchargement Hugging Face                                    */
/* -------------------------------------------------------------------------- */
async function loadModelCatalog() {
  try {
    const resp = await fetch("/api/models/catalog");
    const data = await resp.json();
    const catalogList = document.getElementById("catalog-list");
    if (!catalogList) return;
    catalogList.innerHTML = "";

    const installed = data.catalog.filter((m) => m.is_installed);
    availableInstalledModels = installed;
    remoteApiModels = Array.isArray(data.remote_models) ? data.remote_models : [];
    updateModelUI(currentActiveModel, availableInstalledModels);

    data.catalog.forEach((item) => {
      const card = document.createElement("div");
      card.className = `catalog-item ${item.is_installed ? "is-installed" : ""}`;
      let extraBadges = "";
      if (item.npu_compatible) {
        extraBadges += `<span class="etu-badge etu-badge-cyan" style="margin-left: 6px;">⚡ NPU</span>`;
      }
      if (item.is_installed) {
        extraBadges += `<span class="etu-badge etu-badge-success" style="margin-left: 6px;">✓ Déjà Présent</span>`;
      }

      card.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <div>
            <strong>${escapeHtml(item.name)}</strong>
            ${extraBadges}
          </div>
          <span class="etu-badge etu-badge-${item.compatibility.color}">${item.compatibility.badge}</span>
        </div>
        <p style="font-size: 0.85rem; color: var(--text-muted);">${escapeHtml(item.description)}</p>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px;">
          <span style="font-size: 0.8rem; color: var(--text-muted);">${item.size_gib} Go | RAM requise: ~${item.required_ram_gib} Go</span>
          <div>
            ${
              item.is_installed
                ? `<button class="etu-btn etu-btn-secondary btn-select-model" data-filename="${escapeHtml(item.filename)}" data-name="${escapeHtml(item.name)}">Activer</button>`
                : `<button class="etu-btn etu-btn-primary btn-dl-model" data-repo="${escapeHtml(item.repo)}" data-filename="${escapeHtml(item.filename)}">Télécharger</button>`
            }
          </div>
        </div>
      `;

      const dlBtn = card.querySelector(".btn-dl-model");
      if (dlBtn) {
        dlBtn.addEventListener("click", () => {
          triggerDownload(item.repo, item.filename);
        });
      }

      const selectBtn = card.querySelector(".btn-select-model");
      if (selectBtn) {
        selectBtn.addEventListener("click", () => {
          activateModel(item.filename, item.name);
        });
      }

      catalogList.appendChild(card);
    });
  } catch (err) {
    console.error("Erreur chargement catalogue :", err);
  }
}

async function triggerDownload(repo, filename) {
  try {
    const resp = await fetch("/api/models/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, filename }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Téléchargement refusé");
    if (data.task_id) {
      pollDownloadProgress(data.task_id, filename);
    }
  } catch (err) {
    showToast("Erreur lors du démarrage du téléchargement : " + err, "warning");
  }
}

function pollDownloadProgress(taskId, filename) {
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

  const poller = setInterval(async () => {
    try {
      const resp = await fetch(`/api/models/tasks/${taskId}`);
      const t = await resp.json();

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
        cancelButton.remove();
        if (statusEl) {
          statusEl.textContent = "Téléchargé avec succès !";
          statusEl.style.color = "var(--success)";
        }
        loadModelCatalog();
        loadServerStatus();
        showToast(`✅ Modèle <strong>${escapeHtml(filename)}</strong> prêt à l'emploi !`, "success");
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
      clearInterval(poller);
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
