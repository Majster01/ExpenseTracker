(() => {
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));

  const addKeyword = (row, value) => {
    const keyword = value.trim();
    if (!keyword) return;
    const existing = row.querySelectorAll('.keyword-tag');
    if ([...existing].some(tag => tag.dataset.keyword.toLowerCase() === keyword.toLowerCase())) return;
    const tag = document.createElement('span');
    tag.className = 'keyword-tag';
    tag.dataset.keyword = keyword;
    tag.innerHTML = `<span>${escapeHtml(keyword)}</span><button type="button" class="remove-keyword" aria-label="Remove ${escapeHtml(keyword)}">×</button>`;
    tag.querySelector('.remove-keyword').addEventListener('click', () => tag.remove());
    row.querySelector('.keyword-tags').append(tag);
  };

  const collectKeywords = (row) => [...row.querySelectorAll('.keyword-tag')].map(tag => tag.dataset.keyword);

  const setMessage = (message, error = false) => {
    const element = document.querySelector('#rules-message');
    if (!element) return;
    element.textContent = message;
    element.classList.toggle('error', error);
  };

  // PUT /rules/{category} and POST /categories take structured JSON bodies
  // (a keyword array, an optional rename field) that plain htmx form/hx-include
  // serialization can't express, so these two actions fetch the fragment
  // themselves and hand it to htmx.swap for a consistent server-rendered result.
  const swapRulesPanel = async (response) => {
    const html = await response.text();
    htmx.swap('#rules-panel-body', html, { swapStyle: 'innerHTML' });
    initAll(document.querySelector('#rules-panel-body'));
  };

  const saveRow = async (row) => {
    const category = row.querySelector('.rule-category').value.trim();
    const originalCategory = row.dataset.originalCategory;
    const order = Number(row.dataset.order);
    setMessage('Saving rules...');
    const response = await fetch(`/rules/${encodeURIComponent(category)}`, {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'HX-Request': 'true' },
      body: JSON.stringify({ keywords: collectKeywords(row), order, original_category: originalCategory }),
    });
    await swapRulesPanel(response);
  };

  const initRow = (row) => {
    if (row.dataset.rulesInit) return;
    row.dataset.rulesInit = 'true';
    row.querySelectorAll('.remove-keyword').forEach(button => {
      button.addEventListener('click', () => button.closest('.keyword-tag').remove());
    });
    const keywordInput = row.querySelector('.keyword-input');
    keywordInput.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      addKeyword(row, keywordInput.value);
      keywordInput.value = '';
    });
    row.querySelector('.save-rule').addEventListener('click', () => {
      saveRow(row).catch(error => setMessage(error.message, true));
    });
  };

  const initCategoryForm = (form) => {
    if (form.dataset.rulesInit) return;
    form.dataset.rulesInit = 'true';
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const input = form.querySelector('#new-category');
      const category = input.value.trim();
      if (!category) return;
      setMessage('Adding category...');
      try {
        const response = await fetch('/categories', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', 'HX-Request': 'true' },
          body: JSON.stringify({ category }),
        });
        await swapRulesPanel(response);
      } catch (error) {
        setMessage(error.message, true);
      }
    });
  };

  function initAll(root) {
    if (!root) return;
    root.querySelectorAll('.rule-row').forEach(initRow);
    root.querySelectorAll('#category-create-form').forEach(initCategoryForm);
  }

  document.addEventListener('DOMContentLoaded', () => initAll(document));
  document.body.addEventListener('htmx:afterSwap', (event) => initAll(event.detail.target));
})();
