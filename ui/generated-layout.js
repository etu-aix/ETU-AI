/* Les groupes générés s'intègrent au chat ; un panneau doit être explicite. */
const ETUGeneratedLayout = (() => {
  function integrate(root) {
    for (const node of root.children) {
      if (!/^(DIV|SECTION|ARTICLE|MAIN|FORM)$/.test(node.tagName) ||
          node.dataset.surface === "panel" || node.getAttribute("role") === "img" ||
          node.childElementCount < 2) continue;
      const display = getComputedStyle(node).display;
      if (!["grid", "inline-grid", "flex", "inline-flex"].includes(display)) continue;
      // Garder la grille, les espacements et les surfaces des éléments enfants.
      // Seule l'enveloppe décorative du groupe devient transparente.
      for (const [property, value] of Object.entries({
        background: "transparent", border: "0", "border-radius": "0",
        "box-shadow": "none", padding: "0",
      })) node.style.setProperty(property, value, "important");
    }
  }
  return { integrate };
})();
