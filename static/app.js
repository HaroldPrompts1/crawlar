/**
 * Frontend Application Logic for Crawler Studio
 */

let currentJobId = null;
let pollTimer = null;
let activeTab = 'dashboard';

// --- Initialization ---
document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  loadClients().then(() => {
    refreshAllData();
  });
});

// --- Tab Management ---
function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      switchTab(target);
    });
  });
}

function switchTab(tabId) {
  activeTab = tabId;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === `tab-${tabId}`);
  });

  if (tabId === 'dashboard') refreshDashboard();
  else if (tabId === 'docs') loadDocuments();
  else if (tabId === 'links') loadLinkGraph();
  else if (tabId === 'assets') loadAssets();
  else if (tabId === 'errors') loadErrors();
  else if (tabId === 'config') loadClientConfig();
}

function refreshAllData() {
  refreshDashboard();
  if (activeTab === 'docs') loadDocuments();
  if (activeTab === 'links') loadLinkGraph();
  if (activeTab === 'assets') loadAssets();
  if (activeTab === 'errors') loadErrors();
  if (activeTab === 'config') loadClientConfig();
}

// --- API Helpers ---
async function api(path, options = {}) {
  try {
    const res = await fetch(path, options);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    return await res.json();
  } catch (err) {
    console.error(`API error on ${path}:`, err);
    throw err;
  }
}

function getSelectedClient() {
  return document.getElementById('clientSelect').value || '';
}

function onClientChanged() {
  updateDangerZone();
  refreshAllData();
}

function refreshAllData() {
  refreshDashboard();
  loadGraph();
  if (activeTab === 'docs') loadDocuments();
  if (activeTab === 'errors') loadErrors();
  if (activeTab === 'config') loadClientConfig();
}

async function loadGraph() {
  const client = getSelectedClient();
  const el = document.getElementById('graphBlock');
  if (!client) { el.innerHTML = '<div class="empty-state"><p>Selecciona un cliente</p></div>'; return; }
  try {
    const g = await api('/api/graph?client=' + encodeURIComponent(client));
    const top = (g.pagerank_top || []).map(p =>
      `<tr><td><strong>${escapeHtml(truncate(p.title || p.url, 50))}</strong><br><span style="font-size:0.75rem;">${escapeHtml(truncate(p.url, 60))}</span></td><td>${p.score}</td></tr>`).join('');
    const orph = (g.orphans || []).map(u => `<tr><td style="font-size:0.8rem;">${escapeHtml(truncate(u, 70))}</td></tr>`).join('');
    const gen = (g.generic_anchors || []).map(x => `<tr><td>"${escapeHtml(x.anchor)}"</td><td>x${x.count}</td></tr>`).join('');
    const ext = (g.externals || []).slice(0, 8).map(x => `<tr><td style="font-size:0.8rem;">${escapeHtml(truncate(x.url, 60))}</td><td>x${x.count}</td></tr>`).join('');
    el.innerHTML = `<div style="display:grid; grid-template-columns:1fr 1fr; gap:1rem;">
      <div><h4>Top autoridad (${g.edges || 0} enlaces)</h4><table class="data-table"><tbody>${top || '<tr><td>—</td></tr>'}</tbody></table></div>
      <div><h4>Huérfanas (${g.orphans_count || 0})</h4><table class="data-table"><tbody>${orph || '<tr><td>—</td></tr>'}</tbody></table></div>
      <div><h4>Anchors genéricos</h4><table class="data-table"><tbody>${gen || '<tr><td>—</td></tr>'}</tbody></table></div>
      <div><h4>Salientes externas</h4><table class="data-table"><tbody>${ext || '<tr><td>—</td></tr>'}</tbody></table></div>
      </div>`;
  } catch (e) { el.innerHTML = '<div class="empty-state"><p>Sin datos: corre un rastreo primero</p></div>'; }
}

// --- Client Management ---
async function loadClients() {
  try {
    const data = await api('/api/clients');
    const select = document.getElementById('clientSelect');
    const prev = select.value;
    select.innerHTML = '<option value="">(Todos los clientes)</option>' +
      data.clients.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
    if (prev && data.clients.includes(prev)) {
      select.value = prev;
    }
    updateDangerZone();
  } catch (e) {
    showToast('Error cargando clientes: ' + e.message, 'error');
  }
}

