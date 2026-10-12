/* Icônes SVG centralisées de l’interface ETU-AI. */
(() => {
  const paths = {
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    telemetry: '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-6 6"/>',
    gpu: '<path d="M3 6h12l4 3v6l-4 3H3z"/><circle cx="10" cy="12" r="3.3"/><path d="M10 8.7V12l2.4 2.4M10 12l-2.8 1.4M10 12l2.5-1.5M19 9h2v2h-2m0 2h2v2h-2M6 18v2m4-2v2m4-2v2"/>',
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/><path d="M10 10h4v4h-4z"/>',
    npu: '<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/><circle cx="10" cy="10" r=".8"/><circle cx="14" cy="10" r=".8"/><circle cx="12" cy="14" r=".8"/><path d="m10.7 10.5 1 2.7m1.6-2.7-1 2.7m-1.7-2.2h2.8"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    cloudOff: '<path d="m3 3 18 18"/><path d="M7.1 7.1A6 6 0 0 0 7 19h10a5 5 0 0 0 1.8-.34M16.5 5.5A6 6 0 0 0 7.2 7.2M12 19h5a5 5 0 0 0 1.8-.34M19 13a5 5 0 0 0-3.2-4.67"/>',
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
