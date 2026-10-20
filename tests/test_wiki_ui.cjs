/* Browser regression: real app scripts and Wiki view, with isolated API fixtures.
   Run with Playwright installed; PLAYWRIGHT_MODULE may point to its local package. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const root = path.resolve(__dirname, "..");
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("dialog", dialog => dialog.accept());
    const config = { enabled: true, rag_enabled: true, auto_memory: false, allow_remote: false, embedding_model: "", rag_tokens: 1400, documents: 4, queue_mode: "idle", idle_seconds: 30, paused: false, min_free_mb: 512, vector_limit: 3000, extraction: "rules", extraction_model: "", task_model: "" };
    const docs = new Map([
      ["Projets/ETU.md", { content: "# ETU\nMémoire locale SQLite. Voir [[Architecture]].", hash: "original" }],
      ["Tech/Architecture.md", { content: "# Architecture\nMarkdown et SQLite.", hash: "architecture" }],
    ]);
    const revisions = new Map([...docs].map(([name, doc], index) => [name, [{ id: index + 1, path: name, content: doc.content, created: 1700000000, deleted: 0 }]]));
    let seq = 2, ran = false;
    const jobs = [{ id: "queued", kind: "index", status: "pending", progress: 0, payload: {}, result: null, error: "" }];
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (!url.pathname.startsWith("/api/")) {
        const relative = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, "");
        const file = path.resolve(root, "ui", relative);
        if (!file.startsWith(path.join(root, "ui") + path.sep)) return route.fulfill({ status: 404, body: "" });
        try { return route.fulfill({ path: file }); } catch (_) { return route.fulfill({ status: 404, body: "" }); }
      }
      const method = route.request().method();
      const body = route.request().postDataJSON();
      let result = {};
      const name = url.searchParams.get("path");
      switch (url.pathname) {
        case "/api/setup/status": result = { completed: true }; break;
        case "/api/profiles": result = { profiles: [{ id: "default", name: "ETU-AI", avatar: "✦", instructions: "" }], active_profile: "default" }; break;
        case "/api/conversations": result = { conversations: [] }; break;
        case "/api/status": result = { status: "online", active_remote_model: { id: "api:openai:test-model", name: "test-model" }, active_model: null, installed_models: [{ filename: "local-test.gguf", name: "Local test", source: "local" }], remote_models: [{ id: "api:openai:test-model", model: "test-model", name: "test-model", provider: "openai", provider_label: "OpenAI", source: "api" }], hardware: { cpu: {}, ram: {}, gpus: [], npus: [] } }; break;
        case "/api/models/catalog": result = { catalog: [], installed: [], native_models: [], remote_models: [] }; break;
        case "/api/settings/provider": result = { presets: [], configured: {}, providers: [], active_provider: null }; break;
        case "/api/wiki/settings": if (method === "PUT") Object.assign(config, body); result = config; break;
        case "/api/wiki/documents": result = { documents: [...docs].map(([file, doc]) => ({ path: file, title: file.split("/").pop(), hash: doc.hash })) }; break;
        case "/api/wiki/deleted": result = { documents: [...revisions.keys()].filter(file => !docs.has(file)).map(file => ({ path: file })) }; break;
        case "/api/wiki/document":
          if (method === "GET") {
            if (!docs.has(name)) return route.fulfill({ status: 404, json: { detail: name } });
            result = { path: name, ...docs.get(name), origin: '{"type":"manual"}' };
          } else {
            const file = body.path, old = docs.get(file);
            if ((old?.hash || null) !== (body.expected_hash || null)) return route.fulfill({ status: 409, json: { detail: "Conflit" } });
            const revision = { id: ++seq, path: file, content: method === "DELETE" ? old.content : body.content, created: Date.now() / 1000, deleted: method === "DELETE" ? 1 : 0 };
            if (!revisions.has(file)) revisions.set(file, []);
            revisions.get(file).push(revision);
            if (method === "DELETE") docs.delete(file); else docs.set(file, { content: body.content, hash: String(seq) });
            result = { path: file, hash: docs.get(file)?.hash || null };
          }
          break;
        case "/api/wiki/history": result = { revisions: [...(revisions.get(name) || [])].reverse() }; break;
        case "/api/wiki/restore": {
          const revision = revisions.get(body.path).find(item => item.id === body.revision);
          docs.set(body.path, { content: revision.content, hash: String(++seq) }); result = { path: body.path }; break;
        }
        case "/api/wiki/search": result = { sources: [...docs].filter(([, d]) => d.content.toLowerCase().includes(url.searchParams.get("q").toLowerCase())).map(([file, d]) => ({ path: file, text: d.content })) }; break;
        case "/api/wiki/graph": result = { nodes: [...docs.keys()].map(file => ({ id: file, title: file.split("/").pop().replace(/\.md$/, ""), category: file.split("/")[0] })), edges: [{ source: "Projets/ETU.md", target: "Tech/Architecture.md" }], limit: 1000 }; break;
        case "/api/wiki/jobs": result = { jobs, counts: { pending: jobs.filter(j => j.status === "pending").length }, reason: ran ? "Lancement demandé" : "En attente" }; break;
        case "/api/wiki/jobs/run": ran = true; result = { status: "requested" }; break;
        case "/api/wiki/jobs/queued/cancel": jobs[0].status = "cancelled"; result = { status: "cancelled" }; break;
        default: if (url.pathname.startsWith("/api/wiki/events/")) result = { events: [] };
      }
      await route.fulfill({ json: result });
    });
    await page.goto("http://127.0.0.1:8900/#/settings/wiki");
    await page.getByRole("heading", { name: "Vos connaissances, sur cet ordinateur" }).waitFor();
    const taskModel = page.locator('[name="task_model"]');
    await taskModel.waitFor();
    assert.equal(await taskModel.locator('option[value="local-test"]').count(), 1, "installed local chat model is available for Wiki tasks");
    assert.equal(await taskModel.locator('option[value="api:openai:test-model"]').count(), 1, "configured API chat model is available for Wiki tasks");
    await taskModel.selectOption("api:openai:test-model");
    await page.getByRole("button", { name: "Enregistrer les paramètres" }).click();
    assert.equal(config.task_model, "api:openai:test-model", "selected task model is persisted with Wiki settings");
    await page.locator("#wiki-files").getByRole("button", { name: "Projets/ETU.md", exact: true }).click();
    await page.locator("#wiki-content").waitFor();
    assert.match(await page.locator("#wiki-content").inputValue(), /Mémoire locale/);
    await page.locator("#wiki-content").fill("# ETU\nSQLite corrigé. [[Architecture]]\n<img src=x onerror=window.wikiXss=true>");
    await page.locator("#wiki-actions").getByRole("button", { name: "Aperçu", exact: true }).click();
    assert.equal(await page.evaluate(() => Boolean(window.wikiXss)), false);
    assert.equal(await page.locator("#wiki-preview img").count(), 0);
    await page.locator("#wiki-actions").getByRole("button", { name: "Enregistrer", exact: true }).click();
    await page.getByText("Connaissance enregistrée dans Projets/ETU.md", { exact: true }).waitFor();
    await page.locator("#wiki-links").getByRole("button", { name: "↗ Architecture" }).click();
    await page.waitForFunction(() => document.querySelector("#wiki-path").value === "Tech/Architecture.md");
    await page.getByRole("button", { name: "Nouveau document", exact: true }).click();
    await page.locator("#wiki-path").fill("Cours/RAG.md");
    await page.locator("#wiki-content").fill("# RAG\nContexte retrouvé dans le Wiki.");
    await page.locator("#wiki-actions").getByRole("button", { name: "Enregistrer", exact: true }).click();
    await page.getByText("Connaissance enregistrée dans Cours/RAG.md", { exact: true }).waitFor();
    await page.locator("#wiki-search").fill("retrouvé");
    await page.locator("#wiki-search-button").click();
    await page.getByText("1 documents pertinents trouvés", { exact: true }).waitFor();
    assert.equal(await page.locator("#wiki-files button").count(), 1);
    await page.locator("#wiki-actions").getByRole("button", { name: "Supprimer", exact: true }).click();
    await page.getByText("Document supprimé. Sélectionnez une révision pour le restaurer.", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Documents supprimés", exact: true }).click();
    await page.locator("#wiki-files").getByRole("button", { name: "Cours/RAG.md", exact: true }).click();
    await page.getByText("Historique et restauration", { exact: true }).click();
    await page.locator("#wiki-history button").last().click();
    await page.getByText("Révision restaurée.", { exact: true }).waitFor();
    assert.equal(docs.has("Cours/RAG.md"), true);
    await page.getByRole("button", { name: "Lancer les tâches", exact: true }).click();
    assert.equal(ran, true);
    await page.getByRole("button", { name: "Suspendre / reprendre", exact: true }).click();
    assert.equal(config.paused, true);
    await page.locator("#wiki-jobs").getByRole("button", { name: "Annuler", exact: true }).click();
    await page.locator("#wiki-jobs").getByText("Annulé · Indexation", { exact: true }).waitFor();
    await page.evaluate(() => {
      const message = document.createElement("div"); message.className = "message assistant";
      message.innerHTML = '<div class="avatar"></div><div class="message-bubble">Réponse</div>';
      document.querySelector("#chat-messages").appendChild(message);
      window.ETUWiki.chatEvent({ state: "retrieved", message: "1 source utilisée", sources: [{ path: "Projets/ETU.md", text: "Un passage" }] }, message);
      window.ETUWiki.chatEvent({ state: "saved", path: "Conversations/Test.md" }, message);
      closeSettingsView();
    });
    await page.getByText("A utilisé ses connaissances", { exact: true }).waitFor();
    await page.getByText("A mémorisé une information", { exact: true }).waitFor();
    assert.equal(await page.locator(".wiki-events").evaluate(element => element.nextElementSibling?.classList.contains("message-bubble")), true, "Wiki status appears above the assistant message");
    await page.locator("#wiki-depth").selectOption("1");
    await page.getByText(/2 documents affichés/).waitFor();
    await page.locator("#wiki-depth").selectOption("all");
    await page.locator("#wiki-graph-filter").fill("Tech");
    await page.getByText(/1 documents affichés/).waitFor();
    const canvas = await page.locator("#wiki-graph").boundingBox();
    await page.locator("#wiki-graph").click({ position: { x: canvas.width / 2, y: canvas.height / 2 } });
    await page.waitForFunction(() => document.querySelector("#wiki-path").value === "Tech/Architecture.md");
    await page.locator("#wiki-graph-filter").fill("");
    await page.locator(".settings-content").evaluate(element => { element.scrollTop = 0; });
    const screenshot = process.env.WIKI_SCREENSHOT;
    if (screenshot) await page.screenshot({ path: screenshot, fullPage: true });
    assert.deepEqual(errors, [], `Browser errors: ${errors.join("; ")}`);
    console.log("PASS Wiki browser: edit, safe preview, links, creation, search, delete/restore, graph, queue and chat source navigation");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
