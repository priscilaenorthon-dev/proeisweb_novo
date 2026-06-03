/* Runtime fixes loaded after app.js. Keep this file ASCII-safe. */
(function () {
  const LABELS = {
    events: 'Eventos',
    servicos: 'Servi\u00e7os Marcados',
    listar: 'Listar Vagas',
    run: 'Executar',
    schedule: 'Agendar Automa\u00e7\u00e3o',
    settings: 'Configura\u00e7\u00f5es',
    help: 'Como usar',
    logs: 'Logs',
  };

  const TEXT_FIXES = [
    [new RegExp('Servi(?:\\u00c3\\u00a7|\\u00c3\\u0192\\u00c2\\u00a7)os', 'g'), 'Servi\u00e7os'],
    [new RegExp('Configura(?:\\u00c3\\u00a7|\\u00c3\\u0192\\u00c2\\u00a7)(?:\\u00c3\\u00b5|\\u00c3\\u0192\\u00c2\\u00b5)es', 'g'), 'Configura\u00e7\u00f5es'],
    [new RegExp('Automa(?:\\u00c3\\u00a7|\\u00c3\\u0192\\u00c2\\u00a7)(?:\\u00c3\\u00a3|\\u00c3\\u0192\\u00c2\\u00a3)o', 'g'), 'Automa\u00e7\u00e3o'],
    [new RegExp('Pr(?:\\u00c3\\u00b3|\\u00c3\\u0192\\u00c2\\u00b3)xima', 'g'), 'Pr\u00f3xima'],
    [new RegExp('hor(?:\\u00c3\\u00a1|\\u00c3\\u0192\\u00c2\\u00a1)rio', 'g'), 'hor\u00e1rio'],
    [new RegExp('endere(?:\\u00c3\\u00a7|\\u00c3\\u0192\\u00c2\\u00a7)o', 'g'), 'endere\u00e7o'],
    [new RegExp('n(?:\\u00c3\\u00a3|\\u00c3\\u0192\\u00c2\\u00a3)o', 'g'), 'n\u00e3o'],
    [new RegExp('opera(?:\\u00c3\\u00a7|\\u00c3\\u0192\\u00c2\\u00a7)(?:\\u00c3\\u00a3|\\u00c3\\u0192\\u00c2\\u00a3)o', 'g'), 'opera\u00e7\u00e3o'],
    [new RegExp('marca(?:\\u00c3\\u00a7|\\u00c3\\u0192\\u00c2\\u00a7)(?:\\u00c3\\u00a3|\\u00c3\\u0192\\u00c2\\u00a3)o', 'g'), 'marca\u00e7\u00e3o'],
  ];

  function safeEsc(value) {
    if (typeof esc === 'function') return esc(value);
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function stripVagaSuffix(label) {
    return String(label || '')
      .replace(/\s+(?:disponivel\s+)?reserva\s*[-\u2013]\s*curso.*$/i, '')
      .replace(/\s+\d+\s*[-\u2013]\s*curso.*$/i, '')
      .replace(/\s+eu\s+vou\s*$/i, '')
      .trim();
  }

  function repairText(value) {
    let text = String(value ?? '');
    for (const [pattern, replacement] of TEXT_FIXES) {
      text = text.replace(pattern, replacement);
    }
    return text
      .replace(new RegExp('\\u00f0\\u0178[^\\s]*', 'g'), '')
      .replace(new RegExp('\\u00c3\\u00b0\\u00c5\\u00b8[^\\s]*', 'g'), '')
      .replace(new RegExp('\\u00e2[^\\s]{0,3}', 'g'), '')
      .replace(new RegExp('\\u00c3\\u00a2[^\\s]{0,3}', 'g'), '')
      .replace(/\s{2,}/g, ' ')
      .trim();
  }

  function setNavLabel(id, label) {
    const btn = document.getElementById(`nav-${id}`);
    if (btn) btn.innerHTML = `<span>${label}</span>`;
  }

  function fixChromeText() {
    const logo = document.querySelector('#sidebar .w-9.h-9');
    if (logo) logo.textContent = 'PB';
    const title = document.querySelector('#sidebar h1');
    if (title) title.textContent = 'PROEIS Bot';
    const subtitle = document.querySelector('#sidebar p');
    if (subtitle) subtitle.textContent = 'Automa\u00e7\u00e3o RJ';
    Object.entries(LABELS).forEach(([id, label]) => setNavLabel(id, label));
  }

  function fixMainText() {
    const root = document.getElementById('content');
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const fixed = repairText(node.nodeValue);
      if (fixed && fixed !== node.nodeValue) node.nodeValue = fixed;
    }
  }

  function fixVisibleText() {
    fixChromeText();
    fixMainText();
  }

  function cleanEndereco(value) {
    return stripVagaSuffix(value)
      .replace(/^\s*\d+\s*h\b\s*/i, '')
      .replace(/\s{2,}/g, ' ')
      .trim();
  }

  window.parseVagaLabel = function parseVagaLabel(label) {
    const clean = stripVagaSuffix(label);
    if (!clean) return { nome: '', endereco: '' };
    const timeMatch = clean.match(/\b\d{1,2}(?::\d{2}(?::\d{2})?|h\d{2})\b/i);
    if (!timeMatch) return { nome: clean, endereco: '' };
    const nome = clean.slice(0, timeMatch.index).replace(/\s*[-\u2013]\s*$/, '').trim();
    const afterTime = clean.slice(timeMatch.index + timeMatch[0].length);
    return { nome, endereco: cleanEndereco(afterTime) };
  };

  window.vagaParaEvento = function vagaParaEvento(idx) {
    const vaga = _vagasStore[idx];
    if (!vaga) return;
    const horaFormatted = vaga.hora
      ? vaga.hora.replace(/^(\d{1,2})h(\d{2})$/i, (_, h, m) => `${h.padStart(2, '0')}:${m}`)
      : '';
    const parsed = window.parseVagaLabel(vaga.label || '');
    const prefill = {
      convenio: vaga.convenio || '',
      cpa: vaga.cpa || '',
      data_evento: vaga.data || '',
      hora_evento: horaFormatted,
      disponivel: vaga.tipo === 'reserva' ? 'reserva' : 'nao-reserva',
      quantidade: 1,
      nome_evento: stripVagaSuffix(vaga.nome || parsed.nome),
      turno: '',
      endereco: cleanEndereco(vaga.endereco || parsed.endereco),
      scan_rounds: 1,
      _label: vaga.label,
    };
    openEventModal(null, prefill, () => showToast('Evento criado. Vagas continuam visiveis.'));
  };

  function ensureLogsNav() {
    if (document.getElementById('nav-logs')) return;
    const settings = document.getElementById('nav-settings');
    if (!settings || !settings.parentNode) return;
    const btn = document.createElement('button');
    btn.onclick = () => window.navigate('logs');
    btn.id = 'nav-logs';
    btn.className = 'nav-item w-full';
    btn.innerHTML = '<span>Logs</span>';
    settings.parentNode.insertBefore(btn, settings);
  }

  async function renderLogsPage() {
    ensureLogsNav();
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    document.getElementById('nav-logs')?.classList.add('active');
    const content = document.getElementById('content');
    content.innerHTML = '<div class="p-6"><span class="text-gray-500">Carregando logs...</span></div>';
    let logs = [];
    try {
      const data = await api.get('/api/logs');
      logs = data.logs || [];
    } catch (err) {
      content.innerHTML = `<div class="p-6"><div class="result-badge result-error">Erro ao carregar logs: ${safeEsc(err.message)}</div></div>`;
      return;
    }
    content.innerHTML = `
      <div class="p-6">
        <div class="flex items-center justify-between mb-6">
          <div>
            <h2 class="text-2xl font-bold text-white">Logs</h2>
            <p class="text-gray-500 text-sm mt-1">${logs.length} operacao(oes) recente(s)</p>
          </div>
          <button onclick="renderLogsPage()" class="btn-secondary">Atualizar</button>
        </div>
        ${logs.length === 0 ? `
          <div class="empty-state">
            <p class="text-gray-400 text-lg font-medium">Nenhum log salvo ainda</p>
            <p class="text-gray-600 text-sm mt-2">Execute uma listagem ou marcacao para gerar o primeiro log.</p>
          </div>
        ` : `
          <div class="card overflow-hidden">
            <table class="w-full text-sm">
              <thead>
                <tr class="text-left text-gray-400 border-b border-gray-700">
                  <th class="py-3 px-3">Data</th>
                  <th class="py-3 px-3">Tipo</th>
                  <th class="py-3 px-3">Status</th>
                  <th class="py-3 px-3">ID</th>
                  <th class="py-3 px-3 text-right">Acao</th>
                </tr>
              </thead>
              <tbody>
                ${logs.map(log => `
                  <tr class="border-b border-gray-800 last:border-0">
                    <td class="py-3 px-3 text-gray-300">${safeEsc(formatLogDate(log.created_at))}</td>
                    <td class="py-3 px-3">${safeEsc(log.kind || '-')}</td>
                    <td class="py-3 px-3">${safeEsc(log.status || '-')}</td>
                    <td class="py-3 px-3 font-mono text-xs text-gray-400">${safeEsc(log.op_id || '-')}</td>
                    <td class="py-3 px-3 text-right">
                      <button class="btn-secondary py-1 px-3" onclick="openLogDetail('${safeEsc(log.op_id)}')">Abrir</button>
                    </td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
          <pre id="log-detail" class="log-box mt-4 min-h-[260px] whitespace-pre-wrap"></pre>
        `}
      </div>
    `;
  }

  window.renderLogsPage = renderLogsPage;
  window.openLogDetail = async function openLogDetail(opId) {
    const box = document.getElementById('log-detail');
    if (!box || !opId) return;
    box.textContent = 'Carregando log...';
    try {
      const data = await api.get(`/api/log-content/${encodeURIComponent(opId)}`);
      box.textContent = data.content || '(log vazio)';
    } catch (err) {
      box.textContent = `Erro ao abrir log: ${err.message}`;
    }
  };

  window.formatLogDate = function formatLogDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString('pt-BR');
  };

  const originalNavigate = window.navigate;
  window.navigate = function navigateWithLogs(page) {
    const result = page === 'logs' ? renderLogsPage() : originalNavigate(page);
    Promise.resolve(result).finally(() => setTimeout(fixVisibleText, 0));
    return result;
  };

  ensureLogsNav();
  fixVisibleText();
  setTimeout(fixVisibleText, 100);
})();
