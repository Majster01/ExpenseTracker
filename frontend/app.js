(() => {
  const config = window.EXPENSE_CONFIG || {};
  const form = document.querySelector('#upload-form');
  const fileInput = document.querySelector('#statement-file');
  const fileLabel = document.querySelector('#file-label');
  const uploadButton = document.querySelector('#upload-button');
  const uploadMessage = document.querySelector('#upload-message');
  const authMessage = document.querySelector('#auth-message');
  const userStatus = document.querySelector('#user-status');
  const signOutButton = document.querySelector('#sign-out-button');
  const resultPanel = document.querySelector('#result-panel');
  const authPanel = document.querySelector('#auth-panel');
  const tokenState = { value: null, expiresAt: 0 };
  const googleSheetsScope = 'https://www.googleapis.com/auth/spreadsheets';
  const googleIdentityScopes = 'openid email profile';
  let codeClient = null;
  let refreshTimer = null;
  let refreshPromise = null;
  let loginResolve = null;
  let loginReject = null;

  const setAuthenticatedState = (token) => {
    tokenState.value = token;
    const signedIn = Boolean(token && tokenState.expiresAt > Date.now());
    userStatus.textContent = signedIn ? 'Signed in with Google' : 'Not signed in';
    userStatus.classList.toggle('signed-in', signedIn);
    authPanel.hidden = signedIn;
    signOutButton.hidden = !signedIn;
    updateButton();
  };

  const clearAuthentication = async (message = '') => {
    setAuthenticatedState(null);
    tokenState.expiresAt = 0;
    setMessage(authMessage, message, Boolean(message));
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
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => refreshSession().catch(() => {}), Math.max(1000, payload.expires_in * 1000 - 60000));
  };

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
        setMessage(authMessage, '');
        return payload.access_token;
      })
      .catch((error) => {
        clearAuthentication();
        throw error;
      })
      .finally(() => { refreshPromise = null; });
    return refreshPromise;
  };

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
      setMessage(authMessage, '');
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
      setMessage(authMessage, 'Google sign-in is not configured for this deployment.', true);
      updateButton();
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
    const signInButton = document.createElement('button');
    signInButton.className = 'google-button';
    signInButton.type = 'button';
    signInButton.textContent = 'Sign in with Google';
    signInButton.addEventListener('click', () => {
      loginWithCode().catch((error) => setMessage(authMessage, error.message, true));
    });
    document.querySelector('#google-signin').replaceChildren(signInButton);
    refreshSession().catch(() => {});
  };
  initializeGoogle();
  setAuthenticatedState(null);

  signOutButton.addEventListener('click', async () => {
    window.clearTimeout(refreshTimer);
    await fetch(`${config.apiBaseUrl}/auth/logout`, { method: 'POST', credentials: 'include' });
    await clearAuthentication();
  });

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
