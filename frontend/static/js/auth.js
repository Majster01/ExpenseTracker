(() => {
  const config = window.EXPENSE_CONFIG || {};
  const uploadMessage = document.querySelector('#upload-message');
  const googleSheetsScope = 'https://www.googleapis.com/auth/spreadsheets';
  const googleIdentityScopes = 'openid email profile';
  let codeClient = null;
  let loginResolve = null;
  let loginReject = null;

  const setMessage = (element, message, error = false) => {
    if (!element) return;
    element.textContent = message;
    element.classList.toggle('error', error);
  };

  const handleCodeResponse = async (response) => {
    if (response.error) {
      loginReject?.(new Error('Google sign-in was not completed.'));
      loginResolve = null;
      loginReject = null;
      return;
    }
    try {
      const loginResponse = await fetch(`${config.apiBaseUrl || ''}/auth/login`, {
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
      setMessage(document.querySelector('#upload-message'), '');
      htmx.trigger(document.body, 'auth-changed');
      loginResolve?.(payload.access_token);
    } catch (error) {
      setMessage(document.querySelector('#upload-message'), error.message, true);
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
  };

  document.body.addEventListener('click', (event) => {
    const button = event.target.closest('#auth-button');
    if (!button || button.hasAttribute('hx-post') || button.disabled) return;
    event.preventDefault();
    loginWithCode().catch((error) => setMessage(document.querySelector('#upload-message'), error.message, true));
  });

  initializeGoogle();

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js'));
  }
})();
