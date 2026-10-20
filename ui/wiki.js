/* Local Wiki UI. Untrusted Markdown and job payloads only enter textContent. */
(() => {
  "use strict";
  const byId = id => document.getElementById(id);
  const button = (text, action) => {
    const el = document.createElement("button");
    el.type = "button"; el.className = "etu-btn etu-btn-secondary"; el.textContent = text;
    el.addEventListener("click", () => Promise.resolve().then(action).catch(error => feedback(error.message)));
    return el;
  };
  let cfg = {}, current = null, dirty = false, graph = { nodes: [], edges: [] }, timer, graphObserver;
  let scale = 1, pan = { x: 0, y: 0 }, selectedNode = null, frame = 0, chatTimer;
  async function api(path, method = "GET", body) {
    const response = await fetch(`/api/wiki${path}`, { method, headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.detail || "Wiki indisponible"); error.status = response.status; throw error; }
    return data;
  }
  function feedback(text) { if (byId("wiki-feedback")) byId("wiki-feedback").textContent = text; }
  function provenance(origin) {
    try {
      const data = typeof origin === "string" ? JSON.parse(origin) : origin || {};
      const labels = { manual: "Modification manuelle", conversation: "Conversation", model_proposal: "Proposition du modèle validée", filesystem: "Fichier local", restore: "Restauration", before_edit: "Version avant modification" };
      return (labels[data.type] || "Fichier local") + (data.quote ? ` · « ${data.quote} »` : "");
    } catch (_) { return "Fichier local"; }
  }
  function setup() {
    const mount = byId("wiki-mount");
    if (!mount || mount.children.length) return;
    mount.innerHTML = `<div class="wiki">
      <header class="wiki-header">
        <div class="wiki-heading"><span class="settings-eyebrow">VOTRE ESPACE PERSONNEL</span><h3>Connaissances</h3><p>Vos documents Markdown, leur recherche et les souvenirs de l’assistant, stockés sur cet ordinateur.</p></div>
        <div class="wiki-toolbar wiki-header-actions"><button id="wiki-open-folder" class="etu-btn etu-btn-secondary" type="button">Ouvrir le dossier</button><button id="wiki-open-obsidian" class="etu-btn etu-btn-secondary" type="button">Ouvrir dans Obsidian</button></div>
        <small id="wiki-folder-path" class="wiki-folder-path"></small>
      </header>
      <details class="wiki-config wiki-card"><summary><span><strong>Réglages du Wiki</strong><small>Recherche dans les réponses, mémoire et traitements automatiques</small></span></summary><form id="wiki-settings"><div id="wiki-fields" class="wiki-fields"></div><button class="etu-btn etu-btn-primary" type="submit">Enregistrer les paramètres</button></form></details>
      <div id="wiki-feedback" class="wiki-feedback" role="status" aria-live="polite"></div>
      <section class="wiki-card wiki-documents" aria-labelledby="wiki-documents-title">
        <div class="wiki-documents-head">
          <div class="wiki-section-title"><div><h3 id="wiki-documents-title">Documents</h3><p>Parcourez vos notes ou recherchez un passage.</p></div><span id="wiki-document-count" class="wiki-count" aria-live="polite"></span></div>
          <div class="wiki-documents-controls"><label class="wiki-search"><span class="sr-only">Rechercher dans le Wiki</span><input id="wiki-search" type="search" aria-label="Rechercher dans le Wiki" placeholder="Rechercher un document ou un passage…"><button id="wiki-search-button" class="etu-btn etu-btn-secondary" type="button">Rechercher</button></label><div class="wiki-toolbar"><button id="wiki-new" class="etu-btn etu-btn-primary" type="button">Nouveau document</button><button id="wiki-deleted" class="etu-btn etu-btn-secondary" type="button">Documents supprimés</button></div></div>
        </div>
        <div class="wiki-workspace"><nav id="wiki-files" class="wiki-files" aria-label="Dossiers et connaissances"></nav><div class="wiki-editor">
          <label class="wiki-path-label" for="wiki-path">Titre et emplacement</label><input id="wiki-path" aria-label="Chemin du document" placeholder="Projets/Architecture.md"><textarea id="wiki-content" aria-label="Contenu Markdown" placeholder="# Nouvelle connaissance"></textarea>
          <div class="wiki-editor-footer"><small id="wiki-origin">Sélectionnez un document ou créez-en un.</small><div id="wiki-actions" class="wiki-toolbar"></div></div><div id="wiki-preview" class="wiki-preview" hidden></div><div id="wiki-links" class="wiki-links"></div><details class="wiki-history-details"><summary>Historique et restauration</summary><div id="wiki-history" class="wiki-history"></div></details>
        </div></div>
      </section>
      <details class="wiki-card wiki-secondary"><summary><span><strong>Relations entre les connaissances</strong><small>Visualisez les liens entre vos documents</small></span></summary><div class="wiki-secondary-body"><div class="wiki-graph-controls"><input id="wiki-graph-filter" aria-label="Filtrer le graphe" placeholder="Filtrer par catégorie ou titre"><select id="wiki-depth" aria-label="Distance des relations"><option value="all">Toutes les relations</option><option value="1">Liens directs</option><option value="2">Liens à deux niveaux</option></select><button id="wiki-graph-reset" class="etu-btn etu-btn-secondary" type="button">Recentrer</button></div><canvas id="wiki-graph" class="wiki-graph" role="img" aria-label="Graphe des connaissances : zoom avec la molette, déplacement par glissement, clic pour ouvrir. Les documents sont aussi accessibles dans la liste."></canvas><small id="wiki-graph-info" class="wiki-section-note"></small></div></details>
      <details class="wiki-card wiki-secondary wiki-queue"><summary><span><strong>Traitements en arrière-plan</strong><small id="wiki-queue-summary">Indexation et mémorisation automatique</small></span></summary><div class="wiki-secondary-body"><p id="wiki-queue-status" role="status"></p><div id="wiki-queue-actions" class="wiki-toolbar"></div><details class="wiki-job-history"><summary>Activité récente</summary><div id="wiki-jobs" class="wiki-jobs"></div></details></div></details>
    </div>`;
    const help = {
      enabled: "Active le Wiki local, sa recherche, son interface et sa file de traitements. Le désactiver ne supprime pas les documents.",
      rag_enabled: "Recherche automatiquement des passages pertinents avant une réponse. Avec un fournisseur distant, les passages ne sont transmis que si l’option de partage distant est autorisée.",
      auto_memory: "Analyse les messages pour repérer des faits explicites et durables. Les secrets et informations incertaines sont écartés. Une demande explicite de mémorisation reste possible quand cette option est désactivée.",
      allow_remote: "Autorise l’envoi des passages retrouvés au fournisseur distant utilisé pour la réponse. Désactivé, aucun contenu du Wiki ne lui est transmis.",
      rag_tokens: "Limite la taille totale des extraits ajoutés au contexte du modèle. Réduisez cette valeur pour envoyer moins de texte.",
      documents: "Nombre maximal de documents proposés par une recherche RAG. Plus de documents peuvent apporter du contexte, mais consomment davantage de tokens.",
      embedding_model: "Nom d’un modèle d’embeddings déjà disponible localement. Laissez vide pour garder la recherche textuelle légère ; aucun modèle n’est téléchargé ici.",
      vector_limit: "Nombre maximal de passages vectoriels examinés lors d’une recherche. Une limite basse réduit le travail de calcul.",
      idle_seconds: "Temps sans génération ni recherche ETU-AI avant de lancer les tâches automatiques. La file cède la priorité aux conversations.",
      min_free_mb: "Les tâches de fond attendent que cette quantité de RAM soit libre. Mettez 0 pour désactiver ce seuil.",
      task_model: "La liste reprend les modèles installés et les API configurées dans le sélecteur des chats. Le choix « modèle du chat » suit le modèle courant. Une API peut traiter les tâches dès la fin de la réponse ; le message source est alors envoyé au fournisseur choisi. Un modèle local doit déjà être chargé, afin de ne pas en démarrer un second. Les embeddings utilisent le modèle dédié choisi plus haut.",
      queue_mode: "En mode inactivité, les tâches se lancent seules quand ETU-AI est disponible. En mode manuel, utilisez le bouton de lancement dans la file.",
      extraction: "Utilisé en mode règles ou quand aucun modèle de chat n’est disponible. Les propositions produites par un modèle demandent une validation.",
    };
    const addHelp = (row, key) => {
      const icon = document.createElement("span");
      icon.className = "wiki-help"; icon.tabIndex = 0; icon.setAttribute("role", "img");
      icon.setAttribute("aria-label", `Explication : ${help[key]}`); icon.dataset.tooltip = help[key];
      row.appendChild(icon);
    };
    const fields = [
      ["enabled", "Activer le Wiki", "checkbox"], ["rag_enabled", "Recherche RAG automatique", "checkbox"],
      ["auto_memory", "Mémorisation automatique prudente", "checkbox"], ["allow_remote", "Autoriser l’envoi des passages du Wiki au fournisseur distant", "checkbox"],
      ["rag_tokens", "Budget maximal du contexte RAG (tokens, borne prudente)", "number"], ["documents", "Documents récupérés", "number"],
      ["embedding_model", "Modèle local d’embeddings (vide = mode léger)", "text"], ["vector_limit", "Passages vectoriels examinés au maximum", "number"],
      ["idle_seconds", "Inactivité ETU-AI avant traitement (secondes)", "number"], ["min_free_mb", "RAM libre minimale (Mo)", "number"],
      ["task_model", "Modèle pour les tâches de mémoire (vide = modèle du chat)", "select"],
    ];
    for (const [key, label, type] of fields) {
      const item = document.createElement("label");
      const labelRow = document.createElement("span"); labelRow.className = "wiki-label-row";
      const labelText = document.createElement("span"); labelText.textContent = label;
      labelRow.appendChild(labelText); addHelp(labelRow, key); item.appendChild(labelRow);
      const input = type === "select" ? document.createElement("select") : document.createElement("input");
      if (type !== "select") input.type = type;
      input.name = key;
      if (type === "number") input.min = "0";
      if (key === "task_model") {
        for (const [value, text] of [["", "Modèle sélectionné dans le chat"], ["rules", "Règles légères (sans modèle)"]]) {
          const option = document.createElement("option"); option.value = value; option.textContent = text; input.appendChild(option);
        }
        input.addEventListener("focus", () => loadTaskModels().catch(error => feedback(error.message)));
      }
      item.appendChild(input); byId("wiki-fields").appendChild(item);
    }
    for (const [key, label, choices] of [["queue_mode", "Exécution", [["idle", "Pendant l’inactivité ETU-AI"], ["manual", "Manuelle"]]], ["extraction", "Extraction sans API active", [["rules", "Règles légères : déclarations explicites"], ["local_llm", "LLM local déjà chargé : propositions à valider"]]]]) {
      const item = document.createElement("label");
      const labelRow = document.createElement("span"); labelRow.className = "wiki-label-row";
      const labelText = document.createElement("span"); labelText.textContent = label;
      labelRow.appendChild(labelText); addHelp(labelRow, key); item.appendChild(labelRow);
      const select = document.createElement("select"); select.name = key;
      for (const [value, title] of choices) { const option = document.createElement("option"); option.value = value; option.textContent = title; select.appendChild(option); }
      item.appendChild(select); byId("wiki-fields").appendChild(item);
    }
    byId("wiki-settings").addEventListener("submit", async event => {
      event.preventDefault();
      const values = {};
      for (const input of event.target.elements) if (input.name) values[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
      try { cfg = await api("/settings", "PUT", values); feedback("Paramètres enregistrés."); await jobs(); }
      catch (error) { feedback(error.message); }
    });
    byId("wiki-content").addEventListener("input", () => { dirty = true; });
    byId("wiki-path").addEventListener("input", () => { dirty = true; });
    byId("wiki-search-button").addEventListener("click", () => search().catch(error => feedback(error.message)));
    byId("wiki-open-folder").addEventListener("click", async () => {
      try { await api("/open-folder", "POST"); feedback("Dossier Wiki ouvert dans l’explorateur de fichiers."); }
      catch (error) { feedback(error.message); }
    });
    byId("wiki-open-obsidian").addEventListener("click", async () => {
      try {
        const info = await api("/location");
        if (!info.obsidian_available) { feedback("Obsidian n’est pas installé ou son protocole n’est pas enregistré."); return; }
        if (!info.obsidian_ready) {
          await api("/open-folder", "POST");
          feedback("Dans Obsidian, choisissez « Ouvrir un dossier comme coffre », puis sélectionnez ce dossier Wiki. Vous ne devrez le faire qu’une fois.");
          return;
        }
        await api("/open-obsidian", "POST"); feedback("Ouverture demandée dans Obsidian.");
      } catch (error) { feedback(error.message); }
    });
    byId("wiki-search").addEventListener("keydown", event => { if (event.key === "Enter") search().catch(error => feedback(error.message)); });
    byId("wiki-new").addEventListener("click", () => { if (!discard()) return; current = null; byId("wiki-path").value = ""; byId("wiki-path").readOnly = false; byId("wiki-content").value = "# "; dirty = true; byId("wiki-history").replaceChildren(); byId("wiki-origin").textContent = "Nouveau document"; byId("wiki-links").replaceChildren(); byId("wiki-preview").hidden = true; byId("wiki-path").focus(); });
    byId("wiki-actions").append(button("Enregistrer", save), button("Aperçu", preview), button("Supprimer", remove));
    byId("wiki-deleted").addEventListener("click", async () => {
      const data = await api("/deleted"); byId("wiki-files").replaceChildren();
      for (const doc of data.documents) byId("wiki-files").appendChild(button(doc.path, async () => {
        if (!discard()) return;
        current = { path: doc.path, hash: null, content: "" }; dirty = false;
        byId("wiki-path").value = doc.path; byId("wiki-path").readOnly = true; byId("wiki-content").value = "";
        byId("wiki-origin").textContent = "Document supprimé : sélectionnez une révision pour le restaurer.";
        byId("wiki-preview").hidden = true; byId("wiki-links").replaceChildren(); await history();
      }));
      if (!data.documents.length) byId("wiki-files").textContent = "Aucun document supprimé.";
      byId("wiki-document-count").textContent = `${data.documents.length} supprimé${data.documents.length === 1 ? "" : "s"}`;
    });
    byId("wiki-queue-actions").append(button("Lancer les tâches", async () => { await api("/jobs/run", "POST"); feedback("Lancement demandé. Le chat reste prioritaire."); await jobs(); }), button("Suspendre / reprendre", async () => { cfg = await api("/settings", "PUT", { paused: !cfg.paused }); await jobs(); }), button("Réindexer les fichiers", async () => { await api("/jobs/index", "POST"); await jobs(); }), button("Générer les embeddings", async () => { await api("/jobs/embed", "POST"); await jobs(); }));
    byId("wiki-graph-filter").addEventListener("input", requestDraw);
    byId("wiki-depth").addEventListener("change", requestDraw);
    byId("wiki-graph-reset").addEventListener("click", () => { scale = 1; pan = { x: 0, y: 0 }; requestDraw(); });
    setupCanvas();
  }
  function discard() { return !dirty || window.confirm("Abandonner les modifications du document ?"); }
  function updateFields() {
    for (const input of byId("wiki-settings").elements) if (input.name) { if (input.type === "checkbox") input.checked = cfg[input.name]; else input.value = cfg[input.name]; }
  }
  async function loadTaskModels() {
    const select = byId("wiki-settings")?.elements.namedItem("task_model");
    if (!select) return;
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error("Impossible de charger les modèles disponibles");
    const status = await response.json();
    const saved = select.dataset.loaded === "true" ? select.value : (cfg.task_model || "");
    const activeId = status.active_remote_model?.id || status.active_model || "";
    const active = (status.remote_models || []).find(model => model.id === activeId)
      || (status.installed_models || []).find(model => model.filename?.replace(/\.gguf$/i, "") === activeId || model.name === activeId);
    const chatLabel = active ? `Modèle du chat · ${active.name || active.model || active.filename}` : "Modèle sélectionné dans le chat";
    const options = [["", chatLabel], ["rules", "Règles légères (sans modèle)"]];
    for (const model of status.installed_models || []) {
      if (!model.filename) continue;
      const value = model.filename.replace(/\.gguf$/i, "");
      options.push([value, `${model.name || value} · local`]);
    }
    for (const model of status.remote_models || []) {
      if (model.id) options.push([model.id, `${model.name || model.model} · ${model.provider_label || model.provider}`]);
    }
    if (saved && !options.some(([value]) => value === saved)) options.push([saved, `${saved} · indisponible`]);
    select.replaceChildren();
    for (const [value, label] of options) {
      const option = document.createElement("option"); option.value = value; option.textContent = label; select.appendChild(option);
    }
    select.value = saved;
    select.dataset.chatModel = activeId;
    select.dataset.loaded = "true";
  }
  async function refresh() {
    setup();
    try {
      cfg = await api("/settings");
      const location = await api("/location");
      byId("wiki-folder-path").textContent = location.path;
      try { await loadTaskModels(); } catch (error) {
        feedback(error.message);
        const select = byId("wiki-settings")?.elements.namedItem("task_model");
        if (cfg.task_model && select && ![...select.options].some(option => option.value === cfg.task_model)) {
          const option = document.createElement("option"); option.value = cfg.task_model;
          option.textContent = `${cfg.task_model} · disponibilité inconnue`; select.appendChild(option);
        }
      }
      updateFields(); await Promise.all([files(), loadGraph(), jobs()]);
      clearInterval(timer); timer = setInterval(() => {
        if (document.querySelector('[data-settings-panel="wiki"]')?.hidden === false) {
          jobs().catch(error => feedback(error.message));
          if (Date.now() - lastExternalRefresh > 5000) refreshExternal().catch(error => feedback(error.message));
        } else clearInterval(timer);
      }, 2500);
    } catch (error) { feedback(error.message); }
  }
  async function files() {
    const data = await api("/documents"); const list = byId("wiki-files"); const scroll = list.scrollTop; list.replaceChildren();
    byId("wiki-document-count").textContent = `${data.documents.length} ${data.documents.length === 1 ? "document" : "documents"}`;
    for (const doc of data.documents) { const el = button(doc.path, () => open(doc.path)); el.setAttribute("aria-current", String(current?.path === doc.path)); list.appendChild(el); }
    if (!data.documents.length) list.textContent = "Aucun document. Créez une note pour commencer.";
    list.scrollTop = scroll;
  }
  let lastExternalRefresh = 0;
  async function refreshExternal() {
    lastExternalRefresh = Date.now();
    await Promise.all([files(), loadGraph()]);
    if (!current?.path) return;
    try {
      const doc = await api(`/document?path=${encodeURIComponent(current.path)}`);
      if (doc.hash === current.hash) return;
      if (dirty) { feedback("Ce document a été modifié dans un autre éditeur. Copiez vos changements puis rechargez avant d’enregistrer."); return; }
      current = doc;
      byId("wiki-content").value = doc.content;
      byId("wiki-origin").textContent = `Origine : ${provenance(doc.origin)}`;
      preview(); await history(); feedback(`Document actualisé depuis le disque : ${doc.path}`);
    } catch (error) {
      if (error.status !== 404) { feedback(error.message); return; }
      if (dirty) { feedback("Ce document a été supprimé dans un autre éditeur. Copiez vos changements avant de recharger."); return; }
      current = null; byId("wiki-content").value = "";
      feedback("Le document ouvert a été supprimé depuis un autre éditeur.");
    }
  }
  async function open(path) {
    if (!discard()) return;
    const doc = await api(`/document?path=${encodeURIComponent(path)}`);
    current = doc; dirty = false; selectedNode = path;
    byId("wiki-path").value = doc.path; byId("wiki-path").readOnly = true; byId("wiki-content").value = doc.content;
    byId("wiki-origin").textContent = `Origine : ${provenance(doc.origin)}`;
    preview(); await Promise.all([history(), files()]); requestDraw();
  }
  async function save() {
    const path = byId("wiki-path").value.trim();
    await api("/document", "PUT", { path, content: byId("wiki-content").value, expected_hash: current?.hash || null });
    dirty = false; await open(path); await loadGraph(); feedback(`Connaissance enregistrée dans ${path}`);
  }
  async function remove() {
    if (!current || !window.confirm(`Supprimer ${current.path} ? L’historique permettra de le restaurer.`)) return;
    await api("/document", "DELETE", { path: current.path, expected_hash: current.hash });
    // Keep the path and history visible so deletion can be undone immediately.
    current = { path: current.path, hash: null, content: "" }; dirty = false; byId("wiki-content").value = "";
    await Promise.all([files(), history(), loadGraph()]); feedback("Document supprimé. Sélectionnez une révision pour le restaurer.");
  }
  function preview() {
    const content = byId("wiki-content").value;
    const markdown = content.replace(/^---\r?\n[\s\S]*?\r?\n(?:---|\.{3})\r?\n/, "");
    const renderer = new marked.Renderer();
    renderer.html = token => escapeHtml(token.text);
    byId("wiki-preview").replaceChildren(safeFragment(marked.parse(markdown, { gfm: true, renderer })));
    byId("wiki-preview").hidden = false;
    byId("wiki-links").replaceChildren();
    for (const match of content.matchAll(/\[\[([^\]\n]+)\]\]/g)) {
      const target = match[1].split(/[|#]/)[0];
      const key = target.toLocaleLowerCase();
      const matches = graph.nodes.filter(node => [node.id, node.id.replace(/\.md$/, ""), node.title,
        node.id.split("/").pop().replace(/\.md$/, ""), ...(node.aliases || [])].some(name => name.toLocaleLowerCase() === key));
      if (matches.length === 1) byId("wiki-links").appendChild(button(`↗ ${target}`, () => open(matches[0].id)));
    }
  }
  async function history() {
    const list = byId("wiki-history"); list.replaceChildren(); if (!current) return;
    const data = await api(`/history?path=${encodeURIComponent(current.path)}`);
    for (const revision of data.revisions) {
      const row = document.createElement("div");
      row.appendChild(button(`${new Date(revision.created * 1000).toLocaleString()} · ${revision.deleted ? "suppression" : "révision"} — restaurer`, async () => {
        if (!discard() || !window.confirm("Restaurer cette révision ? La version actuelle restera dans l’historique.")) return;
        await api("/restore", "POST", { path: current.path, revision: revision.id, expected_hash: current.hash });
        dirty = false; await open(current.path); await loadGraph(); feedback("Révision restaurée.");
      })); list.appendChild(row);
    }
  }
  async function search() {
    const q = byId("wiki-search").value.trim(); if (!q) { await files(); feedback(""); return; }
    const data = await api(`/search?q=${encodeURIComponent(q)}`); const list = byId("wiki-files"); list.replaceChildren();
    for (const source of data.sources) { list.appendChild(button(source.path, () => open(source.path))); const excerpt = document.createElement("small"); excerpt.textContent = source.text.slice(0, 200); list.appendChild(excerpt); }
    byId("wiki-document-count").textContent = `${data.sources.length} résultat${data.sources.length === 1 ? "" : "s"}`;
    feedback(`${data.sources.length} documents pertinents trouvés`);
  }
  const states = { pending: "En attente", running: "En cours", completed: "Terminé", failed: "Échec", review: "Validation / conflit", cancelled: "Annulé" };
  const kinds = { index: "Indexation", embed: "Embeddings", extract: "Analyse de conversation", write: "Enregistrement", notion: "Création de notion" };
  async function jobs() {
    const data = await api("/jobs");
    const pending = data.counts.pending || 0, review = data.counts.review || 0, running = data.counts.running || 0;
    byId("wiki-queue-status").textContent = `${cfg.paused ? "Traitements suspendus. " : ""}${pending} en attente · ${review} à valider · ${running} en cours. ${data.reason}`;
    byId("wiki-queue-summary").textContent = `${pending + review + running ? `${pending + review + running} tâche${pending + review + running === 1 ? "" : "s"} active${pending + review + running === 1 ? "" : "s"}` : "Aucune tâche active"} · ${data.reason}`;
    const list = byId("wiki-jobs"); list.replaceChildren();
    const activeJobs = data.jobs.filter(job => !["completed", "cancelled"].includes(job.status));
    const recentCompleted = data.jobs.filter(job => job.status === "completed").slice(0, 5);
    for (const job of [...activeJobs.slice(0, 20), ...recentCompleted]) {
      const row = document.createElement("article"); row.className = "wiki-job";
      const title = document.createElement("strong"); title.textContent = `${states[job.status] || job.status} · ${kinds[job.kind] || job.kind} ${job.payload.path || ""}`; row.appendChild(title);
      const progress = document.createElement("progress"); progress.max = 1; progress.value = job.progress; progress.setAttribute("aria-label", "Progression"); row.appendChild(progress);
      if (job.error) { const error = document.createElement("small"); error.textContent = job.error; row.appendChild(error); }
      const actions = document.createElement("div"); actions.className = "wiki-toolbar";
      if (job.payload.content || job.result) {
        const details = document.createElement("details"), summary = document.createElement("summary"), body = document.createElement("pre");
        summary.textContent = job.status === "review" ? "Comparer la proposition et sa provenance" : "Détails";
        body.textContent = job.payload.content ? `${job.payload.content}\n\nProvenance : ${JSON.stringify(job.payload.origin, null, 2)}` : JSON.stringify(job.result, null, 2);
        details.append(summary, body); row.appendChild(details);
      }
      if (job.payload.path) actions.appendChild(button("Ouvrir le document", () => open(job.payload.path)));
      if (job.status === "review" && job.payload.content) actions.appendChild(button("Fusionner dans l’éditeur", async () => {
        if (!discard()) return;
        let doc;
        try { doc = await api(`/document?path=${encodeURIComponent(job.payload.path)}`); }
        catch (error) { if (!String(error.message).includes(job.payload.path)) throw error; doc = { path: job.payload.path, hash: null, content: "" }; }
        current = doc; selectedNode = doc.path; byId("wiki-path").value = doc.path; byId("wiki-path").readOnly = true;
        byId("wiki-content").value = `${doc.content}\n\n<!-- Proposition à vérifier et fusionner -->\n${job.payload.content}`;
        dirty = true; preview(); feedback("Comparez et corrigez le contenu dans l’éditeur, puis enregistrez et annulez la proposition traitée.");
        byId("wiki-content").focus();
      }));
      if (["review", "failed"].includes(job.status)) actions.appendChild(button(job.status === "review" ? "Valider" : "Réessayer", async () => { await api(`/jobs/${job.id}/approve`, "POST"); await jobs(); }));
      if (["pending", "running", "review"].includes(job.status)) actions.appendChild(button("Annuler", async () => { await api(`/jobs/${job.id}/cancel`, "POST"); await jobs(); }));
      row.appendChild(actions); list.appendChild(row);
    }
  }
  async function loadGraph() { graph = await api("/graph"); requestDraw(); }
  function requestDraw() { if (!frame) frame = requestAnimationFrame(() => { frame = 0; draw(); }); }
  let placed = [];
  function draw() {
    const canvas = byId("wiki-graph"); if (!canvas) return;
    const rect = canvas.getBoundingClientRect(); if (!rect.width) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1); canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
    const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
    const textColor = getComputedStyle(canvas).color, filter = byId("wiki-graph-filter").value.toLowerCase();
    let nodes = graph.nodes.filter(n => `${n.id} ${n.title} ${n.category}`.toLowerCase().includes(filter));
    const depth = byId("wiki-depth").value;
    if (depth !== "all" && selectedNode) {
      const allowed = new Set([selectedNode]);
      for (let i = 0; i < Number(depth); i++) { const previous = new Set(allowed); for (const edge of graph.edges) { if (previous.has(edge.source)) allowed.add(edge.target); if (previous.has(edge.target)) allowed.add(edge.source); } }
      nodes = nodes.filter(n => allowed.has(n.id));
    }
    // Deterministic concentric layout; no continuous force simulation or animation.
    const categories = [...new Set(nodes.map(n => n.category))].sort();
    const spacing = Math.max(19, Math.min(110, 120 / Math.sqrt(Math.max(1, nodes.length - 1))));
    placed = nodes.map((node, index) => { const angle = index * 2.399963; const r = Math.sqrt(index) * spacing; return { ...node, x: rect.width / 2 + (Math.cos(angle) * r + pan.x) * scale, y: rect.height / 2 + (Math.sin(angle) * r + pan.y) * scale }; });
    const map = new Map(placed.map(n => [n.id, n]));
    ctx.strokeStyle = textColor; ctx.globalAlpha = .18; ctx.lineWidth = 1;
    for (const edge of graph.edges) { const a = map.get(edge.source), b = map.get(edge.target); if (!a || !b) continue; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
    ctx.globalAlpha = 1;
    for (const node of placed) {
      ctx.fillStyle = `hsl(${(categories.indexOf(node.category) * 137 + 200) % 360} 65% 60%)`; ctx.beginPath(); ctx.arc(node.x, node.y, node.id === selectedNode ? 8 : 5, 0, Math.PI * 2); ctx.fill();
      if (placed.length < 90 || scale > 1.5 || node.id === selectedNode) { ctx.font = "11px sans-serif"; ctx.fillStyle = textColor; ctx.fillText(node.title.slice(0, 32), node.x + 9, node.y + 4); }
    }
    byId("wiki-graph-info").textContent = `${nodes.length} documents affichés (limite ${graph.limit}) · ${categories.join(" · ")} · Molette : zoom. Glisser : déplacer. Clic : ouvrir.`;
  }
  function setupCanvas() {
    const canvas = byId("wiki-graph"); let start, moved = false;
    canvas.addEventListener("wheel", event => { event.preventDefault(); scale = Math.max(.2, Math.min(6, scale * (event.deltaY < 0 ? 1.12 : .89))); requestDraw(); }, { passive: false });
    canvas.addEventListener("pointerdown", event => { start = { x: event.clientX, y: event.clientY }; moved = false; canvas.setPointerCapture(event.pointerId); });
    canvas.addEventListener("pointermove", event => { if (!start) return; const dx = event.clientX - start.x, dy = event.clientY - start.y; if (Math.abs(dx) + Math.abs(dy) > 2) moved = true; pan.x += dx / scale; pan.y += dy / scale; start = { x: event.clientX, y: event.clientY }; requestDraw(); });
    canvas.addEventListener("pointerup", event => { start = null; if (moved) return; const rect = canvas.getBoundingClientRect(); const node = placed.find(n => Math.hypot(n.x - event.clientX + rect.left, n.y - event.clientY + rect.top) < 14); if (node) open(node.id).catch(error => feedback(error.message)); });
    canvas.addEventListener("pointercancel", () => { start = null; });
    graphObserver = new ResizeObserver(requestDraw); graphObserver.observe(canvas);
  }
  function chatEvent(data, message) {
    if (!message) return;
    message.querySelector(".wiki-events")?.remove();
    const sourceCount = (data.sources || []).length;
    const labels = {
      searching: "Recherche dans les connaissances…",
      retrieved: sourceCount ? "A utilisé ses connaissances" : "",
      consulted: sourceCount ? "A utilisé ses connaissances" : "",
      queued: "Analyse de mémorisation planifiée",
      queued_write: "Enregistrement en attente",
      review: "Information en attente de validation",
      saved: "A mémorisé une information",
      existing: "Information déjà mémorisée",
    };
    const label = labels[data.state] || "";
    if (label) window.ETUAIAddToolBadge?.(message, "wiki", label, "book");
    else window.ETUAIRemoveToolBadge?.(message, "wiki");
  }
  async function restoreEvents(conversation, container) {
    try {
      const data = await api(`/events/${encodeURIComponent(conversation)}`);
      if (typeof currentConversationId !== "undefined" && currentConversationId !== conversation) return;
      for (const event of data.events) {
        const user = [...container.querySelectorAll(".message.user")].find(m => m.dataset.messageId === event.message_id);
        const target = user?.nextElementSibling;
        if (target?.classList.contains("assistant")) chatEvent(event, target);
      }
      watchConversation(conversation);
    } catch (_) { /* Wiki can be disabled or unavailable without hiding chat history. */ }
  }
  function watchConversation(conversation) {
    clearTimeout(chatTimer);
    if (!conversation) return;
    chatTimer = setTimeout(async () => {
      if (typeof currentConversationId !== "undefined" && currentConversationId !== conversation) return;
      if (document.hidden) { watchConversation(conversation); return; }
      await restoreEvents(conversation, byId("chat-messages"));
    }, 8000);
  }
  window.ETUWiki = { refresh, chatEvent, restoreEvents, watchConversation };
})();
