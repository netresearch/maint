(() => {
  const root = document.documentElement;
  const theme = document.querySelector('[data-theme-control]');
  const system = matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try {
    const stored = localStorage.getItem('nr-theme');
    if (['system', 'light', 'dark'].includes(stored)) preference = stored;
  } catch (_) {}
  const applyTheme = () => {
    let effective = preference;
    if (preference === 'system') effective = system.matches ? 'dark' : 'light';
    root.dataset.theme = effective;
    if (theme) theme.value = preference;
    document.dispatchEvent(new Event('nr-theme-change'));
  };
  theme?.addEventListener('change', () => {
    preference = theme.value;
    try { localStorage.setItem('nr-theme', preference); } catch (_) {}
    applyTheme();
  });
  const updateCharts = () => {
    const style = getComputedStyle(root);
    const color = style.getPropertyValue('--fg').trim();
    const border = style.getPropertyValue('--border').trim();
    document.querySelectorAll('canvas').forEach((canvas) => {
      const chart = window.Chart?.getChart(canvas);
      if (!chart) return;
      chart.options.plugins.legend.labels.color = color;
      for (const axis of Object.values(chart.options.scales)) {
        axis.ticks.color = color;
        axis.grid.color = border;
      }
      for (const dataset of chart.data.datasets) {
        if (dataset.themeColor === 'foreground') {
          dataset.borderColor = color;
          dataset.backgroundColor = color;
        }
      }
      chart.update('none');
    });
  };
  document.addEventListener('nr-theme-change', updateCharts);
  system.addEventListener('change', applyTheme);
  applyTheme();
  const toggle = document.querySelector('.nav-toggle');
  const nav = document.querySelector('#site-nav');
  root.classList.add('js');
  const setOpen = (open) => {
    toggle?.setAttribute('aria-expanded', String(open));
    nav?.classList.toggle('is-open', open);
  };
  toggle?.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
  nav?.addEventListener('click', (event) => { if (event.target.closest('a')) setOpen(false); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && toggle?.getAttribute('aria-expanded') === 'true') {
      setOpen(false);
      toggle.focus();
    }
  });
})();
