(() => {
  const config = window.EXPENSE_CONFIG || {};
  const form = document.querySelector('#upload-form');
  const fileInput = document.querySelector('#statement-file');
  const fileLabel = document.querySelector('#file-label');
  const uploadButton = document.querySelector('#upload-button');
  const uploadMessage = document.querySelector('#upload-message');
  const authMessage = document.querySelector('#auth-message');
  const userStatus = document.querySelector('#user-status');
  const resultPanel = document.querySelector('#result-panel');
  const authPanel = document.querySelector('#auth-panel');
  const tokenState = { value: null };

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

  window.handleGoogleCredential = (response) => {
    tokenState.value = response.credential;
    userStatus.textContent = 'Signed in with Google';
    userStatus.classList.add('signed-in');
    authPanel.classList.add('authenticated');
    setMessage(authMessage, '');
    updateButton();
  };

  const initializeGoogle = () => {
    if (!config.googleClientId) {
      setMessage(authMessage, 'Google sign-in is not configured for this deployment.', true);
      updateButton();
      return;
    }
    if (!window.google?.accounts?.id) {
      window.setTimeout(initializeGoogle, 100);
      return;
    }
    window.google.accounts.id.initialize({ client_id: config.googleClientId, callback: window.handleGoogleCredential });
    window.google.accounts.id.renderButton(document.querySelector('#google-signin'), { theme: 'outline', size: 'large', width: 280 });
  };
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
      const headers = tokenState.value ? { Authorization: `Bearer ${tokenState.value}` } : {};
      const response = await fetch(`${config.apiBaseUrl}/statements`, { method: 'POST', headers, body });
      const payload = await response.json().catch(() => ({}));
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
