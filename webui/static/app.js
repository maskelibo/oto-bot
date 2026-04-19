/* ===================================================================
   oto-bot dashboard · client app
   =================================================================== */

// ---------- Helpers ----------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function fmt(n, digits = 2) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toFixed(digits);
}
function fmtPct(n, digits = 1) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return (n * 100).toFixed(digits) + '%';
}
function fmtInt(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('tr-TR');
}
function cls(val, pos='pos', neg='neg') {
  if (val === null || val === undefined || isNaN(val)) return '';
  return val > 0 ? pos : (val < 0 ? neg : '');
}

function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---------- Navigation ----------

const pages = ['overview','executive','agents','org','workflow','projects','goals','pods','live','learn','hr','lab'];

function show(page) {
  pages.forEach(p => {
    const el = document.getElementById('page-' + p);
    if (el) el.classList.toggle('hidden', p !== page);
  });
  $$('.nav-item').forEach(btn => btn.classList.toggle('active', btn.dataset.page === page));
  // Lazy render per page
  renderPage(page);
}

$$('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => show(btn.dataset.page));
});

// ---------- Data cache ----------

let cache = {};

async function refreshAll() {
  try {
    const [stats, agents, goals, current, queue] = await Promise.all([
      api('/api/stats'), api('/api/agents'), api('/api/goals'), api('/api/current'),
      api('/api/robustness/status').catch(() => ({queue_size: 0})),
    ]);
    cache.stats = stats;
    cache.agents = agents;
    cache.goals = goals;
    cache.current = current;
    cache.queue = queue;
    renderSidebar();
    renderLiveNow();
    const active = Array.from($$('.nav-item')).find(b => b.classList.contains('active'));
    if (active) renderPage(active.dataset.page);
  } catch (e) {
    console.error(e);
  }
}

// --------- Live now panel ---------

async function pollLiveNow() {
  try {
    cache.current = await api('/api/current');
    renderLiveNow();
  } catch (e) {}
}

function renderLiveNow() {
  const cur = cache.current || {};
  const hyp = cur.hypothesis || {};
  const active = new Set(cur.active_agents || []);
  const agents = cache.agents || [];

  $('#live-title').textContent = hyp.title || '— hipotez yok —';
  const metaBits = [];
  if (hyp.strategy) metaBits.push(hyp.strategy);
  if (hyp.market) metaBits.push(hyp.market);
  if (hyp.symbol) metaBits.push(hyp.symbol);
  if (hyp.timeframe) metaBits.push(hyp.timeframe);
  $('#live-meta').textContent = metaBits.join(' · ') || 'bilgi yok';

  $('#live-phase').textContent = cur.phase_label || cur.phase || '—';
  $('#live-cycle').textContent = cur.cycle || 0;

  const chipsEl = $('#live-agents');
  if (!agents.length) {
    chipsEl.innerHTML = '<span style="color:var(--text-3)">ajan bilgisi yükleniyor…</span>';
    return;
  }
  chipsEl.innerHTML = agents.map(a => {
    const isActive = active.has(a.name);
    return `<span class="agent-chip ${isActive ? 'active' : ''}" title="${escapeHtml(a.role || '')}">
      <span class="chip-icon">${a.icon}</span>${escapeHtml(a.name)}
    </span>`;
  }).join('');
}

// ---------- Sidebar ----------

function renderSidebar() {
  const s = cache.stats || {};
  $('#stat-experiments').textContent = fmtInt(s.experiments);
  $('#stat-decisions').textContent = fmtInt(s.decisions);
  $('#stat-debates').textContent = fmtInt(s.debates);
  $('#stat-agents').textContent = fmtInt(s.agents_active);
  $('#stat-winners').textContent = fmtInt(s.winners);
  // Queue size
  const q = cache.queue || {};
  const qSize = q.queue_size || 0;
  const qRow = $('#queue-row');
  if (qRow) {
    qRow.style.display = qSize > 0 ? '' : 'none';
    $('#stat-queue').textContent = fmtInt(qSize);
  }
  // Pending approval badge
  refreshPendingBanner();
}

async function refreshPendingBanner() {
  try {
    const r = await api('/api/proposals?status=pending&limit=1');
    const count = (r.counts && r.counts.pending) || 0;
    const banner = $('#pending-approval-banner');
    const badge = $('#pending-count-badge');
    if (count > 0) {
      badge.textContent = count;
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
  } catch (e) {}
}

// ---------- Overview ----------

let sharpeChart = null, roiChart = null;

async function renderOverview() {
  const s = cache.stats || {};
  $('#kpi-experiments').textContent = fmtInt(s.experiments);
  $('#kpi-promoted').textContent = fmtInt(s.experiments_promoted);
  $('#kpi-winners').textContent = fmtInt(s.winners);
  $('#kpi-active-agents').textContent = fmtInt(s.agents_active);
  $('#kpi-active-pods').textContent = fmtInt(s.pods_active);
  $('#kpi-avg-sharpe').textContent = fmt(s.avg_sharpe_recent, 2);

  const exps = await api('/api/experiments?limit=500');
  cache.experiments = exps;

  // Sharpe chart
  const last50 = exps.slice(0, 50).reverse();
  const labels = last50.map((_, i) => i + 1);
  const sharpes = last50.map(r => r.sharpe ?? 0);
  const ctx1 = $('#chart-sharpe').getContext('2d');
  if (sharpeChart) sharpeChart.destroy();
  sharpeChart = new Chart(ctx1, {
    type: 'line',
    data: { labels, datasets: [{ data: sharpes, borderColor: '#818cf8', backgroundColor: 'rgba(129,140,248,0.08)', tension: 0.25, fill: true, pointRadius: 0, borderWidth: 2 }]},
    options: chartOpts({ yTitle: 'Sharpe' }),
  });

  // ROI chart
  const last100 = exps.slice(0, 100).reverse();
  const roiLabels = last100.map((_, i) => i + 1);
  const rois = last100.map(r => (r.roi ?? 0) * 100);
  const ctx2 = $('#chart-roi').getContext('2d');
  if (roiChart) roiChart.destroy();
  roiChart = new Chart(ctx2, {
    type: 'bar',
    data: { labels: roiLabels, datasets: [{ data: rois, backgroundColor: rois.map(v => v >= 0 ? 'rgba(34,197,94,0.55)' : 'rgba(239,68,68,0.55)'), borderWidth: 0 }]},
    options: chartOpts({ yTitle: 'ROI (%)' }),
  });

  // Best 5 / worst 5
  const sorted = exps.filter(e => e.sharpe != null).slice();
  sorted.sort((a,b) => b.sharpe - a.sharpe);
  $('#table-best').innerHTML = renderMiniTable(sorted.slice(0,5), ['hypothesis_title','sharpe','roi','max_drawdown','total_trades']);
  $('#table-worst').innerHTML = renderMiniTable(sorted.slice(-5).reverse(), ['hypothesis_title','sharpe','roi','max_drawdown','total_trades']);

  // Activity feed (decisions)
  const act = await api('/api/activity');
  const feed = act.decisions.slice(0, 8).map(d => `
    <div class="feed-item">
      <div class="meta">${(d.timestamp||'').slice(0,19)} · <b>${escapeHtml(d.decision||'')}</b></div>
      <div class="body">${escapeHtml((d.reasoning||'').slice(0,220))}</div>
    </div>
  `).join('') || `<p class="muted">Henüz karar kaydı yok.</p>`;
  $('#activity-feed').innerHTML = feed;
}

function renderMiniTable(rows, cols) {
  if (!rows.length) return `<p style="color:var(--text-3)">Veri yok.</p>`;
  const headers = { hypothesis_title: 'başlık', sharpe: 'Sharpe', roi: 'ROI', max_drawdown: 'DD', total_trades: 'trade' };
  const head = cols.map(c => `<th>${headers[c] || c}</th>`).join('');
  const body = rows.map(r => `<tr>
    ${cols.map(c => {
      let v = r[c];
      if (c === 'roi' || c === 'max_drawdown') {
        const cssCls = cls(v);
        return `<td class="mono ${cssCls}">${fmtPct(v)}</td>`;
      }
      if (c === 'sharpe') {
        return `<td class="mono ${cls(v)}">${fmt(v)}</td>`;
      }
      if (c === 'total_trades') {
        return `<td class="mono">${fmtInt(v)}</td>`;
      }
      return `<td>${escapeHtml(String(v || '')).slice(0, 60)}</td>`;
    }).join('')}
  </tr>`).join('');
  return `<table class="t"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function chartOpts(opts = {}) {
  return {
    maintainAspectRatio: false,
    plugins: { legend: { display: false }, tooltip: { backgroundColor: '#1c212d', borderColor: '#2f3749', borderWidth: 1, titleColor: '#eef1f6', bodyColor: '#b7bfcd' } },
    scales: {
      x: { grid: { color: '#403f3c' }, ticks: { color: '#9C968A', font: { family: 'JetBrains Mono' } } },
      y: { grid: { color: '#403f3c' }, ticks: { color: '#9C968A', font: { family: 'JetBrains Mono' } }, title: opts.yTitle ? { display: true, text: opts.yTitle, color: '#7a8498' } : undefined },
    },
  };
}

// ---------- Agents ----------

function renderAgents() {
  const list = cache.agents || [];
  // Fill department filter
  const dept = $('#agents-dept');
  if (dept && !dept.dataset.ready) {
    const depts = Array.from(new Set(list.map(a => a.department))).sort();
    dept.innerHTML = `<option value="">Tüm departmanlar</option>` + depts.map(d => `<option value="${d}">${d}</option>`).join('');
    dept.dataset.ready = '1';
    dept.addEventListener('change', renderAgents);
    $('#agents-search').addEventListener('input', renderAgents);
    $('#agents-active-only').addEventListener('change', renderAgents);
  }

  const selectedDept = dept ? dept.value : '';
  const search = ($('#agents-search')?.value || '').toLowerCase();
  const activeOnly = $('#agents-active-only')?.checked;

  let filtered = list;
  if (selectedDept) filtered = filtered.filter(a => a.department === selectedDept);
  if (activeOnly) filtered = filtered.filter(a => a.active);
  if (search) filtered = filtered.filter(a => (a.name||'').toLowerCase().includes(search) || (a.role||'').toLowerCase().includes(search));

  $('#agents-counter').textContent = `${filtered.length} / ${list.length} gösteriliyor`;

  $('#agents-grid').innerHTML = filtered.map(a => {
    const featsHtml = (a.features || []).map(f => `<li>${escapeHtml(f)}</li>`).join('');
    return `
      <article class="agent-card" style="--agent-color: ${a.color}">
        <div class="ac-head">
          <div class="ac-title">
            <div class="ac-icon" style="background: ${a.color}22; border-color: ${a.color}55;">${a.icon}</div>
            <div>
              <h3 class="ac-name">${escapeHtml(a.name)}</h3>
              <div class="ac-role">${escapeHtml(a.role || '')}</div>
            </div>
          </div>
          <div>
            <span class="badge ${a.active ? 'badge-active' : 'badge-retired'}">${a.active ? 'aktif' : 'emekli'}</span>
            <span class="badge badge-dept">${escapeHtml(a.department || '')}</span>
          </div>
        </div>
        <div class="ac-section">
          <div class="ac-section-label">Ne yapıyor</div>
          <div class="ac-what">${escapeHtml(a.what_does || '')}</div>
        </div>
        <div class="ac-section">
          <div class="ac-section-label">Temel özellikler</div>
          <ul class="ac-features">${featsHtml}</ul>
        </div>
      </article>
    `;
  }).join('');
}

// ---------- Org chart ----------

let orgNet = null;

async function renderOrg() {
  const data = await api('/api/org');
  const container = document.getElementById('org-graph');
  if (orgNet) { orgNet.destroy(); orgNet = null; }

  const nodes = new vis.DataSet(data.nodes.map(n => ({
    id: n.id, label: n.label,
    color: { background: n.color, border: n.color, highlight: { background: n.color, border: '#fff' } },
    size: n.size || 22, shape: n.shape || 'dot',
    font: { color: '#eef1f6', face: 'Inter', size: 12, strokeWidth: 0, multi: true },
  })));
  const edges = new vis.DataSet(data.edges.map(e => ({
    from: e.from, to: e.to,
    color: { color: '#3a4257', opacity: 0.6 },
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    smooth: { type: 'continuous' },
  })));
  orgNet = new vis.Network(container, { nodes, edges }, {
    physics: { enabled: true, solver: 'forceAtlas2Based', forceAtlas2Based: { gravitationalConstant: -50, centralGravity: 0.01, springLength: 100 }, stabilization: { iterations: 150 } },
    interaction: { hover: true, zoomView: true, dragNodes: true },
    nodes: { borderWidth: 2, shadow: { enabled: true, color: 'rgba(0,0,0,0.3)', size: 10 } },
  });

  // Committees
  const committees = data.committees || {};
  $('#committees').innerHTML = Object.entries(committees).map(([name, info]) => `
    <div class="committee">
      <h4>${escapeHtml(name)}</h4>
      <p><span class="muted">Başkan:</span> ${escapeHtml(info.chair || '?')} · <span class="muted">ritim:</span> ${escapeHtml(info.cadence || '?')}</p>
      <p>${escapeHtml(info.mandate || '')}</p>
      <div class="members">${(info.members || []).map(m => `<span class="member-pill">${escapeHtml(m)}</span>`).join('')}</div>
    </div>
  `).join('');
}

// ---------- Workflow ----------

let wfNet = null;

async function renderWorkflow() {
  const data = await api('/api/workflow');
  const container = document.getElementById('workflow-graph');
  if (wfNet) { wfNet.destroy(); wfNet = null; }

  const nodes = new vis.DataSet(data.nodes.map(n => ({
    id: n.id, label: n.label,
    color: { background: n.color, border: n.color, highlight: { background: n.color, border: '#fff' } },
    size: n.size || 22, shape: n.shape || 'dot',
    font: { color: '#eef1f6', face: 'Inter', size: 12, strokeWidth: 0, multi: true },
  })));
  const edges = new vis.DataSet(data.edges.map(e => ({
    from: e.from, to: e.to,
    color: { color: e.color || '#4a5266', opacity: 0.7 },
    dashes: !!e.dashes,
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    smooth: { type: 'continuous' },
  })));
  wfNet = new vis.Network(container, { nodes, edges }, {
    physics: { enabled: true, solver: 'forceAtlas2Based', forceAtlas2Based: { gravitationalConstant: -60, centralGravity: 0.005, springLength: 110 }, stabilization: { iterations: 200 } },
    interaction: { hover: true, dragNodes: true },
    nodes: { borderWidth: 2, shadow: { enabled: true, color: 'rgba(0,0,0,0.3)', size: 8 } },
  });
}

// ---------- Projects (experiments) ----------

// Sort state (persists across renders, resets on page reload)
let projectsSort = { key: 'timestamp', dir: 'desc' };

function toggleSort(key) {
  if (projectsSort.key === key) {
    projectsSort.dir = projectsSort.dir === 'desc' ? 'asc' : 'desc';
  } else {
    projectsSort.key = key;
    // İlk tıkta varsayılan yön: sayısallarda yüksekten-alçağa, metinlerde alçaktan-yükseğe
    const numeric = ['roi','win_rate','profit_factor','sharpe','sortino','max_drawdown','cagr','total_trades','winning_trades','losing_trades','fees_est_usd'];
    projectsSort.dir = numeric.includes(key) ? 'desc' : 'asc';
  }
  renderProjects();
}
window.toggleSort = toggleSort;

let projectsView = 'bots'; // 'bots' | 'runs'

function setProjectsView(v) {
  projectsView = v;
  document.getElementById('view-bots')?.classList.toggle('active', v === 'bots');
  document.getElementById('view-runs')?.classList.toggle('active', v === 'runs');
  const gridEl = document.getElementById('bots-grid');
  const runsEl = document.getElementById('runs-card');
  if (gridEl) gridEl.style.display = (v === 'bots') ? 'grid' : 'none';
  if (runsEl) runsEl.style.display = (v === 'runs') ? 'block' : 'none';
  renderProjects();
}
window.setProjectsView = setProjectsView;

async function renderBotsGrid() {
  let bots = cache.bots;
  if (!bots) {
    bots = await api('/api/bots?limit=2000');
    cache.bots = bots;
  }

  // Apply filters (family/market/promoted)
  const famVal = $('#projects-family')?.value;
  const mktVal = $('#projects-market')?.value;
  const promotedOnly = $('#projects-promoted-only')?.checked;
  const showAll = $('#projects-show-all')?.checked;

  let sub = bots.slice();
  if (famVal) sub = sub.filter(b => b.strategy_family === famVal);
  if (mktVal) sub = sub.filter(b => (b.symbols_seen || []).some(s => s.includes(mktVal) || mktVal.includes(s)));
  if (promotedOnly) sub = sub.filter(b => b.promoted_count > 0);

  // Tier-based filter
  const tier = $('#projects-tier')?.value || '50';
  if (!showAll) {
    const field = tier === '20' ? 'qualified_20' :
                  tier === '30' ? 'qualified_30' : 'qualified_50';
    sub = sub.filter(b => b[field] === true);
  }

  // Sort — dropdown'dan seçilen alan descending
  const sortKey = $('#bots-sort')?.value || 'linear_score';
  sub.sort((a, b) => {
    const va = a[sortKey] ?? 0;
    const vb = b[sortKey] ?? 0;
    if (va !== vb) return vb - va;
    // Tiebreak by avg_sharpe
    return (b.avg_sharpe || 0) - (a.avg_sharpe || 0);
  });

  // KPIs — all 3 tiers
  const totalAll = bots.length;
  const q20 = bots.filter(x => x.qualified_20).length;
  const q30 = bots.filter(x => x.qualified_30).length;
  const q50 = bots.filter(x => x.qualified_50).length;

  $('#projects-kpis').innerHTML = [
    { label: `Seçilen tier · ${tier}%`, val: fmtInt(sub.length), note: `${fmtInt(totalAll)} bot'tan` },
    { label: '%20 yıllık', val: fmtInt(q20), note: 'ulaşılabilir hedef' },
    { label: '%30 yıllık', val: fmtInt(q30), note: 'makul hedef' },
    { label: '🏆 %50 yıllık', val: fmtInt(q50), note: 'sert hedef', cls: 'accent-gold' },
  ].map(k => `<div class="kpi ${k.cls||''}"><div class="kpi-label">${k.label}</div><div class="kpi-value mono">${k.val}</div>${k.note ? `<div class="kpi-delta">${k.note}</div>` : ''}</div>`).join('');

  if (!sub.length) {
    const showAllVal = $('#projects-show-all')?.checked;
    const msg = showAllVal
      ? 'Hiç bot yok.'
      : `Henüz <b>%50 lineer yıllık büyüme</b> filtresini geçen bot yok.<br>
         Otonom loop yeni botlar üretmeye devam ediyor (${fmtInt(totalAll)} bot test edildi, hiçbiri bu kriteri geçemedi).<br>
         <br>
         <b>Ne istiyoruz:</b> 1 yılda ≥%50, 2 yılda ≥%100, 3 yılda ≥%150 ROI...<br>
         <br>
         ⚙️ <b>"Tüm botları göster"</b> toggle'ını açarsan tüm botları görebilirsin.`;
    $('#bots-grid').innerHTML = `
      <div style="grid-column:1/-1;padding:40px;text-align:center;background:var(--surface-2);border:1px dashed var(--border);border-radius:14px">
        <div style="font-size:48px;margin-bottom:12px">🏆</div>
        <p style="color:var(--text-2);line-height:1.7;font-size:0.95rem">${msg}</p>
      </div>`;
    return;
  }

  $('#bots-grid').innerHTML = sub.slice(0, 300).map(b => {
    const promoPct = Math.round(b.promotion_rate * 100);
    const horizonOrder = ['1yr','2yr','3yr','4yr','5yr'];
    const horizonChips = horizonOrder.map(h => {
      const stat = b.horizons[h];
      if (!stat) {
        return `<div class="horizon-chip empty">
          <div class="hc-label">${h}</div>
          <div class="hc-value">—</div>
        </div>`;
      }
      const cls = stat.profit_usd_est >= 0 ? 'pos' : 'neg';
      const sign = stat.profit_usd_est >= 0 ? '+' : '';
      return `<div class="horizon-chip active clickable" onclick="event.stopPropagation();openHorizon('${b.bot_id}','${h}')" title="Tıkla, ${stat.runs} run detayını gör">
        <div class="hc-label">${h}</div>
        <div class="hc-value ${cls}">${sign}$${fmtInt(stat.profit_usd_est)}</div>
      </div>`;
    }).join('');

    const sharpeCls = b.avg_sharpe >= 1 ? 'pos' : (b.avg_sharpe < 0 ? 'neg' : '');
    const roiCls = b.avg_roi >= 0 ? 'pos' : 'neg';
    const score = b.linear_score || 0;
    const badgeLabel = score >= 5 ? '🏆 5/5 horizon' : score >= 3 ? `⭐ ${score}/5 horizon` : `${score}/5 horizon`;
    const badgeCss = score >= 5 ? 'background:rgba(232,180,100,0.2);color:#E8B464;border-color:rgba(232,180,100,0.5)' :
                     score >= 3 ? 'background:rgba(126,182,104,0.18);color:#7EB668;border-color:rgba(126,182,104,0.4)' :
                     'background:var(--surface-2);color:var(--text-3)';
    return `
      <div class="bot-card" onclick="openBotDetail('${b.bot_id}')">
        <div class="bot-head">
          <div class="bot-icon">🤖</div>
          <div class="bot-title">
            <div class="bot-name">${escapeHtml(b.bot_name)}</div>
            <div class="bot-meta">${escapeHtml(b.strategy_family)} · ${b.total_runs} run · ${b.symbols_seen.length} sembol</div>
          </div>
          <span class="badge" style="${badgeCss};border-width:1px;border-style:solid;flex-shrink:0">${badgeLabel}</span>
        </div>
        <div class="bot-stats">
          <div class="bot-stat"><div class="bs-label">Avg Sharpe</div><div class="bs-value ${sharpeCls}">${fmt(b.avg_sharpe)}</div></div>
          <div class="bot-stat"><div class="bs-label">Avg ROI</div><div class="bs-value ${roiCls}">${fmtPct(b.avg_roi)}</div></div>
          <div class="bot-stat"><div class="bs-label">Worst DD</div><div class="bs-value neg">${fmtPct(b.worst_dd)}</div></div>
        </div>
        <div class="horizon-row">${horizonChips}</div>
        <div class="bot-promotion-bar"><div class="bot-promotion-fill" style="width:${promoPct}%"></div></div>
        <div class="bot-footer">
          <span>Promotion rate: <b>${promoPct}%</b> (${b.promoted_count}/${b.total_runs})</span>
          <span>Max ${fmt(b.longest_run_months,1)} ay</span>
        </div>
      </div>
    `;
  }).join('');
}

