(() => {
  'use strict';

  const state = { scope: 'lifetime', snapshot: null, history: null, sortKey: 'stars', sortDir: -1 };

  const KPI_FIELDS = {
    lifetime: [
      ['stars', 'Stars'],
      ['forks', 'Forks'],
      ['contributors', 'Contributors'],
      ['external_contributors', 'External contrib.'],
      ['issues_closed', 'Issues closed'],
      ['prs_merged', 'PRs merged'],
      ['releases', 'Releases'],
      ['commits', 'Commits'],
      ['packagist_downloads', 'Packagist DL'],
    ],
    recent_30d: [
      ['commits_30d', 'Commits'],
      ['issues_opened_30d', 'Issues opened'],
      ['prs_opened_30d', 'PRs opened'],
      ['prs_merged_30d', 'PRs merged'],
      ['releases_30d', 'Releases'],
      ['packagist_downloads_30d', 'Packagist DL'],
    ],
  };

  const nf = new Intl.NumberFormat('en');

  function fmt(n) { return (n === null || n === undefined) ? '—' : nf.format(n); }

  async function loadJSON(path) {
    const res = await fetch(path, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`failed to load ${path}: ${res.status}`);
    return res.json();
  }

  async function init() {
    try {
      const [snapshot, history] = await Promise.all([
        loadJSON('data/latest.json'),
        loadJSON('data/history.json').catch(() => ({ daily: [] })),
      ]);
      state.snapshot = snapshot;
      state.history = history;
    } catch (err) {
      document.getElementById('meta').textContent = 'Failed to load data: ' + err.message;
      return;
    }

    renderMeta();
    renderKPIs();
    renderCategoryCards();
    renderCharts();
    renderTable();
    attachHandlers();
  }

  function renderMeta() {
    const s = state.snapshot;
    const ts = new Date(s.generated_at).toLocaleString();
    const traffic = s.traffic_available ? 'traffic enabled' : 'traffic disabled (no PAT)';
    document.getElementById('meta').textContent =
      `${s.repos.length} repos · last updated ${ts} · ${traffic}`;
  }

  function renderKPIs() {
    const totals = state.snapshot.totals;
    const fields = KPI_FIELDS[state.scope];
    const el = document.getElementById('kpis');
    el.innerHTML = fields.map(([key, label]) => `
      <div class="kpi">
        <div class="kpi-label">${label}</div>
        <div class="kpi-value">${fmt(totals[key] ?? 0)}</div>
      </div>`).join('');
  }

  function renderCategoryCards() {
    const t3x = state.snapshot.repos.filter(r => r.category === 'typo3-extension');
    const skills = state.snapshot.repos.filter(r => r.category === 'skill');

    function card(title, cls, repos) {
      const sum = (path) => repos.reduce((acc, r) => acc + (r[path[0]]?.[path[1]] ?? 0), 0);
      return `
        <div class="card">
          <h3><span class="pill ${cls}">${title}</span> · ${repos.length} repos</h3>
          <div class="stat"><span>Stars</span><span>${fmt(sum(['lifetime', 'stars']))}</span></div>
          <div class="stat"><span>Forks</span><span>${fmt(sum(['lifetime', 'forks']))}</span></div>
          <div class="stat"><span>Contributors</span><span>${fmt(sum(['lifetime', 'contributors']))}</span></div>
          <div class="stat"><span>External contrib.</span><span>${fmt(sum(['lifetime', 'external_contributors']))}</span></div>
          <div class="stat"><span>Releases</span><span>${fmt(sum(['lifetime', 'releases']))}</span></div>
          <div class="stat"><span>Packagist DL</span><span>${fmt(sum(['lifetime', 'packagist_downloads']))}</span></div>
          <div class="stat"><span>Commits (30d)</span><span>${fmt(sum(['recent_30d', 'commits']))}</span></div>
        </div>`;
    }

    document.getElementById('category-cards').innerHTML =
      card('TYPO3 extensions', 'typo3', t3x) + card('Skills', 'skill', skills);
  }

  function renderCharts() {
    const daily = (state.history.daily || []).slice(-90);
    const labels = daily.map(d => d.date);

    const ctxStars = document.getElementById('chart-stars');
    if (ctxStars && window.Chart) {
      new Chart(ctxStars, {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'Stars', data: daily.map(d => d.totals?.stars ?? 0), borderColor: '#2F99A4', tension: 0.2 },
            { label: 'Forks', data: daily.map(d => d.totals?.forks ?? 0), borderColor: '#FF4D00', tension: 0.2 },
            { label: 'Contributors', data: daily.map(d => d.totals?.contributors ?? 0), borderColor: '#9aa0a6', tension: 0.2 },
          ],
        },
        options: chartOptions('Cumulative stars / forks / contributors (90 days)'),
      });
    }

    const ctxAct = document.getElementById('chart-activity');
    if (ctxAct && window.Chart) {
      new Chart(ctxAct, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            { label: 'Commits (30d trailing)', data: daily.map(d => d.totals?.commits_30d ?? 0), backgroundColor: '#2F99A4' },
            { label: 'PRs merged (30d)', data: daily.map(d => d.totals?.prs_merged_30d ?? 0), backgroundColor: '#FF4D00' },
            { label: 'Releases (30d)', data: daily.map(d => d.totals?.releases_30d ?? 0), backgroundColor: '#9aa0a6' },
          ],
        },
        options: chartOptions('Activity (30-day trailing totals, sampled daily)'),
      });
    }
  }

  function chartOptions(title) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        title: { display: true, text: title, color: '#e8eaed' },
        legend: { labels: { color: '#e8eaed' } },
      },
      scales: {
        x: { ticks: { color: '#9aa0a6' }, grid: { color: '#2a2f38' } },
        y: { ticks: { color: '#9aa0a6' }, grid: { color: '#2a2f38' }, beginAtZero: true },
      },
    };
  }

  function renderTable() {
    const tbody = document.querySelector('#repo-table tbody');
    const filter = document.getElementById('repo-filter').value.toLowerCase();
    const showT3x = document.getElementById('filter-t3x').checked;
    const showSkill = document.getElementById('filter-skill').checked;

    let rows = state.snapshot.repos.filter(r => {
      if (r.category === 'typo3-extension' && !showT3x) return false;
      if (r.category === 'skill' && !showSkill) return false;
      if (filter && !r.name.toLowerCase().includes(filter) && !(r.description || '').toLowerCase().includes(filter)) return false;
      return true;
    });

    rows.sort((a, b) => {
      const k = state.sortKey;
      let va, vb;
      if (k === 'name' || k === 'language') {
        va = (a[k] || '').toString(); vb = (b[k] || '').toString();
        return state.sortDir * va.localeCompare(vb);
      }
      if (k === 'commits_30d') { va = a.recent_30d?.commits ?? 0; vb = b.recent_30d?.commits ?? 0; }
      else if (k === 'blast_radius') { va = a.blast_radius ?? 0; vb = b.blast_radius ?? 0; }
      else { va = a.lifetime?.[k] ?? 0; vb = b.lifetime?.[k] ?? 0; }
      return state.sortDir * (va - vb);
    });

    tbody.innerHTML = rows.map(r => `
      <tr data-name="${r.name}">
        <td>
          <a href="${r.url}" target="_blank" rel="noopener">${r.name}</a>
          <span class="pill ${r.category === 'typo3-extension' ? 'typo3' : 'skill'}">${r.category === 'typo3-extension' ? 'TYPO3' : 'Skill'}</span>
        </td>
        <td>${r.language || '—'}</td>
        <td class="num">${fmt(r.lifetime.stars)}</td>
        <td class="num">${fmt(r.lifetime.forks)}</td>
        <td class="num">${fmt(r.lifetime.contributors)}</td>
        <td class="num">${fmt(r.lifetime.external_contributors)}</td>
        <td class="num">${fmt(r.lifetime.issues_open)}</td>
        <td class="num">${fmt(r.lifetime.prs_merged)}</td>
        <td class="num">${fmt(r.lifetime.releases)}</td>
        <td class="num">${fmt(r.recent_30d.commits)}</td>
        <td class="num">${fmt(r.lifetime.packagist_downloads)}</td>
        <td class="num">${fmt(r.blast_radius)}</td>
      </tr>`).join('');
  }

  function renderDetail(name) {
    const r = state.snapshot.repos.find(x => x.name === name);
    if (!r) return;
    const panel = document.getElementById('repo-detail');
    document.getElementById('detail-title').innerHTML =
      `<a href="${r.url}" target="_blank" rel="noopener">${r.full_name}</a>`;

    const traffic = r.traffic_14d;
    const trafficBlock = traffic ? `
      <div class="card">
        <h4>Traffic (last 14 days)</h4>
        <div class="stat"><span>Views (total / unique)</span><span>${fmt(traffic.views_total)} / ${fmt(traffic.views_unique)}</span></div>
        <div class="stat"><span>Clones (total / unique)</span><span>${fmt(traffic.clones_total)} / ${fmt(traffic.clones_unique)}</span></div>
        <div class="stat"><span>Top referrers</span><span></span></div>
        <ul class="referrer-list">${(traffic.top_referrers || []).map(rf => `<li>${rf.referrer}: ${fmt(rf.count)} views / ${fmt(rf.uniques)} unique</li>`).join('')}</ul>
      </div>` : '<div class="card"><h4>Traffic</h4><p>Traffic data not available (requires PAT with repo scope).</p></div>';

    const packagist = r.packagist ? `
      <div class="card">
        <h4>Packagist</h4>
        <div class="stat"><span>Total downloads</span><span>${fmt(r.packagist.total)}</span></div>
        <div class="stat"><span>Monthly</span><span>${fmt(r.packagist.monthly)}</span></div>
        <div class="stat"><span>Daily</span><span>${fmt(r.packagist.daily)}</span></div>
        <p><a href="${r.packagist.url}" target="_blank" rel="noopener">${r.packagist.name}</a></p>
      </div>` : '';

    const contributors = `
      <div class="card">
        <h4>Top contributors</h4>
        ${(r.top_contributors || []).map(c => `<div class="stat"><span><a href="${c.url}" target="_blank" rel="noopener">${c.login}</a></span><span>${fmt(c.contributions)}</span></div>`).join('')}
      </div>`;

    const release = r.latest_release ? `
      <div class="card">
        <h4>Latest release</h4>
        <div class="stat"><span>Tag</span><span><a href="${r.latest_release.url}" target="_blank" rel="noopener">${r.latest_release.tag_name}</a></span></div>
        <div class="stat"><span>Published</span><span>${r.latest_release.published_at ? new Date(r.latest_release.published_at).toLocaleDateString() : '—'}</span></div>
        <div class="stat"><span>Release downloads (total)</span><span>${fmt(r.lifetime.release_downloads)}</span></div>
      </div>` : '';

    document.getElementById('detail-body').innerHTML = `
      <p>${r.description || ''}</p>
      <div class="detail-grid">
        ${contributors}
        ${release}
        ${packagist}
        ${trafficBlock}
      </div>`;
    panel.hidden = false;
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function attachHandlers() {
    document.querySelectorAll('.toggle').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.toggle').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.scope = btn.dataset.scope;
        renderKPIs();
      });
    });

    document.querySelectorAll('#repo-table th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.sort;
        state.sortDir = (state.sortKey === key) ? -state.sortDir : -1;
        state.sortKey = key;
        renderTable();
      });
    });

    document.getElementById('repo-filter').addEventListener('input', renderTable);
    document.getElementById('filter-t3x').addEventListener('change', renderTable);
    document.getElementById('filter-skill').addEventListener('change', renderTable);

    document.querySelector('#repo-table tbody').addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-name]');
      if (tr) renderDetail(tr.dataset.name);
    });
  }

  init();
})();
