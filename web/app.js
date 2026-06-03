/* =========================================================
   PROEIS Bot — Frontend SPA
   ========================================================= */

// ── State ─────────────────────────────────────────────────
const state = {
  page: 'events',
  options: null,
  envDefaults: null,
  running: false,
  abortController: null,
};

// ── Storage ────────────────────────────────────────────────
const storage = {
  getEvents() {
    try { return JSON.parse(localStorage.getItem('proeis_events') || '[]'); }
    catch { return []; }
  },
  saveEvents(ev) { localStorage.setItem('proeis_events', JSON.stringify(ev)); },
  getSettings() {
    try { return JSON.parse(localStorage.getItem('proeis_settings') || '{}'); }
    catch { return {}; }
  },
  saveSettings(s) { localStorage.setItem('proeis_settings', JSON.stringify(s)); },
};

// ── API helpers ────────────────────────────────────────────
const api = {
  async get(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },
  async *stream(url, body, signal) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
    if (!r.ok) {
      const txt = await r.text().catch(() => '');
      throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try { yield JSON.parse(line.slice(6)); } catch { /* ignore */ }
        }
      }
    }
    if (buf.startsWith('data: ')) {
      try { yield JSON.parse(buf.slice(6)); } catch { /* ignore */ }
    }
  },
};

// ── Router ─────────────────────────────────────────────────
function navigate(page) {
  state.page = page;
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const nav = document.getElementById(`nav-${page}`);
  if (nav) nav.classList.add('active');
  renderPage();
}

// ── Options loader ─────────────────────────────────────────
async function loadOptions() {
  if (state.options) return state.options;
  try { state.options = await api.get('/api/options'); }
  catch { state.options = { convenios: [], cpas: [] }; }
  return state.options;
}

async function loadEnvDefaults() {
  if (state.envDefaults) return state.envDefaults;
  try { state.envDefaults = await api.get('/api/env-defaults'); }
  catch { state.envDefaults = {}; }
  return state.envDefaults;
}

function hasLocalCredentials(settings) {
  return Boolean(settings.login && settings.password && settings.gemini_api_key);
}

async function hasUsableCredentials(settings) {
  if (hasLocalCredentials(settings)) return true;
  const env = await loadEnvDefaults();
  return Boolean(env.has_login && env.has_password && env.has_gemini_api_key);
}

// ── Utils ──────────────────────────────────────────────────
function esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function badge(text, color) {
  const colors = {
    blue:   'bg-blue-900/50 text-blue-300 border-blue-700',
    purple: 'bg-purple-900/50 text-purple-300 border-purple-700',
    green:  'bg-green-900/50 text-green-300 border-green-700',
    orange: 'bg-orange-900/50 text-orange-300 border-orange-700',
    gray:   'bg-gray-800 text-gray-400 border-gray-600',
  };
  return `<span class="inline-flex items-center px-2 py-0.5 rounded text-xs border ${colors[color] || colors.gray} font-medium">${esc(text)}</span>`;
}

// ── Events Page ────────────────────────────────────────────
async function renderEventsPage() {
  const opts = await loadOptions();
  const events = storage.getEvents();
  const content = document.getElementById('content');

  content.innerHTML = `
    <div class="p-6">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h2 class="text-2xl font-bold text-white">Eventos</h2>
          <p class="text-gray-500 text-sm mt-1">${events.length} evento(s) cadastrado(s)</p>
        </div>
        <button onclick="openEventModal(null)" class="btn-primary">+ Adicionar Evento</button>
      </div>

      ${events.length === 0 ? `
        <div class="empty-state">
          <div class="text-5xl mb-4">📭</div>
          <p class="text-gray-400 text-lg font-medium">Nenhum evento cadastrado</p>
          <p class="text-gray-600 text-sm mt-2">Clique em "Adicionar Evento" para começar</p>
        </div>
      ` : `
        <div class="space-y-3" id="events-list">
          ${events.map((ev, i) => `
            <div class="event-card">
              <div class="flex items-start justify-between gap-4">
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2 flex-wrap mb-2">
                    ${badge(ev.convenio || '—', 'blue')}
                    ${badge(ev.cpa || '—', 'purple')}
                    ${badge(ev.disponivel === 'reserva' ? 'Reserva' : 'Titular', ev.disponivel === 'reserva' ? 'orange' : 'green')}
                  </div>
                  <div class="flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-400">
                    <span title="Data">📅 ${esc(formatListPreview(ev.data_evento, 'Próxima data disponível'))}</span>
                    ${ev.hora_evento ? `<span title="Hora">🕐 ${esc(formatListPreview(ev.hora_evento, 'Qualquer horário'))}</span>` : ''}
                    ${ev.nome_evento ? `<span title="Nome">🏷️ ${esc(ev.nome_evento)}</span>` : ''}
                    ${ev.endereco ? `<span title="Endereço">📍 ${esc(ev.endereco)}</span>` : ''}
                  </div>
                </div>
                <div class="flex gap-2 shrink-0 mt-1">
                  <button onclick="openEventModal(${i})" class="btn-icon" title="Editar">✏️</button>
                  <button onclick="deleteEvent(${i})" class="btn-icon btn-icon-red" title="Excluir">🗑️</button>
                </div>
              </div>
            </div>
          `).join('')}
        </div>
      `}
    </div>
  `;
}

function splitCsv(value) {
  return String(value || '').split(',').map(v => v.trim()).filter(Boolean);
}

function formatListPreview(value, fallback) {
  const items = splitCsv(value);
  if (items.length === 0) return fallback;
  if (items.length <= 2) return items.join(', ');
  return `${items.slice(0, 2).join(', ')} +${items.length - 2}`;
}

function toIsoDate(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
  const m = raw.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
  if (m) return `${m[3]}-${m[2]}-${m[1]}`;
  return raw;
}

function timeOptions() {
  const out = [];
  for (let h = 0; h < 24; h++) {
    for (const m of [0, 30]) {
      out.push(`${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`);
    }
  }
  return out;
}

function expandEventForRun(ev) {
  const dates = splitCsv(ev.data_evento);
  const times = splitCsv(ev.hora_evento);
  const dateList = dates.length ? dates : [''];
  const timeList = times.length ? times : [''];
  const expanded = [];
  for (const data_evento of dateList) {
    for (const hora_evento of timeList) {
      expanded.push({
        ...ev,
        data_evento,
        hora_evento,
        quantidade: 1,
        scan_rounds: 1,
        turno: '',
      });
    }
  }
  return expanded;
}