function updateDangerZone() {
  const client = getSelectedClient();
  const nameEl = document.getElementById('dangerClientName');
  const btn = document.getElementById('btnDeleteClient');
  const hint = document.getElementById('dangerClientHint');
  if (!nameEl || !btn) return;
  if (client) {
    nameEl.textContent = client;
    btn.disabled = false;
    btn.textContent = `Eliminar "${client}" completo`;
    if (hint) hint.textContent = 'Se borrará todo lo de este cliente.';
  } else {
    nameEl.textContent = '(ninguno — elige uno arriba)';
    btn.disabled = true;
    btn.textContent = 'Eliminar cliente seleccionado';
    if (hint) hint.textContent = 'Selecciona un cliente para activar.';
  }
}

// --- Dashboard & Metrics ---
async function refreshDashboard() {
  const client = getSelectedClient();
  try {
    const [statsData, dashData] = await Promise.all([
      api('/api/stats' + (client ? `?client=${encodeURIComponent(client)}` : '')),
      api('/api/dashboard' + (client ? `?client=${encodeURIComponent(client)}` : ''))
    ]);

    const byStatus = statsData.by_status || {};
    const okCount = byStatus['ok'] || 0;
    const skippedCount = (byStatus['near_dup'] || 0) + (byStatus['domain_cap'] || 0) + 
      (byStatus['too_deep'] || 0) + (byStatus['skipped_robots'] || 0) + (byStatus['not_found'] || 0);
    const errorCount = (byStatus['fetch_error'] || 0);

    document.getElementById('metricOk').textContent = okCount.toLocaleString();
    document.getElementById('metricDocs').textContent = (statsData.docs || 0).toLocaleString();
    document.getElementById('metricSkipped').textContent = skippedCount.toLocaleString();
    document.getElementById('metricErrors').textContent = errorCount.toLocaleString();

    if (dashData.words_total != null) {
      document.getElementById('metricWordsTotal').textContent = `${dashData.words_total.toLocaleString()} palabras extraídas`;
    }

    // Status Breakdown Table
    const breakdownEl = document.getElementById('statusBreakdown');
    if (Object.keys(byStatus).length === 0) {
      breakdownEl.innerHTML = '<div class="empty-state"><p>Sin datos todavía</p></div>';
    } else {
      breakdownEl.innerHTML = `
        <table class="data-table">
          <thead>
            <tr><th>Estado</th><th>Cantidad</th><th>Porcentaje</th></tr>
          </thead>
          <tbody>
            ${Object.entries(byStatus).map(([status, count]) => {
              const total = Object.values(byStatus).reduce((a, b) => a + b, 0) || 1;
              const pct = ((count / total) * 100).toFixed(1);
              return `
                <tr>
                  <td><span class="status-pill pill-${statusClass(status)}">${escapeHtml(status)}</span></td>
                  <td><strong>${count.toLocaleString()}</strong></td>
                  <td>
                    <div style="display:flex; align-items:center; gap:0.5rem;">
                      <div style="height:6px; width:60px; background:rgba(255,255,255,0.1); border-radius:3px; overflow:hidden;">
                        <div style="height:100%; width:${pct}%; background:var(--accent);"></div>
                      </div>
                      <span style="font-size:0.75rem; color:var(--text-dim);">${pct}%</span>
                    </div>
                  </td>
                </tr>
              `;
            }).join('')}
          </tbody>
        </table>
      `;
    }

    // Recent Activity Table
    const recentEl = document.getElementById('recentActivity');
    const recent = statsData.recent || [];
    if (recent.length === 0) {
      recentEl.innerHTML = '<div class="empty-state"><p>Aún no hay páginas rastreadas</p></div>';
    } else {
      recentEl.innerHTML = `
        <table class="data-table">
          <thead>
            <tr><th>Título</th><th>URL</th><th>Fecha</th></tr>
          </thead>
          <tbody>
            ${recent.map(r => `
              <tr>
                <td><strong>${escapeHtml(r.title || 'Sin título')}</strong></td>
                <td><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener" style="font-size:0.75rem;">${escapeHtml(truncate(r.url, 45))}</a></td>
                <td style="font-size:0.75rem; color:var(--text-dim);">${formatDate(r.last_crawled)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

  } catch (e) {
    console.error(e);
  }
}

// --- Live Crawler Controller ---
async function handleStartCrawl(e) {
  e.preventDefault();
  const urlRaw = document.getElementById('urlInput').value.trim();
  const seeds = urlRaw.split(/\s+/).filter(Boolean);
  if (!seeds.length) {
    showToast('Por favor introduce al menos una URL válida', 'error');
    return;
  }

  const maxPages = parseInt(document.getElementById('maxPagesInput').value, 10) || 50;
  const workers = parseInt(document.getElementById('workersInput').value, 10) || 1;
  const withSitemap = document.getElementById('sitemapCheck').checked;
  const useRender = document.getElementById('renderCheck').checked;
  const client = getSelectedClient();

  try {
    setCrawlerButtonsRunning(true);
    clearLog();
    appendLogEntry({ t: nowTime(), type: 'start', url: seeds[0], outcome: `Iniciando crawl (${maxPages} máx, ${workers} workers)` });

    const res = await api('/api/crawl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seeds, max_pages: maxPages, workers, with_sitemap: withSitemap, use_render: useRender, client })
    });

    currentJobId = res.job_id;
    showToast('Rastreo iniciado correctamente', 'success');
    
    // Auto-select client if created
    if (res.client) {
      await loadClients();
      document.getElementById('clientSelect').value = res.client;
    }

    startPolling();
  } catch (err) {
    setCrawlerButtonsRunning(false);
    showToast('Error al iniciar crawl: ' + err.message, 'error');
  }
}

async function handleRecrawl() {
  const client = getSelectedClient();
  try {
    setCrawlerButtonsRunning(true);
    clearLog();
    appendLogEntry({ t: nowTime(), type: 'start', outcome: `Revisando cliente: ${client || 'todos'}` });

    const res = await api('/api/recrawl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client })
    });

    currentJobId = res.job_id;
    showToast('Revisita iniciada', 'success');
    startPolling();
  } catch (err) {
    setCrawlerButtonsRunning(false);
    showToast('Error al revisitar: ' + err.message, 'error');
  }
}

