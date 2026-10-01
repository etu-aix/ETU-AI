/**
 * ETU-AI Client Application
 * Gère le streaming SSE, l'injection HTML de l'IA, la persistance SQLite et les modèles.
 */

let currentConversationId = null;
let activeDownloadPollers = {};

document.addEventListener("DOMContentLoaded", () => {
  initModals();
  initChatForm();
  loadConversations();
  loadServerStatus();
  loadModelCatalog();

  document.getElementById("btn-new-chat").addEventListener("click", () => {
    startNewConversation();
  });
});

/* -------------------------------------------------------------------------- */
/* Modals Setup                                                               */
/* -------------------------------------------------------------------------- */
function initModals() {
  document.getElementById("btn-open-models").addEventListener("click", () => {
    openModal("modal-models");
    loadModelCatalog();
  });

  document.getElementById("btn-open-pairing").addEventListener("click", () => {
    openModal("modal-pairing");
  });

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

  // Formulaire d'appairage
  document.getElementById("form-pairing").addEventListener("submit", async (e) => {
    e.preventDefault();
    const token = document.getElementById("pairing-token-input").value.trim();
    const studentId = document.getElementById("student-id-input").value.trim();
    try {
      const resp = await fetch("/api/handshake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pairing_token: token, student_id: studentId }),
      });
      const data = await resp.json();
      if (data.success) {
        alert(data.message);
        closeModal("modal-pairing");
        loadServerStatus();
      }
    } catch (err) {
      alert("Erreur lors de l'appairage : " + err);
    }
  });

  // Formulaire téléchargement direct HF
  document.getElementById("form-custom-download").addEventListener("submit", async (e) => {
    e.preventDefault();
    const repo = document.getElementById("hf-repo").value.trim();
    const filename = document.getElementById("hf-filename").value.trim();
    if (!repo || !filename) return;
    await triggerDownload(repo, filename);
  });
}

function openModal(id) {
  document.getElementById(id).classList.add("active");
}

function closeModal(id) {
  document.getElementById(id).classList.remove("active");
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

    backendText.textContent = `Backend : ${(data.backend || "CPU").toUpperCase()}`;
    if (data.paired_with_etu) {
      pairingText.textContent = "etu-aix : Connecté";
      pairingText.style.color = "var(--success)";
    } else {
      pairingText.textContent = "etu-aix : Non appairé";
      pairingText.style.color = "var(--text-muted)";
    }
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
    const resp = await fetch(`/api/conversations/${convId}/messages`);
    const data = await resp.json();
    const container = document.getElementById("chat-messages");
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
  document.getElementById("chat-messages").innerHTML = `
    <div class="message assistant">
      <div class="avatar ai-avatar">AI</div>
      <div class="message-bubble">
        <p class="etu-text">Nouvelle discussion démarrée. Que souhaitez-vous faire ?</p>
      </div>
    </div>
  `;
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

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.dispatchEvent(new Event("submit"));
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    input.value = "";

    // 1. Afficher le message utilisateur
    renderMessage("user", escapeHtml(message));

    // 2. Créer la bulle assistant pour le streaming
    const assistantBubble = renderMessage("assistant", "");
    const bubbleContent = assistantBubble.querySelector(".message-bubble");

    // 3. Appel au flux SSE
    try {
      const resp = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: currentConversationId,
          message: message,
        }),
      });

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
        const lines = buffer.split("\n\n");
        buffer = lines.pop(); // Conserver le dernier chunk incomplet

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
    } catch (err) {
      bubbleContent.innerHTML += `<p class="etu-text etu-badge-warning">Erreur de connexion au serveur : ${err}</p>`;
    }
  });
}

function handleStreamEvent(data, bubbleEl) {
  if (data.event === "token" && data.html) {
    bubbleEl.innerHTML += data.html;
    attachInteractiveButtons(bubbleEl);
    scrollToBottom();
  } else if (data.event === "tool_start" && data.html) {
    bubbleEl.innerHTML += data.html;
    scrollToBottom();
  } else if (data.event === "tool_result") {
    // Retire le badge de chargement s'il existe
    const badge = bubbleEl.querySelector(".etu-loading-badge");
    if (badge) badge.remove();
  }
}