function eventModalStateFrom(ev) {
  const today = new Date();
  const selectedDates = splitCsv(ev.data_evento).map(toIsoDate).filter(Boolean);
  const parsedFirst = selectedDates[0] ? new Date(`${selectedDates[0]}T12:00:00`) : today;
  const first = Number.isNaN(parsedFirst.getTime()) ? today : parsedFirst;
  return {
    selectedDates,
    selectedTimes: splitCsv(ev.hora_evento),
    type: ev.disponivel === 'reserva' ? 'reserva' : 'nao-reserva',
    year: first.getFullYear(),
    month: first.getMonth(),
    calendarOpen: false,
    timesOpen: false,
  };
}

function initEventModalControls() {
  renderEventCalendar();
  renderEventTimes();
  updateEventModalValues();
}

function updateEventModalValues() {
  const s = state.eventModal;
  if (!s) return;
  const dates = [...s.selectedDates].sort();
  const times = [...s.selectedTimes].sort();
  const datesInput = document.getElementById('event-dates-value');
  const timesInput = document.getElementById('event-times-value');
  const datesText = document.getElementById('event-dates-text');
  const timesText = document.getElementById('event-times-text');
  const typeInput = document.getElementById('event-type-value');

  if (datesInput) datesInput.value = dates.join(',');
  if (timesInput) timesInput.value = times.join(',');
  if (typeInput) typeInput.value = s.type;
  if (datesText) datesText.textContent = dates.length ? dates.join(', ') : 'Selecione as datas';
  if (timesText) timesText.textContent = times.length ? times.join(', ') : 'Selecione os horários';
}

function toggleEventCalendarPanel() {
  const s = state.eventModal;
  if (!s) return;
  s.calendarOpen = !s.calendarOpen;
  document.getElementById('event-calendar')?.classList.toggle('hidden', !s.calendarOpen);
}

function toggleEventTimesPanel() {
  const s = state.eventModal;
  if (!s) return;
  s.timesOpen = !s.timesOpen;
  document.getElementById('event-times-options')?.classList.toggle('hidden', !s.timesOpen);
}

function changeEventCalendarMonth(delta) {
  const s = state.eventModal;
  if (!s) return;
  const d = new Date(s.year, s.month + delta, 1);
  s.year = d.getFullYear();
  s.month = d.getMonth();
  renderEventCalendar();
}

function toggleEventDate(iso) {
  const s = state.eventModal;
  if (!s) return;
  const idx = s.selectedDates.indexOf(iso);
  if (idx >= 0) s.selectedDates.splice(idx, 1);
  else s.selectedDates.push(iso);
  renderEventCalendar();
  updateEventModalValues();
}

function renderEventCalendar() {
  const s = state.eventModal;
  const el = document.getElementById('event-calendar');
  if (!s || !el) return;
  const monthNames = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro'];
  const first = new Date(s.year, s.month, 1);
  const start = new Date(s.year, s.month, 1 - first.getDay());
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const selected = s.selectedDates.includes(iso);
    const muted = d.getMonth() !== s.month;
    cells.push(`
      <button type="button" class="calendar-day ${selected ? 'selected' : ''} ${muted ? 'muted' : ''}"
        onclick="toggleEventDate('${iso}')">${d.getDate()}</button>
    `);
  }

  el.innerHTML = `
    <div class="calendar-head">
      <button type="button" class="btn-icon" onclick="changeEventCalendarMonth(-1)">‹</button>
      <strong>${monthNames[s.month]} ${s.year}</strong>
      <button type="button" class="btn-icon" onclick="changeEventCalendarMonth(1)">›</button>
    </div>
    <div class="calendar-weekdays">
      ${['D','S','T','Q','Q','S','S'].map(d => `<span>${d}</span>`).join('')}
    </div>
    <div class="calendar-grid">${cells.join('')}</div>
  `;
}

function toggleEventTime(time) {
  const s = state.eventModal;
  if (!s) return;
  const idx = s.selectedTimes.indexOf(time);
  if (idx >= 0) s.selectedTimes.splice(idx, 1);
  else s.selectedTimes.push(time);
  renderEventTimes();
  updateEventModalValues();
}

function renderEventTimes() {
  const s = state.eventModal;
  const el = document.getElementById('event-times-options');
  if (!s || !el) return;
  el.innerHTML = timeOptions().map(t => `
    <button type="button" class="time-option ${s.selectedTimes.includes(t) ? 'selected' : ''}"
      onclick="toggleEventTime('${t}')">${t}</button>
  `).join('');
}

function setEventType(type) {
  const s = state.eventModal;
  if (!s) return;
  s.type = type;
  document.querySelectorAll('.event-type-toggle button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.type === type);
  });
  updateEventModalValues();
}

