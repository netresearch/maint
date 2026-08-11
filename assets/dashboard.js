/**
 * Progressive enhancement for the impact dashboard.
 *
 * Every figure, the full repository table and the chart series are already in
 * the HTML when this file runs. Nothing here fetches data or writes a number the
 * page did not state. It adds: the lifetime / 30-day toggle, table sorting and
 * filtering, the charts, and the copy-citation button.
 */
(() => {
  'use strict';

  const locale = document.documentElement.lang || 'en';

  // ── Scope toggle ───────────────────────────────────────────────────────────
  // Both panels are rendered; this only chooses which one is shown.
  const panels = document.querySelectorAll('[data-scope-panel]');
  const toggles = document.querySelectorAll('.toggle[data-scope]');

  if (panels.length && toggles.length) {
    const show = (scope) => {
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.scopePanel !== scope;
      });
      toggles.forEach((button) => {
        const active = button.dataset.scope === scope;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      });
    };
    toggles.forEach((button) => {
      button.addEventListener('click', () => show(button.dataset.scope));
    });
    show('lifetime');
  }

  // ── Repository table: sort and filter ──────────────────────────────────────
  const table = document.getElementById('repo-table');
  if (table) {
    const tbody = table.tBodies[0];
    const rows = Array.from(tbody.rows);
    const search = document.getElementById('repo-filter');
    const categories = document.getElementById('category-filters');
    let sortKey = 'stars';
    let sortDir = -1;

    const value = (row, key) =>
      key === 'name' ? row.dataset.name : Number(row.dataset[key] || 0);

    const apply = () => {
      const term = (search?.value || '').trim().toLowerCase();
      const disabled = new Set(
        Array.from(categories?.querySelectorAll('input[data-cat]') || [])
          .filter((box) => !box.checked)
          .map((box) => box.dataset.cat),
      );

      const sorted = [...rows].sort((a, b) => {
        if (sortKey === 'name') {
          return sortDir * a.dataset.name.localeCompare(b.dataset.name, locale);
        }
        return sortDir * (value(a, sortKey) - value(b, sortKey));
      });

      sorted.forEach((row) => tbody.appendChild(row));
      rows.forEach((row) => {
        const matchesTerm = !term || row.dataset.name.toLowerCase().includes(term);
        row.hidden = !matchesTerm || disabled.has(row.dataset.cat);
      });
    };

    table.querySelectorAll('th[data-sort]').forEach((th) => {
      th.tabIndex = 0;
      th.setAttribute('role', 'button');
      const sort = () => {
        const key = th.dataset.sort;
        sortDir = sortKey === key ? -sortDir : -1;
        sortKey = key;
        table.querySelectorAll('th[data-sort]').forEach((other) => {
          other.removeAttribute('aria-sort');
        });
        th.setAttribute('aria-sort', sortDir === -1 ? 'descending' : 'ascending');
        apply();
      };
      th.addEventListener('click', sort);
      th.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          sort();
        }
      });
    });

    search?.addEventListener('input', apply);
    categories?.addEventListener('change', apply);
  }

  // ── Charts, drawn from the rendered history table ──────────────────────────
  // The table is the source. If Chart.js is missing the table remains, which is
  // the accessible representation anyway.
  const historyTable = document.getElementById('history-table');
  if (historyTable && window.Chart) {
    const rows = Array.from(historyTable.tBodies[0].rows);
    const column = (index) =>
      rows.map((row) => Number(row.cells[index].textContent.replace(/\D/g, '') || 0));
    const labels = rows.map((row) => row.cells[0].textContent.trim());

    const axes = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#e8eaed' } } },
      scales: {
        x: { ticks: { color: '#9aa0a6' }, grid: { color: '#2a2f38' } },
        y: { ticks: { color: '#9aa0a6' }, grid: { color: '#2a2f38' }, beginAtZero: true },
      },
    };

    const cumulative = document.getElementById('chart-cumulative');
    if (cumulative) {
      new Chart(cumulative, {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: historyTable.rows[0].cells[1].textContent, data: column(1), borderColor: '#2F99A4', tension: 0.2 },
            { label: historyTable.rows[0].cells[2].textContent, data: column(2), borderColor: '#FF4D00', tension: 0.2 },
            { label: historyTable.rows[0].cells[3].textContent, data: column(3), borderColor: '#9aa0a6', tension: 0.2 },
          ],
        },
        options: axes,
      });
    }

    const activity = document.getElementById('chart-activity');
    if (activity) {
      new Chart(activity, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            { label: historyTable.rows[0].cells[4].textContent, data: column(4), backgroundColor: '#2F99A4' },
            { label: historyTable.rows[0].cells[5].textContent, data: column(5), backgroundColor: '#FF4D00' },
            { label: historyTable.rows[0].cells[6].textContent, data: column(6), backgroundColor: '#9aa0a6' },
          ],
        },
        options: axes,
      });
    }
  }

  // ── Copy citation ──────────────────────────────────────────────────────────
  const copyButton = document.getElementById('copy-citation');
  const citation = document.getElementById('citation-text');
  if (copyButton && citation && navigator.clipboard) {
    const original = copyButton.textContent;
    copyButton.addEventListener('click', async () => {
      await navigator.clipboard.writeText(citation.textContent.trim());
      copyButton.textContent = copyButton.dataset.copied || 'Copied';
      setTimeout(() => {
        copyButton.textContent = original;
      }, 2000);
    });
  }
})();