async function openBotDetail(botId) {
  try {
    const b = await api(`/api/bot/${botId}`);
    if (!b || b.error) return alert('Bot bulunamadı');

    // Also fetch bot-level (for horizons)
    const bots = cache.bots || [];
    const botMeta = bots.find(x => x.bot_id === botId);

    renderBotDetailModal(b, botMeta);
  } catch (e) { alert('Hata: ' + e.message); }
}
window.openBotDetail = openBotDetail;

async function bulkLongHorizonTest() {
  if (!confirm('Tüm avg_sharpe >= 0 botları 1yr + 3yr horizon\'larda test kuyruğuna eklensin mi?\n\n~300 bot × 2 test = ~600 test sırada olacak.\nOrchestrator saatte ~500 cycle yapıyor, bu 1-2 saat alır.')) return;
  try {
    const r = await fetch('/api/bots/test-all-long-horizon?min_avg_sharpe=0&horizons=1,3&max_bots=300', { method: 'POST' }).then(x => x.json());
    if (r.error) return alert('Hata: ' + r.error);
    alert(`✅ ${r.scheduled_tests} test kuyruğa eklendi\n\nSeçilen bot: ${r.eligible_bots}/${r.total_bots_seen}\nHorizon: ${r.horizons.join(' + ')} yıl`);
  } catch (e) { alert('Hata: ' + e.message); }
}
window.bulkLongHorizonTest = bulkLongHorizonTest;

async function pruneBotsAggressive() {
  try {
    const dry = await fetch('/api/bots/prune?min_avg_sharpe=0.5&min_avg_roi=0.0&min_runs=2&dry_run=true', { method: 'POST' }).then(r => r.json());
    if (!confirm(`🔥 AGRESİF MOD: ${dry.bots_to_delete} bot (${dry.experiments_to_delete} deney) silinecek.\n\nKriter: avg Sharpe < 0.5 VEYA avg ROI < 0 · ≥2 run.\n\nKalan sadece anlamlı botlar olacak. Devam?`)) return;
    const res = await fetch('/api/bots/prune?min_avg_sharpe=0.5&min_avg_roi=0.0&min_runs=2', { method: 'POST' }).then(r => r.json());
    alert(`🔥 Silindi:\n• ${res.bots_deleted} bot\n• ${res.experiments_deleted} deney`);
    cache.bots = null;
    renderProjects();
  } catch (e) { alert('Hata: ' + e.message); }
}
window.pruneBotsAggressive = pruneBotsAggressive;

async function testLongHorizon(botId) {
  if (!confirm('Bu bot 1/2/3/5 yıl pencerelerde tekrar test edilsin mi?\n\n~24 varyant kuyruğa eklenecek (3 sembol × 2 timeframe × 4 horizon).\nOrchestrator bunları otomatik işler.')) return;
  try {
    const r = await fetch(`/api/bot/${botId}/test-long-horizon`, { method: 'POST' }).then(x => x.json());
    if (r.error) return alert('Hata: ' + r.error);
    const sample = (r.sample_tests || []).map(t => `${t.symbol}/${t.timeframe}/${t.years}yr`).join('\n');
    alert(`✅ ${r.scheduled} test kuyruğa eklendi.\n\nÖrnekler:\n${sample}`);
  } catch (e) { alert('Hata: ' + e.message); }
}
window.testLongHorizon = testLongHorizon;

async function pruneBots() {
  try {
    const dry = await fetch('/api/bots/prune?dry_run=true', { method: 'POST' }).then(r => r.json());
    if (!confirm(`${dry.bots_to_delete} başarısız bot (${dry.experiments_to_delete} deney) silinecek.\n\nKriter: avg Sharpe < 0 VE avg ROI < -5% VE >=2 run.\n\nDevam?`)) return;
    const res = await fetch('/api/bots/prune', { method: 'POST' }).then(r => r.json());
    alert(`✅ Silindi:\n• ${res.bots_deleted} bot\n• ${res.experiments_deleted} deney`);
    cache.bots = null;
    renderProjects();
  } catch (e) { alert('Hata: ' + e.message); }
}
window.pruneBots = pruneBots;