async function handleCancelJob() {
  if (!currentJobId) return;
  try {
    await api(`/api/cancel/${currentJobId}`, { method: 'POST' });
    showToast('Cancelación solicitada', 'info');
  } catch (err) {
    showToast('Error cancelando: ' + err.message, 'error');
  }
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(pollJob, 1000);
  pollJob();
}

async function pollJob() {
  if (!currentJobId) return;
  try {
    const job = await api(`/api/job/${currentJobId}`);
    
    // Update live indicators
    const isRunning = job.status === 'running';
    document.getElementById('livePulse').classList.toggle('active', isRunning);
    document.getElementById('liveStatusText').textContent = isRunning ? 'Rastreando en vivo...' : (job.status === 'done' ? 'Completado' : 'Detenido');
    document.getElementById('liveCountsText').textContent = `${job.done} / ${job.planned || '?'} procesadas`;
    document.getElementById('liveCurrentUrl').textContent = job.current ? `⏳ ${job.current}` : (isRunning ? 'Procesando lote...' : 'Listo');

    const pct = job.planned > 0 ? Math.min(100, Math.round((job.done / job.planned) * 100)) : (job.done > 0 ? 100 : 0);
    document.getElementById('progressBarFill').style.width = `${pct}%`;

    // Render Events in terminal
    renderEvents(job.events || []);

    if (job.status === 'done' || job.status === 'cancelled') {
      clearInterval(pollTimer);
      setCrawlerButtonsRunning(false);
      showToast(`Rastreo finalizado: ${job.done} páginas procesadas`, 'success');
      loadClients();
      refreshDashboard();
      if (activeTab === 'docs') loadDocuments();
      if (activeTab === 'errors') loadErrors();
    }
  } catch (e) {
    console.error('Poll error', e);
  }
}

function setCrawlerButtonsRunning(running) {
  document.getElementById('btnCrawl').disabled = running;
  document.getElementById('btnCancel').disabled = !running;
}

function renderEvents(events) {
  const terminal = document.getElementById('terminalLog');
  terminal.innerHTML = events.map(e => formatEventRow(e)).join('');
  terminal.scrollTop = terminal.scrollHeight;
}

function appendLogEntry(e) {
  const terminal = document.getElementById('terminalLog');
  terminal.innerHTML += formatEventRow(e);
  terminal.scrollTop = terminal.scrollHeight;
}

