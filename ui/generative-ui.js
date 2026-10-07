/* Interfaces natives du chat et mises à jour inspirées de ui-beta. */
const ETUChatUI = (() => {
  const baseCSS = `:host{display:block;color:var(--text-main);font:inherit;overflow-wrap:anywhere}
    *{box-sizing:border-box}p{margin:0 0 .7em}h2,h3,h4{margin:.5em 0}
    form{display:grid;gap:12px}label{display:grid;gap:6px}
    input,select,textarea,button{font:inherit;color:inherit}
    input:not([type=checkbox]):not([type=radio]),select,textarea{width:100%;padding:10px 12px;background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:9px}
    button{padding:9px 14px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:9px;cursor:pointer}
    input[type=checkbox],input[type=radio]{accent-color:var(--accent-color)}
    button:disabled{opacity:.5;cursor:wait}[hidden]{display:none!important}`;
  const safeTags = new Set("P DIV SPAN SECTION ARTICLE MAIN ASIDE HEADER FOOTER NAV STRONG EM B I BR HR UL OL LI H1 H2 H3 H4 H5 H6 PRE CODE BLOCKQUOTE TABLE THEAD TBODY TFOOT TR TH TD CAPTION BUTTON FORM LABEL INPUT TEXTAREA SELECT OPTION OPTGROUP FIELDSET LEGEND DETAILS SUMMARY PROGRESS METER OUTPUT CANVAS SVG G PATH CIRCLE RECT LINE POLYLINE POLYGON ELLIPSE TEXT DEFS LINEARGRADIENT RADIALGRADIENT STOP TITLE DESC IMG TEMPLATE STYLE".split(" "));
  let callbacks = {};
  const originalColors = new WeakMap();

  function luminance(color) {
    const components = color.match(/rgba?\(([^)]+)\)/)?.[1].match(/[\d.]+/g)?.map(Number);
    if (!components || (components.length > 3 && components[3] < .5)) return null;
    const rgb = components.slice(0, 3).map(value => {
      const channel = value / 255;
      return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
    });
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
  }

  function readableColors(host) {
    const root = host.shadowRoot;
    // Restaurer les couleurs du modèle avant de recalculer pour le thème actif.
    for (const node of root.querySelectorAll("*")) {
      const original = originalColors.get(node);
      if (original) {
        node.style.setProperty("color", original.value, original.priority);
        originalColors.delete(node);
      }
    }
    const backdrop = luminance(getComputedStyle(document.getElementById("chat-messages")).backgroundColor) ??
      luminance(getComputedStyle(document.getElementById("chat-main") || document.body).backgroundColor) ?? .01;
    function visit(parent, background) {
      for (const node of parent.children) {
        if (["STYLE", "TEMPLATE"].includes(node.tagName)) continue;
        const computed = getComputedStyle(node);
        const gradient = computed.backgroundImage.includes("gradient(") ? computed.backgroundImage.match(/rgba?\([^)]+\)/g) || [] : [];
        const stops = gradient.map(luminance).filter(value => value !== null);
        const ownBackground = stops.length ? stops.reduce((a, b) => a + b, 0) / stops.length : luminance(computed.backgroundColor);
        const effective = ownBackground ?? background;
        const foreground = luminance(computed.color);
        if (foreground !== null && (Math.max(foreground, effective) + .05) / (Math.min(foreground, effective) + .05) < 4.5) {
          originalColors.set(node, { value: node.style.getPropertyValue("color"), priority: node.style.getPropertyPriority("color") });
          node.style.setProperty("color", effective > .179 ? "#111111" : "#ffffff", "important");
        }
        visit(node, effective);
      }
    }
    visit(root, backdrop);
  }

  function refreshTheme() {
    document.querySelectorAll("#chat-messages .authored-inline").forEach(readableColors);
  }

  function css(text, chat = false) {
    // Pas de chargement réseau, ni de règle globale hors du chat.
    if (/@import|url\s*\(|expression\s*\(|image-set\s*\(|-moz-binding|:host|::slotted/i.test(text)) return "";
    if (!chat) return text;
    const sheet = new CSSStyleSheet();
    try { sheet.replaceSync(text); } catch { return ""; }
    function rules(list) {
      return [...list].map(rule => {
        if (rule.type === CSSRule.STYLE_RULE) {
          const selectors = rule.selectorText.replace(/(^|[\s,>+~])(?:html|body|:root)(?=[\s,.#:[>+~]|$)/g, "$1:scope");
          return `${selectors}{${rule.style.cssText}}`;
        }
        if (rule.type === CSSRule.MEDIA_RULE || rule.type === CSSRule.SUPPORTS_RULE)
          return `${rule.cssText.slice(0, rule.cssText.indexOf("{"))}{${rules(rule.cssRules)}}`;
        return "";
      }).join("\n");
    }
    return `@scope (#chat-messages) {${rules(sheet.cssRules)}}`;
  }

  function fragment(html) {
    const source = document.createElement("template");
    source.innerHTML = String(html).slice(0, 200000)
      .replace(/<\?(marker|start)\s+name=["']([\w/-]{1,100})["']\s*\??>/gi, "<!--etu-$1:$2-->")
      .replace(/<\?end\s*\??>/gi, "<!--etu-end-->");
    const content = source.content;
    function clean(parent) {
      for (const node of [...parent.childNodes]) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        if (!safeTags.has(node.tagName.toUpperCase())) { node.remove(); continue; }
        if (node.tagName === "STYLE") node.textContent = css(node.textContent);
        for (const attr of [...node.attributes]) {
          const name = attr.name.toLowerCase();
          const value = attr.value.trim();
          if (name.startsWith("on") || ["srcdoc", "srcset", "poster", "background", "action", "formaction", "form", "target", "autofocus", "is", "nonce", "slot"].includes(name) || /url\s*\(/i.test(value) ||
              (/^(?:href|xlink:href)$/.test(name) && !value.startsWith("#")) ||
              (name === "src" && !/^data:image\/(?:png|jpeg|gif|webp);base64,/i.test(value)) ||
              (name === "style" && !css(value)) ||
              (name === "type" && node.tagName === "INPUT" && /^(?:password|file|image)$/i.test(value))) {
            if (name === "type") node.setAttribute("type", "text");
            else node.removeAttribute(attr.name);
          }
        }
        clean(node.tagName === "TEMPLATE" ? node.content : node);
      }
    }
    clean(content);
    return content;
  }

  function formPrompt(form, submitter) {
    const fields = [];
    for (const field of form.elements) {
      if (field.disabled || !/^(INPUT|SELECT|TEXTAREA)$/.test(field.tagName) || /^(submit|button|reset|file|password)$/i.test(field.type)) continue;
      const name = field.name || field.id || field.labels?.[0]?.textContent?.trim();
      if (!name) continue;
      if (field.type === "radio" && !field.checked) continue;
      if (field.type === "checkbox") fields.push([name, field.checked]);
      else if (field.tagName === "SELECT" && field.multiple)
        [...field.selectedOptions].forEach(option => fields.push([name, option.value]));
      else fields.push([name, field.value]);
    }
    if (submitter?.name) fields.push([submitter.name, submitter.value]);
    return { description: form.dataset.description || form.getAttribute("aria-label") || form.id || "Réponses au formulaire", fields };
  }

  function bind(root) {
    root.addEventListener("submit", event => {
      event.preventDefault();
      if (!event.isTrusted) return;
      const prompt = formPrompt(event.target, event.submitter);
      callbacks.submit?.(`[formulaire] ${prompt.description}\n${JSON.stringify(prompt.fields)}`);
    }, true);
    root.addEventListener("click", event => {
      if (!event.isTrusted) return;
      const button = event.target.closest?.("[data-copy],[data-action],[data-chat-action]");
      if (!button) return;
      // Un bouton de formulaire transmet les champs, pas seulement son action.
      if (button.closest("form") && button.type === "submit") return;
      event.preventDefault();
      if (button.hasAttribute("data-copy")) callbacks.copy?.(button.dataset.copy);
      else callbacks.submit?.(button.dataset.chatAction ?? button.dataset.action);
    });
  }

  function createHost(container, className = "authored-inline") {
    const host = document.createElement("div");
    host.className = className;
    const root = host.attachShadow({ mode: "open" });
    bind(root);
    container.append(host);
    return host;
  }

  function render(container, html, complete = true) {
    let host = container.querySelector(".authored-inline");
    if (!host) { container.replaceChildren(); host = createHost(container); }
    host.inert = !complete;
    if (host.lastMarkup === html) return host.hasContent;
    host.lastMarkup = html;
    const base = document.createElement("style");
    base.textContent = baseCSS;
    const content = fragment(html);
    content.querySelectorAll("template[for]").forEach(template => template.remove());
    host.hasContent = [...content.childNodes].some(node => node.nodeType === Node.ELEMENT_NODE ? node.tagName !== "STYLE" : node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    host.shadowRoot.replaceChildren(base, content);
    ETUGeneratedLayout.integrate(host.shadowRoot);
    readableColors(host);
    return host.hasContent;
  }

  function replace(root, marker, content) {
    const target = [...root.querySelectorAll("[data-update-marker]")].find(node => node.dataset.updateMarker === marker);
    if (target) { target.replaceChildren(content); return true; }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_COMMENT);
    let start = null;
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.data === `etu-marker:${marker}`) {
        const slot = document.createElement("div");
        slot.dataset.updateMarker = marker;
        slot.append(content);
        node.replaceWith(slot);
        return true;
      }
      if (node.data === `etu-start:${marker}`) start = node;
      if (node.data === "etu-end" && start && start.parentNode === node.parentNode) {
        const range = document.createRange();
        range.setStartAfter(start); range.setEndBefore(node); range.deleteContents();
        node.before(content);
        return true;
      }
    }
    return false;
  }

  function patch(marker, html) {
    if (!/^[\w/-]{1,100}$/.test(marker)) return;
    const chat = document.getElementById("chat-messages");
    if (["chat-style", "/style/chat-overrides"].includes(marker)) {
      let style = chat.querySelector(":scope > .generated-chat-style");
      if (!style) { style = document.createElement("style"); style.className = "generated-chat-style"; chat.prepend(style); }
      const parsed = fragment(html);
      style.textContent = css([...parsed.querySelectorAll("style")].map(node => node.textContent).join("\n"), true);
      refreshTheme();
      return;
    }
    if (marker === "chat-canvas") {
      let canvas = chat.querySelector(":scope > .generated-chat-canvas");
      if (!canvas) { canvas = document.createElement("section"); canvas.className = "generated-chat-canvas"; chat.prepend(canvas); }
      if (/<script\b/i.test(html) && callbacks.renderProgram) {
        canvas.replaceChildren();
        callbacks.renderProgram(canvas, html);
      } else render(canvas, html);
      setView(true);
      return;
    }
    for (const host of chat.querySelectorAll(".authored-inline")) {
      if (replace(host.shadowRoot, marker, fragment(html))) {
        ETUGeneratedLayout.integrate(host.shadowRoot);
        readableColors(host);
        return;
      }
    }
    if (replace(chat, marker, fragment(html))) return;
    for (const frame of chat.querySelectorAll(".authored-preview-frame")) {
      // Les patchs sont aussi rejoués après le chargement d'un ancien message.
      frame.previewPatches ||= [];
      frame.previewPatches.push({ marker, html });
      frame.contentWindow?.postMessage({ type: "etu-preview-patch", marker, html }, "*");
    }
  }

  function reset() {
    const chat = document.getElementById("chat-messages");
    chat.querySelectorAll(":scope > .generated-chat-style, :scope > .generated-chat-canvas").forEach(node => node.remove());
    setView(false);
  }
  function setView(active) {
    const chat = document.getElementById("chat-messages");
    const canvas = chat.querySelector(":scope > .generated-chat-canvas");
    chat.classList.toggle("generated-view", !!canvas && active);
    if (canvas) canvas.hidden = !active;
    callbacks.viewChanged?.(!!canvas, !!canvas && active);
    if (active) chat.scrollTop = 0;
  }
  return { configure: options => { callbacks = options; }, render, patch, reset, formPrompt, refreshTheme,
    showConversation: () => setView(false), toggleView: () => setView(!document.getElementById("chat-messages").classList.contains("generated-view")) };
})();