function renderBotDetailModal(b, botMeta) {
  const profitCls = b.total_profit_usd >= 0 ? '' : 'neg';
  const profitSign = b.total_profit_usd >= 0 ? '+' : '';

  // Horizon chips from botMeta.horizons
  const horizonOrder = ['1yr','2yr','3yr','4yr','5yr'];
  const horizons = botMeta?.horizons || {};
  const horizonChips = horizonOrder.map(h => {
    const stat = horizons[h];
    if (!stat) {
      return `<div class="horizon-chip empty"><div class="hc-label">${h}</div><div class="hc-value">—</div></div>`;
    }
    const cls = stat.profit_usd_est >= 0 ? 'pos' : 'neg';
    const sign = stat.profit_usd_est >= 0 ? '+' : '';
    return `<div class="horizon-chip active clickable" onclick="openHorizon('${b.bot_id}','${h}')">
      <div class="hc-label">${h}</div>
      <div class="hc-value ${cls}">${sign}$${fmtInt(stat.profit_usd_est)}</div>
    </div>`;
  }).join('');

  // By-symbol + by-timeframe tables
  const symRows = (b.by_symbol || []).slice(0, 10).map(s => `
    <tr>
      <td>${escapeHtml(s.key || '—')}</td>
      <td class="mono">${fmtInt(s.runs)}</td>
      <td class="mono ${cls(s.avg_sharpe)}">${fmt(s.avg_sharpe)}</td>
      <td class="mono ${cls(s.avg_roi)}">${fmtPct(s.avg_roi)}</td>
      <td class="mono ${cls(s.total_profit_usd)}">${s.total_profit_usd >= 0 ? '+' : ''}$${fmtInt(s.total_profit_usd)}</td>
    </tr>
  `).join('');

  const tfRows = (b.by_timeframe || []).slice(0, 10).map(s => `
    <tr>
      <td>${escapeHtml(s.key || '—')}</td>
      <td class="mono">${fmtInt(s.runs)}</td>
      <td class="mono ${cls(s.avg_sharpe)}">${fmt(s.avg_sharpe)}</td>
      <td class="mono ${cls(s.avg_roi)}">${fmtPct(s.avg_roi)}</td>
      <td class="mono ${cls(s.total_profit_usd)}">${s.total_profit_usd >= 0 ? '+' : ''}$${fmtInt(s.total_profit_usd)}</td>
    </tr>
  `).join('');

  const recentRows = (b.recent_runs || []).slice(0, 30).map(r => {
    const profCls2 = (r.profit_usd || 0) >= 0 ? 'pos' : 'neg';
    const profSign = (r.profit_usd || 0) >= 0 ? '+' : '';
    return `
      <tr onclick="openProjectDetail({experiment_id:'${r.experiment_id || ''}',strategy_family:'${b.strategy_family || ''}'})">
        <td class="mono">${(r.timestamp || '').slice(5,16).replace('T',' ')}</td>
        <td>${escapeHtml(r.symbol || '—')}</td>
        <td>${escapeHtml(r.timeframe || '—')}</td>
        <td>${escapeHtml(r.regime || '—')}</td>
        <td class="mono ${cls(r.sharpe)}">${fmt(r.sharpe)}</td>
        <td class="mono ${cls(r.roi)}">${fmtPct(r.roi)}</td>
        <td class="mono ${profCls2}">${profSign}$${fmtInt(r.profit_usd)}</td>
        <td>${escapeHtml(r.duration_human || '')}</td>
        <td>${r.promoted ? '<span style="color:#7EB668">✓</span>' : ''}</td>
      </tr>
    `;
  }).join('');

  // Params list
  const params = b.strategy_params || {};
  const paramRows = Object.entries(params).map(([k,v]) =>
    `<tr><td style="color:var(--text-3);padding:4px 10px">${escapeHtml(k)}</td><td class="mono" style="padding:4px 10px">${escapeHtml(String(v))}</td></tr>`
  ).join('');

  const html = `
    <div class="modal-backdrop" onclick="if(event.target === this) closeProjectModal()">
      <div class="modal-box">
        <div class="modal-head">
          <div style="flex:1; min-width:0">
            <h2>🤖 ${escapeHtml(b.bot_name)}</h2>
            <div class="modal-sub">
              ${escapeHtml(b.strategy_family || '')} ailesi ·
              ${b.total_runs} run ·
              ${(botMeta?.symbols_seen || []).length} sembol ·
              ${(botMeta?.timeframes_seen || []).length} timeframe
            </div>
          </div>
          <div style="display:flex;gap:8px;align-items:flex-start">
            <button onclick="testLongHorizon('${b.bot_id}')"
              style="background:var(--accent-soft);border:1px solid var(--accent);color:var(--accent-hi);padding:8px 14px;border-radius:8px;cursor:pointer;font-weight:500;font-size:0.82rem"
              title="Bu botu 1/2/3/5 yıl pencerelerde test kuyruğuna ekle">
              📅 Uzun horizon testi
            </button>
            <button class="modal-close" onclick="closeProjectModal()">✕</button>
          </div>
        </div>

        <div class="modal-body">
          <div class="usd-hero ${profitCls}">
            <div class="usd-label">Toplam simüle P/L · $10,000 notional</div>
            <div class="usd-row">
              <span class="usd-start">∑</span>
              <span class="usd-arrow">→</span>
              <span class="usd-final">${profitSign}$${fmtInt(b.total_profit_usd)}</span>
            </div>
            <div class="usd-meta">
              <span class="chip">Ort. Sharpe ${fmt(b.avg_sharpe)}</span>
              <span class="chip">Ort. ROI ${fmtPct(b.avg_roi)}</span>
              <span class="chip">Ort. CAGR ${fmtPct(b.avg_cagr)}</span>
              <span class="chip">Toplam simüle süre ${fmt(b.total_simulated_months, 1)} ay</span>
            </div>
          </div>

          <div class="modal-section-title">📅 Yıl-yıl breakdown · $10.000 ile başlasaydın</div>
          ${(() => {
            const fullOrder = ['<1yr', '1yr', '2yr', '3yr', '4yr', '5yr'];
            const yearsLabel = {
              '<1yr': '< 1 yıl', '1yr': '1 yıl', '2yr': '2 yıl',
              '3yr': '3 yıl', '4yr': '4 yıl', '5yr': '5 yıl'
            };
            const rows = fullOrder.map(h => {
              const stat = horizons[h];
              if (!stat) {
                return `<tr style="opacity:0.35">
                  <td style="padding:10px 14px;color:var(--text-3);font-weight:500">${yearsLabel[h]}</td>
                  <td colspan="5" style="padding:10px 14px;color:var(--text-3);font-size:0.82rem">— run yok</td>
                </tr>`;
              }
              const prof = stat.profit_usd_est || 0;
              const cls2 = prof >= 0 ? 'pos' : 'neg';
              const sign = prof >= 0 ? '+' : '';
              const finalUsd = stat.final_usd_est || 10000;

              let targetCol;
              if (h === '<1yr') {
                targetCol = `<span style="color:var(--text-3);font-size:0.78rem">— lineer hedef ≥1 yıl için</span>`;
              } else {
                const yearsNum = parseInt(h);
                const requiredRoi = yearsNum * 0.50;
                const passed = (stat.avg_roi || 0) >= requiredRoi;
                targetCol = `
                  ${passed ? '<span style="color:#7EB668;font-weight:700">✓</span>' : '<span style="color:#D66B5C">✗</span>'}
                  <span style="color:var(--text-3);font-size:0.76rem;margin-left:4px">hedef ≥${fmtPct(requiredRoi)}</span>
                `;
              }

              return `<tr onclick="openHorizon('${b.bot_id}','${h}')" style="cursor:pointer">
                <td style="padding:12px 14px;color:var(--text-1);font-weight:600;font-family:'JetBrains Mono',monospace">${yearsLabel[h]}</td>
                <td class="mono" style="padding:12px 14px"><b>$${fmtInt(finalUsd)}</b></td>
                <td class="mono ${cls2}" style="padding:12px 14px"><b>${sign}$${fmtInt(prof)}</b></td>
                <td class="mono ${cls(stat.avg_roi)}" style="padding:12px 14px">${fmtPct(stat.avg_roi)}</td>
                <td class="mono ${cls(stat.avg_sharpe)}" style="padding:12px 14px">${fmt(stat.avg_sharpe)}</td>
                <td style="padding:12px 14px">${targetCol}</td>
                <td class="mono" style="padding:12px 14px;color:var(--text-3)">${stat.runs} run</td>
              </tr>`;
            }).join('');

            // Data durumu özeti
            const hasLongHorizon = horizonOrder.some(h => horizons[h]);
            const notice = !hasLongHorizon ? `
              <div style="background:rgba(232,180,100,0.10);border:1px solid rgba(232,180,100,0.3);border-radius:10px;padding:12px 14px;margin-top:10px;color:#E8B464;font-size:0.84rem;line-height:1.55">
                ⚠️ Bu botun tüm run'ları <b>&lt; 1 yıl</b> penceresinde. Daha uzun horizon (1+ yıl)
                için backtest engine'e daha fazla geçmiş veri gerekli. Loop çalıştıkça
                1yr/2yr bucket'ları dolacak.
              </div>
            ` : '';

            return `
              <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden">
                <table class="trades-mini" style="width:100%">
                  <thead><tr>
                    <th>süre</th><th>final $</th><th>kâr/zarar</th>
                    <th>ort. ROI</th><th>ort. Sharpe</th>
                    <th>lineer %50 hedef</th><th>run</th>
                  </tr></thead>
                  <tbody>${rows}</tbody>
                </table>
              </div>
              ${notice}
              <p style="color:var(--text-3);font-size:0.80rem;margin-top:8px">
                💡 Satıra tıkla → o yıl diliminin tüm run'larını gör. Hedef: her yıl için +%50 ROI (lineer büyüme).
              </p>
            `;
          })()}

          <div class="modal-section-title">🏆 En iyi / en kötü run</div>
          <div class="row-2">
            <div class="stat-box" style="padding:14px">
              <div class="sb-label" style="color:#7EB668">EN İYİ</div>
              <div style="color:var(--text-1);font-family:'JetBrains Mono',monospace;margin-top:4px">
                ${escapeHtml(b.best_run.symbol || '—')} / ${escapeHtml(b.best_run.timeframe || '—')}
              </div>
              <div style="color:var(--text-3);font-size:0.82rem;margin-top:4px">
                Sharpe ${fmt(b.best_run.sharpe)} · ROI ${fmtPct(b.best_run.roi)} · ${escapeHtml(b.best_run.duration_human || '')}
              </div>
            </div>
            <div class="stat-box" style="padding:14px">
              <div class="sb-label" style="color:#D66B5C">EN KÖTÜ</div>
              <div style="color:var(--text-1);font-family:'JetBrains Mono',monospace;margin-top:4px">
                ${escapeHtml(b.worst_run.symbol || '—')} / ${escapeHtml(b.worst_run.timeframe || '—')}
              </div>
              <div style="color:var(--text-3);font-size:0.82rem;margin-top:4px">
                Sharpe ${fmt(b.worst_run.sharpe)} · ROI ${fmtPct(b.worst_run.roi)} · ${escapeHtml(b.worst_run.duration_human || '')}
              </div>
            </div>
          </div>

          <div class="row-2">
            <div>
              <div class="modal-section-title">📊 Sembol bazında</div>
              <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden">
                <table class="trades-mini">
                  <thead><tr><th>Sembol</th><th>run</th><th>Sharpe</th><th>ROI</th><th>P/L $</th></tr></thead>
                  <tbody>${symRows || '<tr><td colspan="5" style="color:var(--text-3)">veri yok</td></tr>'}</tbody>
                </table>
              </div>
            </div>
            <div>
              <div class="modal-section-title">⏰ Timeframe bazında</div>
              <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden">
                <table class="trades-mini">
                  <thead><tr><th>TF</th><th>run</th><th>Sharpe</th><th>ROI</th><th>P/L $</th></tr></thead>
                  <tbody>${tfRows || '<tr><td colspan="5" style="color:var(--text-3)">veri yok</td></tr>'}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="modal-section-title">🕐 Son 30 run (tıkla → tam detay)</div>
          <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden;max-height:400px;overflow-y:auto">
            <table class="trades-mini">
              <thead><tr>
                <th>zaman</th><th>sembol</th><th>TF</th><th>rejim</th>
                <th>Sharpe</th><th>ROI</th><th>P/L $</th><th>süre</th><th>prom</th>
              </tr></thead>
              <tbody>${recentRows || '<tr><td colspan="9" style="color:var(--text-3)">run yok</td></tr>'}</tbody>
            </table>
          </div>

          ${paramRows ? `
          <div class="modal-section-title">⚙️ Parametreler (tüm run'lar bu param setini kullandı)</div>
          <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden">
            <table class="trades-mini"><tbody>${paramRows}</tbody></table>
          </div>
          ` : ''}
        </div>
      </div>
    </div>
  `;

  closeProjectModal();
  const wrap = document.createElement('div');
  wrap.id = 'project-modal-wrap';
  wrap.innerHTML = html;
  document.body.appendChild(wrap);
  document.body.style.overflow = 'hidden';
}

async function openHorizon(botId, horizon) {
  try {
    const h = await api(`/api/bot/${botId}/horizon/${horizon}`);
    if (!h || h.error || h.runs === 0) {
      alert(`${horizon} için çalıştırılmış run yok.`);
      return;
    }
    renderHorizonModal(h);
  } catch (e) { alert('Hata: ' + e.message); }
}
window.openHorizon = openHorizon;

function renderHorizonModal(h) {
  const profitCls = h.total_profit_usd >= 0 ? 'pos' : 'neg';
  const sign = h.total_profit_usd >= 0 ? '+' : '';
  const tf_to_min = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440};

  const itemsRows = (h.items || []).map(it => {
    // Compute avg trade duration in days
    let tradeDays = '—';
    if (it.avg_trade_duration && it.timeframe) {
      const mins = it.avg_trade_duration * (tf_to_min[it.timeframe] || 60);
      const days = mins / (60 * 24);
      tradeDays = days < 1 ? `${(days * 24).toFixed(1)}sa` : `${days.toFixed(1)}g`;
    }
    const roiCls = (it.roi || 0) >= 0 ? 'pos' : 'neg';
    const sharpeCls = (it.sharpe || 0) >= 0 ? 'pos' : 'neg';
    const profitCls2 = (it.profit_usd || 0) >= 0 ? 'pos' : 'neg';
    const profitSign = (it.profit_usd || 0) >= 0 ? '+' : '';
    return `
      <tr onclick="openProjectDetail({experiment_id:'${it.experiment_id || ''}',strategy_family:'${h.strategy_family || ''}'})">
        <td class="mono">${(it.timestamp || '').slice(5,16).replace('T',' ')}</td>
        <td>${escapeHtml(it.symbol || '—')}</td>
        <td>${escapeHtml(it.timeframe || '—')}</td>
        <td>${escapeHtml(it.regime || '—')}</td>
        <td class="mono ${sharpeCls}">${fmt(it.sharpe)}</td>
        <td class="mono ${roiCls}">${fmtPct(it.roi)}</td>
        <td class="mono ${profitCls2}">${profitSign}$${fmtInt(it.profit_usd)}</td>
        <td class="mono">${escapeHtml(it.duration_human || '')}</td>
        <td class="mono">${fmtInt(it.total_trades)}</td>
        <td class="mono">${tradeDays}</td>
        <td>${it.promoted ? '<span style="color:#7EB668">✓</span>' : ''}</td>
      </tr>
    `;
  }).join('');

  const html = `
    <div class="modal-backdrop" onclick="if(event.target === this) closeProjectModal()">
      <div class="modal-box">
        <div class="modal-head">
          <div style="flex:1;min-width:0">
            <h2>🤖 ${escapeHtml(h.bot_name)} · ${escapeHtml(h.horizon)} horizon</h2>
            <div class="modal-sub">${escapeHtml(h.strategy_family || '')} · ${h.runs} run bu zaman diliminde</div>
          </div>
          <button class="modal-close" onclick="closeProjectModal()">✕</button>
        </div>
        <div class="modal-body">
          <div class="usd-hero ${profitCls}">
            <div class="usd-label">${escapeHtml(h.horizon)} · $10,000 başlangıç</div>
            <div class="usd-row">
              <span class="usd-start">$10,000</span>
              <span class="usd-arrow">→</span>
              <span class="usd-final">$${fmtInt(h.final_usd_est || 0)}</span>
            </div>
            <div class="usd-meta">
              <span class="chip">Ort. ROI ${fmtPct(h.avg_roi)}</span>
              <span class="chip">Ort. Sharpe ${fmt(h.avg_sharpe)}</span>
              <span class="chip">Toplam simüle P/L ${sign}$${fmtInt(h.total_profit_usd)}</span>
              <span class="chip">${h.runs} run</span>
            </div>
          </div>

          <div class="modal-section-title">🏆 En iyi / en kötü run</div>
          <div class="row-2">
            <div class="stat-box" style="padding:14px">
              <div class="sb-label" style="color:#7EB668">EN İYİ</div>
              <div style="color:var(--text-1);font-family:'JetBrains Mono',monospace;margin-top:4px">
                ${escapeHtml(h.best.symbol || '—')} / ${escapeHtml(h.best.timeframe || '—')}
              </div>
              <div style="color:var(--text-3);font-size:0.82rem;margin-top:4px">
                Sharpe ${fmt(h.best.sharpe)} · ROI ${fmtPct(h.best.roi)}
              </div>
            </div>
            <div class="stat-box" style="padding:14px">
              <div class="sb-label" style="color:#D66B5C">EN KÖTÜ</div>
              <div style="color:var(--text-1);font-family:'JetBrains Mono',monospace;margin-top:4px">
                ${escapeHtml(h.worst.symbol || '—')} / ${escapeHtml(h.worst.timeframe || '—')}
              </div>
              <div style="color:var(--text-3);font-size:0.82rem;margin-top:4px">
                Sharpe ${fmt(h.worst.sharpe)} · ROI ${fmtPct(h.worst.roi)}
              </div>
            </div>
          </div>

          <div class="modal-section-title">📋 Tüm run'lar (tıkla → detay)</div>
          <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden;max-height:500px;overflow-y:auto">
            <table class="trades-mini">
              <thead><tr>
                <th>zaman</th><th>sembol</th><th>TF</th><th>rejim</th>
                <th>Sharpe</th><th>ROI</th><th>P/L $</th>
                <th>süre</th><th>trade</th><th>ort. poz.</th><th>prom</th>
              </tr></thead>
              <tbody>${itemsRows || '<tr><td colspan="11" style="color:var(--text-3)">Bu horizon\'da run yok</td></tr>'}</tbody>
            </table>
          </div>

          <p style="color:var(--text-3);font-size:0.80rem;margin-top:12px">
            💡 <b>"ort. poz."</b> = ortalama tek trade pozisyonda tutulma süresi. "2.5g" = 2.5 gün.
          </p>
        </div>
      </div>
    </div>
  `;
  closeProjectModal();
  const wrap = document.createElement('div');
  wrap.id = 'project-modal-wrap';
  wrap.innerHTML = html;
  document.body.appendChild(wrap);
  document.body.style.overflow = 'hidden';
}

async function renderProjects() {
  if (projectsView === 'bots') {
    cache.bots = null;  // force fresh
    await renderBotsGrid();
    return;
  }

  const exps = cache.experiments || await api('/api/experiments?limit=1000');
  cache.experiments = exps;

  // Fill selects
  const famSel = $('#projects-family');
  const mktSel = $('#projects-market');
  if (!famSel.dataset.ready) {
    const families = Array.from(new Set(exps.map(e => e.strategy_family).filter(Boolean))).sort();
    const markets = Array.from(new Set(exps.map(e => e.market).filter(Boolean))).sort();
    famSel.innerHTML = `<option value="">Tüm aileler</option>` + families.map(f => `<option value="${f}">${f}</option>`).join('');
    mktSel.innerHTML = `<option value="">Tüm marketler</option>` + markets.map(m => `<option value="${m}">${m}</option>`).join('');
    famSel.dataset.ready = '1';
    famSel.addEventListener('change', renderProjects);
    mktSel.addEventListener('change', renderProjects);
    $('#projects-promoted-only').addEventListener('change', renderProjects);
    $('#projects-min-trades').addEventListener('input', renderProjects);
    const minMonthsEl = $('#projects-min-months');
    if (minMonthsEl) minMonthsEl.addEventListener('input', renderProjects);
    const showAllEl = $('#projects-show-all');
    if (showAllEl) showAllEl.addEventListener('change', renderProjects);
  }

  let sub = exps.slice();
  if (famSel.value) sub = sub.filter(e => e.strategy_family === famSel.value);
  if (mktSel.value) sub = sub.filter(e => e.market === mktSel.value);
  if ($('#projects-promoted-only').checked) sub = sub.filter(e => e.promoted);
  const minT = parseInt($('#projects-min-trades').value || 0);
  if (minT > 0) sub = sub.filter(e => (e.total_trades || 0) >= minT);
  const minMonths = parseFloat($('#projects-min-months')?.value || 0);
  if (minMonths > 0) sub = sub.filter(e => (e.duration_months_est || 0) >= minMonths);

  // Sıralama
  const { key, dir } = projectsSort;
  const mul = dir === 'asc' ? 1 : -1;
  sub.sort((a, b) => {
    let va = a[key], vb = b[key];
    if (typeof va === 'boolean') va = va ? 1 : 0;
    if (typeof vb === 'boolean') vb = vb ? 1 : 0;
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * mul;
    return String(va).localeCompare(String(vb)) * mul;
  });

  // KPI
  const avgOr = (key, fn) => {
    const vals = sub.map(e => e[key]).filter(v => v != null);
    return vals.length ? fn(vals) : null;
  };
  const mean = arr => arr.reduce((a,b)=>a+b, 0) / arr.length;
  const sum = arr => arr.reduce((a,b)=>a+b, 0);

  const totalTrades = sum(sub.map(e => e.total_trades || 0));
  const totalWins = sum(sub.map(e => e.winning_trades || 0));
  const totalLosses = sum(sub.map(e => e.losing_trades || 0));
  const totalFees = sum(sub.map(e => e.fees_est_usd || 0));

  $('#projects-kpis').innerHTML = [
    { label: 'Ort. Sharpe', val: fmt(avgOr('sharpe', mean), 2) },
    { label: 'Ort. ROI', val: fmtPct(avgOr('roi', mean)) },
    { label: 'Ort. WR', val: fmtPct(avgOr('win_rate', mean)) },
    { label: 'Toplam trade', val: fmtInt(totalTrades) },
    { label: 'Kazanan / kaybeden', val: `${fmtInt(totalWins)} / ${fmtInt(totalLosses)}` },
    { label: 'Tahmini komisyon $', val: fmtInt(Math.round(totalFees)) },
  ].map(k => `<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-value mono">${k.val}</div></div>`).join('');

  // Table
  $('#projects-table').innerHTML = renderProjectsTable(sub.slice(0, 500));
  // row click handlers
  $$('#projects-table tr[data-idx]').forEach(tr => {
    tr.addEventListener('click', () => openProjectDetail(sub[parseInt(tr.dataset.idx)]));
  });
}

