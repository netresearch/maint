(() => {
  const root = document.documentElement;
  const theme = document.querySelector('#theme-toggle');
  const system = matchMedia('(prefers-color-scheme: dark)');
  const isDark = () => root.dataset.theme ? root.dataset.theme === 'dark' : system.matches;
  const sync = () => theme?.setAttribute('aria-pressed', String(isDark()));
  theme?.addEventListener('click', () => {
    root.dataset.theme = isDark() ? 'light' : 'dark';
    try { localStorage.setItem('nr-theme', root.dataset.theme); } catch (_) {}
    sync();
    document.dispatchEvent(new Event('nr-theme-change'));
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
  system.addEventListener('change', () => { sync(); updateCharts(); });
  sync();
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
