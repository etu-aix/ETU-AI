(() => {
  const publish = document.getElementById('profile-workshop-publish');
  if (!publish) return;
  const description = document.getElementById('profile-workshop-description');
  const modelButton = document.getElementById('profile-workshop-model-button');
  const modelMenu = document.getElementById('profile-workshop-model-menu');
  const modelList = document.getElementById('profile-workshop-model-list');
  const modelName = document.getElementById('profile-workshop-model-name');
  const apiHint = document.getElementById('profile-workshop-api-hint');
  const terms = document.getElementById('profile-workshop-terms');
  const status = document.getElementById('profile-workshop-status');
  const modelByKey = new Map();
  let selectedModelKey = '';
  function modelKey(model) {
    if (!model) return '';
    if (model.type === 'api') return `api:${model.provider}:${model.model_id}`;
    return `${model.type || 'gguf'}:${model.repo || ''}:${model.filename || ''}`;
  }
  function refreshModelChoices(initialModel = null) {
    selectedModelKey = modelKey(initialModel);
    modelList.replaceChildren();
    modelByKey.clear();
    const addChoice = (label, key, model = null, item = null) => {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'model-dropdown-item';
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', String(key === selectedModelKey));
      const isApi = model?.type === 'api';
      const providerLogoIds = new Set(['openai','anthropic','gemini','mistral','openrouter','groq','together','deepseek','xai','cerebras','fireworks','perplexity','nvidia','sambanova','deepinfra','github-models','maia-systems']);
      let badge = '';
      if (isApi) {
        const provider = model.provider || '';
        const logo = providerLogoIds.has(provider) ? `<img src="/static/provider-icons/${escapeHtml(provider)}.svg" alt="" aria-hidden="true">` : '';
        badge = `<span class="model-provider-logo" title="${escapeHtml(item?.provider_label || provider || 'API')}" aria-label="Fournisseur API">${logo}<span>${escapeHtml((item?.provider_label || provider || 'API').slice(0, 2).toUpperCase())}</span></span>`;
      } else if (item) {
        const compatibility = item.compatibility || {};
        const supported = [];
        if (compatibility.has_gpu_acceleration) supported.push({id:'gpu',label:'GPU'});
        if (compatibility.npu_supported || item.npu_supported || compatibility.has_npu_acceleration) supported.push({id:'npu',label:'NPU OpenVINO validé'});
        else if (item.npu_candidate || compatibility.npu_candidate) supported.push({id:'npu',label:'NPU candidat, non validé'});
        if (!supported.length) supported.push({id:'cpu',label:'CPU'});
        badge = supported.map(({id,label}) => `<span class="model-hardware-badge ${id}" title="${label}" aria-label="${label}">${window.ETUSVG?.icon(id) || ''}</span>`).join('');
      }
      const parameterCount = item && typeof getModelParameterCount === 'function' ? getModelParameterCount(item) : '';
      option.innerHTML = `<span class="model-dropdown-item-left"><span class="model-item-check">${key === selectedModelKey ? '✓' : ''}</span><span class="model-item-details"><span class="model-item-title"></span><span class="model-item-meta">${escapeHtml(parameterCount)}</span></span></span><span class="model-dropdown-item-right">${badge}</span>`;
      option.querySelector('.model-item-title').textContent = label;
      option.setAttribute('aria-label', `${label}${parameterCount ? ` · ${parameterCount}` : ''}`);
      option.addEventListener('click', () => {
        selectedModelKey = key;
        modelName.textContent = label;
        apiHint.hidden = model?.type !== 'api';
        [...modelList.querySelectorAll('[role="option"]')].forEach(item => {
          const selected = item === option;
          item.setAttribute('aria-selected', String(selected));
          item.classList.toggle('active', selected);
          item.querySelector('.model-item-check').textContent = selected ? '✓' : '';
        });
        modelMenu.classList.remove('active');
        modelButton.classList.remove('open');
        modelButton.setAttribute('aria-expanded', 'false');
      });
      if (model) modelByKey.set(key, model);
      modelList.appendChild(option);
    };
    addChoice('Aucun modèle associé', '');
    for (const item of [...availableInstalledModels, ...remoteApiModels]) {
      if (item.source === 'api') {
        const model = {type:'api', provider:item.provider, model_id:item.model || item.name || item.id};
        const key = modelKey(model);
        if (!model.provider || !model.model_id || modelByKey.has(key)) continue;
        addChoice(`${item.name || model.model_id} · ${item.provider_label || 'API'} :cloud`, key, model, item);
        continue;
      }
      if (!item.filename || !item.filename.toLowerCase().endsWith('.gguf')) continue;
      const model = item.repo && item.repo !== 'local'
        ? {type:'gguf', repo:item.repo, filename:item.filename}
        : {type:'local', filename:item.filename};
      const key = modelKey(model);
      if (modelByKey.has(key)) continue;
      addChoice(item.name || item.filename, key, model, item);
    }
    if (selectedModelKey && !modelByKey.has(selectedModelKey)) {
      addChoice(`${initialModel?.model_id || initialModel?.filename || initialModel?.repo || 'Modèle enregistré'} (indisponible)`, selectedModelKey, initialModel);
    }
    const selected = [...modelList.querySelectorAll('[role="option"]')].find(option => option.getAttribute('aria-selected') === 'true');
    modelName.textContent = selected?.querySelector('.model-item-title')?.textContent || 'Aucun modèle associé';
    apiHint.hidden = initialModel?.type !== 'api';
    window.workshopSelectedModelKey = selectedModelKey;
  }
  window.refreshWorkshopModelChoices = refreshModelChoices;
  modelButton.addEventListener('click', () => {
    const open = !modelMenu.classList.contains('active');
    modelMenu.classList.toggle('active', open);
    modelButton.classList.toggle('open', open);
    modelButton.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', event => {
    if (event.target.closest('.profile-workshop-model-selector')) return;
    modelMenu.classList.remove('active');
    modelButton.classList.remove('open');
    modelButton.setAttribute('aria-expanded', 'false');
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    modelMenu.classList.remove('active');
    modelButton.classList.remove('open');
    modelButton.setAttribute('aria-expanded', 'false');
  });
  let refreshing = false;
  let publicationExists = false;
  const publishLabel = 'Publier dans le Workshop';
  const updateLabel = 'Mettre à jour dans le Workshop';
  async function refreshImportedProfiles() {
    if (refreshing || document.hidden || document.getElementById('modal-profiles')?.classList.contains('active')) return;
    refreshing = true;
    try { await loadProfiles(); renderProfileManager(); } catch (_) {} finally { refreshing = false; }
  }
  window.addEventListener('focus', refreshImportedProfiles);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshImportedProfiles(); });
  setInterval(() => {
    const panel = document.querySelector('[data-settings-panel="profiles"]');
    if (panel && !panel.hidden) refreshImportedProfiles();
  }, 5000);
  let loading = 0;
  window.refreshWorkshopPublication = async profile => {
    const generation = ++loading;
    description.value = ''; refreshModelChoices(); terms.checked = false;
    publicationExists = false;
    publish.textContent = publishLabel;
    status.textContent = profile ? '' : 'Enregistrez d’abord ce profil.';
    publish.disabled = !profile;
    if (!profile) return;
    try {
      const response = await fetch('/api/profiles/' + encodeURIComponent(profile.id) + '/workshop');
      if (generation !== loading) return;
      if (response.status === 404) {
        status.textContent = 'Redémarrez ETU-AI pour activer le Workshop après cette mise à jour.';
        publish.disabled = true;
        return;
      }
      if (!response.ok) return;
      const body = await response.json();
      if (generation !== loading) return;
      publicationExists = body.published === true || (body.published == null && Boolean(body.description));
      publish.textContent = publicationExists ? updateLabel : publishLabel;
      description.value = body.description || '';
      refreshModelChoices(body.model || null);
    } catch (_) {}
  };
  publish.addEventListener('click', async () => {
    const profileIdInput = document.getElementById('profile-id');
    const profileNameInput = document.getElementById('profile-name');
    const profileInstructionsInput = document.getElementById('profile-instructions');
    const profileModelInput = document.getElementById('profile-model');
    if (!profileIdInput || !profileNameInput || !profileInstructionsInput || !profileModelInput) {
      status.textContent = 'Le formulaire de profil est incomplet. Rechargez ETU-AI puis réessayez.';
      return;
    }
    const profileId = profileIdInput.value;
    if (!profileId || !description.value.trim() || !terms.checked) { status.textContent = 'Enregistrez le profil, ajoutez une description et acceptez les conditions.'; return; }
    const updating = publicationExists;
    publish.disabled = true; status.textContent = updating ? 'Mise à jour en cours…' : 'Publication en cours…';
    try {
      const saved = await fetch('/api/profiles/' + encodeURIComponent(profileId), {
        method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          name: profileNameInput.value.trim(),
          avatar: pendingAvatarData || profileAvatarFallback,
          instructions: profileInstructionsInput.value.trim(),
          model: profileModelInput.value || null
        })
      });
      if (!saved.ok) throw new Error('Enregistrement du profil impossible. Vérifiez son nom et ses instructions.');
      await loadProfiles();
      const model = modelByKey.get(selectedModelKey) || null;
      const response = await fetch('/api/profiles/' + encodeURIComponent(profileId) + '/workshop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({description:description.value.trim(), model, accept_workshop_terms:true}) });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Publication impossible. Vérifiez le modèle et la connexion ETU.');
      publicationExists = true;
      publish.textContent = updateLabel;
      status.textContent = 'Profil ' + (updating ? 'mis à jour' : 'publié') + ' dans le Workshop de votre compte ETU.';
    } catch (error) { status.textContent = error.message; }
    finally { publish.disabled = false; }
  });
})();