function renderProjectsTable(rows) {
  if (!rows.length) return `<p style="padding:20px;color:var(--text-3)">Henüz deney yok.</p>`;

  const cols = [
    ['zaman',       'timestamp'],
    ['başlık',      'hypothesis_title'],
    ['aile',        'strategy_family'],
    ['market',      'market'],
    ['ROI',         'roi'],
    ['$10k → $',    'final_capital_usd'],
    ['kâr $',       'profit_usd'],
    ['süre (ay)',   'duration_months_est'],
    ['Sharpe',      'sharpe'],
    ['DD',          'max_drawdown'],
    ['CAGR',        'cagr'],
    ['trade',       'total_trades'],
    ['kazanan',     'winning_trades'],
    ['kaybeden',    'losing_trades'],
    ['PF',          'profit_factor'],
    ['promote',     'promoted'],
  ];

  const { key, dir } = projectsSort;
  const arrow = dir === 'asc' ? ' ▲' : ' ▼';
  const head = cols.map(([label, k]) => {
    const active = k === key ? 'sorted' : '';
    return `<th class="sortable ${active}" onclick="toggleSort('${k}')">${label}${k === key ? arrow : ''}</th>`;
  }).join('');

  const body = rows.map((r, i) => `
    <tr data-idx="${i}" data-exp-id="${escapeHtml(r.experiment_id || '')}">
      <td class="mono">${(r.timestamp||'').slice(5,16).replace('T',' ')}</td>
      <td title="${escapeHtml(r.hypothesis_title||'')}">${escapeHtml((r.hypothesis_title||'').slice(0, 50))}</td>
      <td>${escapeHtml(r.strategy_family||'')}</td>
      <td>${escapeHtml(r.market||'')}</td>
      <td class="mono ${cls(r.roi)}">${fmtPct(r.roi)}</td>
      <td class="mono ${cls(r.profit_usd)}">$${fmtInt(r.final_capital_usd)}</td>
      <td class="mono ${cls(r.profit_usd)}">${r.profit_usd >= 0 ? '+' : ''}$${fmtInt(r.profit_usd)}</td>
      <td class="mono">${fmt(r.duration_months_est, 1)}</td>
      <td class="mono ${cls(r.sharpe)}">${fmt(r.sharpe)}</td>
      <td class="mono ${cls(r.max_drawdown)}">${fmtPct(r.max_drawdown)}</td>
      <td class="mono ${cls(r.cagr)}">${fmtPct(r.cagr)}</td>
      <td class="mono">${fmtInt(r.total_trades)}</td>
      <td class="mono pos">${fmtInt(r.winning_trades)}</td>
      <td class="mono neg">${fmtInt(r.losing_trades)}</td>
      <td class="mono">${fmt(r.profit_factor)}</td>
      <td>${r.promoted ? '<span style="color:#4ade80">✓</span>' : ''}</td>
    </tr>
  `).join('');
  return `<table class="t"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function openProjectDetail(r) {
  if (!r) return;
  let data = r;
  let philosophy = null;
  let peers = null;
  let botHistory = null;
  try {
    const [exp, phil, pr] = await Promise.all([
      r.experiment_id ? api(`/api/experiment/${r.experiment_id}`) : Promise.resolve(r),
      api(`/api/strategy-philosophy/${encodeURIComponent(r.strategy_family || 'day')}`),
      r.experiment_id ? api(`/api/experiment/${r.experiment_id}/peers`) : Promise.resolve(null),
    ]);
    if (exp && !exp.error) data = exp;
    philosophy = phil;
    peers = pr;
    // Fetch bot history using bot_id from enriched response
    if (data.bot_id) {
      try {
        const bh = await api(`/api/bot/${data.bot_id}`);
        if (bh && !bh.error) botHistory = bh;
      } catch (e) {}
    }
  } catch (e) { console.error(e); }
  renderProjectModal(data, philosophy, peers, botHistory);
}

function renderProjectModal(r, philosophy = null, peers = null, botHistory = null) {
  const profitClass = (r.profit_usd || 0) >= 0 ? '' : 'neg';
  const profitSign = (r.profit_usd || 0) >= 0 ? '+' : '';
  const months = r.duration_months_est || 0;
  const monthsLabel = months >= 12 ? `${(months/12).toFixed(1)} yıl (${months.toFixed(1)} ay)` : `${months.toFixed(1)} ay`;

  const statBox = (label, value, cssCls = '') =>
    `<div class="stat-box"><div class="sb-label">${label}</div><div class="sb-value ${cssCls}">${value}</div></div>`;

  const wr = (r.win_rate || 0);
  const totalTrades = r.total_trades || 0;
  const winTrades = r.winning_trades || 0;
  const lossTrades = r.losing_trades || 0;

  const html = `
    <div class="modal-backdrop" onclick="if(event.target === this) closeProjectModal()">
      <div class="modal-box">
        <div class="modal-head">
          <div style="flex:1; min-width:0">
            <h2>🤖 ${escapeHtml(r.bot_name || r.hypothesis_title || 'Bot projesi')}</h2>
            <div class="modal-sub">
              ${escapeHtml(r.hypothesis_title || '')}<br>
              <span style="color:var(--text-3)">${escapeHtml(r.strategy_family || '')} ·
              ${escapeHtml(r.market || '')} ·
              rejim: ${escapeHtml(r.regime || '—')} ·
              süre: <b style="color:var(--text-1)">${escapeHtml(r.duration_human || '—')}</b> ·
              ${r.promoted ? '<span style="color:#4ade80">✓ promote</span>' : '<span style="color:#f59e0b">iterate</span>'}</span>
            </div>
          </div>
          <div style="display:flex;gap:8px;align-items:flex-start">
            <button onclick="scheduleRobustness('${escapeHtml(r.experiment_id || '')}')"
                    style="background:rgba(251,191,36,0.14);border:1px solid rgba(251,191,36,0.35);color:#fbbf24;padding:8px 14px;border-radius:8px;cursor:pointer;font-weight:500;font-size:0.82rem">
              🎯 Robustness Suite
            </button>
            <button class="modal-close" onclick="closeProjectModal()">✕</button>
          </div>
        </div>

        <div class="modal-body">

          <!-- USD simulation hero -->
          <div class="usd-hero ${profitClass}">
            <div class="usd-label">$10,000 ile başlansaydı · ${escapeHtml(r.duration_human || monthsLabel)}</div>
            <div class="usd-row">
              <span class="usd-start">$${fmtInt(r.start_capital_usd || 10000)}</span>
              <span class="usd-arrow">→</span>
              <span class="usd-final">$${fmtInt(r.final_capital_usd || 0)}</span>
            </div>
            <div class="usd-meta">
              <span class="chip">kâr ${profitSign}$${fmtInt(r.profit_usd || 0)}</span>
              <span class="chip">ROI ${fmtPct(r.roi)}</span>
              <span class="chip">CAGR ${fmtPct(r.cagr)}</span>
              <span class="chip">${fmt(r.duration_days, 1)} gün / ${fmt(r.duration_hours, 0)} saat</span>
            </div>
          </div>

          ${renderProjectionBlock(r)}

          ${renderBotHistoryBlock(botHistory)}

          <!-- Payoff summary -->
          <div class="payoff-summary">
            <div class="payoff-pill">
              <div class="pp-icon win">✓</div>
              <div>
                <div class="pp-label">Kazanan işlem</div>
                <div class="pp-value pos">${fmtInt(winTrades)} <span style="font-size:0.78rem;color:var(--text-3)">(${fmtPct(wr)})</span></div>
              </div>
            </div>
            <div class="payoff-pill">
              <div class="pp-icon loss">✗</div>
              <div>
                <div class="pp-label">Kaybeden işlem</div>
                <div class="pp-value neg">${fmtInt(lossTrades)} <span style="font-size:0.78rem;color:var(--text-3)">(${fmtPct(1 - wr)})</span></div>
              </div>
            </div>
            <div class="payoff-pill">
              <div class="pp-icon neutral">∑</div>
              <div>
                <div class="pp-label">Toplam işlem</div>
                <div class="pp-value">${fmtInt(totalTrades)}</div>
              </div>
            </div>
          </div>

          <!-- Charts row 1 -->
          <div class="modal-section-title">📈 Simüle edilmiş sermaye eğrisi + işlem dağılımı</div>
          <div class="chart-grid">
            <div class="chart-card">
              <h4>$10,000 → $${fmtInt(r.final_capital_usd || 0)} · sentetik yol</h4>
              <div class="mini-chart tall"><canvas id="modal-chart-equity"></canvas></div>
            </div>
            <div class="chart-card">
              <h4>Kazanan vs Kaybeden</h4>
              <div class="mini-chart tall"><canvas id="modal-chart-donut"></canvas></div>
            </div>
          </div>

          <!-- Charts row 2 -->
          <div class="modal-section-title">📊 İşlem başına karşılaştırma</div>
          <div class="chart-grid-3">
            <div class="chart-card">
              <h4>Ortalama (%)</h4>
              <div class="mini-chart"><canvas id="modal-chart-avg"></canvas></div>
            </div>
            <div class="chart-card">
              <h4>Ekstrem (%)</h4>
              <div class="mini-chart"><canvas id="modal-chart-extreme"></canvas></div>
            </div>
            <div class="chart-card">
              <h4>Max ardışık</h4>
              <div class="mini-chart"><canvas id="modal-chart-streak"></canvas></div>
            </div>
          </div>

          <!-- Risk-adjusted radar -->
          <div class="modal-section-title">🎯 Risk-adjusted performans</div>
          <div class="chart-grid">
            <div class="chart-card">
              <h4>Radar skoru (0-100 normalize)</h4>
              <div class="mini-chart tall"><canvas id="modal-chart-radar"></canvas></div>
            </div>
            <div class="chart-card" style="display:flex;align-items:center;">
              <div style="width:100%">
                ${statBox('Sharpe', fmt(r.sharpe), cls(r.sharpe))}
                ${statBox('Sortino', fmt(r.sortino), cls(r.sortino))}
                ${statBox('Calmar', fmt(r.calmar), cls(r.calmar))}
                ${statBox('Profit Factor', fmt(r.profit_factor))}
                ${statBox('Max DD', fmtPct(r.max_drawdown), cls(r.max_drawdown))}
                ${statBox('Stability', fmt(r.stability_score))}
              </div>
            </div>
          </div>

          <!-- Trade breakdown -->
          <div class="modal-section-title">📋 Detaylı istatistikler</div>
          <div class="stat-grid">
            ${statBox('Win rate', fmtPct(wr))}
            ${statBox('Beklenti/işlem', fmt(r.expectancy, 4))}
            ${(() => {
              // Avg trade duration in days
              const tfMin = {'1m':1,'5m':5,'15m':15,'30m':30,'1h':60,'4h':240,'1d':1440}[r.bar_timeframe] || 60;
              const bars = r.avg_trade_duration || 0;
              const tdMins = bars * tfMin;
              const tdDays = tdMins / (60 * 24);
              const txt = tdDays < 1 ? `${(tdDays * 24).toFixed(1)} saat` : `${tdDays.toFixed(1)} gün`;
              return statBox('Ort. pozisyon süresi', txt);
            })()}
            ${statBox('En yüksek kazanç', fmtPct(r.largest_win_pct), 'pos')}
            ${statBox('En yüklü kayıp', fmtPct(r.largest_loss_pct), 'neg')}
            ${statBox('Ort. kazanan', fmtPct(r.avg_win_pct), 'pos')}
            ${statBox('Ort. kaybeden', fmtPct(r.avg_loss_pct), 'neg')}
            ${statBox('Max ardışık kazanç', fmtInt(r.max_consecutive_wins), 'pos')}
            ${statBox('Max ardışık kayıp', fmtInt(r.max_consecutive_losses), 'neg')}
          </div>

          <!-- USD estimates (per-trade, based on $600 notional) -->
          <div class="modal-section-title">💵 USD tahmini · işlem başına $600 notional</div>
          <div class="stat-grid">
            ${statBox('En yüksek kazanç $', `$${fmt(r.largest_win_usd_est, 2)}`, 'pos')}
            ${statBox('En yüklü kayıp $', `$${fmt(r.largest_loss_usd_est, 2)}`, 'neg')}
            ${statBox('Ort. kazanan $', `$${fmt(r.avg_win_usd_est, 2)}`, 'pos')}
            ${statBox('Ort. kaybeden $', `$${fmt(r.avg_loss_usd_est, 2)}`, 'neg')}
            ${statBox('Toplam komisyon ≈', `$${fmtInt(r.fees_est_usd)}`)}
            ${statBox('Gross profit', fmtPct(r.gross_profit_pct), 'pos')}
          </div>

          ${renderPhilosophyBlock(philosophy, r)}

          ${renderTopTradesBlock(r)}

          ${renderExitReasonsBlock(r)}

          ${renderPeerBlock(peers)}

          ${renderCeoAdvice(r, philosophy, peers)}

          <!-- Meta -->
          <div class="modal-section-title">🧭 Meta</div>
          <div class="stat-grid">
            ${statBox('Sembol', escapeHtml(r.symbol || '—'))}
            ${statBox('Long / Short', `${fmtInt(r.long_trades)} / ${fmtInt(r.short_trades)}`)}
            ${statBox('Avg kaldıraç', fmt(r.avg_leverage) + 'x')}
            ${statBox('Max kaldıraç', fmt(r.max_leverage_used) + 'x')}
            ${statBox('Backtest bar', fmtInt(r.duration_bars))}
            ${statBox('Timeframe', escapeHtml(r.bar_timeframe || '—'))}
            ${statBox('Rejim', escapeHtml(r.regime || '—'))}
            ${statBox('WF Sharpe', r.walkforward_sharpe != null ? fmt(r.walkforward_sharpe) : '—')}
            ${statBox('MC 95% DD', r.montecarlo_95_drawdown != null ? fmtPct(r.montecarlo_95_drawdown) : '—')}
            ${statBox('Experiment ID', `<span style="font-size:0.72rem">${escapeHtml((r.experiment_id||'').slice(0,8))}…</span>`)}
          </div>

          <div class="modal-section-title">📝 Notlar</div>
          <pre class="terminal" style="height:140px">${escapeHtml(r.notes || '(not yok)')}</pre>

        </div>
      </div>
    </div>
  `;

  closeProjectModal();
  const wrap = document.createElement('div');
  wrap.id = 'project-modal-wrap';
  wrap.innerHTML = html;
  document.body.appendChild(wrap);
  document.body.style.overflow = 'hidden';

  // Initialize charts after modal is in DOM
  setTimeout(() => initModalCharts(r), 40);
}

// ---------- Modal sub-blocks ----------

function renderProjectionBlock(r) {
  const proj = r.projection;
  if (!proj) return '';
  const cagr = r.cagr || 0;
  const cagrClass = cagr >= 0 ? 'pos' : 'neg';
  return `
    <div class="modal-section-title">📅 12 / 24 / 60 ay projeksiyon (CAGR ${fmtPct(cagr)} baz alınarak)</div>
    <div class="stat-grid">
      <div class="stat-box"><div class="sb-label">1 ay</div><div class="sb-value mono ${cagrClass}">$${fmtInt(proj.m1)}</div></div>
      <div class="stat-box"><div class="sb-label">3 ay</div><div class="sb-value mono ${cagrClass}">$${fmtInt(proj.m3)}</div></div>
      <div class="stat-box"><div class="sb-label">6 ay</div><div class="sb-value mono ${cagrClass}">$${fmtInt(proj.m6)}</div></div>
      <div class="stat-box"><div class="sb-label">12 ay</div><div class="sb-value mono ${cagrClass}">$${fmtInt(proj.m12)}</div></div>
      <div class="stat-box"><div class="sb-label">24 ay</div><div class="sb-value mono ${cagrClass}">$${fmtInt(proj.m24)}</div></div>
      <div class="stat-box"><div class="sb-label">60 ay</div><div class="sb-value mono ${cagrClass}">$${fmtInt(proj.m60)}</div></div>
    </div>
    <p style="color:var(--text-3);font-size:0.78rem;margin-top:4px">
      ⚠️ Projeksiyon: CAGR'ın değişmediği varsayımıyla ekstrapolasyon. Kısa pencerede (1-3 ay) hesaplanan CAGR 12 ay boyunca tutmayabilir — sadece gösterge.
    </p>
  `;
}

function renderBotHistoryBlock(bh) {
  if (!bh || bh.error) return '';

  const symRows = (bh.by_symbol || []).map(s => `
    <tr>
      <td>${escapeHtml(s.key || '—')}</td>
      <td class="mono">${fmtInt(s.runs)}</td>
      <td class="mono ${cls(s.avg_sharpe)}">${fmt(s.avg_sharpe)}</td>
      <td class="mono ${cls(s.avg_roi)}">${fmtPct(s.avg_roi)}</td>
      <td class="mono ${cls(s.total_profit_usd)}">${s.total_profit_usd >= 0 ? '+' : ''}$${fmtInt(s.total_profit_usd)}</td>
    </tr>
  `).join('');

  const tfRows = (bh.by_timeframe || []).map(s => `
    <tr>
      <td>${escapeHtml(s.key || '—')}</td>
      <td class="mono">${fmtInt(s.runs)}</td>
      <td class="mono ${cls(s.avg_sharpe)}">${fmt(s.avg_sharpe)}</td>
      <td class="mono ${cls(s.avg_roi)}">${fmtPct(s.avg_roi)}</td>
      <td class="mono ${cls(s.total_profit_usd)}">${s.total_profit_usd >= 0 ? '+' : ''}$${fmtInt(s.total_profit_usd)}</td>
    </tr>
  `).join('');

  const recentRows = (bh.recent_runs || []).map(r => `
    <tr>
      <td class="mono">${(r.timestamp || '').slice(5,16).replace('T',' ')}</td>
      <td>${escapeHtml(r.symbol || '—')}</td>
      <td>${escapeHtml(r.timeframe || '—')}</td>
      <td>${escapeHtml(r.regime || '—')}</td>
      <td class="mono ${cls(r.sharpe)}">${fmt(r.sharpe)}</td>
      <td class="mono ${cls(r.roi)}">${fmtPct(r.roi)}</td>
      <td class="mono ${cls(r.profit_usd)}">${r.profit_usd >= 0 ? '+' : ''}$${fmtInt(r.profit_usd)}</td>
      <td>${escapeHtml(r.duration_human || '')}</td>
      <td>${r.promoted ? '<span style="color:#4ade80">✓</span>' : ''}</td>
    </tr>
  `).join('');

  return `
    <div class="modal-section-title">📚 ${escapeHtml(bh.bot_name || '')} — toplam geçmiş</div>
    <div class="payoff-summary">
      <div class="payoff-pill">
        <div class="pp-icon neutral">∑</div>
        <div>
          <div class="pp-label">Toplam run</div>
          <div class="pp-value">${fmtInt(bh.total_runs)}</div>
        </div>
      </div>
      <div class="payoff-pill">
        <div class="pp-icon ${bh.total_profit_usd >= 0 ? 'win' : 'loss'}">$</div>
        <div>
          <div class="pp-label">Toplam kâr/zarar (simüle)</div>
          <div class="pp-value ${bh.total_profit_usd >= 0 ? 'pos' : 'neg'}">${bh.total_profit_usd >= 0 ? '+' : ''}$${fmtInt(bh.total_profit_usd)}</div>
        </div>
      </div>
      <div class="payoff-pill">
        <div class="pp-icon neutral">⏱️</div>
        <div>
          <div class="pp-label">Toplam simüle süre</div>
          <div class="pp-value">${fmt(bh.total_simulated_months, 1)} ay</div>
        </div>
      </div>
    </div>

    <div class="row-2">
      <div>
        <h4 style="color:var(--text-2);font-size:0.88rem;margin-bottom:8px">📊 Sembol bazında</h4>
        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;overflow:hidden">
          <table class="trades-mini">
            <thead><tr><th>Sembol</th><th>run</th><th>avg Sharpe</th><th>avg ROI</th><th>toplam $</th></tr></thead>
            <tbody>${symRows}</tbody>
          </table>
        </div>
      </div>
      <div>
        <h4 style="color:var(--text-2);font-size:0.88rem;margin-bottom:8px">⏰ Timeframe bazında</h4>
        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;overflow:hidden">
          <table class="trades-mini">
            <thead><tr><th>TF</th><th>run</th><th>avg Sharpe</th><th>avg ROI</th><th>toplam $</th></tr></thead>
            <tbody>${tfRows}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div style="margin-top:14px">
      <h4 style="color:var(--text-2);font-size:0.88rem;margin-bottom:8px">🕐 Son 50 run</h4>
      <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;overflow:hidden;max-height:320px;overflow-y:auto">
        <table class="trades-mini">
          <thead><tr>
            <th>zaman</th><th>sembol</th><th>TF</th><th>rejim</th>
            <th>Sharpe</th><th>ROI</th><th>P/L $</th><th>süre</th><th>prom</th>
          </tr></thead>
          <tbody>${recentRows}</tbody>
        </table>
      </div>
    </div>

    <p style="color:var(--text-3);font-size:0.80rem;margin-top:8px">
      En iyi run: <b style="color:#4ade80">${escapeHtml(bh.best_run.symbol || '—')}/${escapeHtml(bh.best_run.timeframe || '—')}</b>
      (Sharpe ${fmt(bh.best_run.sharpe)}, ${fmtPct(bh.best_run.roi)}, ${escapeHtml(bh.best_run.duration_human || '')}) ·
      En kötü: <b style="color:#ef7e7e">${escapeHtml(bh.worst_run.symbol || '—')}/${escapeHtml(bh.worst_run.timeframe || '—')}</b>
      (Sharpe ${fmt(bh.worst_run.sharpe)}, ${fmtPct(bh.worst_run.roi)})
    </p>
  `;
}

async function scheduleRobustness(experimentId) {
  if (!experimentId) return;
  try {
    const r = await fetch(`/api/robustness/schedule?experiment_id=${encodeURIComponent(experimentId)}`, { method: 'POST' })
      .then(x => x.json());
    if (r.error) {
      alert('Hata: ' + r.error);
    } else {
      alert(`${r.scheduled} varyant test kuyruğuna alındı.\n\n` + (r.tests || []).map(t => `${t.symbol} / ${t.timeframe}`).join('\n'));
    }
  } catch (e) {
    alert('Hata: ' + e.message);
  }
}
window.scheduleRobustness = scheduleRobustness;

function renderPhilosophyBlock(phil, r) {
  if (!phil || !phil.title) return '';
  const favBadges = (phil.favorable_regimes || []).map(rg => `<span class="badge-regime fav">${escapeHtml(rg)}</span>`).join('');
  const killBadges = (phil.killer_regimes || []).map(rg => `<span class="badge-regime kill">${escapeHtml(rg)}</span>`).join('');
  const entryLi = (phil.entry_signals || []).map(s => `<li>${escapeHtml(s)}</li>`).join('');
  const exitLi = (phil.exit_signals || []).map(s => `<li>${escapeHtml(s)}</li>`).join('');

  // Show actual params used
  const params = r.strategy_params || {};
  const paramRows = Object.entries(params).map(([k, v]) =>
    `<tr><td style="color:var(--text-3);padding:4px 8px;font-size:0.78rem">${escapeHtml(k)}</td>
         <td class="mono" style="padding:4px 8px;font-size:0.78rem">${escapeHtml(String(v))}</td></tr>`
  ).join('');

  return `
    <div class="modal-section-title">🧠 Strateji felsefesi</div>
    <div class="philosophy-card">
      <div class="ph-title">${escapeHtml(phil.title)}</div>
      <div class="ph-tagline">${escapeHtml(phil.tagline || '')}</div>
      <div class="ph-how">${escapeHtml(phil.how_it_works || '')}</div>
      <div class="ph-row">
        <div class="ph-key">🟢 Favor rejim</div>
        <div class="ph-val">${favBadges || '—'}</div>
      </div>
      <div class="ph-row">
        <div class="ph-key">🔴 Öldürücü rejim</div>
        <div class="ph-val">${killBadges || '—'}</div>
      </div>
      <div class="ph-row">
        <div class="ph-key">⏱️ Tipik holding</div>
        <div class="ph-val">${escapeHtml(phil.typical_holding || '—')}</div>
      </div>
      <div class="ph-row">
        <div class="ph-key">🔼 Giriş sinyalleri</div>
        <div class="ph-val"><ul>${entryLi}</ul></div>
      </div>
      <div class="ph-row">
        <div class="ph-key">🔽 Çıkış sinyalleri</div>
        <div class="ph-val"><ul>${exitLi}</ul></div>
      </div>
      ${paramRows ? `
      <div class="ph-row">
        <div class="ph-key">⚙️ Bu deneyde kullanılan parametreler</div>
        <div class="ph-val"><table style="width:100%"><tbody>${paramRows}</tbody></table></div>
      </div>` : ''}
    </div>
  `;
}

function renderTopTradesBlock(r) {
  const pnl = r.top_trades_by_pnl || [];
  const lev = r.top_trades_by_leverage || [];
  if (!pnl.length && !lev.length) return '';

  const tradeRow = (t, i) => `
    <tr>
      <td>${i + 1}</td>
      <td class="dir-${t.direction}">${t.direction === 'long' ? '⬆ LONG' : '⬇ SHORT'}</td>
      <td>${escapeHtml(t.symbol || '')}</td>
      <td>${fmt(t.entry_price, 4)}</td>
      <td>${fmt(t.exit_price, 4)}</td>
      <td class="${t.pnl > 0 ? 'pos' : 'neg'}">${t.pnl > 0 ? '+' : ''}${fmt(t.pnl, 2)}</td>
      <td class="${t.return_pct > 0 ? 'pos' : 'neg'}">${fmtPct(t.return_pct)}</td>
      <td><span class="lev">${fmt(t.leverage, 1)}x</span></td>
      <td>${fmtInt(t.duration_bars)} bar</td>
      <td><span class="reason">${escapeHtml(t.exit_reason)}</span></td>
    </tr>
  `;

  return `
    <div class="modal-section-title">🏆 En kazançlı 5 işlem</div>
    <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;overflow:hidden">
      <table class="trades-mini">
        <thead><tr>
          <th>#</th><th>yön</th><th>sembol</th><th>giriş</th><th>çıkış</th>
          <th>PnL $</th><th>getiri</th><th>lev</th><th>süre</th><th>çıkış nedeni</th>
        </tr></thead>
        <tbody>${pnl.length ? pnl.map(tradeRow).join('') : '<tr><td colspan="10" style="text-align:center;color:var(--text-3)">trade verisi yok</td></tr>'}</tbody>
      </table>
    </div>

    <div class="modal-section-title">⚡ En yüksek kaldıraçlı 5 işlem</div>
    <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;overflow:hidden">
      <table class="trades-mini">
        <thead><tr>
          <th>#</th><th>yön</th><th>sembol</th><th>giriş</th><th>çıkış</th>
          <th>PnL $</th><th>getiri</th><th>lev</th><th>süre</th><th>çıkış nedeni</th>
        </tr></thead>
        <tbody>${lev.length ? lev.map(tradeRow).join('') : '<tr><td colspan="10" style="text-align:center;color:var(--text-3)">trade verisi yok</td></tr>'}</tbody>
      </table>
    </div>
  `;
}

function renderExitReasonsBlock(r) {
  const counts = r.exit_reason_counts || {};
  const entries = Object.entries(counts);
  if (!entries.length) return '';

  const total = entries.reduce((s, [_, v]) => s + v, 0) || 1;
  const pills = entries
    .sort((a,b) => b[1] - a[1])
    .map(([reason, cnt]) => {
      const pct = (cnt / total) * 100;
      return `
        <div class="payoff-pill" style="padding:10px 14px">
          <div class="pp-icon neutral" style="width:32px;height:32px;font-size:14px">${fmtInt(cnt)}</div>
          <div>
            <div class="pp-label">${escapeHtml(reason)}</div>
            <div class="pp-value" style="font-size:0.95rem">${pct.toFixed(1)}%</div>
          </div>
        </div>
      `;
    }).join('');

  return `
    <div class="modal-section-title">🚪 Çıkış nedeni dağılımı</div>
    <div class="payoff-summary" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">${pills}</div>
  `;
}

function renderPeerBlock(peers) {
  if (!peers || peers.error) return '';

  const topRows = (peers.family_top5 || []).map((p, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${escapeHtml((p.title || '').slice(0, 40))}</td>
      <td>${escapeHtml(p.symbol || '')}</td>
      <td class="${(p.sharpe || 0) > 0 ? 'pos' : 'neg'}">${fmt(p.sharpe)}</td>
      <td class="${(p.roi || 0) > 0 ? 'pos' : 'neg'}">${fmtPct(p.roi)}</td>
    </tr>
  `).join('');

  const sharpeVs = peers.target_sharpe != null && peers.family_avg_sharpe != null
    ? (peers.target_sharpe - peers.family_avg_sharpe).toFixed(2) : '—';
  const sharpeVsCls = parseFloat(sharpeVs) > 0 ? 'pos' : (parseFloat(sharpeVs) < 0 ? 'neg' : '');

  return `
    <div class="modal-section-title">🏅 Peer karşılaştırması · ${escapeHtml(peers.strategy_family)} ailesi</div>
    <div class="peer-box">
      <div class="peer-header">
        <div>
          <span class="rank">#${peers.rank_in_family}</span>
          <span class="rank-sub">/ ${peers.total_in_family}</span>
        </div>
        <div style="text-align:right">
          <div class="percentile">Top <b style="color:var(--text-1)">${peers.percentile}%</b></div>
          <div class="percentile">Sharpe avg (aile): <b class="mono">${fmt(peers.family_avg_sharpe)}</b></div>
          <div class="percentile">Bu strateji avg'ın: <b class="mono ${sharpeVsCls}">${parseFloat(sharpeVs) > 0 ? '+' : ''}${sharpeVs}</b></div>
        </div>
      </div>
      <table class="trades-mini" style="width:100%">
        <thead><tr><th>#</th><th>aile top-5 başlık</th><th>sembol</th><th>Sharpe</th><th>ROI</th></tr></thead>
        <tbody>${topRows || '<tr><td colspan="5" style="color:var(--text-3)">veri yok</td></tr>'}</tbody>
      </table>
    </div>
  `;
}