async function openEventModal(index, prefill = null, afterSave = null) {
  const opts = await loadOptions();
  const events = storage.getEvents();
  const ev = index !== null ? events[index] : (prefill || {});
  const isEdit = index !== null;

  const convOptions = opts.convenios.map(c =>
    `<option value="${esc(c.label)}" ${ev.convenio === c.label ? 'selected' : ''}>${esc(c.label)}</option>`
  ).join('');
  const cpaOptions = opts.cpas.map(c =>
    `<option value="${esc(c.label)}" ${ev.cpa === c.label ? 'selected' : ''}>${esc(c.label)}</option>`
  ).join('');

  const fromVaga = !isEdit && ev._label;
  state.eventModal = eventModalStateFrom(ev);

  document.getElementById('modal-box').innerHTML = `
    <div class="event-modal">
      <div class="event-modal-head">
        <span class="text-blue-500 text-xl">▣</span>
        <h3 class="text-xl font-bold text-white">${isEdit ? 'Editar Evento' : 'Novo Evento'}</h3>
      </div>

      <form id="event-form" class="event-modal-body">
        ${fromVaga ? `<p class="text-xs text-teal-400 truncate" title="${esc(ev._label)}">📋 Vaga: ${esc(ev._label)}</p>` : ''}

        <div class="form-group">
          <label class="form-label">Nome do Evento</label>
          <input type="text" name="nome_evento" class="form-input"
            placeholder="Nome do evento"
            value="${esc(ev.nome_evento||'')}">
          <p class="form-hint">ⓘ Você pode usar * para qualquer texto desconhecido.</p>
        </div>

        <div class="form-group">
          <label class="form-label">Endereço</label>
          <input type="text" name="endereco" class="form-input"
            placeholder="Endereço do evento (Ex: RUA*CENTRO)"
            value="${esc(ev.endereco||'')}">
          <p class="form-hint">ⓘ Você pode usar * para qualquer texto desconhecido.</p>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div class="form-group">
            <label class="form-label">Convênio</label>
            <select name="convenio" class="form-select" required>
              <option value="">Não Selecionado</option>${convOptions}
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">CPA</label>
            <select name="cpa" class="form-select" required>
              <option value="">Não Selecionado</option>${cpaOptions}
            </select>
          </div>
        </div>

        <div class="form-group">
          <label class="form-label">Datas</label>
          <button type="button" id="event-dates-text" class="selection-field" onclick="toggleEventCalendarPanel()">Selecione as datas</button>
          <input type="hidden" name="data_evento" id="event-dates-value">
          <div id="event-calendar" class="calendar-panel hidden"></div>
        </div>

        <div class="form-group">
          <label class="form-label">Horários</label>
          <button type="button" id="event-times-text" class="selection-field" onclick="toggleEventTimesPanel()">Selecione os horários</button>
          <input type="hidden" name="hora_evento" id="event-times-value">
          <div id="event-times-options" class="time-options hidden"></div>
        </div>

        <div class="form-group">
          <label class="form-label">Tipo de Vaga</label>
          <div class="event-type-toggle">
            <button type="button" data-type="nao-reserva" class="${state.eventModal.type === 'nao-reserva' ? 'active' : ''}" onclick="setEventType('nao-reserva')">Titular</button>
            <button type="button" data-type="reserva" class="${state.eventModal.type === 'reserva' ? 'active' : ''}" onclick="setEventType('reserva')">Reserva</button>
          </div>
          <input type="hidden" name="disponivel" id="event-type-value">
        </div>

        <div class="event-modal-actions">
          <button type="button" onclick="closeModal()" class="btn-secondary">Cancelar</button>
          <button type="submit" class="btn-primary">${isEdit ? 'Salvar' : 'Salvar'}</button>
        </div>
      </form>
    </div>
  `;
  initEventModalControls();

  document.getElementById('event-form').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const data = Object.fromEntries(fd.entries());
    data.quantidade = 1;
    data.scan_rounds = 1;
    data.turno = '';
    const evs = storage.getEvents();
    if (isEdit) evs[index] = data;
    else evs.push(data);
    storage.saveEvents(evs);
    closeModal();
    if (afterSave) {
      afterSave();
    } else {
      renderEventsPage();
    }
  });

  openModal();
}

function deleteEvent(index) {
  if (!confirm('Excluir este evento?')) return;
  const evs = storage.getEvents();
  evs.splice(index, 1);
  storage.saveEvents(evs);
  renderEventsPage();
}

// ── Run Page ───────────────────────────────────────────────
let _logEl = null;

async function renderRunPage() {
  const events = storage.getEvents();
  const content = document.getElementById('content');

  if (events.length === 0) {
    content.innerHTML = `
      <div class="p-6">
        <h2 class="text-2xl font-bold text-white mb-2">Executar Automação</h2>
        <div class="empty-state mt-10">
          <div class="text-5xl mb-4">📭</div>
          <p class="text-gray-400 text-lg font-medium">Nenhum evento cadastrado</p>
          <button onclick="navigate('events')" class="btn-secondary mt-4">→ Ir para Eventos</button>
        </div>
      </div>
    `;
    return;
  }

  content.innerHTML = `
    <div class="p-6 flex flex-col gap-5" style="min-height:100%">
      <div>
        <h2 class="text-2xl font-bold text-white">Executar Automação</h2>
        <p class="text-gray-500 text-sm mt-1">Selecione os eventos e inicie a execução</p>
      </div>
      <div class="flex gap-5 flex-1">
        <div class="w-72 shrink-0 flex flex-col gap-4">
          <div class="card">
            <div class="flex items-center justify-between mb-3">
              <span class="text-sm font-semibold text-white">Eventos</span>
              <label class="text-xs text-gray-500 cursor-pointer select-none">
                <input type="checkbox" id="select-all" class="mr-1" onchange="toggleAll(this.checked)">Todos
              </label>
            </div>
            <div class="space-y-2">
              ${events.map((ev, i) => `
                <label class="flex items-start gap-2 cursor-pointer select-none">
                  <input type="checkbox" class="ev-cb mt-0.5" data-i="${i}" checked>
                  <div class="text-sm min-w-0">
                    <div class="text-gray-200 truncate font-medium">${esc(ev.convenio || '—')}</div>
                    <div class="text-gray-500 text-xs truncate">${esc(formatListPreview(ev.data_evento, 'Próxima data'))} · ${esc(ev.cpa || '—')}</div>
                  </div>
                </label>
              `).join('')}
            </div>
          </div>
          <button id="run-btn" onclick="startRun()" class="btn-primary w-full">▶ Executar</button>
          <button id="stop-btn" onclick="stopRun()" class="btn-danger w-full hidden">⏹ Parar</button>
        </div>
        <div class="flex-1 flex flex-col gap-4 min-w-0">
          <div class="card flex flex-col" style="flex:1; min-height:360px">
            <div class="flex items-center justify-between mb-3">
              <span class="text-sm font-semibold text-white">Log</span>
              <button onclick="clearLog()" class="text-xs text-gray-600 hover:text-gray-400 transition">Limpar</button>
            </div>
            <div id="log-el" class="terminal flex-1 overflow-auto">
              <span class="text-gray-600">Aguardando execução...</span>
            </div>
          </div>
          <div id="results-el" class="space-y-2 hidden"></div>
        </div>
      </div>
    </div>
  `;
  _logEl = document.getElementById('log-el');
}

// ── Listar Vagas Page ──────────────────────────────────────
let _listLogEl = null;
let _vagasStore = []; // guarda vagas da sessão atual para referenciar por índice

