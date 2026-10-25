(() => {
  const root = document.documentElement;
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
  window.addEventListener('nr-theme-change', updateCharts);
  updateCharts();
})();
