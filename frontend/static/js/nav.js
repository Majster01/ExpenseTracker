(() => {
  const openDrawer = () => {
    document.getElementById('nav-drawer')?.classList.add('open');
    document.getElementById('nav-backdrop')?.classList.add('open');
    document.getElementById('nav-toggle')?.setAttribute('aria-expanded', 'true');
  };

  const closeDrawer = () => {
    document.getElementById('nav-drawer')?.classList.remove('open');
    document.getElementById('nav-backdrop')?.classList.remove('open');
    document.getElementById('nav-toggle')?.setAttribute('aria-expanded', 'false');
  };

  // Delegated on document (not the drawer/toggle nodes themselves) because
  // hx-boost re-renders the whole body -- including this markup -- on every
  // page navigation, which would otherwise drop directly-attached listeners.
  document.addEventListener('click', (event) => {
    if (event.target.closest('#nav-toggle')) { openDrawer(); return; }
    if (event.target.closest('#nav-close') || event.target.closest('#nav-backdrop')) { closeDrawer(); return; }
    if (event.target.closest('.nav-link')) { closeDrawer(); }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDrawer();
  });
})();
