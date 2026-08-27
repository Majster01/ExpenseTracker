(() => {
  const config = window.EXPENSE_CONFIG || {};
  const form = document.querySelector('#upload-form');
  const fileInput = document.querySelector('#statement-file');
  const fileLabel = document.querySelector('#file-label');
  const uploadButton = document.querySelector('#upload-button');
  const uploadMessage = document.querySelector('#upload-message');
  const userStatus = document.querySelector('#user-status');
  const authButton = document.querySelector('#auth-button');
  const resultPanel = document.querySelector('#result-panel');
  const rulesPanel = document.querySelector('#rules-panel');
  const rulesList = document.querySelector('#rules-list');
  const rulesMessage = document.querySelector('#rules-message');
  const addRuleButton = document.querySelector('#add-rule-button');
  const tokenState = { value: null, expiresAt: 0 };
  const googleSheetsScope = 'https://www.googleapis.com/auth/spreadsheets';
  const googleIdentityScopes = 'openid email profile';
  let codeClient = null;
  let refreshTimer = null;
  let refreshPromise = null;
  let loginResolve = null;
  let loginReject = null;
  let isAdmin = false;

  const setAuthenticatedState = (token) => {
    tokenState.value = token;
    const signedIn = Boolean(token && tokenState.expiresAt > Date.now());
    userStatus.textContent = signedIn ? 'Signed in with Google' : 'Not signed in';
    userStatus.classList.toggle('signed-in', signedIn);
    authButton.textContent = signedIn ? 'Sign out' : 'Sign in';
    authButton.disabled = false;
    updateButton();
  };

  const setLoadingState = () => {
    userStatus.textContent = 'Restoring session...';
    userStatus.classList.remove('signed-in');
    authButton.textContent = 'Restoring...';
    authButton.disabled = true;
    uploadButton.disabled = true;
  };

  const clearAuthentication = async (message = '') => {
    isAdmin = false;
    rulesPanel.hidden = true;
    rulesList.replaceChildren();
    setAuthenticatedState(null);
    tokenState.expiresAt = 0;
    setMessage(uploadMessage, message, Boolean(message));
  };

  const setMessage = (element, message, error = false) => {
    element.textContent = message;
    element.classList.toggle('error', error);
  };

  const updateButton = () => {
    uploadButton.disabled = !config.googleClientId || !fileInput.files.length || !tokenState.value;
  };

  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    fileLabel.textContent = file ? file.name : 'Select a PDF statement';
    setMessage(uploadMessage, '');
    updateButton();
  });

  const storeAccessToken = (payload) => {
    tokenState.value = payload.access_token;
    tokenState.expiresAt = Date.now() + (payload.expires_in * 1000);
    setAuthenticatedState(payload.access_token);
    isAdmin = Boolean(payload.is_admin);
    rulesPanel.hidden = !isAdmin;
    if (isAdmin) loadRules().catch(error => setMessage(rulesMessage, error.message, true));
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => refreshSession().catch(() => {}), Math.max(1000, payload.expires_in * 1000 - 60000));
  };

  const ruleRequest = (url, options = {}) => fetch(`${config.apiBaseUrl}${url}`, {
    ...options,
    credentials: 'include',
    headers: { ...(options.headers || {}), 'Content-Type': 'application/json' },
  });

  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));

  const renderRule = (rule) => {
    const row = document.createElement('div');
    row.className = 'rule-row';
    row.dataset.order = rule.order;
    row.dataset.originalCategory = rule.category;
    row.innerHTML = `<div class="rule-row-heading"><input class="rule-category" value="${escapeHtml(rule.category)}" aria-label="Category name"><button class="delete-rule" type="button">Delete</button></div><label class="field-label">Keywords, one per line</label><textarea class="rule-keywords" rows="3">${escapeHtml(rule.keywords.join('\n'))}</textarea><button class="primary-button save-rule" type="button">Save category <span>→</span></button>`;
    row.querySelector('.save-rule').addEventListener('click', () => saveRule(row).catch(error => setMessage(rulesMessage, error.message, true)));
    row.querySelector('.delete-rule').addEventListener('click', () => deleteRule(row).catch(error => setMessage(rulesMessage, error.message, true)));
    return row;
  };

  const loadRules = async () => {
    const response = await ruleRequest('/rules');
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || 'Could not load category rules.');
    rulesList.replaceChildren(...payload.rules.map(renderRule));
  };

  const saveRule = async (row) => {
    const category = row.querySelector('.rule-category').value.trim();
    const keywords = row.querySelector('.rule-keywords').value.split('\n').map(value => value.trim()).filter(Boolean);
    setMessage(rulesMessage, 'Saving rules...');
    const originalCategory = row.dataset.originalCategory;
    if (originalCategory !== category) {
      const deleteResponse = await ruleRequest(`/rules/${encodeURIComponent(originalCategory)}`, { method: 'DELETE' });
      if (!deleteResponse.ok) throw new Error('Could not rename category rule.');
    }
    const response = await ruleRequest(`/rules/${encodeURIComponent(category)}`, { method: 'PUT', body: JSON.stringify({ keywords, order: Number(row.dataset.order) }) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || 'Could not save category rule.');
    setMessage(rulesMessage, 'Category rule saved.');
    await loadRules();
  };

  const deleteRule = async (row) => {
    const category = row.querySelector('.rule-category').value.trim();
    if (!category || !window.confirm(`Delete ${category}?`)) return;
    setMessage(rulesMessage, 'Deleting rule...');
    const response = await ruleRequest(`/rules/${encodeURIComponent(category)}`, { method: 'DELETE' });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || 'Could not delete category rule.');
    }
    setMessage(rulesMessage, 'Category rule deleted.');
    await loadRules();
  };

  addRuleButton.addEventListener('click', () => {
    const row = renderRule({ category: 'New category', keywords: [], order: rulesList.children.length });
    rulesList.append(row);
    row.querySelector('.rule-category').select();
  });

  const refreshSession = () => {
    if (refreshPromise) return refreshPromise;
    refreshPromise = fetch(`${config.apiBaseUrl}/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    })
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || 'Google session has expired.');
        storeAccessToken(payload);
        setMessage(uploadMessage, '');
        return payload.access_token;
      })
      .catch((error) => {
        clearAuthentication();
        throw error;
      })
      .finally(() => { refreshPromise = null; });
    return refreshPromise;
  };

  const restoreSession = () => refreshSession().catch(() => {});

  const handleCodeResponse = async (response) => {
    if (response.error) {
      loginReject?.(new Error('Google sign-in was not completed.'));
      loginResolve = null;
      loginReject = null;
      return;
    }
    try {
      const loginResponse = await fetch(`${config.apiBaseUrl}/auth/login`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify({ code: response.code }),
      });
      const payload = await loginResponse.json().catch(() => ({}));
      if (!loginResponse.ok) throw new Error(payload.detail || 'Google sign-in failed.');
      storeAccessToken(payload);
      setMessage(uploadMessage, '');
      loginResolve?.(payload.access_token);
    } catch (error) {
      loginReject?.(error);
    } finally {
      loginResolve = null;
      loginReject = null;
    }
  };

  const loginWithCode = () => new Promise((resolve, reject) => {
    if (!codeClient) {
      reject(new Error('Google sign-in is not ready.'));
      return;
    }
    loginResolve = resolve;
    loginReject = reject;
    codeClient.requestCode();
  });

  const initializeGoogle = () => {
    if (!config.googleClientId) {
      setMessage(uploadMessage, 'Google sign-in is not configured for this deployment.', true);
      return;
    }
    if (!window.google?.accounts?.oauth2) {
      window.setTimeout(initializeGoogle, 100);
      return;
    }
    codeClient = window.google.accounts.oauth2.initCodeClient({
      client_id: config.googleClientId,
      scope: `${googleIdentityScopes} ${googleSheetsScope}`,
      ux_mode: 'popup',
      callback: handleCodeResponse,
    });
    console.info('[auth] OAuth code client initialized', {
      clientId: config.googleClientId,
      requestedScope: googleSheetsScope,
    });
  };
  setLoadingState();
  authButton.addEventListener('click', async () => {
    if (tokenState.value) {
      window.clearTimeout(refreshTimer);
      await fetch(`${config.apiBaseUrl}/auth/logout`, { method: 'POST', credentials: 'include' });
      await clearAuthentication();
      return;
    }
    loginWithCode().catch((error) => setMessage(uploadMessage, error.message, true));
  });
  window.addEventListener('pageshow', restoreSession);
  initializeGoogle();

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = fileInput.files[0];
    if (!file || file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
      setMessage(uploadMessage, 'Choose a PDF statement.', true);
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setMessage(uploadMessage, 'That file is larger than 10 MB.', true);
      return;
    }
    uploadButton.disabled = true;
    uploadButton.querySelector('span').textContent = '...';
    setMessage(uploadMessage, 'Processing statement...');
    const body = new FormData();
    body.append('parser_type', document.querySelector('#parser-type').value);
    body.append('file', file);
    try {
      if (!tokenState.value || tokenState.expiresAt <= Date.now() + 5000) {
        await refreshSession();
      }
      const sendUpload = () => fetch(`${config.apiBaseUrl}/statements`, {
        method: 'POST',
        credentials: 'include',
        body,
      });
      let response = await sendUpload();
      let payload = await response.json().catch(() => ({}));
      console.info('[auth] Statement authorization response', {
        status: response.status,
        detail: payload.detail || 'none',
      });
      if (response.status === 401) {
        console.warn('[auth] Application session rejected; requesting a fresh session');
        await refreshSession();
        response = await sendUpload();
        payload = await response.json().catch(() => ({}));
        console.info('[auth] Statement retry authorization response', {
          status: response.status,
          detail: payload.detail || 'none',
        });
        if (response.status === 401) {
          await clearAuthentication('Your Google session expired. Sign in again to continue.');
        }
      }
      if (!response.ok) throw new Error(payload.detail || 'Statement processing failed.');
      document.querySelector('#added-count').textContent = payload.rows_added ?? 0;
      document.querySelector('#review-count').textContent = payload.needs_categorization ?? 0;
      document.querySelector('#sheet-link').href = config.sheetUrl || '#';
      resultPanel.hidden = false;
      resultPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setMessage(uploadMessage, '');
    } catch (error) {
      setMessage(uploadMessage, error.message, true);
    } finally {
      uploadButton.querySelector('span').textContent = '→';
      updateButton();
    }
  });

  document.querySelector('#another-button').addEventListener('click', () => {
    resultPanel.hidden = true;
    form.reset();
    fileLabel.textContent = 'Select a PDF statement';
    updateButton();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js'));
  }
})();