function formatEventRow(e) {
  let badgeClass = 'badge-done';
  let badgeText = e.type || 'INFO';
  
  if (e.type === 'page_start') { badgeClass = 'badge-start'; badgeText = 'FETCH'; }
  else if (e.type === 'page_done') { badgeClass = 'badge-done'; badgeText = 'OK'; }
  else if (e.type === 'skipped') { badgeClass = 'badge-skip'; badgeText = 'SKIP'; }
  else if (e.type === 'error' || e.error) { badgeClass = 'badge-err'; badgeText = 'ERR'; }
  else if (e.type === 'finished') { badgeClass = 'badge-finish'; badgeText = 'DONE'; }

  const urlText = e.url ? `<span style="color:#f3f4f6;">${escapeHtml(e.url)}</span>` : '';
  const outcomeText = e.outcome ? `<span style="color:#93c5fd;"> → ${escapeHtml(e.outcome)}</span>` : '';
  const reasonText = e.reason ? `<span style="color:#fcd34d;"> (${escapeHtml(e.reason)})</span>` : '';
  const errText = e.error ? `<span style="color:#f87171;"> ERR: ${escapeHtml(e.error)}</span>` : '';
  const procText = e.processed != null ? `<span style="color:#86efac;"> Total: ${e.processed} págs</span>` : '';

  return `<div class="log-entry">
    <span class="log-time">[${e.t || nowTime()}]</span>
    <span class="log-badge ${badgeClass}">${badgeText}</span>
    <span>${urlText}${outcomeText}${reasonText}${errText}${procText}</span>
  </div>`;
}

function clearLog() {
  document.getElementById('terminalLog').innerHTML = '';
}

// --- Search Explorer ---
async function executeSearch() {
  const query = document.getElementById('searchQuery').value.trim();
  if (!query) {
    showToast('Ingresa términos de búsqueda', 'info');
    return;
  }
  const client = getSelectedClient();
  const container = document.getElementById('searchResultsContainer');
  container.innerHTML = '<div class="empty-state"><p>Buscando en el índice FTS5...</p></div>';

  try {
    const res = await api(`/api/search?q=${encodeURIComponent(query)}&client=${encodeURIComponent(client)}`);
    const results = res.results || [];
    
    if (!results.length) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">🔍</div>
          <p>No se encontraron resultados para "<strong>${escapeHtml(query)}</strong>"</p>
        </div>`;
      return;
    }

    container.innerHTML = results.map(r => `
      <div class="search-result-item">
        <div class="search-res-title">${escapeHtml(r.title || 'Documento sin título')} <span class="status-pill pill-ok">score ${r.score != null ? r.score : '—'}</span></div>
        <div class="search-res-url"><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.url)}</a></div>
        <div class="search-res-snippet">${r.snip || 'Sin texto coincidente'}</div>
        <div style="margin-top:0.6rem; display:flex; gap:0.5rem;">
          <button class="btn btn-sm" onclick="previewDoc('${encodeURIComponent(r.url)}')">Ver Texto</button>
          ${r.replica ? `<a href="/replica/${escapeHtml(r.replica)}" target="_blank" class="btn btn-sm btn-primary">📖 Réplica</a>` : ''}
        </div>
      </div>
    `).join('');
  } catch (err) {
    showToast('Error en búsqueda: ' + err.message, 'error');
  }
}

// --- Documents & Replicas Table ---
async function loadDocuments() {
  const client = getSelectedClient();
  const tbody = document.getElementById('docsTableBody');
  tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;">Cargando documentos...</td></tr>';

  try {
    const data = await api('/api/docs' + (client ? `?client=${encodeURIComponent(client)}` : ''));
    const docs = data.docs || [];
    if (!docs.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;" class="empty-state"><p>No hay documentos indexados</p></td></tr>';
      return;
    }

    tbody.innerHTML = docs.map(d => `
      <tr>
        <td><strong>${escapeHtml(d.title || '(Sin título)')}</strong></td>
        <td><a href="${escapeHtml(d.url)}" target="_blank" rel="noopener" style="font-size:0.8rem;">${escapeHtml(truncate(d.url, 45))}</a></td>
        <td><span class="status-pill pill-ok">${(d.words || 0).toLocaleString()} palabras</span></td>
        <td>
          <div style="display:flex; gap:0.4rem;">
            <a href="/replica/${escapeHtml(d.replica)}" target="_blank" class="btn btn-sm btn-primary">📖 Réplica</a>
            <button class="btn btn-sm" onclick="previewDoc('${encodeURIComponent(d.url)}')">🔍 Detalle</button>
            <button class="btn btn-sm btn-danger" onclick="deleteDoc('${encodeURIComponent(d.url)}')">🗑️</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    showToast('Error cargando documentos: ' + err.message, 'error');
  }
}

