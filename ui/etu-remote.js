(() => {
  const host = document.getElementById('settings-etu-mount');
  if (!host) return;
  const root = document.createElement('section'); root.className = 'etu-connection-card';
  root.innerHTML = '<h4>Connexion ETU 2</h4><p data-status role="status" aria-live="polite">Vérification de la connexion…</p><p data-device class="etu-text" hidden></p><label data-name-label class="etu-connection-name">Nom de cet appareil <input data-name maxlength="100" autocomplete="off"></label><div data-pairing hidden><p>Saisissez ce code dans ETU 2 → Paramètres → ETU-AI, puis cliquez sur Connecter.</p><strong data-code class="etu-connection-code"></strong><p class="etu-text">Le code expire après 5 minutes.</p></div><button data-connect class="etu-btn etu-btn-primary" type="button" disabled>Afficher le code</button><button data-disconnect class="etu-btn etu-btn-secondary etu-connection-remove" type="button" hidden disabled>Mettre fin à la connexion</button>';
  host.append(root);
  const status = root.querySelector('[data-status]'), code = root.querySelector('[data-code]');
  const connect = root.querySelector('[data-connect]'), disconnect = root.querySelector('[data-disconnect]');
  const deviceName = root.querySelector('[data-name]');
  deviceName.addEventListener('input', () => { deviceName.dataset.edited = 'true'; });
  let busy = false;
  async function request(method = 'GET', body) {
    const response = await fetch('/api/etu/remote' + (method === 'POST' ? '/pair' : ''), {method, cache:'no-store', headers:body ? {'Content-Type':'application/json'} : {}, body:body ? JSON.stringify(body) : undefined});
    if (!response.ok) throw new Error('Connexion à ETU 2 indisponible. Vérifiez la connexion et réessayez.');
    return response.json();
  }
  function render(data) {
    status.textContent = ({disabled:'Affichez le code, puis saisissez-le dans ETU 2. La connexion se fait automatiquement.',connecting:'Connexion à ETU 2…',reconnecting:'Reconnexion à ETU 2…',pairing:'Saisissez le code dans ETU 2 pour terminer la connexion.',connected:'ETU 2 et ETU-AI sont connectés dans les deux sens.',revoked:'Connexion retirée dans ETU 2. Vous pouvez générer un nouveau code.',expired:'Code expiré. Affichez un nouveau code.',rejected:'Association refusée dans ETU 2.'})[data.status] || 'Connexion à vérifier.';
    code.textContent = data.pairing_code || '';
    root.querySelector('[data-pairing]').hidden = !data.pairing_code;
    root.querySelector('[data-device]').hidden = !data.pair_id;
    root.querySelector('[data-device]').textContent = data.device_name || 'Cet appareil';
    if (data.error_code === 'DEPENDENCY_MISSING') status.textContent = 'La connexion distante nécessite les dépendances ETU-AI à jour. Redémarrez avec run_launcher.py après leur installation.';
    const pending = !data.pair_id && ['connecting','pairing','reconnecting'].includes(data.status) && data.expires_at * 1000 > Date.now();
    connect.disabled = !!data.pair_id || pending;
    connect.hidden = !!data.pair_id || pending;
    connect.textContent = data.status === 'expired' || (data.status === 'pairing' && !pending) ? 'Afficher un nouveau code' : 'Afficher le code';
    disconnect.disabled = !data.pair_id && !['connecting','pairing','reconnecting'].includes(data.status);
    disconnect.hidden = disconnect.disabled;
    deviceName.disabled = !!data.pair_id || pending;
    root.querySelector('[data-name-label]').hidden = !!data.pair_id || pending;
    if (data.pair_id || !deviceName.dataset.edited) deviceName.value = data.device_name || '';
  }
  async function run(action) {
    if (busy) return; busy = true; connect.disabled = disconnect.disabled = true;
    try {render(await action());} catch (error) {status.textContent = error.message;} finally {busy = false;}
  }
  window.refreshEtuRemoteConnection = () => run(() => request());
  connect.addEventListener('click', () => run(() => request('POST', deviceName.value.trim() ? {device_name:deviceName.value.trim()} : {})));
  disconnect.addEventListener('click', () => run(() => request('DELETE')));
  document.getElementById('btn-open-etu-connection')?.addEventListener('click', () => run(() => request()));
  setInterval(() => {
    const settingsPanel = root.closest('[data-settings-panel="etu"]');
    const visible = settingsPanel && !settingsPanel.hidden || root.closest('.modal-overlay')?.classList.contains('active');
    if (!busy && !document.hidden && visible) run(() => request());
  }, 2000);
})();