function renderCeoAdvice(r, phil, peers) {
  // CEO-level generative recommendations based on metrics
  const recs = [];

  // Short-horizon warning (LONG-HORIZON BIAS)
  const months = r.duration_months_est || 0;
  if (months < 2) {
    recs.push(`<b>🕐 Kısa pencere uyarısı</b>: bu bot sadece <b>${escapeHtml(r.duration_human || '?')}</b> çalışmış. Kısa pencerede ${fmtPct(r.cagr)} CAGR görüntüsü yanıltıcı olabilir — 12 ay boyunca aynı performans garantisi yok. En az 180 gün backtest gerekli.`);
  } else if (months < 6) {
    recs.push(`<b>⏱️ Orta pencere</b>: ${escapeHtml(r.duration_human || '?')} backtest. 12+ ay veriyle doğrula. Mevcut CAGR ${fmtPct(r.cagr)} → 12 aylık ekstrapolasyon: $${fmtInt((r.projection || {}).m12 || 0)}.`);
  }

  // Sample size
  if ((r.total_trades || 0) < 100) {
    recs.push(`<b>Örneklem küçük</b> (${fmtInt(r.total_trades)} trade). Daha uzun veri penceresinde tekrar test et — mevcut sonuçlar anlamlı değil.`);
  }

  // Regime mismatch
  if (phil && phil.killer_regimes && phil.killer_regimes.includes(r.regime)) {
    recs.push(`<b>Rejim uyumsuz</b>: bu strateji ${r.regime} rejiminde kötü — veriyi ${phil.favorable_regimes.join('/')} rejim koşullarına filtrele.`);
  }

  // Low edge
  if ((r.profit_factor || 0) < 1.2 && (r.total_trades || 0) >= 30) {
    recs.push(`<b>Marjinal edge</b> (PF ${fmt(r.profit_factor)}). Fee + slippage + funding sonrası canlı ortamda erir — skip.`);
  }

  // Leverage warning
  if ((r.max_leverage_used || 0) >= 4) {
    recs.push(`<b>Yüksek kaldıraç</b> (${fmt(r.max_leverage_used)}x max). Tail-event durumunda kritik zarar riski — Apex onayı şart.`);
  }

  // Stability
  if ((r.stability_score || 0) < 0.5) {
    recs.push(`<b>Düşük stabilite</b>. Parametre duyarlılığı yüksek — ±10% parametre değiştirince performans çökebilir.`);
  }

  // Strong candidate
  if ((r.sharpe || 0) >= 1.5 && (r.total_trades || 0) >= 100 && (r.max_drawdown || 0) >= -0.12) {
    recs.push(`<b>Güçlü aday</b>: Sharpe ${fmt(r.sharpe)}, ${fmtInt(r.total_trades)} trade, DD ${fmtPct(r.max_drawdown)}. Stres lab + walk-forward sonrası paper trading pod'u için uygun.`);
  }

  // Peer rank
  if (peers && peers.rank_in_family && peers.total_in_family > 20) {
    if (peers.percentile >= 90) {
      recs.push(`<b>Ailede ilk %10</b> (rank #${peers.rank_in_family}/${peers.total_in_family}). Bu parametre alanını detaylı incele.`);
    } else if (peers.percentile < 40) {
      recs.push(`<b>Ailede ortalamanın altı</b> (rank #${peers.rank_in_family}/${peers.total_in_family}). Bu yönde daha fazla deney önerme.`);
    }
  }

  // Live caveat
  recs.push(`<b>Backtest caveat</b>: bu sonuç tarihsel veriden. Canlı piyasada slippage + latency + market impact fark yaratır. Paper trading 30+ gün zorunlu.`);

  if (!recs.length) return '';

  const liHtml = recs.map(t => `<li>${t}</li>`).join('');
  return `
    <div class="ceo-advice">
      <div class="ceo-label">👑 Atlas CEO değerlendirmesi</div>
      <div class="ceo-body">
        <ul>${liHtml}</ul>
      </div>
    </div>
  `;
}

// ---------- Modal charts ----------

const modalCharts = {};

function destroyModalCharts() {
  Object.values(modalCharts).forEach(c => { try { c.destroy(); } catch (e) {} });
  Object.keys(modalCharts).forEach(k => delete modalCharts[k]);
}