async function deleteDoc(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  if (!confirm(`¿Eliminar el registro completo?\n\n${url}\n\nSe borra de pages, documents, índice FTS, visits, entities, grafo, réplica y crudo.`)) return;
  try {
    await api(`/api/doc?url=${encodeURIComponent(url)}`, { method: 'DELETE' });
    showToast('Registro eliminado', 'success');
    loadDocuments();
    refreshDashboard();
    if (activeTab === 'errors') loadErrors();
  } catch (err) {
    showToast('Error eliminando: ' + err.message, 'error');
  }
}

async function deleteClient() {
  const client = getSelectedClient();
  if (!client) {
    showToast('Selecciona un cliente primero', 'warning');
    return;
  }
  if (!confirm(`¿Eliminar TODOS los registros de "${client}"?\n\nPáginas, documentos, índice, visits, grafo, entities, assets, réplicas y crudos. No se puede deshacer.`)) return;
  try {
    const res = await api(`/api/client?client=${encodeURIComponent(client)}`, { method: 'DELETE' });
    showToast(`Cliente "${client}" eliminado: ${res.urls || 0} urls`, 'success');
    await loadClients();
    document.getElementById('clientSelect').value = '';
    updateDangerZone();
    refreshAllData();
  } catch (err) {
    showToast('Error eliminando cliente: ' + err.message, 'error');
  }
}

async function previewDoc(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  try {
    const doc = await api(`/api/doc?url=${encodeURIComponent(url)}`);
    if (!doc) throw new Error('No encontrado');

    document.getElementById('modalDocTitle').textContent = doc.title || url;
    const imgs = (doc.images || []).map(im => {
      const src = typeof im === 'string' ? im : (im.src || '');
      const alt = typeof im === 'object' ? (im.alt || '') : '';
      const role = typeof im === 'object' ? (im.role || 'content') : 'content';
      return `<tr><td style="font-size:0.75rem;"><a href="${escapeHtml(src)}" target="_blank" rel="noopener">${escapeHtml(truncate(src, 55))}</a>`
        + (alt ? `<br><span style="color:var(--text-dim);">alt: ${escapeHtml(alt)}</span>` : '')
        + `</td><td><span class="status-pill pill-${role === 'chrome' ? 'pending' : (role === 'tracking' ? 'error' : 'ok')}">${escapeHtml(role)}</span></td></tr>`;
    }).join('');
    document.getElementById('modalDocBody').innerHTML = `
      <p style="font-size:0.8rem; color:var(--text-muted); margin-bottom:1rem;"><strong>URL:</strong> <a href="${escapeHtml(doc.url)}" target="_blank">${escapeHtml(doc.url)}</a></p>
      <div style="display:flex; gap:1rem; margin-bottom:1rem;">
        <span class="status-pill pill-ok">${doc.word_count || 0} palabras</span>
        <span class="status-pill pill-in-progress">${(doc.headings || []).length} encabezados</span>
        <span class="status-pill pill-pending">${(doc.images || []).length} imágenes</span>
      </div>
      <h4 style="margin-top:1rem; margin-bottom:0.5rem;">Imágenes (URL + alt + rol por comparación):</h4>
      <table class="data-table"><tbody>${imgs || '<tr><td>—</td></tr>'}</tbody></table>
      <h4 style="margin-top:1rem; margin-bottom:0.5rem;">Contenido Extraído Normalizado:</h4>
      <div style="background:rgba(0,0,0,0.3); padding:1rem; border-radius:var(--radius-md); font-size:0.85rem; max-height:300px; overflow-y:auto; white-space:pre-wrap;">${escapeHtml(doc.text || '(Sin texto)')}</div>
    `;
    document.getElementById('docModal').classList.add('active');
  } catch (err) {
    showToast('Error al obtener detalle: ' + err.message, 'error');
  }
}

function closeDocModal() {
  document.getElementById('docModal').classList.remove('active');
}