async function renderListarPage() {
  const opts = await loadOptions();
  const content = document.getElementById('content');

  content.innerHTML = `
    <div class="p-6 flex flex-col gap-5" style="min-height:100%">
      <div>
        <h2 class="text-2xl font-bold text-white">Listar Vagas</h2>
        <p class="text-gray-500 text-sm mt-1">Consulta todas as vagas disponíveis sem marcar</p>
      </div>
      <div class="flex gap-5 flex-1">
        <!-- Painel esquerdo -->
        <div class="w-72 shrink-0 flex flex-col gap-4">
          <div class="card">
            <span class="text-sm font-semibold text-white block mb-4">Filtros</span>
            <div class="space-y-4">
              <div class="form-group">
                <label class="form-label">Convênio *</label>
                <select id="lv-convenio" class="form-select">
                  <option value="">Selecione...</option>
                  ${opts.convenios.map(c => `<option value="${esc(c.label)}">${esc(c.label)}</option>`).join('')}
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">CPA *</label>
                <select id="lv-cpa" class="form-select">
                  <option value="">Selecione...</option>
                  ${opts.cpas.map(c => `<option value="${esc(c.label)}">${esc(c.label)}</option>`).join('')}
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">Data <span class="text-gray-600 font-normal">(opcional)</span></label>
                <input type="text" id="lv-data" class="form-input" placeholder="dd/mm/aaaa">
                <p class="text-xs text-gray-600 mt-1">Vazio → varre todas as datas</p>
              </div>
            </div>
          </div>
          <button id="list-btn" onclick="startListVagas()" class="btn-primary w-full">🔍 Listar Vagas</button>
          <button id="list-stop-btn" onclick="stopList()" class="btn-danger w-full hidden">⏹ Parar</button>
        </div>
        <!-- Painel direito -->
        <div class="flex-1 flex flex-col gap-4 min-w-0">
          <div class="card flex flex-col" style="min-height:200px; max-height:260px">
            <div class="flex items-center justify-between mb-3">
              <span class="text-sm font-semibold text-white">Log</span>
              <button onclick="clearListLog()" class="text-xs text-gray-600 hover:text-gray-400">Limpar</button>
            </div>
            <div id="list-log-el" class="terminal flex-1 overflow-auto">
              <span class="text-gray-600">Selecione convênio e CPA e clique em Listar...</span>
            </div>
          </div>
          <div id="vagas-el" class="hidden flex-1 flex flex-col">
            <div class="flex items-center justify-between mb-2">
              <span class="text-sm font-semibold text-white">Vagas encontradas</span>
              <span id="vagas-count" class="text-xs text-gray-500"></span>
            </div>
            <div class="card p-0 overflow-auto flex-1">
              <table class="vagas-table w-full">
                <thead>
                  <tr>
                    <th style="width:90px">Data</th>
                    <th style="width:70px">Hora</th>
                    <th style="width:90px">Tipo</th>
                    <th style="width:120px">Convênio</th>
                    <th style="min-width:180px">Nome do Evento</th>
                    <th style="min-width:180px">Endereço</th>
                    <th style="width:40px"></th>
                  </tr>
                </thead>
                <tbody id="vagas-body"></tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  _listLogEl = document.getElementById('list-log-el');

  // Pré-seleciona com o primeiro evento cadastrado, se houver
  const evs = storage.getEvents();
  if (evs.length > 0) {
    const first = evs[0];
    const selConv = document.getElementById('lv-convenio');
    const selCpa  = document.getElementById('lv-cpa');
    if (selConv && first.convenio) selConv.value = first.convenio;
    if (selCpa  && first.cpa)     selCpa.value  = first.cpa;
  }
}

function appendListLog(text, cls = '') {
  if (!_listLogEl) return;
  const atBottom = _listLogEl.scrollHeight - _listLogEl.clientHeight <= _listLogEl.scrollTop + 20;
  const hint = _listLogEl.querySelector('span');
  if (hint) _listLogEl.innerHTML = '';
  const d = document.createElement('div');
  d.className = `log-line ${cls}`;
  d.textContent = text;
  _listLogEl.appendChild(d);
  if (atBottom) _listLogEl.scrollTop = _listLogEl.scrollHeight;
}

function clearListLog() {
  if (_listLogEl) _listLogEl.innerHTML = '<span class="text-gray-600">Log limpo.</span>';
  const v = document.getElementById('vagas-el');
  if (v) v.classList.add('hidden');
  const vb = document.getElementById('vagas-body');
  if (vb) vb.innerHTML = '';
  const vc = document.getElementById('vagas-count');
  if (vc) vc.textContent = '';
}

async function startListVagas() {
  const settings = storage.getSettings();
  const convenio       = document.getElementById('lv-convenio')?.value || '';
  const cpa            = document.getElementById('lv-cpa')?.value || '';
  const dataEspecifica = document.getElementById('lv-data')?.value.trim() || '';

  if (!convenio || !cpa) { alert('Selecione convênio e CPA.'); return; }
  if (!(await hasUsableCredentials(settings))) {
    if (confirm('Credenciais não configuradas. Ir para Configurações?')) navigate('settings');
    return;
  }

  state.running = true;
  state.abortController = new AbortController();
  document.getElementById('list-btn')?.classList.add('hidden');
  document.getElementById('list-stop-btn')?.classList.remove('hidden');

  if (_listLogEl) _listLogEl.innerHTML = '';
  _vagasStore = [];
  const vagasEl   = document.getElementById('vagas-el');
  const vagasBody = document.getElementById('vagas-body');
  const vagasCount = document.getElementById('vagas-count');
  if (vagasEl) { vagasEl.classList.add('hidden'); }
  if (vagasBody) vagasBody.innerHTML = '';
  let totalVagas = 0;
  let _currentTipo = 'nao-reserva'; // rastreia pelo stream de log

  appendListLog(`=== Listando: ${convenio} / ${cpa} ===`, 'text-teal-400 font-semibold');

  const body = { ...settingsToBody(settings), convenio, cpa, data_especifica: dataEspecifica };

  try {
    for await (const msg of api.stream('/api/list-vagas', body, state.abortController.signal)) {
      if (!state.running) break;
      if (msg.type === 'log') {
        // Detecta tipo (reserva / titular) pelos logs da lib
        if (/\(reserva\)/i.test(msg.line))     _currentTipo = 'reserva';
        if (/\(nao-reserva\)/i.test(msg.line)) _currentTipo = 'nao-reserva';

        if (msg.line.startsWith('[VAGA]')) {
          try {
            const vaga = JSON.parse(msg.line.slice(6).trim());
            // Extrai hora do label (ex: "14h30", "22:00")
            const horaM = (vaga.label || '').match(/\b(\d{1,2})[hH:](\d{2})\b/);
            const hora = horaM ? `${horaM[1]}h${horaM[2]}` : '';
            const { nome: nomeEvento, endereco: endEvento } = parseVagaLabel(vaga.label || '');
            totalVagas++;
            if (vagasBody) {
              const vagaIdx = _vagasStore.length;
              _vagasStore.push({ ...vaga, convenio, cpa, tipo: _currentTipo, hora });
              const isReserva = _currentTipo === 'reserva';
              const tipoBadge = isReserva
                ? '<span class="badge-tipo reserva">Reserva</span>'
                : '<span class="badge-tipo titular">Titular</span>';
              const tr = document.createElement('tr');
              tr.innerHTML = `
                <td class="font-mono whitespace-nowrap">${esc(vaga.data || '—')}</td>
                <td class="font-mono whitespace-nowrap text-teal-400">${esc(hora) || '—'}</td>
                <td>${tipoBadge}</td>
                <td><div title="${esc(convenio)}">${esc(convenio)}</div><div class="text-gray-600 text-xs">${esc(cpa)}</div></td>
                <td><div title="${esc(nomeEvento)}">${esc(nomeEvento || vaga.label || '—')}</div></td>
                <td><div title="${esc(endEvento)}">${esc(endEvento || '—')}</div></td>
                <td class="text-center"><button class="btn-vaga-add" title="Criar evento" onclick="vagaParaEvento(${vagaIdx})">➕</button></td>
              `;
              vagasBody.appendChild(tr);
            }
            if (vagasEl) vagasEl.classList.remove('hidden');
            if (vagasCount) vagasCount.textContent = `${totalVagas} vaga(s)`;
          } catch { appendListLog(msg.line, 'text-gray-300'); }
        } else {
          appendListLog(msg.line, logClass(msg.line));
        }
      } else if (msg.type === 'done') {
        if (msg.status === 'erro') appendListLog(`[ERRO] ${msg.message}`, 'text-red-400');
        else appendListLog(`✓ Total: ${msg.total || totalVagas} vaga(s) encontrada(s)`, 'text-green-400');
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') appendListLog(`[ERRO] ${err.message}`, 'text-red-400');
    else appendListLog('[PARADO] Cancelado.', 'text-orange-400');
  }

  if (totalVagas === 0 && state.running) appendListLog('Nenhuma vaga encontrada.', 'text-gray-500');
  appendListLog('=== Concluído ===', 'text-teal-400 font-semibold');

  state.running = false;
  document.getElementById('list-btn')?.classList.remove('hidden');
  document.getElementById('list-stop-btn')?.classList.add('hidden');
}

function stopList() {
  state.abortController?.abort();
  state.running = false;
  document.getElementById('list-btn')?.classList.remove('hidden');
  document.getElementById('list-stop-btn')?.classList.add('hidden');
}

function toggleAll(checked) {
  document.querySelectorAll('.ev-cb').forEach(cb => { cb.checked = checked; });
}

function appendLog(text, cls = '') {
  if (!_logEl) return;
  const atBottom = _logEl.scrollHeight - _logEl.clientHeight <= _logEl.scrollTop + 20;
  const hint = _logEl.querySelector('span');
  if (hint) _logEl.innerHTML = '';
  const d = document.createElement('div');
  d.className = `log-line ${cls}`;
  d.textContent = text;
  _logEl.appendChild(d);
  if (atBottom) _logEl.scrollTop = _logEl.scrollHeight;
}

function clearLog() {
  if (_logEl) _logEl.innerHTML = '<span class="text-gray-600">Log limpo.</span>';
  const r = document.getElementById('results-el');
  if (r) { r.innerHTML = ''; r.classList.add('hidden'); }
}

// ── Executar ───────────────────────────────────────────────
async function startRun() {
  const settings = storage.getSettings();
  const allEvents = storage.getEvents();
  const selected = Array.from(document.querySelectorAll('.ev-cb:checked'))
    .map(cb => allEvents[parseInt(cb.dataset.i)])
    .flatMap(expandEventForRun);

  if (selected.length === 0) { alert('Selecione pelo menos um evento.'); return; }
  if (!(await hasUsableCredentials(settings))) {
    if (confirm('Credenciais não configuradas. Ir para Configurações?')) navigate('settings');
    return;
  }

  _setRunning(true);

  if (_logEl) _logEl.innerHTML = '';
  const resultsEl = document.getElementById('results-el');
  if (resultsEl) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); }

  appendLog(`=== Iniciando: ${selected.length} evento(s) ===`, 'text-teal-400 font-semibold');

  for (let i = 0; i < selected.length; i++) {
    if (!state.running) break;
    const ev = selected[i];
    appendLog(`\n▶ Evento ${i + 1}/${selected.length}: ${ev.convenio} / ${ev.cpa}`, 'text-yellow-400 font-semibold');

    const body = {
      ...ev,
      ...settingsToBody(settings),
      dry_run: false,
    };

    let finalStatus = 'erro';
    let finalMsg = '';

    try {
      for await (const msg of api.stream('/api/run', body, state.abortController.signal)) {
        if (!state.running) break;
        if (msg.type === 'log') {
          appendLog(msg.line, logClass(msg.line));
        } else if (msg.type === 'done') {
          finalStatus = msg.status;
          finalMsg = msg.message || '';
        }
      }
    } catch (err) {
      if (err.name === 'AbortError') { appendLog('[PARADO] Cancelado.', 'text-orange-400'); break; }
      finalStatus = 'erro'; finalMsg = err.message;
      appendLog(`[ERRO] ${err.message}`, 'text-red-400');
    }

    if (resultsEl) {
      resultsEl.classList.remove('hidden');
      const defs = {
        confirmado: { cls: 'result-ok',   icon: '✅', label: 'Confirmado' },
        dry_run:    { cls: 'result-info',  icon: '🔍', label: 'Dry Run OK' },
        pendente:   { cls: 'result-warn',  icon: '⏳', label: 'Pendente' },
        erro:       { cls: 'result-err',   icon: '❌', label: finalMsg ? `Erro: ${finalMsg}` : 'Erro' },
      };
      const d = defs[finalStatus] || defs.erro;
      const el = document.createElement('div');
      el.className = `result-badge ${d.cls}`;
      el.innerHTML = `<span>${d.icon}</span><span class="font-medium">${esc(ev.convenio)}</span><span class="text-gray-500 text-xs">${esc(ev.data_evento || 'Próx. data')}${ev.hora_evento ? ` · ${esc(ev.hora_evento)}` : ''} · ${esc(ev.cpa)}</span><span class="ml-auto text-sm">${esc(d.label)}</span>`;
      resultsEl.appendChild(el);
    }
  }

  appendLog('\n=== Finalizado ===', 'text-teal-400 font-semibold');
  _setRunning(false);
}

// ── Label parser ──────────────────────────────────────────
function parseVagaLabel(label) {
  if (!label) return { nome: '', endereco: '' };
  // Formato: "NOME HH:MM:SS [N h] ENDEREÇO" ou "NOME HHhMM ENDEREÇO"
  const m = label.match(/^(.+?)\s+(\d{1,2}:\d{2}:\d{2})\s+(\d+\s*[–\-]\s*[Cc]urso\s+)?(\d+\s*h\s+)?(.*)$/);
  if (m) {
    return {
      nome:     m[1].replace(/\s*[-–]\s*$/, '').trim(),
      endereco: (m[5] || '').trim(),
    };
  }
  // Formato HHhMM
  const m2 = label.match(/^(.+?)\s+\d{1,2}h\d{2}\s+(.*)$/i);
  if (m2) return { nome: m2[1].trim(), endereco: m2[2].trim() };
  // Sem padrão: tudo como nome
  return { nome: label, endereco: '' };
}

// ── Vaga → Evento ──────────────────────────────────────────
function vagaParaEvento(idx) {
  const vaga = _vagasStore[idx];
  if (!vaga) return;

  // Tenta extrair hora do label (ex: "14h30", "14:30", "14H00")
  const horaMatch = vaga.label?.match(/\b(\d{1,2})[hH:](\d{2})\b/);
  const hora = horaMatch ? `${horaMatch[1]}h${horaMatch[2]}` : '';

  // Converte data de "dd/mm/yyyy" para o formato aceito pelo bot
  const data = vaga.data || '';

  const prefill = {
    convenio:    vaga.convenio || '',
    cpa:         vaga.cpa      || '',
    data_evento: data,
    hora_evento: '',          // vazio: não filtra por hora ao executar (mais robusto)
    disponivel:  vaga.tipo === 'reserva' ? 'reserva' : 'nao-reserva',
    quantidade:  1,
    nome_evento: '',          // vazio: não filtra por nome ao executar
    turno:       '',
    endereco:    '',
    scan_rounds: 1,
    _label:      vaga.label,
  };

  openEventModal(null, prefill, () => {
    // Fica na página Listar Vagas e mostra confirmação
    showToast('✅ Evento criado! Vagas continuam visíveis.');
  });
}

// ── Helpers ────────────────────────────────────────────────
function settingsToBody(s) {
  return {
    login:               s.login,
    password:            s.password,
    gemini_api_key:      s.gemini_api_key,
    http_attempts:       s.http_attempts || 0,
    connect_timeout:     s.connect_timeout || 0,
    read_timeout:        s.read_timeout || 0,
    filter_max_attempts: s.filter_max_attempts || 0,
  };
}

function logClass(line) {
  const lo = line.toLowerCase();
  if (lo.includes('erro') || lo.includes('falha') || lo.includes('error')) return 'text-red-400';
  if (lo.includes('confirmad') || lo.includes('marcad') || lo.includes('sucesso')) return 'text-green-400';
  if (lo.includes('captcha')) return 'text-purple-400';
  if (lo.includes('aviso') || lo.includes('warn')) return 'text-yellow-300';
  if (lo.includes('[vagas]') || lo.includes('vaga(s)')) return 'text-teal-400';
  return 'text-gray-300';
}

function _setRunning(on) {
  state.running = on;
  if (on) {
    state.abortController = new AbortController();
    document.getElementById('run-btn')?.classList.add('hidden');
    document.getElementById('stop-btn')?.classList.remove('hidden');
  } else {
    document.getElementById('run-btn')?.classList.remove('hidden');
    document.getElementById('stop-btn')?.classList.add('hidden');
  }
}

function stopRun() {
  state.abortController?.abort();
  state.running = false;
  document.getElementById('run-btn')?.classList.remove('hidden');
  document.getElementById('stop-btn')?.classList.add('hidden');
}

// ── Settings Page ──────────────────────────────────────────
async function renderSettingsPage() {
  // Carrega apenas flags/defaults seguros; segredos do servidor não são expostos no navegador.
  let envDef = {};
  try { envDef = await loadEnvDefaults(); } catch { /* offline */ }
  const saved = storage.getSettings();
  const s = {};
  const keys = ['http_attempts','connect_timeout','read_timeout','filter_max_attempts'];
  for (const k of keys) {
    s[k] = saved[k] !== undefined && saved[k] !== '' && saved[k] !== 0
      ? saved[k]
      : (envDef[k] ?? (typeof envDef[k] === 'number' ? 0 : ''));
  }
  s.login = saved.login || '';
  s.password = saved.password || '';
  s.gemini_api_key = saved.gemini_api_key || '';
  const serverCreds = envDef.has_login && envDef.has_password && envDef.has_gemini_api_key;

  document.getElementById('content').innerHTML = `
    <div class="p-6 max-w-2xl">
      <div class="mb-6">
        <h2 class="text-2xl font-bold text-white">Configurações</h2>
        <p class="text-gray-500 text-sm mt-1">Use credenciais locais ou as configuradas no servidor</p>
      </div>

      <div class="card mb-5">
        <h3 class="font-semibold text-white mb-4">🔑 Credenciais PROEIS</h3>
        ${serverCreds ? '<div class="result-badge result-info mb-4"><span>!</span><span>Credenciais configuradas no servidor. Você pode deixar estes campos em branco.</span></div>' : ''}
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div class="form-group">
            <label class="form-label">Login (Matrícula) *</label>
            <input id="s-login" type="text" class="form-input" placeholder="Ex: 51134322" value="${esc(s.login||'')}">
          </div>
          <div class="form-group">
            <label class="form-label">Senha *</label>
            <input id="s-password" type="password" class="form-input" placeholder="Sua senha" value="${esc(s.password||'')}">
          </div>
        </div>
      </div>

      <div class="card mb-5">
        <h3 class="font-semibold text-white mb-4">🤖 APIs de IA (CAPTCHA)</h3>
        <div class="space-y-4">
          <div class="form-group">
            <label class="form-label">Gemini API Key *</label>
            <input id="s-gemini" type="password" class="form-input" placeholder="AIzaSy..." value="${esc(s.gemini_api_key||'')}">
            <p class="text-xs text-gray-600 mt-1">
              Obtenha em <a href="https://aistudio.google.com/app/apikey" target="_blank" class="text-teal-400 hover:underline">Google AI Studio</a>
            </p>
          </div>
        </div>
      </div>

      <div class="card mb-6">
        <h3 class="font-semibold text-white mb-4">⚙️ Avançado <span class="text-gray-600 font-normal text-xs">(0 = usar padrão do .env)</span></h3>
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <div class="form-group">
            <label class="form-label">Tent. HTTP</label>
            <input id="s-http-attempts" type="number" class="form-input" min="0" max="10" value="${esc(String(s.http_attempts||0))}">
          </div>
          <div class="form-group">
            <label class="form-label">Timeout Conn (s)</label>
            <input id="s-connect-timeout" type="number" class="form-input" min="0" max="30" value="${esc(String(s.connect_timeout||0))}">
          </div>
          <div class="form-group">
            <label class="form-label">Timeout Read (s)</label>
            <input id="s-read-timeout" type="number" class="form-input" min="0" max="60" value="${esc(String(s.read_timeout||0))}">
          </div>
          <div class="form-group">
            <label class="form-label">Max Filtros</label>
            <input id="s-filter-attempts" type="number" class="form-input" min="0" max="20" value="${esc(String(s.filter_max_attempts||0))}">
          </div>
        </div>
      </div>

      <div class="flex flex-wrap gap-3">
        <button onclick="saveSettings()" class="btn-primary">💾 Salvar</button>
        <button onclick="testApi()" class="btn-secondary">🔗 Testar API</button>
        <button onclick="clearSettings()" class="btn-danger">🗑️ Limpar</button>
      </div>

      <div id="s-feedback" class="mt-4 hidden"></div>
    </div>
  `;
}

function saveSettings() {
  const s = {
    login:               document.getElementById('s-login')?.value || '',
    password:            document.getElementById('s-password')?.value || '',
    gemini_api_key:      document.getElementById('s-gemini')?.value || '',
    http_attempts:       parseInt(document.getElementById('s-http-attempts')?.value) || 0,
    connect_timeout:     parseInt(document.getElementById('s-connect-timeout')?.value) || 0,
    read_timeout:        parseInt(document.getElementById('s-read-timeout')?.value) || 0,
    filter_max_attempts: parseInt(document.getElementById('s-filter-attempts')?.value) || 0,
  };
  storage.saveSettings(s);
  showFeedback('✅ Configurações salvas com sucesso!', 'ok');
}

async function testApi() {
  const login        = document.getElementById('s-login')?.value || '';
  const password     = document.getElementById('s-password')?.value || '';
  const gemini_key   = document.getElementById('s-gemini')?.value || '';

  if (!login || !password || !gemini_key) {
    const env = await loadEnvDefaults();
    if (!(env.has_login && env.has_password && env.has_gemini_api_key)) {
      showFeedback('⚠️ Preencha login, senha e Gemini API Key antes de testar.', 'info');
      return;
    }
  }

  showFeedback('⏳ Fazendo login no PROEIS para verificar a conta... (aguarde ~20s)', 'info');
  try {
    const r = await fetch('/api/test-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login, password, gemini_api_key: gemini_key }),
    });
    const data = await r.json();
    if (data.ok) {
      showFeedback(`✅ Login OK! Conta: ${data.nome || data.login}`, 'ok');
    } else {
      showFeedback(`❌ Falha no login: ${data.message}`, 'err');
    }
  } catch (err) {
    showFeedback(`❌ Erro: ${err.message}`, 'err');
  }
}

function clearSettings() {
  if (!confirm('Limpar TODAS as configurações? Credenciais serão apagadas.')) return;
  localStorage.removeItem('proeis_settings');
  renderSettingsPage();
}

function showFeedback(msg, type) {
  const el = document.getElementById('s-feedback');
  if (!el) return;
  const cls = {
    ok:   'bg-green-900/30 text-green-400 border border-green-800',
    err:  'bg-red-900/30 text-red-400 border border-red-800',
    info: 'bg-blue-900/30 text-blue-400 border border-blue-800',
  }[type] || '';
  el.className = `mt-4 p-3 rounded-lg text-sm ${cls}`;
  el.textContent = msg;
  el.classList.remove('hidden');
}

// ── Toast ──────────────────────────────────────────────────
function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('toast-show'));
  setTimeout(() => { t.classList.remove('toast-show'); setTimeout(() => t.remove(), 300); }, 2800);
}

// ── Modal ──────────────────────────────────────────────────
function openModal() {
  document.getElementById('modal-overlay').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}
function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  document.body.style.overflow = '';
  state.eventModal = null;
}
document.getElementById('modal-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

// ── Serviços Marcados Page ─────────────────────────────────
async function renderServicosPage() {
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="p-6">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h2 class="text-2xl font-bold text-white">Serviços Marcados</h2>
          <p class="text-gray-500 text-sm mt-1">Serviços agendados no portal PROEIS</p>
        </div>
        <button id="srv-reload-btn" onclick="loadServicos()" class="btn-primary">🔄 Atualizar</button>
      </div>
      <div id="srv-body">
        <div class="empty-state">
          <div class="text-5xl mb-4">📅</div>
          <p class="text-gray-400">Clique em Atualizar para carregar seus serviços</p>
        </div>
      </div>
    </div>
  `;
  // Carrega automaticamente se tiver credenciais salvas
  const s = storage.getSettings();
  if (await hasUsableCredentials(s)) loadServicos();
}

