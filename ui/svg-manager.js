/* Icônes SVG centralisées de l’interface ETU-AI. */
(() => {
  const paths = {
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    telemetry: '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-6 6"/>',
  };

  window.ETUSVG = Object.freeze({
    icon(name, className = "") {
      const content = paths[name];
      if (!content) return "";
      const safeClass = String(className).replace(/[^\w -]/g, "");
      return `<svg${safeClass ? ` class="${safeClass}"` : ""} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${content}</svg>`;
    },
  });
})();