function initModalCharts(r) {
  destroyModalCharts();

  const baseOpts = () => ({
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: '#b7bfcd', font: { family: 'Inter', size: 11 } } },
      tooltip: { backgroundColor: '#1c212d', borderColor: '#2f3749', borderWidth: 1, titleColor: '#eef1f6', bodyColor: '#b7bfcd' },
    },
  });
  const axisColor = '#5b6478';
  const gridColor = '#1f2330';

  // Chart 1: Synthetic equity curve
  // Generate a plausible path from win_rate / avg_win / avg_loss / total_trades
  const tradesN = Math.max(r.total_trades || 20, 1);
  const wr = r.win_rate || 0;
  const avgW = r.avg_win_pct || 0.01;
  const avgL = r.avg_loss_pct || -0.01;
  const startUsd = r.start_capital_usd || 10000;
  const notional = 600; // $600 per trade
  // Seeded LCG for reproducibility
  let seed = 12345;
  for (const c of (r.experiment_id || '')) seed = (seed * 31 + c.charCodeAt(0)) & 0xffffffff;
  const rand = () => { seed = (seed * 1664525 + 1013904223) & 0xffffffff; return (seed >>> 0) / 4294967296; };

  const equityPath = [startUsd];
  let cap = startUsd;
  for (let i = 0; i < Math.min(tradesN, 500); i++) {
    const isWin = rand() < wr;
    // Jitter around avg win/loss
    const magnitude = (isWin ? avgW : avgL) * (0.6 + rand() * 0.8);
    cap = cap + (notional * magnitude);
    equityPath.push(Math.max(cap, 0));
  }
  // Scale end to match true final_capital_usd
  const scale = (r.final_capital_usd || startUsd) / (equityPath[equityPath.length - 1] || 1);
  const equityScaled = equityPath.map(v => startUsd + (v - startUsd) * scale);

  modalCharts.equity = new Chart(document.getElementById('modal-chart-equity').getContext('2d'), {
    type: 'line',
    data: {
      labels: equityScaled.map((_, i) => i),
      datasets: [{
        label: 'Sermaye ($)',
        data: equityScaled,
        borderColor: (r.profit_usd || 0) >= 0 ? '#4ade80' : '#ef7e7e',
        backgroundColor: (r.profit_usd || 0) >= 0 ? 'rgba(74,222,128,0.12)' : 'rgba(239,126,126,0.12)',
        fill: true, tension: 0.18, pointRadius: 0, borderWidth: 2,
      }],
    },
    options: {
      ...baseOpts(),
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: axisColor, maxTicksLimit: 8 }, title: { display: true, text: 'işlem sırası', color: '#7a8498', font: { size: 10 } } },
        y: { grid: { color: gridColor }, ticks: { color: axisColor, callback: v => '$' + Math.round(v).toLocaleString() } },
      },
    },
  });

  // Chart 2: Donut
  modalCharts.donut = new Chart(document.getElementById('modal-chart-donut').getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: ['Kazanan', 'Kaybeden'],
      datasets: [{
        data: [r.winning_trades || 0, r.losing_trades || 0],
        backgroundColor: ['#22c55e', '#ef4444'],
        borderColor: '#12161e', borderWidth: 3,
      }],
    },
    options: {
      ...baseOpts(),
      cutout: '62%',
      plugins: {
        ...baseOpts().plugins,
        legend: { position: 'bottom', labels: { color: '#b7bfcd', font: { family: 'Inter', size: 11 } } },
      },
    },
  });

  // Chart 3: Avg pct
  modalCharts.avg = new Chart(document.getElementById('modal-chart-avg').getContext('2d'), {
    type: 'bar',
    data: {
      labels: ['Ort. kazanan', 'Ort. kaybeden'],
      datasets: [{
        data: [(r.avg_win_pct || 0) * 100, (r.avg_loss_pct || 0) * 100],
        backgroundColor: ['#22c55e', '#ef4444'], borderWidth: 0, borderRadius: 4,
      }],
    },
    options: {
      ...baseOpts(),
      plugins: { ...baseOpts().plugins, legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: axisColor } },
        y: { grid: { color: gridColor }, ticks: { color: axisColor, callback: v => v.toFixed(1) + '%' } },
      },
    },
  });

  // Chart 4: Extreme pct
  modalCharts.extreme = new Chart(document.getElementById('modal-chart-extreme').getContext('2d'), {
    type: 'bar',
    data: {
      labels: ['En yüksek kazanç', 'En yüklü kayıp'],
      datasets: [{
        data: [(r.largest_win_pct || 0) * 100, (r.largest_loss_pct || 0) * 100],
        backgroundColor: ['#22c55e', '#ef4444'], borderWidth: 0, borderRadius: 4,
      }],
    },
    options: {
      ...baseOpts(),
      plugins: { ...baseOpts().plugins, legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: axisColor } },
        y: { grid: { color: gridColor }, ticks: { color: axisColor, callback: v => v.toFixed(1) + '%' } },
      },
    },
  });

  // Chart 5: Streaks
  modalCharts.streak = new Chart(document.getElementById('modal-chart-streak').getContext('2d'), {
    type: 'bar',
    data: {
      labels: ['Kazanç', 'Kayıp'],
      datasets: [{
        data: [r.max_consecutive_wins || 0, r.max_consecutive_losses || 0],
        backgroundColor: ['#22c55e', '#ef4444'], borderWidth: 0, borderRadius: 4,
      }],
    },
    options: {
      ...baseOpts(),
      plugins: { ...baseOpts().plugins, legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: axisColor } },
        y: { grid: { color: gridColor }, ticks: { color: axisColor, stepSize: 1 }, beginAtZero: true },
      },
    },
  });

  // Chart 6: Radar (risk-adjusted)
  // Normalize metrics to 0-100 for display
  const norm = (v, minV, maxV) => Math.max(0, Math.min(100, ((v - minV) / (maxV - minV)) * 100));
  const radarData = [
    norm(r.sharpe || 0, -3, 4),
    norm(r.sortino || 0, -3, 5),
    norm(r.calmar || 0, -2, 5),
    norm(r.profit_factor || 0, 0, 3),
    norm(-(r.max_drawdown || 0), 0, 0.3),  // inverse so higher = better
    norm(r.stability_score || 0, 0, 1),
  ];
  modalCharts.radar = new Chart(document.getElementById('modal-chart-radar').getContext('2d'), {
    type: 'radar',
    data: {
      labels: ['Sharpe', 'Sortino', 'Calmar', 'PF', 'DD⁻¹', 'Stability'],
      datasets: [{
        label: 'Skor',
        data: radarData,
        backgroundColor: 'rgba(129,140,248,0.18)',
        borderColor: '#818cf8',
        pointBackgroundColor: '#818cf8',
        borderWidth: 2,
      }],
    },
    options: {
      ...baseOpts(),
      plugins: { ...baseOpts().plugins, legend: { display: false } },
      scales: {
        r: {
          angleLines: { color: '#2c3447' },
          grid: { color: '#242b3c' },
          pointLabels: { color: '#cdd6e0', font: { family: 'Inter', size: 11 } },
          ticks: { display: false, stepSize: 20 },
          suggestedMin: 0, suggestedMax: 100,
        },
      },
    },
  });
}

function closeProjectModal() {
  const existing = document.getElementById('project-modal-wrap');
  if (existing) existing.remove();
  document.body.style.overflow = '';
}
window.closeProjectModal = closeProjectModal;

// Hide old side-card detail completely
function closeProjectDetail() {
  const el = $('#project-detail');
  if (el) el.style.display = 'none';
}
window.closeProjectDetail = closeProjectDetail;

// ---------- Goals ----------

async function renderGoals() {
  const g = cache.goals || await api('/api/goals');
  cache.goals = g;

  const winPct = Math.min(100, (g.winners / g.target_winners) * 100);
  $('#goal-winners-bar').style.width = winPct + '%';
  $('#goal-winners-caption').textContent = `${g.winners} / ${g.target_winners} kazanan bulundu`;

  const cagrPct = Math.min(100, (g.best_cagr / g.target_cagr) * 100) || 0;
  $('#goal-cagr-bar').style.width = cagrPct + '%';
  $('#goal-cagr-caption').textContent = `${fmtPct(g.best_cagr)} / ${fmtPct(g.target_cagr)} hedef`;

  const sharpePct = Math.min(100, (g.best_sharpe / g.target_sharpe) * 100) || 0;
  $('#goal-sharpe-bar').style.width = sharpePct + '%';
  $('#goal-sharpe-caption').textContent = `${fmt(g.best_sharpe)} / ${fmt(g.target_sharpe)} hedef`;

  const ddOk = g.best_dd >= g.target_dd;
  $('#goal-dd-bar').style.width = (ddOk ? 100 : 50) + '%';
  $('#goal-dd-caption').textContent = `${fmtPct(g.best_dd)} / ${fmtPct(g.target_dd)} sınır`;

  // Winners table
  const wins = await api('/api/winners');
  if (!wins.length) {
    $('#winners-table').innerHTML = `<p style="color:var(--text-3)">Henüz kazanan yok. Otonom loop arıyor.</p>`;
  } else {
    const keys = Object.keys(wins[0]);
    $('#winners-table').innerHTML = `
      <div class="table-wrap"><table class="t">
        <thead><tr>${keys.map(k => `<th>${escapeHtml(k)}</th>`).join('')}</tr></thead>
        <tbody>${wins.map(w => `<tr>${keys.map(k => `<td class="mono">${escapeHtml(String(w[k]))}</td>`).join('')}</tr>`).join('')}</tbody>
      </table></div>
    `;
  }
}

// ---------- Pods ----------