async function loadServicos() {
  const s = storage.getSettings();
  if (!(await hasUsableCredentials(s))) {
    if (confirm('Credenciais não configuradas. Ir para Configurações?')) navigate('settings');
    return;
  }
  const btn = document.getElementById('srv-reload-btn');
  const body = document.getElementById('srv-body');
  if (btn) btn.disabled = true;
  if (body) body.innerHTML = '<div class="flex items-center gap-3 text-gray-400 py-10 justify-center"><span class="animate-spin text-2xl">⏳</span> Fazendo login e carregando serviços...</div>';

  try {
    const r = await fetch('/api/servicos-marcados', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login: s.login, password: s.password, gemini_api_key: s.gemini_api_key }),
    });
    const data = await r.json();

    if (!data.ok) {
      if (body) body.innerHTML = `<div class="result-badge result-err">❌ ${esc(data.message)}</div>`;
      return;
    }

    if (!data.servicos || data.servicos.length === 0) {
      if (body) body.innerHTML = `
        <div class="empty-state">
          <div class="text-5xl mb-4">🎉</div>
          <p class="text-gray-400 text-lg font-medium">Nenhum serviço agendado</p>
          ${data.nome ? `<p class="text-gray-600 text-sm mt-2">Conta: ${esc(data.nome)}</p>` : ''}
        </div>`;
      return;
    }

    if (body) body.innerHTML = `
      ${data.nome ? `<p class="text-teal-400 text-sm mb-4 font-medium">👤 ${esc(data.nome)} — ${data.servicos.length} serviço(s) agendado(s)</p>` : ''}
      <div class="space-y-3">
        ${data.servicos.map(s => `
          <div class="srv-card">
            <div class="flex items-start justify-between gap-4 flex-wrap">
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap mb-2">
                  <span class="text-white font-semibold text-base">${esc(s.evento || '—')}</span>
                  <span class="badge-tipo ${s.tipo_vaga?.toLowerCase().includes('reserva') ? 'reserva' : 'titular'}">${esc(s.tipo_vaga || '—')}</span>
                </div>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 text-sm text-gray-400">
                  ${s.data_hora ? `<span>📅 ${esc(s.data_hora)}</span>` : ''}
                  ${s.convenio  ? `<span>🏢 ${esc(s.convenio)}</span>` : ''}
                  ${s.ponto_encontro ? `<span>📍 ${esc(s.ponto_encontro)}</span>` : ''}
                  ${s.endereco  ? `<span>🗺️ ${esc(s.endereco)}</span>` : ''}
                  ${s.complemento ? `<span>🏠 ${esc(s.complemento)}</span>` : ''}
                </div>
              </div>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (err) {
    if (body) body.innerHTML = `<div class="result-badge result-err">❌ Erro: ${esc(err.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Como usar Page ─────────────────────────────────────────
async function renderHelpPage() {
  document.getElementById('content').innerHTML = `
    <div class="p-6 max-w-3xl">
      <div class="mb-6">
        <h2 class="text-2xl font-bold text-white">Como usar</h2>
        <p class="text-gray-500 text-sm mt-1">Fluxo rápido para confirmar que está tudo pronto.</p>
      </div>

      <div class="space-y-4">
        <div class="card">
          <div class="flex items-start gap-3">
            <span class="inline-flex w-7 h-7 shrink-0 items-center justify-center rounded bg-teal-900/60 text-teal-300 text-sm font-bold">1</span>
            <div class="min-w-0">
              <h3 class="font-semibold text-white">Teste a API primeiro</h3>
              <p class="text-sm text-gray-400 mt-1">
                Abra Configurações, confira login, senha e Gemini API Key, depois clique em Testar API.
                Se aparecer Login OK, o sistema está pronto.
              </p>
              <button onclick="navigate('settings')" class="btn-primary mt-3">Abrir Configurações</button>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="flex items-start gap-3">
            <span class="inline-flex w-7 h-7 shrink-0 items-center justify-center rounded bg-gray-800 text-gray-300 text-sm font-bold">2</span>
            <div class="min-w-0">
              <h3 class="font-semibold text-white">Cadastre um evento</h3>
              <p class="text-sm text-gray-400 mt-1">
                Em Eventos, informe convênio, CPA, datas, horários e tipo de vaga.
              </p>
              <button onclick="navigate('events')" class="btn-secondary mt-3">Ir para Eventos</button>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="flex items-start gap-3">
            <span class="inline-flex w-7 h-7 shrink-0 items-center justify-center rounded bg-gray-800 text-gray-300 text-sm font-bold">3</span>
            <div class="min-w-0">
              <h3 class="font-semibold text-white">Consulte antes de executar</h3>
              <p class="text-sm text-gray-400 mt-1">
                Use Listar Vagas para ver oportunidades disponíveis sem marcar nada.
              </p>
              <button onclick="navigate('listar')" class="btn-secondary mt-3">Listar Vagas</button>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="flex items-start gap-3">
            <span class="inline-flex w-7 h-7 shrink-0 items-center justify-center rounded bg-gray-800 text-gray-300 text-sm font-bold">4</span>
            <div class="min-w-0">
              <h3 class="font-semibold text-white">Execute a automação</h3>
              <p class="text-sm text-gray-400 mt-1">
                Em Executar, selecione os eventos e acompanhe o log até aparecer o resultado final.
              </p>
              <button onclick="navigate('run')" class="btn-secondary mt-3">Ir para Executar</button>
            </div>
          </div>
        </div>

        <div class="result-badge result-info">
          <span>!</span>
          <span>Se algo falhar, volte em Configurações e rode Testar API novamente.</span>
        </div>
      </div>
    </div>
  `;
}

// ── Render dispatcher ──────────────────────────────────────
async function renderPage() {
  const c = document.getElementById('content');
  c.innerHTML = '<div class="flex items-center justify-center h-full"><span class="text-gray-600">Carregando...</span></div>';
  if      (state.page === 'events')   await renderEventsPage();
  else if (state.page === 'servicos') await renderServicosPage();
  else if (state.page === 'listar')   await renderListarPage();
  else if (state.page === 'run')      await renderRunPage();
  else if (state.page === 'settings') await renderSettingsPage();
  else if (state.page === 'help')     await renderHelpPage();
  else {
    state.page = 'help';
    await renderHelpPage();
  }
}

// ── API status ─────────────────────────────────────────────
async function checkApiStatus() {
  try {
    await api.get('/api/health');
    document.getElementById('status-dot').className = 'w-2 h-2 rounded-full bg-green-400 shrink-0';
    document.getElementById('status-text').textContent = 'API online';
  } catch {
    document.getElementById('status-dot').className = 'w-2 h-2 rounded-full bg-red-400 shrink-0';
    document.getElementById('status-text').textContent = 'API offline';
  }
}

// ── Init ───────────────────────────────────────────────────
(async () => {
  navigate('events');
  checkApiStatus();
  loadOptions();
})();