// --- Errors Diagnostics ---
async function loadErrors() {
  const client = getSelectedClient();
  const tbody = document.getElementById('errorsTableBody');
  tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;">Cargando fallos...</td></tr>';

  try {
    const data = await api('/api/stats' + (client ? `?client=${encodeURIComponent(client)}` : ''));
    const failures = data.failures || [];
    if (!failures.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;" class="empty-state"><p>✅ No hay fallos registrados</p></td></tr>';
      return;
    }

    tbody.innerHTML = failures.map(f => `
      <tr>
        <td><a href="${escapeHtml(f.url)}" target="_blank" rel="noopener" style="font-size:0.8rem;">${escapeHtml(truncate(f.url, 50))}</a></td>
        <td><span class="status-pill pill-error">${escapeHtml(f.error || 'error')}</span></td>
        <td><code>${f.http_status || '—'}</code></td>
        <td><strong>x${f.retry_count || 0}</strong></td>
        <td>
          <div style="display:flex; gap:0.4rem;">
            <button class="btn btn-sm btn-primary" onclick="retrySingleUrl('${encodeURIComponent(f.url)}')">Reintentar</button>
            <button class="btn btn-sm btn-danger" onclick="deleteDoc('${encodeURIComponent(f.url)}')">🗑️</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    showToast('Error cargando diagnósticos: ' + err.message, 'error');
  }
}

async function retrySingleUrl(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  try {
    await api('/api/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    showToast('URL reencolada para reintento', 'success');
    loadErrors();
    refreshDashboard();
  } catch (e) {
    showToast('Error reintentando: ' + e.message, 'error');
  }
}

async function retryAllFailed() {
  const client = getSelectedClient();
  try {
    const res = await api('/api/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client, all: true })
    });
    showToast(`Se reencolaron ${res.count || 0} URLs fallidas`, 'success');
    loadErrors();
    refreshDashboard();
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

// --- Client Configuration Settings ---
const CONFIG_METADATA = {
  max_depth: { label: 'Profundidad Máxima', desc: 'Niveles de enlaces a seguir desde la semilla' },
  delay_floor: { label: 'Delay Base (segundos)', desc: 'Espera mínima de cortesía por dominio' },
  max_pages_domain: { label: 'Tope de Páginas por Dominio', desc: 'Límite máximo de páginas a descargar' },
  render_min_chars: { label: 'Mínimo Caracteres para Render', desc: 'Si el HTML trae menos texto, activa headless JS' },
  render_min_density: { label: 'Densidad de Texto Mínima', desc: 'Ratio texto/HTML para detectar cáscaras SPA' },
  render_min_html: { label: 'Tamaño HTML para Chequeo SPA', desc: 'Bytes mínimos de HTML para evaluar densidad' },
  max_retries: { label: 'Máximo de Reintentos', desc: 'Intentos antes de enviar la URL a cuarentena' },
  boiler_ratio: { label: 'Ratio de Boilerplate', desc: 'Frecuencia cross-page para eliminar cabeceras/footers' },
  workers: { label: 'Workers Paralelos', desc: 'Número de threads para descargas simultáneas' },
  fts_weights: { label: 'Pesos FTS [URL, Título, Texto]', desc: 'Cuánto manda cada columna en el BM25. Default [3,2,1]: la URL pesa triple' },
};

async function loadClientConfig() {
  const client = getSelectedClient();
  const grid = document.getElementById('configFieldsGrid');
  grid.innerHTML = '<p>Cargando ajustes...</p>';

  try {
    const cfg = await api('/api/config' + (client ? `?client=${encodeURIComponent(client)}` : ''));
    grid.innerHTML = Object.entries(cfg).map(([k, v]) => {
      const meta = CONFIG_METADATA[k] || { label: k, desc: '' };
      const valStr = Array.isArray(v) ? JSON.stringify(v) : v;
      return `
        <div class="config-item">
          <label for="cfg_${k}">${escapeHtml(meta.label)}</label>
          <div class="config-desc">${escapeHtml(meta.desc)}</div>
          <input type="text" id="cfg_${k}" data-key="${k}" value="${escapeHtml(String(valStr))}">
        </div>
      `;
    }).join('');
  } catch (err) {
    showToast('Error cargando configuración: ' + err.message, 'error');
  }
}

async function saveClientConfig() {
  const client = getSelectedClient();
  if (!client) {
    showToast('Selecciona un cliente específico para guardar sus ajustes', 'warning');
    return;
  }
  const patch = {};
  document.querySelectorAll('[id^=cfg_]').forEach(input => {
    const key = input.dataset.key;
    const raw = input.value.trim();
    if (raw.startsWith('[')) {
      try { patch[key] = JSON.parse(raw); } catch (e) { /* conserva valor actual */ }
    } else if (key === 'backoff') {
      try { patch[key] = JSON.parse(raw); } catch (e) { patch[key] = [2, 8, 30]; }
    } else if (/^[0-9]+$/.test(raw)) {
      patch[key] = parseInt(raw, 10);
    } else if (/^[0-9]*\.[0-9]+$/.test(raw)) {
      patch[key] = parseFloat(raw);
    } else {
      patch[key] = raw;
    }
  });

  try {
    await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client, cfg: patch })
    });
    showToast('Ajustes guardados correctamente para ' + client, 'success');
  } catch (err) {
    showToast('Error guardando ajustes: ' + err.message, 'error');
  }
}

// --- Maintenance Actions ---
async function triggerStripBoilerplate() {
  const client = getSelectedClient();
  if (!client) {
    showToast('Selecciona un cliente primero', 'warning');
    return;
  }
  try {
    const res = await api('/api/strip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client })
    });
    currentJobId = res.job_id;
    switchTab('crawler');
    startPolling();
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
  }
}

async function triggerReindex() {
  const client = getSelectedClient();
  const scope = client || 'todos los clientes';
  if (!confirm(`¿Re-indexar el crudo de ${scope}?\n\nRelee los archivos originales de data/raw/ con los parsers actuales y regenera documentos, índice FTS y réplicas. NO vuelve a descargar nada de la web.`)) return;
  try {
    const res = await api('/api/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client })
    });
    currentJobId = res.job_id;
    switchTab('crawler');
    startPolling();
  } catch (e) {
    showToast('Error re-indexando: ' + e.message, 'error');
  }
}

// --- Links Audit & Graph Explorer ---
async function loadLinkGraph() {
  const client = getSelectedClient();
  if (!client) {
    document.getElementById('pagerankContainer').innerHTML = '<div class="empty-state"><p>Selecciona un cliente para ver su auditoría de enlaces</p></div>';
    document.getElementById('orphansTableContainer').innerHTML = '<div class="empty-state"><p>Selecciona un cliente</p></div>';
    document.getElementById('depthHistContainer').innerHTML = '<p style="color:var(--text-dim);">Sin cliente seleccionado</p>';
    document.getElementById('genericAnchorsContainer').innerHTML = '<div class="empty-state"><p>Sin datos</p></div>';
    return;
  }

  try {
    const report = await api(`/api/graph?client=${encodeURIComponent(client)}`);
    
    document.getElementById('metricGraphEdges').textContent = (report.edges || 0).toLocaleString();
    document.getElementById('metricGraphOrphans').textContent = (report.orphans_count || 0).toLocaleString();

    // PageRank
    const prContainer = document.getElementById('pagerankContainer');
    const topPr = report.pagerank_top || [];
    if (!topPr.length) {
      prContainer.innerHTML = '<div class="empty-state"><p>No hay datos de PageRank calculados</p></div>';
    } else {
      prContainer.innerHTML = `
        <table class="data-table">
          <thead><tr><th>Página</th><th>Score</th><th>Acciones</th></tr></thead>
          <tbody>
            ${topPr.map(r => `
              <tr>
                <td>
                  <strong>${escapeHtml(r.title || r.url)}</strong><br>
                  <a href="${escapeHtml(r.url)}" target="_blank" rel="noopener" style="font-size:0.75rem;">${escapeHtml(truncate(r.url, 45))}</a>
                </td>
                <td><span class="status-pill pill-ok">${r.score}</span></td>
                <td><button class="btn btn-sm" onclick="previewDoc('${encodeURIComponent(r.url)}')">Ver</button></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

    // Depth Histogram
    const depthEl = document.getElementById('depthHistContainer');
    const depthHist = report.depth_hist || {};
    const totalPages = Object.values(depthHist).reduce((a, b) => a + b, 0) || 1;
    depthEl.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:0.5rem;">
        ${Object.entries(depthHist).sort((a, b) => +a[0] - +b[0]).map(([d, count]) => {
          const pct = Math.round((count / totalPages) * 100);
          return `
            <div>
              <div style="display:flex; justify-content:space-between; font-size:0.8rem; margin-bottom:0.2rem;">
                <span>Nivel ${d} (${d == 0 ? 'Semillas' : d + ' clics'})</span>
                <span style="font-weight:600;">${count} págs (${pct}%)</span>
              </div>
              <div style="height:6px; background:rgba(255,255,255,0.1); border-radius:3px; overflow:hidden;">
                <div style="height:100%; width:${pct}%; background:var(--accent);"></div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;

    // Generic Anchors
    const genEl = document.getElementById('genericAnchorsContainer');
    const genAnchors = report.generic_anchors || [];
    if (!genAnchors.length) {
      genEl.innerHTML = '<div class="empty-state"><p>✅ No se detectaron anchors genéricos</p></div>';
    } else {
      genEl.innerHTML = `
        <table class="data-table">
          <thead><tr><th>Texto del Enlace</th><th>Ocurrencias</th></tr></thead>
          <tbody>
            ${genAnchors.map(a => `
              <tr>
                <td><span class="status-pill pill-warning">⚠️ "${escapeHtml(a.anchor)}"</span></td>
                <td><strong>${a.count} veces</strong></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

    // Orphans Table
    const orphContainer = document.getElementById('orphansTableContainer');
    const orphans = report.orphans || [];
    if (!orphans.length) {
      orphContainer.innerHTML = '<div class="empty-state"><p>✅ Excelente: no hay páginas huérfanas</p></div>';
    } else {
      orphContainer.innerHTML = `
        <table class="data-table">
          <thead><tr><th>URL Huérfana</th><th>Acción</th></tr></thead>
          <tbody>
            ${orphans.map(u => `
              <tr>
                <td><a href="${escapeHtml(u)}" target="_blank" rel="noopener">${escapeHtml(u)}</a></td>
                <td><button class="btn btn-sm" onclick="previewDoc('${encodeURIComponent(u)}')">Ver Detalle</button></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

  } catch (err) {
    showToast('Error cargando grafo de enlaces: ' + err.message, 'error');
  }
}

// --- Media & Assets Classifier ---
async function loadAssets() {
  const client = getSelectedClient();
  const tbody = document.getElementById('assetsTableBody');
  if (!client) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;" class="empty-state"><p>Selecciona un cliente para ver sus assets</p></td></tr>';
    return;
  }
  tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;">Cargando assets...</td></tr>';

  try {
    const res = await api(`/api/assets?client=${encodeURIComponent(client)}`);
    const assets = res.assets || [];
    if (!assets.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;" class="empty-state"><p>No hay imágenes clasificadas aún. Ejecutá "Limpiar Boilerplate" para clasificar.</p></td></tr>';
      return;
    }

    tbody.innerHTML = assets.map(a => {
      let rolePill = 'pill-ok';
      if (a.role === 'chrome') rolePill = 'pill-in-progress';
      if (a.role === 'tracking') rolePill = 'pill-error';

      return `
        <tr>
          <td>
            <div style="display:flex; align-items:center; gap:0.75rem;">
              <img src="${escapeHtml(a.src)}" alt="" style="width:44px; height:44px; object-fit:cover; border-radius:6px; background:rgba(0,0,0,0.3);" onerror="this.style.display='none'">
              <a href="${escapeHtml(a.src)}" target="_blank" rel="noopener" style="font-size:0.75rem; word-break:break-all;">${escapeHtml(truncate(a.src, 50))}</a>
            </div>
          </td>
          <td><span class="status-pill ${rolePill}">${escapeHtml(a.role || 'content')}</span></td>
          <td style="color:var(--text-muted); font-size:0.8rem;">${escapeHtml(a.alt || a.caption || '—')}</td>
          <td><strong>${a.pages_count || 1}</strong> págs</td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    showToast('Error cargando assets: ' + err.message, 'error');
  }
}

// --- UI Utilities ---
function statusClass(st) {
  if (st === 'ok') return 'ok';
  if (st === 'pending') return 'pending';
  if (st === 'in_progress') return 'in-progress';
  if (st.includes('error')) return 'error';
  return 'skipped';
}

function showToast(msg, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  const icon = type === 'success' ? '✅' : (type === 'error' ? '❌' : (type === 'warning' ? '⚠️' : 'ℹ️'));
  toast.innerHTML = `<span>${icon}</span> <span>${escapeHtml(msg)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    toast.style.transition = 'all 0.25s ease';
    setTimeout(() => toast.remove(), 250);
  }, 4000);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function truncate(str, max) {
  if (!str) return '';
  return str.length > max ? str.substring(0, max) + '…' : str;
}

function formatDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function nowTime() {
  return new Date().toTimeString().split(' ')[0];
}