async function renderPods() {
  const pods = await api('/api/pods');

  const counts = { active: 0, halved: 0, retired: 0 };
  pods.forEach(p => {
    if (counts[p.status] !== undefined) counts[p.status]++;
  });

  $('#pods-kpis').innerHTML = [
    { label: 'Aktif', val: counts.active },
    { label: "Halve'lenmiş", val: counts.halved },
    { label: 'Emekli', val: counts.retired },
  ].map(k => `<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-value mono">${k.val}</div></div>`).join('');

  if (!pods.length) {
    $('#pods-table').innerHTML = `<p style="padding:20px;color:var(--text-3)">Henüz pod yok. Bir strateji promote edilince açılacak.</p>`;
    return;
  }

  const cols = [
    ['strategy_family','aile'], ['market','market'], ['allocated_capital','tahsis $'],
    ['current_capital','mevcut $'], ['peak_capital','zirve $'], ['drawdown_pct','DD %'],
    ['sharpe_30d','Sharpe 30g'], ['trades_30d','trade 30g'], ['win_rate_30d','WR 30g'],
    ['correlation_to_book','korelasyon'], ['status','durum'],
  ];
  const head = cols.map(([_,lbl]) => `<th>${lbl}</th>`).join('');
  const body = pods.map(p => `<tr>${cols.map(([k]) => {
    let v = p[k];
    if (k === 'drawdown_pct' || k === 'win_rate_30d') return `<td class="mono ${cls(v)}">${fmtPct(v)}</td>`;
    if (typeof v === 'number') return `<td class="mono">${fmt(v)}</td>`;
    return `<td>${escapeHtml(String(v ?? ''))}</td>`;
  }).join('')}</tr>`).join('');
  $('#pods-table').innerHTML = `<table class="t"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// ---------- Live ----------

async function renderLive() {
  const [log, act, status, resources] = await Promise.all([
    api('/api/log?lines=120'),
    api('/api/activity'),
    api('/api/agent-status'),
    api('/api/resources'),
  ]);
  const box = $('#log-box');
  box.textContent = log.text || '';
  box.scrollTop = box.scrollHeight;

  // Resource / token panel
  const mb = (resources.db_file_size_bytes / 1048576).toFixed(2);
  const tokK = (resources.total_stored_tokens_est / 1000).toFixed(1);
  $('#resource-panel').innerHTML = `
    <div class="kpi">
      <div class="kpi-label">Cycle sayısı</div>
      <div class="kpi-value mono">${fmtInt(resources.experiments_count)}</div>
      <div class="kpi-note">Tamamlanan deney</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Depolanan tahmini token</div>
      <div class="kpi-value mono">${tokK}K</div>
      <div class="kpi-note">Memory'de saklı metin · LLM'e gönderilseydi ≈ ${fmtInt(resources.total_stored_tokens_est)} token</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Cycle başına ~token</div>
      <div class="kpi-value mono">${fmtInt(resources.avg_tokens_per_cycle_est)}</div>
      <div class="kpi-note">Ortalama metin üretim boyutu</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">DB boyutu</div>
      <div class="kpi-value mono">${mb} MB</div>
      <div class="kpi-note">SQLite dosyası · ${fmtInt(resources.total_rows)} satır</div>
    </div>
    <div class="kpi accent-gold">
      <div class="kpi-label">Gerçek LLM token</div>
      <div class="kpi-value mono">0</div>
      <div class="kpi-note"><b style="color:#fbbf24">Loop LLM çağrısı yapmıyor</b> — ajanlar Python rule-based. Sadece Claude oturumu token yakar.</div>
    </div>
  `;

  // Agent status grid
  $('#agent-status-grid').innerHTML = (status.agents || []).map(a => `
    <div class="status-card ${a.status}" style="--agent-color:${a.color}">
      <div class="sc-icon" style="background:${a.color}22;">${a.icon}</div>
      <div class="sc-body">
        <div class="sc-name">${escapeHtml(a.name)}</div>
        <div class="sc-status"><span class="sc-dot"></span>${escapeHtml(a.status_label)}</div>
      </div>
    </div>
  `).join('');

  $('#activity-hypos').innerHTML = act.hypotheses.slice(0, 8).map(h => `
    <div><span class="primary">${escapeHtml(h.title || '')}</span><span class="secondary">${(h.timestamp || '').slice(5,16).replace('T',' ')}</span></div>
  `).join('') || `<p class="muted" style="color:var(--text-3)">Henüz yok</p>`;

  $('#activity-decs').innerHTML = act.decisions.slice(0, 8).map(d => `
    <div><span class="primary">${escapeHtml(d.decision || '')}</span><span class="secondary">${(d.timestamp || '').slice(5,16).replace('T',' ')}</span></div>
  `).join('') || `<p class="muted" style="color:var(--text-3)">Henüz yok</p>`;

  $('#activity-debs').innerHTML = act.debates.slice(0, 5).map(d => {
    const args = Array.isArray(d.arguments) ? d.arguments : [];
    return `
      <details class="debate-item">
        <summary>🗣️ ${escapeHtml((d.topic || '').slice(0, 80))}</summary>
        <div class="conclusion">${escapeHtml(d.conclusion || '')}</div>
        ${args.map(a => `
          <div class="debate-arg">
            <span class="name">${escapeHtml(a.agent_name || '')}</span>
            <span class="pos">[${escapeHtml(a.position || '')}]</span>
            <span class="reason">${escapeHtml(a.reasoning || '')}</span>
          </div>
        `).join('')}
      </details>
    `;
  }).join('') || `<p class="muted" style="color:var(--text-3)">Henüz yok</p>`;
}

// ---------- Router ----------

function renderPage(page) {
  switch (page) {
    case 'overview':  renderOverview(); break;
    case 'agents':    renderAgents(); break;
    case 'org':       renderOrg(); break;
    case 'workflow':  renderWorkflow(); break;
    case 'projects':  renderProjects(); break;
    case 'goals':     renderGoals(); break;
    case 'pods':      renderPods(); break;
    case 'live':      renderLive(); break;
    case 'learn':     renderLearn(); break;
    case 'hr':        renderHR(); break;
    case 'lab':       renderLab(); break;
    case 'executive': renderExecutive(); break;
  }
}

async function renderExecutive() {
  const period = $('#exec-period')?.value || '24';
  const data = await api(`/api/executive-summary?hours=${period}`);

  const act = data.activity || {};
  const top = data.top_bots || [];
  const near = data.near_miss_candidates || [];
  const weaknesses = data.weaknesses || [];
  const recs = data.recommendations || [];

  const topRows = top.map((b, i) => `
    <tr onclick="openBotDetail('${b.bot_id}')" style="cursor:pointer">
      <td style="color:var(--accent);font-weight:700">#${i+1}</td>
      <td style="font-family:'JetBrains Mono',monospace;color:var(--text-1)">${escapeHtml(b.bot_name)}</td>
      <td>${escapeHtml(b.strategy_family)}</td>
      <td class="mono ${cls(b.avg_sharpe)}">${fmt(b.avg_sharpe)}</td>
      <td class="mono ${cls(b.avg_roi)}">${fmtPct(b.avg_roi)}</td>
      <td class="mono">${fmtInt(b.total_runs)}</td>
      <td><b style="color:${b.linear_score >= 3 ? '#7EB668' : '#9C968A'}">${b.linear_score}/5</b></td>
    </tr>
  `).join('');

  const nearRows = near.map(b => `
    <tr onclick="openBotDetail('${b.bot_id}')" style="cursor:pointer">
      <td style="font-family:'JetBrains Mono',monospace;color:var(--text-1)">${escapeHtml(b.bot_name)}</td>
      <td class="mono ${cls(b.avg_sharpe)}">${fmt(b.avg_sharpe)}</td>
      <td class="mono ${cls(b.avg_roi)}">${fmtPct(b.avg_roi)}</td>
      <td>${(b.qualified_horizons || []).join(', ') || '—'}</td>
      <td style="color:var(--text-2);font-size:0.86rem">${escapeHtml(b.hint)}</td>
    </tr>
  `).join('');

  const weakLi = weaknesses.map(w => `<li>${escapeHtml(w)}</li>`).join('');
  const recCards = recs.map(r => `
    <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:10px">
      <div style="color:var(--text-1);font-weight:600;margin-bottom:6px">${escapeHtml(r.title)}</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px">
        <div><div style="color:var(--text-3);font-size:0.70rem;text-transform:uppercase;letter-spacing:0.08em">Efor</div><div style="color:var(--text-1);font-family:'JetBrains Mono',monospace;font-size:0.88rem">${escapeHtml(r.effort)}</div></div>
        <div><div style="color:var(--text-3);font-size:0.70rem;text-transform:uppercase;letter-spacing:0.08em">Kazanım</div><div style="color:var(--text-2);font-size:0.82rem;line-height:1.45">${escapeHtml(r.expected_gain)}</div></div>
        <div><div style="color:var(--text-3);font-size:0.70rem;text-transform:uppercase;letter-spacing:0.08em">ROI etkisi</div><div style="color:#7EB668;font-size:0.82rem;line-height:1.45">${escapeHtml(r.expected_roi_impact)}</div></div>
      </div>
    </div>
  `).join('');

  $('#exec-body').innerHTML = `
    <!-- CEO diagnosis -->
    <div class="ceo-advice" style="margin-bottom:20px">
      <div class="ceo-label">👑 CEO Tanısı</div>
      <div class="ceo-body">${escapeHtml(data.ceo_diagnosis)}</div>
    </div>

    <!-- Activity KPIs -->
    <div class="kpi-grid small">
      <div class="kpi"><div class="kpi-label">Tamamlanan cycle</div><div class="kpi-value mono">${fmtInt(act.cycles_done)}</div></div>
      <div class="kpi"><div class="kpi-label">Verilen karar</div><div class="kpi-value mono">${fmtInt(act.decisions_made)}</div></div>
      <div class="kpi accent-gold"><div class="kpi-label">Promote edilen</div><div class="kpi-value mono">${fmtInt(act.promoted_count)}</div></div>
      <div class="kpi"><div class="kpi-label">Simüle net P/L</div><div class="kpi-value mono ${cls(act.net_simulated_pnl_usd)}">$${fmtInt(act.net_simulated_pnl_usd)}</div></div>
    </div>

    <!-- Top bots -->
    <div class="card">
      <div class="card-head"><h3>🏆 En iyi 5 bot</h3></div>
      ${top.length ? `
        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden">
          <table class="trades-mini" style="width:100%">
            <thead><tr>
              <th>#</th><th>bot</th><th>aile</th><th>Sharpe</th><th>ROI</th><th>run</th><th>horizon ✓</th>
            </tr></thead>
            <tbody>${topRows}</tbody>
          </table>
        </div>
      ` : '<p style="color:var(--text-3)">Henüz bot yok.</p>'}
    </div>

    <!-- Near-miss (geliştirme adayları) -->
    <div class="card">
      <div class="card-head"><h3>🔧 Geliştirme adayları <span class="muted">— az bir gayret ile hedefi geçecekler</span></h3></div>
      ${nearRows ? `
        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;overflow:hidden">
          <table class="trades-mini" style="width:100%">
            <thead><tr>
              <th>bot</th><th>Sharpe</th><th>ROI</th><th>geçen horizon</th><th>tavsiye</th>
            </tr></thead>
            <tbody>${nearRows}</tbody>
          </table>
        </div>
      ` : '<p style="color:var(--text-3)">Uygun aday yok (hepsi ya çok iyi ya çok kötü).</p>'}
    </div>

    <!-- Weaknesses -->
    ${weaknesses.length ? `
      <div class="card" style="border-left:3px solid #D66B5C">
        <div class="card-head"><h3>⚠️ Eksikler / Bottleneck'ler</h3></div>
        <ul style="padding-left:20px;margin:0;color:var(--text-2);line-height:1.7">${weakLi}</ul>
      </div>
    ` : ''}

    <!-- Recommendations -->
    <div class="card">
      <div class="card-head">
        <h3>💡 CEO tavsiyeleri · <span style="color:var(--text-3);font-weight:500;font-size:0.92rem">bu iyileştirmeler yapılırsa realistik hedef</span></h3>
      </div>
      <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:14px;display:flex;gap:30px;flex-wrap:wrap">
        <div>
          <div style="color:var(--text-3);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.08em">mevcut ort. ROI</div>
          <div style="color:var(--text-1);font-family:'JetBrains Mono',monospace;font-size:1.2rem;font-weight:700">${fmtPct(data.current_avg_roi)}</div>
        </div>
        <div>
          <div style="color:var(--text-3);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.08em">mevcut en iyi</div>
          <div style="color:var(--text-1);font-family:'JetBrains Mono',monospace;font-size:1.2rem;font-weight:700">${fmtPct(data.current_best_roi)}</div>
        </div>
        <div>
          <div style="color:var(--text-3);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.08em">realistik hedef (iyileştirmelerle)</div>
          <div style="color:#E8B464;font-family:'JetBrains Mono',monospace;font-size:1.2rem;font-weight:700">${fmtPct(data.realistic_target_roi)}</div>
        </div>
      </div>
      ${recCards}
    </div>
  `;
}
window.renderExecutive = renderExecutive;

// ---------- Laboratuvar page ----------

let proposalsChart = null;

async function renderLab() {
  const [propResp, llmStatus, settings] = await Promise.all([
    api('/api/proposals?status='),
    api('/api/llm/status'),
    api('/api/settings'),
  ]);

  // Settings toggle
  const sb = $('#settings-body');
  sb.innerHTML = `
    <label style="display:flex;align-items:center;gap:12px;cursor:pointer">
      <div class="toggle-switch">
        <input type="checkbox" id="tg-auto-approve" ${settings.auto_approve ? 'checked' : ''}>
        <span class="toggle-slider"></span>
      </div>
      <div>
        <div style="color:var(--text-1);font-weight:600;font-size:0.9rem">Otomatik onay</div>
        <div style="color:var(--text-3);font-size:0.76rem">Ekran başında değilken sistem önerileri otomatik onaylasın</div>
      </div>
    </label>

    <label style="display:flex;align-items:center;gap:12px;cursor:pointer">
      <div class="toggle-switch">
        <input type="checkbox" id="tg-auto-reject" ${settings.auto_reject_critical ? 'checked' : ''}>
        <span class="toggle-slider"></span>
      </div>
      <div>
        <div style="color:var(--text-1);font-weight:600;font-size:0.9rem">Kritik risk'i otomatik reddet</div>
        <div style="color:var(--text-3);font-size:0.76rem">"critical" etiketli öneriler otomatik reddedilsin</div>
      </div>
    </label>

    <label style="display:flex;align-items:center;gap:12px;cursor:pointer">
      <div class="toggle-switch">
        <input type="checkbox" id="tg-hermes-auto" ${settings.hermes_auto_scan ? 'checked' : ''}>
        <span class="toggle-slider"></span>
      </div>
      <div>
        <div style="color:var(--text-1);font-weight:600;font-size:0.9rem">Hermes periyodik GitHub taraması</div>
        <div style="color:var(--text-3);font-size:0.76rem">Günde 1 kez otomatik tarama (24 saatte bir)</div>
      </div>
    </label>

    <div style="display:flex;align-items:center;gap:10px">
      <label style="color:var(--text-2);font-size:0.86rem">Onay bekleme (dk):</label>
      <input type="number" id="tg-delay" value="${settings.auto_approve_delay_minutes}" min="1" max="1440" class="input narrow" style="width:90px">
    </div>

    <button onclick="triggerAutoApprove()" style="background:rgba(34,197,94,0.14);border:1px solid #22c55e;color:#22c55e;padding:8px 14px;border-radius:8px;cursor:pointer;font-weight:500">⚡ Şimdi çalıştır</button>
  `;

  // Bind toggle change events
  ['tg-auto-approve', 'tg-auto-reject', 'tg-hermes-auto', 'tg-delay'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', saveSettings);
  });

  const counts = propResp.counts || {};
  $('#lab-kpis').innerHTML = [
    { label: '⏳ Onay bekleyen', val: fmtInt(counts.pending || 0), cls: 'accent-gold' },
    { label: '✅ Onaylanmış', val: fmtInt(counts.approved || 0) },
    { label: '❌ Reddedilmiş', val: fmtInt(counts.rejected || 0) },
    { label: '🚀 Uygulanmış', val: fmtInt(counts.implemented || 0) },
  ].map(k => `
    <div class="kpi ${k.cls || ''}">
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value mono">${k.val}</div>
    </div>
  `).join('');

  // LLM status
  const budget = llmStatus.budget || {};
  $('#llm-status').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div class="stat-box">
        <div class="sb-label">CLI erişilebilir</div>
        <div class="sb-value ${llmStatus.available ? 'pos' : 'neg'}">${llmStatus.available ? '✓ evet' : '✗ yok'}</div>
      </div>
      <div class="stat-box">
        <div class="sb-label">Binary</div>
        <div class="sb-value mono" style="font-size:0.76rem">${escapeHtml((llmStatus.binary || '—').slice(-40))}</div>
      </div>
      <div class="stat-box">
        <div class="sb-label">Bugünkü çağrı</div>
        <div class="sb-value mono">${fmtInt(budget.daily_calls || 0)} / ${fmtInt(llmStatus.daily_cap || 100)}</div>
      </div>
      <div class="stat-box">
        <div class="sb-label">Toplam çağrı</div>
        <div class="sb-value mono">${fmtInt(budget.total_calls || 0)}</div>
      </div>
    </div>
    <p style="color:var(--text-3);font-size:0.80rem;margin-top:10px">
      Loop rule-based çalışıyor (ücretsiz). LLM opsiyonel — sadece enrichment için. Günlük ${fmtInt(llmStatus.daily_cap || 100)} çağrı limiti.
    </p>
  `;

  // Proposals doughnut
  const statuses = ['pending', 'approved', 'rejected', 'implemented'];
  const colors = { pending: '#fbbf24', approved: '#22c55e', rejected: '#ef4444', implemented: '#6366f1' };
  const data = statuses.map(s => counts[s] || 0);
  const total = data.reduce((a,b)=>a+b, 0);
  const ctx = document.getElementById('chart-proposals').getContext('2d');
  if (proposalsChart) proposalsChart.destroy();
  if (total > 0) {
    proposalsChart = new Chart(ctx, {
      type: 'doughnut',
      data: { labels: statuses, datasets: [{ data, backgroundColor: statuses.map(s => colors[s]), borderColor: '#12161e', borderWidth: 3 }] },
      options: { maintainAspectRatio: false, cutout: '60%', plugins: { legend: { position: 'bottom', labels: { color: '#b7bfcd', font: { family: 'Inter', size: 11 } } } } },
    });
  } else {
    document.getElementById('chart-proposals').getContext('2d').clearRect(0, 0, 1000, 300);
  }

  // Filter change hook
  const statusSel = $('#prop-status');
  if (!statusSel.dataset.ready) {
    statusSel.addEventListener('change', loadProposalsList);
    statusSel.dataset.ready = '1';
  }
  loadProposalsList();
}

async function loadProposalsList() {
  const status = $('#prop-status').value;
  const url = '/api/proposals' + (status ? `?status=${status}` : '');
  const resp = await api(url);
  const list = resp.proposals || [];

  if (!list.length) {
    $('#proposals-list').innerHTML = `<div class="card"><p style="color:var(--text-3);padding:10px;text-align:center">Bu durumda öneri yok. "GitHub tara" butonuyla Hermes'i çalıştır.</p></div>`;
    return;
  }

  $('#proposals-list').innerHTML = list.map(p => {
    const statusColor = {pending:'#fbbf24',approved:'#22c55e',rejected:'#ef4444',implemented:'#6366f1'}[p.status] || '#9ca3af';
    const actionBtns = p.status === 'pending'
      ? `<button onclick="decideProposal('${p.id}','approved')" style="background:rgba(34,197,94,0.14);border:1px solid #22c55e;color:#22c55e;padding:6px 14px;border-radius:6px;cursor:pointer;font-weight:600">✅ Onayla</button>
         <button onclick="decideProposal('${p.id}','rejected')" style="background:rgba(239,68,68,0.14);border:1px solid #ef4444;color:#ef4444;padding:6px 14px;border-radius:6px;cursor:pointer;font-weight:600">❌ Reddet</button>`
      : `<span style="color:${statusColor};text-transform:uppercase;font-weight:700;font-size:0.78rem;letter-spacing:0.1em">${p.status}</span>`;

    const steps = (p.action_steps || []).map(s => `<li style="color:var(--text-2);font-size:0.85rem;margin-bottom:3px">${escapeHtml(s)}</li>`).join('');
    const metadata = p.metadata || {};
    const starsBadge = metadata.stars ? `<span class="badge badge-dept" style="background:rgba(251,191,36,0.1);color:#fbbf24;border-color:rgba(251,191,36,0.3)">⭐ ${metadata.stars.toLocaleString()}</span>` : '';

    return `
      <div class="card" style="border-left:3px solid ${statusColor}">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:10px">
          <div style="flex:1;min-width:0">
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
              <span class="badge badge-dept">${escapeHtml(p.proposal_type)}</span>
              <span style="color:var(--text-3);font-size:0.78rem">${escapeHtml(p.author_agent)}</span>
              ${starsBadge}
              <span style="color:var(--text-3);font-size:0.75rem;margin-left:auto">${(p.timestamp||'').slice(0,10)}</span>
            </div>
            <h3 style="margin:0">${escapeHtml(p.title)}</h3>
            <p style="color:var(--text-2);font-size:0.88rem;line-height:1.55;margin-top:8px;white-space:pre-wrap">${escapeHtml(p.summary)}</p>
            ${p.source_url ? `<p style="margin-top:6px"><a href="${escapeHtml(p.source_url)}" target="_blank" style="color:var(--accent-hi);text-decoration:none">🔗 ${escapeHtml(p.source_url)}</a></p>` : ''}
          </div>
          <div style="display:flex;gap:8px;flex-shrink:0">${actionBtns}</div>
        </div>
        <details>
          <summary style="cursor:pointer;color:var(--text-3);font-size:0.82rem;padding:6px 0">📋 Detay + aksiyonlar</summary>
          <div style="padding:10px 0">
            <p style="color:var(--text-2);font-size:0.85rem;margin-bottom:8px"><b>Fayda:</b> ${escapeHtml(p.estimated_benefit || '—')}</p>
            <p style="color:var(--text-2);font-size:0.85rem;margin-bottom:8px"><b>Risk:</b> ${escapeHtml(p.estimated_risk || '—')}</p>
            <p style="color:var(--text-2);font-size:0.85rem;margin-bottom:4px"><b>Aksiyon adımları:</b></p>
            <ol style="padding-left:20px;margin:0">${steps}</ol>
          </div>
        </details>
      </div>
    `;
  }).join('');
}

async function saveSettings() {
  const params = new URLSearchParams();
  params.set('auto_approve', $('#tg-auto-approve').checked);
  params.set('auto_reject_critical', $('#tg-auto-reject').checked);
  params.set('hermes_auto_scan', $('#tg-hermes-auto').checked);
  params.set('auto_approve_delay_minutes', parseInt($('#tg-delay').value) || 10);
  try {
    await fetch('/api/settings?' + params.toString(), { method: 'POST' });
  } catch (e) { console.error(e); }
}
window.saveSettings = saveSettings;

async function triggerAutoApprove() {
  try {
    const r = await fetch('/api/auto-approve/trigger', { method: 'POST' }).then(x => x.json());
    alert(`AutoApprover çalıştı.\nİşlenen: ${r.processed}\nOnaylanan: ${r.approved}\nReddedilen: ${r.rejected}`);
    renderLab();
  } catch (e) { alert('Hata: ' + e.message); }
}
window.triggerAutoApprove = triggerAutoApprove;

async function decideProposal(id, decision) {
  const reason = decision === 'rejected' ? (prompt('Red sebebi (ops.):') || '') : '';
  try {
    const r = await fetch(`/api/proposals/decide?proposal_id=${encodeURIComponent(id)}&decision=${decision}&reason=${encodeURIComponent(reason)}`, { method: 'POST' }).then(x => x.json());
    if (r.ok) renderLab();
    else alert('Hata: ' + JSON.stringify(r));
  } catch (e) {
    alert('Hata: ' + e.message);
  }
}
window.decideProposal = decideProposal;

async function scanGitHub(useLlm) {
  const msg = useLlm
    ? 'Claude CLI ile tarama başlat? (daha uzun sürer, daha iyi öneri)'
    : 'Rule-based GitHub tarama başlat? (hızlı, ücretsiz)';
  if (!confirm(msg)) return;
  const btns = document.querySelectorAll('button[onclick^="scanGitHub"]');
  btns.forEach(b => { b.disabled = true; b.textContent = '⏳ Taranıyor... (30-90 sn)'; });
  try {
    const r = await fetch(`/api/git-research/scan?use_llm=${useLlm}&max_repos=15`, { method: 'POST' }).then(x => x.json());
    alert(`Tarama tamamlandı.\nTaranan repo: ${r.repos_scanned}\nÜretilen öneri: ${r.proposals_created}\nLLM kullanıldı: ${r.use_llm ? 'evet' : 'hayır'}`);
    renderLab();
  } catch (e) {
    alert('Hata: ' + e.message);
  } finally {
    btns.forEach(b => { b.disabled = false; });
    renderLab();
  }
}
window.scanGitHub = scanGitHub;

