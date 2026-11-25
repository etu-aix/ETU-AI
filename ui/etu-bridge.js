/* Consent controls whether ETU tools may be used with third-party providers. */
(() => {
  const labels = {'etu.profile.group': 'Groupe', 'etu.schedule.read': 'Emploi du temps et carte des prises', 'etu.schedule.write': 'Modifier mon emploi du temps', 'etu.workspace.read': 'Workspace', 'etu.ai.models.read': 'Modèles ETU-AI', 'etu.ai.model.select': 'Changer le modèle ETU-AI'};
  const configuration = document.getElementById('modal-configuration');
  const connection = document.getElementById('modal-etu-connection');
  const status = document.getElementById('etu-connection-status');
  const content = document.getElementById('etu-connection-entries');
  const configButton = document.getElementById('btn-reopen-wizard');
  const remoteConsent = document.getElementById('etu-remote-tools-consent');
  const remoteConsentStatus = document.getElementById('etu-remote-tools-status');
  async function loadRemoteConsent() {
    if (!remoteConsent || !remoteConsentStatus) return;
    try {
      const response = await fetch('/api/etu/remote-tools', {cache: 'no-store'});
      if (!response.ok) throw new Error();
      remoteConsent.checked = Boolean((await response.json()).enabled);
      remoteConsentStatus.textContent = remoteConsent.checked
        ? 'Le partage ETU est autorisé pour les fournisseurs compatibles.'
        : 'Le partage des informations ETU avec les fournisseurs tiers est désactivé. Les résultats restent sur cet ordinateur.';
      remoteConsentStatus.dataset.state = remoteConsent.checked ? 'enabled' : 'disabled';
    } catch {
      remoteConsentStatus.textContent = 'Impossible de charger ce réglage. Redémarrez ETU-AI si la version vient d’être mise à jour.';
      remoteConsentStatus.dataset.state = 'error';
    }
  }
  if (remoteConsent && remoteConsentStatus) {
    remoteConsent.addEventListener('change', async () => {
      remoteConsent.disabled = true;
      try {
        const response = await fetch('/api/etu/remote-tools', {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({enabled: remoteConsent.checked})
        });
        if (!response.ok) throw new Error();
        const result = await response.json();
        remoteConsent.checked = Boolean(result.enabled);
        remoteConsentStatus.textContent = result.enabled
          ? 'Le partage ETU est autorisé pour les fournisseurs compatibles.'
          : 'Le partage des informations ETU avec les fournisseurs tiers est désactivé. Les résultats restent sur cet ordinateur.';
        remoteConsentStatus.dataset.state = result.enabled ? 'enabled' : 'disabled';
      } catch {
        remoteConsent.checked = !remoteConsent.checked;
        remoteConsentStatus.textContent = 'Enregistrement impossible. Vérifiez que l’interface locale est à jour.';
        remoteConsentStatus.dataset.state = 'error';
      } finally {
        remoteConsent.disabled = false;
      }
    });
    loadRemoteConsent();
  }
  if (!configuration || !connection || !configButton) return;
  let previous = '';
  let refreshing = false;
  let busy = false;
  let pendingIds = new Set();
  let lastFocus = null;

  function open(dialog) {
    lastFocus = document.activeElement;
    for (const other of [configuration, connection]) {
      other.classList.remove('active');
      other.setAttribute('aria-hidden', 'true');
      other.inert = true;
    }
    dialog.inert = false;
    dialog.setAttribute('aria-hidden', 'false');
    openModal(dialog.id);
    dialog.querySelector('button')?.focus();
  }
  function close(dialog) {
    closeModal(dialog.id);
    dialog.setAttribute('aria-hidden', 'true');
    dialog.inert = true;
    const target = lastFocus && !lastFocus.closest('[inert]') ? lastFocus : configButton;
    target.focus();
  }
  configButton.addEventListener('click', () => open(configuration));
  document.getElementById('btn-open-etu-connection').addEventListener('click', () => {
    open(connection);
    refresh(true);
  });
  document.getElementById('btn-back-configuration').addEventListener('click', () => open(configuration));
  document.getElementById('btn-reinstall-app').addEventListener('click', () => {
    configuration.setAttribute('aria-hidden', 'true');
    configuration.inert = true;
  });
  document.getElementById('btn-refresh-etu-connection').addEventListener('click', () => refresh(true));
  for (const dialog of [configuration, connection]) {
    dialog.querySelector('.modal-close').addEventListener('click', () => close(dialog));
    dialog.addEventListener('click', event => { if (event.target === dialog) close(dialog); });
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(dialog); }
      if (event.key === 'Tab') {
        const buttons = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, a[href]')].filter(element => element.getClientRects().length && !element.closest('[inert]'));
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
  }
  function errorMessage(response) {
    if (response.status === 404) return 'La connexion ETU 2 nécessite de redémarrer ETU-AI pour charger la nouvelle version.';
    if (response.status === 403) return 'Rechargez cette page pour accéder aux connexions locales. Si le problème persiste, redémarrez ETU-AI.';
    return 'Impossible de charger les connexions. Réessayez avec Actualiser.';
  }
  async function action(url, method, body, card) {
    if (busy) return;
    busy = true;
    card.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const response = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, ...(body ? {body: JSON.stringify(body)} : {})});
      if (!response.ok) throw new Error(response.status === 404 ? 'Cette demande a expiré. Actualisez les connexions.' : errorMessage(response));
      previous = '';
      busy = false;
      await refresh(true);
      document.getElementById('btn-refresh-etu-connection').focus();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      busy = false;
      card.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  function button(text, callback) {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = 'etu-btn etu-btn-secondary';
    element.textContent = text;
    element.onclick = callback;
    return element;
  }
  async function refresh(force = false) {
    if (document.hidden || refreshing || busy) return;
    refreshing = true;
    try {
      const response = await fetch('/api/etu/pairings', {cache: 'no-store'});
      if (!response.ok) throw new Error(errorMessage(response));
      const data = await response.json();
      const newPending = data.pending.some(entry => !pendingIds.has(entry.pair_id));
      pendingIds = new Set(data.pending.map(entry => entry.pair_id));
      document.getElementById('etu-local-connection').hidden = !data.pending.length && !data.grants.length;
      if (newPending) { document.getElementById('etu-local-connection').open = true; open(connection); }
      status.textContent = data.pending.length ? 'Une demande attend votre autorisation.' : data.grants.length ? 'ETU 2 est autorisé à se connecter.' : 'Aucune connexion autorisée. En attente d’une demande depuis ETU 2.';
      const fingerprint = JSON.stringify(data);
      if (!force && previous === fingerprint) return;
      previous = fingerprint;
      content.replaceChildren();
      for (const [pending, entries] of [[true, data.pending], [false, data.grants]]) {
        for (const entry of entries) {
          const card = document.createElement('section');
          card.className = 'etu-connection-card';
          const heading = document.createElement('p');
          heading.textContent = (pending ? 'Demande de connexion' : 'Connexion autorisée') + ' : ' + entry.origin;
          const permissions = document.createElement('p');
          permissions.textContent = 'Autorisations : ' + entry.permissions.map(p => labels[p] || p).join(', ');
          card.append(heading, permissions);
          const actions = document.createElement('div');
          actions.className = 'etu-connection-actions';
          const path = '/api/etu/pairings/' + encodeURIComponent(entry.pair_id);
          if (pending) actions.append(button('Autoriser', () => action(path, 'POST', {approve: true}, card)), button('Refuser', () => action(path, 'POST', {approve: false}, card)));
          else actions.append(button('Retirer cette autorisation', () => action(path, 'DELETE', null, card)));
          card.append(actions);
          content.append(card);
        }
      }
    } catch (error) {
      status.textContent = error.message || 'ETU-AI est indisponible. Vérifiez que l’application est démarrée.';
    } finally {
      refreshing = false;
    }
  }
  refresh();
  setInterval(refresh, 3000);
  document.addEventListener('visibilitychange', () => refresh());
})();
