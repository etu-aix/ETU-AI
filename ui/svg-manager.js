/* Icônes SVG centralisées de l’interface ETU-AI. */
(() => {
  const paths = {
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
    close: '<path d="m18 6-12 12M6 6l12 12"/>',
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    telemetry: '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-6 6"/>',
    gpu: '<path d="M3 6h12l4 3v6l-4 3H3z"/><circle cx="10" cy="12" r="3.3"/><path d="M10 8.7V12l2.4 2.4M10 12l-2.8 1.4M10 12l2.5-1.5M19 9h2v2h-2m0 2h2v2h-2M6 18v2m4-2v2m4-2v2"/>',
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/><path d="M10 10h4v4h-4z"/>',
    npu: '<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/><circle cx="10" cy="10" r=".8"/><circle cx="14" cy="10" r=".8"/><circle cx="12" cy="14" r=".8"/><path d="m10.7 10.5 1 2.7m1.6-2.7-1 2.7m-1.7-2.2h2.8"/>',
    download: '<path d="M12 3v12m-4-4 4 4 4-4M4 17v3h16v-3"/>',
    upload: '<path d="M12 16V4m-5 5 5-5 5 5"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18m0-18a15 15 0 0 0 0 18"/>',
    book: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 0 4 21.5z"/><path d="M4 5.5v16M8 7h8m-8 4h8"/>',
    update: '<path d="M3 12a9 9 0 0 1 15.36-6.36L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.36 6.36L3 16M8 16H3v5"/>',
    bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
    alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/>',
    power: '<path d="M12 2v10M6.2 5.7a9 9 0 1 0 11.6 0"/>',
    sleep: '<path d="M20 14.4A8.5 8.5 0 0 1 9.6 4 8.5 8.5 0 1 0 20 14.4Z"/>',
    restart: '<path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.9 1 6.7 2.7L21 8"/><path d="M21 3v5h-5"/>',
    externalLink: '<path d="M14 3h7v7m0-7L10 14"/><path d="M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
    heart: '<path d="M20.8 8.6c0 4.2-8.8 10.2-8.8 10.2S3.2 12.8 3.2 8.6a4.6 4.6 0 0 1 8.8-1.9 4.6 4.6 0 0 1 8.8 1.9Z"/>',
    memory: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 9v6m4-6v6m4-6v6M6 18v3m6-3v3m6-3v3"/>',
    hint: '<path d="M9 18h6m-5 3h4M9 14.5a7 7 0 1 1 6 0L14 18h-4z"/>',
    thinking: '<path d="M9 18h6m-5 3h4m-5-6.5a6.5 6.5 0 1 1 6 0L14 18h-4z"/><path d="M12 7v4m-2-2h4"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.6 9a2.5 2.5 0 1 1 4.2 1.8c-1.1 1-1.8 1.3-1.8 3M12 17h.01"/>',
    hub: '<circle cx="12" cy="12" r="3"/><circle cx="4" cy="5" r="1.5"/><circle cx="20" cy="5" r="1.5"/><circle cx="12" cy="21" r="1.5"/><path d="m6 6.5 4 3.5m8-3.5-4 3.5m-2 5v4.5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    cloudOff: '<path d="m3 3 18 18"/><path d="M7.1 7.1A6 6 0 0 0 7 19h10a5 5 0 0 0 1.8-.34M16.5 5.5A6 6 0 0 0 7.2 7.2M12 19h5a5 5 0 0 0 1.8-.34M19 13a5 5 0 0 0-3.2-4.67"/>',
    settings: '<path d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z"/><path d="m19.4 15 .1.1a1.8 1.8 0 0 1-2.5 2.5l-.1-.1a1.8 1.8 0 0 0-3 .9v.2a1.8 1.8 0 0 1-3.6 0v-.2a1.8 1.8 0 0 0-3-.9l-.1.1a1.8 1.8 0 0 1-2.5-2.5l.1-.1a1.8 1.8 0 0 0-.9-3h-.2a1.8 1.8 0 0 1 0-3.6h.2a1.8 1.8 0 0 0 .9-3l-.1-.1a1.8 1.8 0 0 1 2.5-2.5l.1.1a1.8 1.8 0 0 0 3-.9v-.2a1.8 1.8 0 0 1 3.6 0v.2a1.8 1.8 0 0 0 3 .9l.1-.1a1.8 1.8 0 0 1 2.5 2.5l-.1.1a1.8 1.8 0 0 0 .9 3h.2a1.8 1.8 0 0 1 0 3.6h-.2a1.8 1.8 0 0 0-.9 3Z"/>',
  };
  const assets = {
    windows: "/static/platform-icons/windows.svg",
    macos: "/static/platform-icons/macos.svg",
    linux: "/static/platform-icons/linux.svg",
    nvidia: "/static/platform-icons/nvidia.svg",
    vulkan: "/static/platform-icons/vulkan.svg",
  };

  window.ETUSVG = Object.freeze({
    icon(name, className = "") {
      const content = paths[name];
      const safeClass = String(className).replace(/[^\w -]/g, "");
      if (assets[name]) return `<img src="${assets[name]}"${safeClass ? ` class="${safeClass}"` : ""} alt="" aria-hidden="true">`;
      if (!content) return "";
      return `<svg${safeClass ? ` class="${safeClass}"` : ""} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${content}</svg>`;
    },
  });
})();