function attachInteractiveButtons(container) {
  container.querySelectorAll(".etu-btn[data-action]").forEach((btn) => {
    if (!btn.dataset.bound) {
      btn.dataset.bound = "true";
      btn.addEventListener("click", () => {
        const action = btn.dataset.action;
        const payload = btn.dataset.payload || btn.textContent;
        alert(`Action déclenchée : ${action} (${payload})`);
      });
    }
  });
}

function renderMessage(role, contentHtml) {
  const container = document.getElementById("chat-messages");
  const msgDiv = document.createElement("div");
  msgDiv.className = `message ${role}`;

  const isUser = role === "user";
  msgDiv.innerHTML = `
    <div class="avatar ${isUser ? "user-avatar" : "ai-avatar"}">${isUser ? "VOUS" : "AI"}</div>
    <div class="message-bubble">${contentHtml}</div>
  `;
  container.appendChild(msgDiv);
  attachInteractiveButtons(msgDiv);
  scrollToBottom();
  return msgDiv;
}

function scrollToBottom() {
  const container = document.getElementById("chat-messages");
  container.scrollTop = container.scrollHeight;
}

/* -------------------------------------------------------------------------- */
/* Catalogue & Téléchargement Hugging Face                                    */
/* -------------------------------------------------------------------------- */
async function loadModelCatalog() {
  try {
    const resp = await fetch("/api/models/catalog");
    const data = await resp.json();
    const catalogList = document.getElementById("catalog-list");
    catalogList.innerHTML = "";

    data.catalog.forEach((item) => {
      const card = document.createElement("div");
      card.className = "catalog-item";
      card.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <strong>${escapeHtml(item.name)}</strong>
          <span class="etu-badge etu-badge-${item.compatibility.color}">${item.compatibility.badge}</span>
        </div>
        <p style="font-size: 0.85rem; color: var(--text-muted);">${escapeHtml(item.description)}</p>
        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px;">
          <span style="font-size: 0.8rem; color: var(--text-muted);">${item.size_gib} Go | RAM requise: ~${item.required_ram_gib} Go</span>
          <div>
            ${
              item.is_installed
                ? `<button class="etu-btn etu-btn-secondary btn-select-model" data-filename="${item.filename}">Activer</button>`
                : `<button class="etu-btn etu-btn-primary btn-dl-model" data-repo="${item.repo}" data-filename="${item.filename}">Télécharger</button>`
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
          activateModel(item.filename);
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
    if (data.task_id) {
      pollDownloadProgress(data.task_id, filename);
    }
  } catch (err) {
    alert("Erreur lors du démarrage du téléchargement : " + err);
  }
}

function pollDownloadProgress(taskId, filename) {
  const container = document.getElementById("active-tasks-container");
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
  `;
  container.prepend(taskBox);

  const poller = setInterval(async () => {
    try {
      const resp = await fetch(`/api/models/tasks/${taskId}`);
      const t = await resp.json();

      const percentEl = taskBox.querySelector(".task-percent");
      const fillEl = taskBox.querySelector(".progress-bar-fill");
      const speedEl = taskBox.querySelector(".task-speed");
      const statusEl = taskBox.querySelector(".task-status");

      percentEl.textContent = `${t.percentage}%`;
      fillEl.style.width = `${t.percentage}%`;
      speedEl.textContent = `${t.speed_mbps} Mo/s`;
      statusEl.textContent = t.status;

      if (t.status === "completed") {
        clearInterval(poller);
        statusEl.textContent = "Téléchargé avec succès !";
        statusEl.style.color = "var(--success)";
        loadModelCatalog();
      } else if (t.status === "error") {
        clearInterval(poller);
        statusEl.textContent = `Erreur: ${t.error_message}`;
        statusEl.style.color = "var(--danger)";
      }
    } catch (e) {
      clearInterval(poller);
    }
  }, 1000);
}

async function activateModel(filename) {
  const modelStem = filename.replace(/\.gguf$/i, "");
  try {
    const resp = await fetch("/api/models/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelStem }),
    });
    const data = await resp.json();
    if (data.success) {
      document.getElementById("active-model-name").textContent = `Modèle : ${modelStem}`;
      alert(`Modèle '${modelStem}' activé avec succès !`);
      closeModal("modal-models");
    } else {
      alert("Erreur activation : " + (data.error || "Inconnu"));
    }
  } catch (err) {
    alert("Erreur de requête : " + err);
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
