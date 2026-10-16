// Shared behaviour for every page: theme, mobile menu, toasts, dialogs, forms.
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // Theme: follows the system unless the person picked one (kept in this browser).
  const root = document.documentElement;
  const applyTheme = t => { root.dataset.theme = t; try { localStorage.setItem('theme', t); } catch (e) {} };
  $('#theme-toggle')?.addEventListener('click', () => applyTheme(root.dataset.theme === 'dark' ? 'light' : 'dark'));

  // Mobile menu.
  const nav = $('#nav'), toggle = $('#nav-toggle');
  toggle?.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', open);
  });
  document.addEventListener('click', e => {
    if (nav?.classList.contains('open') && !e.target.closest('#nav, #nav-toggle')) nav.classList.remove('open');
  });

  // Toasts: success messages leave by themselves; warnings and errors stay until closed.
  const dismiss = t => { t.classList.add('leaving'); t.addEventListener('animationend', () => t.remove(), { once: true }); };
  $$('.toast').forEach(t => {
    $('.close', t)?.addEventListener('click', () => dismiss(t));
    if (t.dataset.flash === 'ok') setTimeout(() => dismiss(t), 6000);
  });

  // Dialogs: click outside or press Escape to close; [data-close] closes the nearest dialog.
  $$('dialog').forEach(d => {
    d.addEventListener('click', e => { if (e.target === d) d.close(); });
    $$('[data-close]', d).forEach(b => b.addEventListener('click', () => d.close()));
  });

  // Forms: a confirmation when asked for, then a spinner on the button so nothing is sent twice.
  document.addEventListener('submit', e => {
    const form = e.target;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    const btn = e.submitter || $('button:not([type="button"])', form);
    if (btn && !btn.hasAttribute('formnovalidate')) {
      btn.classList.add('is-loading');
      setTimeout(() => { btn.disabled = true; }, 0);  // after the form data is collected
      setTimeout(() => { btn.disabled = false; btn.classList.remove('is-loading'); }, 15000);  // in case the page stays
    }
  });
  $$('[data-autosubmit]').forEach(el => el.addEventListener('change', () => el.form?.requestSubmit()));

  // Keyboard: "/" focuses the page's search box.
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !e.target.closest('input, textarea, select') && $('#search')) { e.preventDefault(); $('#search').focus(); }
  });

  // Settings-style pages: highlight the section in view in the side menu.
  const toc = $('.toc');
  if (toc && 'IntersectionObserver' in window) {
    const links = $$('a[href^="#"]', toc), targets = links.map(a => $(a.getAttribute('href'))).filter(Boolean);
    const io = new IntersectionObserver(entries => {
      const visible = entries.filter(x => x.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (visible) links.forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#' + visible.target.id));
    }, { rootMargin: '-80px 0px -60% 0px' });
    targets.forEach(t => io.observe(t));
  }
})();
