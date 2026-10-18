/* Shared dialog and injected-list menu controls. */
window.ETUComponents = (() => {
  const dialogs = new Map();
  const focusable = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

  function dialog(overlay) {
    if (dialogs.has(overlay.id)) return dialogs.get(overlay.id);
    let returnFocus = null;
    const controller = {
      open() {
        returnFocus = document.activeElement;
        overlay.inert = false;
        overlay.removeAttribute("aria-hidden");
        overlay.classList.add("active");
        (overlay.querySelector("[autofocus]") || overlay.querySelector(focusable))?.focus();
      },
      close() {
        if (!overlay.classList.contains("active")) return;
        overlay.classList.remove("active");
        overlay.setAttribute("aria-hidden", "true");
        overlay.inert = true;
        if (returnFocus?.isConnected) returnFocus.focus();
      },
      get isOpen() { return overlay.classList.contains("active"); },
    };
    overlay.inert = true;
    overlay.setAttribute("aria-hidden", "true");
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        controller.close();
      } else if (event.key === "Tab") {
        const controls = [...overlay.querySelectorAll(focusable)].filter((node) => node.getClientRects().length);
        if (!controls.length) return;
        if (event.shiftKey && document.activeElement === controls[0]) {
          controls.at(-1).focus();
          event.preventDefault();
        } else if (!event.shiftKey && document.activeElement === controls.at(-1)) {
          controls[0].focus();
          event.preventDefault();
        }
      }
    });
    dialogs.set(overlay.id, controller);
    return controller;
  }

  function getDialog(id) { return dialogs.get(id); }

  function actionMenu(trigger, panel) {
    document.body.append(panel);
    panel.setAttribute("role", "menu");
    panel.setAttribute("aria-labelledby", trigger.id);
    const options = () => [...panel.querySelectorAll("button:not([disabled])")];
    options().forEach(option => option.setAttribute("role", "menuitem"));
    const position = () => {
      if (panel.hidden) return;
      const anchor = trigger.getBoundingClientRect();
      const bounds = panel.getBoundingClientRect();
      const above = anchor.top - bounds.height - 8;
      const top = above >= 12 ? above : anchor.bottom + 8;
      panel.style.left = `${Math.max(12, Math.min(anchor.left, window.innerWidth - bounds.width - 12))}px`;
      panel.style.top = `${Math.max(12, Math.min(top, window.innerHeight - bounds.height - 12))}px`;
    };
    const close = (restoreFocus = false) => {
      panel.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (restoreFocus) trigger.focus();
    };
    const open = (last = false) => {
      panel.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      position();
      (last ? options().at(-1) : options()[0])?.focus();
    };
    trigger.addEventListener("click", () => panel.hidden ? open() : close());
    trigger.addEventListener("keydown", event => {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        open(event.key === "ArrowUp");
      }
    });
    panel.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); close(true); return; }
      if (event.key === "Tab") { close(); return; }
      const items = options();
      const current = items.indexOf(document.activeElement);
      let next;
      if (event.key === "ArrowDown") next = (current + 1) % items.length;
      else if (event.key === "ArrowUp") next = (current - 1 + items.length) % items.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = items.length - 1;
      else return;
      event.preventDefault();
      items[next]?.focus();
    });
    document.addEventListener("pointerdown", event => {
      if (!panel.contains(event.target) && !trigger.contains(event.target)) close();
    });
    window.addEventListener("resize", position);
    document.addEventListener("scroll", position, true);
    return { open, close };
  }

  function listMenu(root, { onChange = () => {} } = {}) {
    const trigger = root.querySelector("[data-menu-trigger]");
    const list = root.querySelector("[data-menu-list]");
    const chosen = root.querySelector("[data-menu-chosen]");
    const icon = root.querySelector("[data-menu-icon]");
    const chosenBadges = root.querySelector("[data-menu-badges]");
    let items = [];
    let value = "";
    const setIcon = (target, name) => {
      target.innerHTML = window.ETUSVG?.icon(name, "etu-list-menu-symbol") || "";
    };
    const setBadges = (target, badges = []) => {
      if (!target) return;
      target.replaceChildren();
      for (const name of badges) {
        const badge = document.createElement("span");
        badge.className = `etu-list-menu-badge etu-list-menu-badge-${name}`;
        badge.title = ({ cpu: "CPU", gpu: "GPU", npu: "NPU", nvidia: "NVIDIA", vulkan: "Vulkan" })[name] || name;
        badge.setAttribute("aria-label", badge.title);
        badge.innerHTML = window.ETUSVG?.icon(name) || "";
        target.append(badge);
      }
    };
    const enabledOptions = () => [...list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')];
    const close = (restoreFocus = false) => {
      list.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (restoreFocus) trigger.focus();
    };
    const open = (focusOption = false) => {
      if (trigger.disabled) return;
      list.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      const bounds = root.getBoundingClientRect();
      root.classList.toggle("etu-list-menu-up", window.innerHeight - bounds.bottom < Math.min(list.scrollHeight + 10, 390) && bounds.top > window.innerHeight - bounds.bottom);
      if (focusOption) (list.querySelector('[aria-selected="true"]') || enabledOptions()[0])?.focus();
    };
    const select = (id, notify = true) => {
      const item = items.find((candidate) => candidate.id === id && !candidate.disabled);
      if (!item) return;
      value = item.id;
      chosen.textContent = item.label;
      setIcon(icon, item.platform);
      setBadges(chosenBadges, item.badges);
      list.querySelectorAll('[role="option"]').forEach((option) => {
        option.setAttribute("aria-selected", String(option.dataset.value === value));
      });
      close();
      if (notify) onChange(item);
    };
    trigger.addEventListener("click", () => list.hidden ? open() : close());
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        open(true);
      } else if (event.key === "Escape") close();
    });
    list.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
        return;
      }
      const options = enabledOptions();
      if (!options.length) return;
      const index = options.indexOf(document.activeElement);
      let next = index;
      if (event.key === "ArrowDown") next = (index + 1) % options.length;
      else if (event.key === "ArrowUp") next = (index - 1 + options.length) % options.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = options.length - 1;
      else if (event.key === "Tab") { close(); return; }
      else return;
      event.preventDefault();
      options[next].focus();
    });
    document.addEventListener("pointerdown", (event) => {
      if (!root.contains(event.target)) close();
    });
    return {
      get value() { return value; },
      get selectedItem() { return items.find((item) => item.id === value) || null; },
      setItems(nextItems, selectedId = "") {
        items = nextItems;
        list.replaceChildren();
        let lastGroup = null;
        let lastSubgroup = null;
        items.forEach((item) => {
          if (item.group && item.group !== lastGroup) {
            const heading = document.createElement("div");
            heading.className = "etu-list-menu-group";
            heading.textContent = `Version ${item.group}`;
            list.append(heading);
            lastGroup = item.group;
            lastSubgroup = null;
          }
          if (item.subgroup && item.subgroup !== lastSubgroup) {
            const heading = document.createElement("div");
            heading.className = "etu-list-menu-subgroup";
            heading.textContent = item.subgroup;
            list.append(heading);
            lastSubgroup = item.subgroup;
          }
          const option = document.createElement("button");
          option.type = "button";
          option.className = "etu-list-menu-option";
          option.setAttribute("role", "option");
          option.setAttribute("aria-selected", "false");
          option.setAttribute("aria-disabled", String(Boolean(item.disabled)));
          option.disabled = Boolean(item.disabled);
          option.dataset.value = item.id;
          const mark = document.createElement("span");
          mark.className = "etu-list-menu-icon";
          setIcon(mark, item.platform);
          const copy = document.createElement("span");
          copy.className = "etu-list-menu-copy";
          const title = document.createElement("strong");
          title.textContent = item.optionLabel || item.label;
          copy.append(title);
          if (item.description) {
            const detail = document.createElement("small");
            detail.textContent = item.description;
            copy.append(detail);
          }
          const badges = document.createElement("span");
          badges.className = "etu-list-menu-badges";
          badges.setAttribute("aria-hidden", "true");
          setBadges(badges, item.badges);
          option.append(mark, copy, badges);
          option.addEventListener("click", () => select(item.id));
          list.append(option);
        });
        const first = items.find((item) => !item.disabled);
        value = "";
        trigger.disabled = !first;
        chosen.textContent = "Choisir une version";
        icon.replaceChildren();
        chosenBadges?.replaceChildren();
        if (first) select(items.some((item) => item.id === selectedId && !item.disabled) ? selectedId : first.id, false);
      },
      close,
    };
  }

  return { dialog, getDialog, listMenu, actionMenu };
})();