async function ingestCurriculum(useLlm) {
  const msg = useLlm
    ? 'Borsanın İzinden derslerini Claude CLI ile ingest et? (daha zengin, ~2 dk)'
    : 'Borsanın İzinden derslerini rule-based ingest et? (hızlı, ücretsiz)';
  if (!confirm(msg)) return;
  const btns = document.querySelectorAll('button[onclick^="ingestCurriculum"]');
  btns.forEach(b => { b.disabled = true; b.textContent = '⏳ Yükleniyor...'; });
  try {
    const r = await fetch(`/api/curriculum/ingest-borsaninizinden?use_llm=${useLlm}`, { method: 'POST' }).then(x => x.json());
    alert(`Curriculum ingest tamamlandı.\n\nMakale çekildi: ${r.articles_fetched}\nDers oluşturuldu: ${r.total_lessons}\nLLM: ${r.use_llm ? 'evet' : 'hayır'}\n\nDersler "Öğrenilenler" sekmesinde "source:curriculum" tag'iyle görünür.`);
    renderLab();
  } catch (e) {
    alert('Hata: ' + e.message);
  } finally {
    btns.forEach(b => { b.disabled = false; });
    renderLab();
  }
}
window.ingestCurriculum = ingestCurriculum;

// ---------- HR page ----------

let hrCapacityChart = null;

async function renderHR() {
  const data = await api('/api/hiring');

  $('#hr-kpis').innerHTML = [
    { label: 'Toplam hire', val: fmtInt(data.summary.total_hires) },
    { label: 'Toplam retire', val: fmtInt(data.summary.total_retires) },
    { label: 'Aktif specialist', val: fmtInt(data.summary.active_specialists), cls: 'accent-gold' },
    { label: 'Review aralığı', val: '50 cycle', note: 'otomatik' },
  ].map(k => `
    <div class="kpi ${k.cls || ''}">
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value mono">${k.val}</div>
      ${k.note ? `<div class="kpi-delta">${k.note}</div>` : ''}
    </div>
  `).join('');

  // Department capacity chart
  const depts = Object.entries(data.department_counts || {}).sort((a,b) => b[1] - a[1]);
  const DEPT_COLORS = {
    Executive: '#fbbf24', Research: '#60a5fa', Simulation: '#f59e0b',
    Governance: '#ef4444', Execution: '#34d399',
    Knowledge: '#a78bfa', Analytics: '#f472b6',
  };
  const ctx = document.getElementById('chart-dept-capacity').getContext('2d');
  if (hrCapacityChart) hrCapacityChart.destroy();
  hrCapacityChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: depts.map(([k]) => k),
      datasets: [{
        label: 'Ajan sayısı',
        data: depts.map(([_,v]) => v),
        backgroundColor: depts.map(([k]) => DEPT_COLORS[k] || '#9ca3af'),
        borderWidth: 0, borderRadius: 6,
      }],
    },
    options: {
      maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { backgroundColor: '#1c212d', borderColor: '#2f3749', borderWidth: 1 } },
      scales: {
        x: { grid: { color: '#403f3c' }, ticks: { color: '#5b6478', stepSize: 1 }, beginAtZero: true },
        y: { grid: { display: false }, ticks: { color: '#cdd6e0', font: { size: 12 } } },
      },
    },
  });

  // Policy rules
  const rules = data.policy.rules || [];
  $('#hr-policy').innerHTML = `<ul style="padding-left:20px;margin:0">${rules.map(r => `<li style="margin-bottom:6px">${escapeHtml(r)}</li>`).join('')}</ul>`;

  // Specialists
  const specs = data.specialists || [];
  $('#hr-specialists').innerHTML = specs.length ? `
    <table class="t">
      <thead><tr><th>Specialist</th><th>Rol</th><th>Departman</th><th>Hired@cycle</th><th>Mandate</th></tr></thead>
      <tbody>${specs.map(s => `
        <tr>
          <td style="color:var(--gold);font-weight:600">${escapeHtml(s.name)}</td>
          <td>${escapeHtml(s.role)}</td>
          <td>${escapeHtml(s.department)}</td>
          <td class="mono">${fmtInt(s.hired_at_cycle)}</td>
          <td style="max-width:400px;white-space:normal;color:var(--text-2)">${escapeHtml(s.mandate)}</td>
        </tr>
      `).join('')}</tbody>
    </table>
  ` : `<p style="color:var(--text-3);padding:10px">Henüz specialist yok. CEO iş yükü analizi yaptığında gerekiyorsa otomatik oluşturacak.</p>`;

  // Events feed
  const events = data.events || [];
  $('#hr-events').innerHTML = events.length ? events.map(e => {
    const ts = (e.timestamp || '').slice(0, 19).replace('T', ' ');
    const icon = e.action === 'hire' ? '✅' : '❌';
    const color = e.action === 'hire' ? '#4ade80' : '#ef7e7e';
    return `
      <div style="padding:12px 14px;border-left:3px solid ${color};background:var(--surface-2);margin-bottom:8px;border-radius:6px">
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:4px;font-size:0.82rem">
          <span>${icon}</span>
          <span style="color:${color};font-weight:600;text-transform:uppercase">${e.action}</span>
          <span style="color:var(--text-1);font-weight:600">${escapeHtml(e.agent_name)}</span>
          <span style="color:var(--text-3)">· ${escapeHtml(e.department || '')}</span>
          <span style="color:var(--text-3);margin-left:auto;font-family:monospace;font-size:0.76rem">${ts} · cycle ${e.cycle_number || 0}</span>
        </div>
        <div style="color:var(--text-2);font-size:0.86rem;margin-left:24px">${escapeHtml(e.reason || '')}</div>
        <div style="color:var(--text-3);font-size:0.78rem;margin-left:24px;margin-top:4px">${escapeHtml(e.mandate || '')}</div>
      </div>
    `;
  }).join('') : `<p style="color:var(--text-3);padding:10px">Henüz hiring olayı yok.</p>`;
}

async function triggerHRReview() {
  try {
    const r = await fetch('/api/hiring/trigger', { method: 'POST' }).then(x => x.json());
    alert(`HR review tamamlandı. ${r.applied ? r.applied.length : 0} değişiklik uygulandı.`);
    renderHR();
  } catch (e) {
    alert('HR review hatası: ' + e.message);
  }
}
window.triggerHRReview = triggerHRReview;

// --------- Learning page ---------

let curveChart = null;

const learnCharts = {};

async function renderLearn() {
  const [lessons, stats, scopesResp] = await Promise.all([
    api('/api/lessons?limit=200'),
    api('/api/lessons-stats'),
    api('/api/learning-scopes'),
  ]);

  // Summary KPIs
  $('#learn-kpis').innerHTML = [
    { label: 'Toplam ders', val: fmtInt(stats.total), note: 'Journal\'da saklı insight' },
    { label: 'Modellerde kullanıldı', val: fmtInt(stats.used_in_models), note: 'En az 1 cycle\'da atıfa alınmış' },
    { label: '👑 CEO onaylı', val: fmtInt(stats.ceo_approved), note: '3+ atıf alan — network\'e entegre', cls: 'accent-gold' },
    { label: 'Dormant', val: fmtInt(stats.dormant), note: 'Henüz okunmadı', cls: '' },
  ].map(k => `
    <div class="kpi ${k.cls || ''}">
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value mono">${k.val}</div>
      <div class="kpi-delta">${k.note}</div>
    </div>
  `).join('');

  // Scope selector for curve chart
  const scopeSel = $('#learn-scope');
  if (!scopeSel.dataset.ready) {
    const opts = (scopesResp.scopes || []).map(s => `<option value="${s}">${s}</option>`).join('');
    scopeSel.innerHTML = opts || `<option value="all">Tümü (all)</option>`;
    if (![...scopeSel.options].some(o => o.value === 'all')) {
      scopeSel.insertAdjacentHTML('afterbegin', `<option value="all">Tümü (all)</option>`);
    }
    scopeSel.value = 'all';
    scopeSel.dataset.ready = '1';
    scopeSel.addEventListener('change', () => loadCurve(scopeSel.value));
  }
  loadCurve(scopeSel.value || 'all');

  // Lessons timeline chart
  renderLessonsTimeline(stats.timeline || []);

  // Distribution charts
  renderDistributionChart('chart-severity', stats.severity_counts || {}, {
    critical: '#ef4444', high: '#f59e0b', medium: '#fbbf24', info: '#60a5fa', low: '#6b7280',
  });
  renderDistributionChart('chart-regime', stats.regime_counts || {}, {
    trend_up: '#22c55e', trend_down: '#ef7e7e', range: '#a78bfa',
    high_vol: '#fb923c', crisis: '#ef4444', bull: '#22c55e', bear: '#ef7e7e',
    unknown: '#6b7280',
  });
  renderDistributionChart('chart-strat', stats.strategy_counts || {}, {
    day: '#60a5fa', swing: '#f472b6', scalp: '#34d399',
  });

  // Per-agent table with CEO-approved badge
  const perAgent = Object.entries(stats.per_agent || {}).sort((a,b) => b[1] - a[1]);
  $('#lessons-by-agent').innerHTML = perAgent.length
    ? `<table class="t"><thead><tr><th>Ajan</th><th>Ders sayısı</th></tr></thead><tbody>${
        perAgent.map(([a, c]) => `<tr><td>${escapeHtml(a)}</td><td class="mono">${fmtInt(c)}</td></tr>`).join('')
      }</tbody></table>`
    : `<p style="color:var(--text-3);padding:10px">Henüz ders yok.</p>`;

  // Top-referenced
  const topRefs = stats.top_referenced || [];
  $('#top-referenced-lessons').innerHTML = topRefs.length ? topRefs.map(l => `
    <div style="padding:10px 12px;border-bottom:1px solid var(--border);">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;font-size:0.78rem">
        <span class="badge badge-dept" style="background:rgba(251,191,36,0.14);color:#fbbf24;border-color:rgba(251,191,36,0.3)">👑 ${l.times_referenced}× atıf</span>
        <span style="color:var(--text-3)">${escapeHtml(l.author)}</span>
        <span style="color:var(--text-3)">· ${escapeHtml(l.severity)}</span>
      </div>
      <div style="color:var(--text-1);font-size:0.88rem;line-height:1.5">${escapeHtml(l.content)}</div>
    </div>
  `).join('') : `<p style="color:var(--text-3);padding:10px">Henüz 3+ atıf alan ders yok.</p>`;

  // Filters
  const agentSel = $('#lesson-agent');
  const mktSel = $('#lesson-market');
  const stratSel = $('#lesson-strategy');
  const sevSel = $('#lesson-severity');
  if (!agentSel.dataset.ready) {
    const agents = Array.from(new Set(lessons.map(l => l.author_agent))).sort();
    const markets = Array.from(new Set(lessons.map(l => l.market).filter(m => m && m !== '*'))).sort();
    const strats = Array.from(new Set(lessons.map(l => l.strategy_family).filter(s => s && s !== '*'))).sort();
    agentSel.innerHTML = `<option value="">Tüm ajanlar</option>` + agents.map(a => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`).join('');
    mktSel.innerHTML = `<option value="">Tüm marketler</option>` + markets.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
    stratSel.innerHTML = `<option value="">Tüm stratejiler</option>` + strats.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
    agentSel.dataset.ready = '1';
    [agentSel, mktSel, stratSel, sevSel].forEach(el => el.addEventListener('change', reloadLessons));
  }

  reloadLessons();
}

async function reloadLessons() {
  const agent = $('#lesson-agent').value;
  const market = $('#lesson-market').value;
  const strategy = $('#lesson-strategy').value;
  const severity = $('#lesson-severity').value;
  const params = new URLSearchParams();
  if (agent) params.set('agent', agent);
  if (market) params.set('market', market);
  if (strategy) params.set('strategy', strategy);
  if (severity) params.set('severity', severity);
  params.set('limit', '300');
  const lessons = await api('/api/lessons?' + params.toString());

  if (!lessons.length) {
    $('#lessons-table').innerHTML = `<p style="padding:20px;color:var(--text-3)">Filtreye uyan ders yok.</p>`;
    return;
  }
  const rows = lessons.map(l => {
    const sevColor = {
      critical: '#ef4444', high: '#f59e0b', medium: '#fbbf24', info: '#9ca3af', low: '#6b7280'
    }[l.severity] || '#9ca3af';
    return `<tr>
      <td class="mono">${(l.created_at||'').slice(5,16).replace('T',' ')}</td>
      <td>${escapeHtml(l.author_agent)}</td>
      <td style="color:${sevColor}">${escapeHtml(l.severity)}</td>
      <td>${escapeHtml(l.market)}</td>
      <td>${escapeHtml(l.strategy_family)}</td>
      <td>${escapeHtml(l.regime)}</td>
      <td>${escapeHtml(l.symbol)}</td>
      <td style="white-space:normal;max-width:500px;color:var(--text-1)">${escapeHtml(l.content)}</td>
      <td class="mono">${fmtInt(l.times_referenced)}</td>
    </tr>`;
  }).join('');

  $('#lessons-table').innerHTML = `<table class="t">
    <thead><tr>
      <th>zaman</th><th>ajan</th><th>severity</th><th>market</th>
      <th>aile</th><th>rejim</th><th>sembol</th><th>içerik</th><th>ref</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLessonsTimeline(timeline) {
  const labels = timeline.map(t => t.day.slice(5));
  const total = timeline.map(t => t.total);
  const approved = timeline.map(t => t.approved);
  const ctx = document.getElementById('chart-lessons-timeline').getContext('2d');
  if (learnCharts.timeline) learnCharts.timeline.destroy();
  learnCharts.timeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Toplam', data: total, backgroundColor: 'rgba(129,140,248,0.55)', borderWidth: 0 },
        { label: '👑 CEO onaylı', data: approved, backgroundColor: 'rgba(251,191,36,0.85)', borderWidth: 0 },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#b7bfcd' } }, tooltip: { backgroundColor: '#1c212d', borderColor: '#2f3749', borderWidth: 1 } },
      scales: {
        x: { grid: { color: '#403f3c' }, ticks: { color: '#5b6478' }, stacked: true },
        y: { grid: { color: '#403f3c' }, ticks: { color: '#5b6478', stepSize: 1 }, beginAtZero: true },
      },
    },
  });
}

function renderDistributionChart(canvasId, counts, colorMap) {
  const entries = Object.entries(counts).sort((a,b) => b[1] - a[1]);
  const labels = entries.map(([k,_]) => k);
  const data = entries.map(([_,v]) => v);
  const colors = labels.map(l => colorMap[l] || '#9ca3af');
  const ctx = document.getElementById(canvasId).getContext('2d');
  if (learnCharts[canvasId]) learnCharts[canvasId].destroy();
  learnCharts[canvasId] = new Chart(ctx, {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: '#12161e', borderWidth: 3 }] },
    options: {
      maintainAspectRatio: false,
      cutout: '62%',
      plugins: {
        legend: { position: 'bottom', labels: { color: '#b7bfcd', font: { family: 'Inter', size: 11 }, padding: 10 } },
        tooltip: { backgroundColor: '#1c212d', borderColor: '#2f3749', borderWidth: 1 },
      },
    },
  });
}

async function loadCurve(scope) {
  const data = await api('/api/learning-curve?scope=' + encodeURIComponent(scope));
  const pts = data.points || [];
  const labels = pts.map(p => p.bucket);
  const sharpe = pts.map(p => p.avg_sharpe);
  const promo = pts.map(p => p.promotion_rate * 100);
  const samples = pts.map(p => p.sample_size);

  const ctx = document.getElementById('chart-curve').getContext('2d');
  if (curveChart) curveChart.destroy();
  curveChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Avg Sharpe', data: sharpe, borderColor: '#818cf8', backgroundColor: 'rgba(129,140,248,0.1)', tension: 0.3, fill: true, yAxisID: 'y' },
        { label: 'Promote %', data: promo, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.05)', tension: 0.3, yAxisID: 'y1' },
        { label: 'Sample size', data: samples, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.05)', tension: 0.3, yAxisID: 'y2', hidden: true },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#b7bfcd', font: { family: 'Inter' } } },
        tooltip: { backgroundColor: '#1c212d', borderColor: '#2f3749', borderWidth: 1 },
      },
      scales: {
        x: { grid: { color: '#403f3c' }, ticks: { color: '#9C968A', font: { family: 'JetBrains Mono' } } },
        y: { position: 'left', grid: { color: '#403f3c' }, ticks: { color: '#818cf8' }, title: { display: true, text: 'Sharpe', color: '#818cf8' } },
        y1: { position: 'right', grid: { display: false }, ticks: { color: '#22c55e' }, title: { display: true, text: 'Promote %', color: '#22c55e' } },
        y2: { display: false },
      },
    },
  });
}

// ---------- Auto-refresh ----------

let refreshTimer = null;
let livePollTimer = null;
function startRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  if (livePollTimer) clearInterval(livePollTimer);
  if (!$('#auto-refresh').checked) return;
  refreshTimer = setInterval(refreshAll, 15000);
  // Live-now panel polls more frequently (every 2 sn) since phases are fast
  livePollTimer = setInterval(pollLiveNow, 2000);
}
$('#auto-refresh').addEventListener('change', startRefresh);

// ---------- Init ----------

(async function init() {
  await refreshAll();
  show('overview');
  startRefresh();
})();
