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
    if (logo) {
      logo.classList.add('proeis-logo');
    }
    const title = document.querySelector('#sidebar h1');
    if (title) title.textContent = 'PROEIS Bot';
    const subtitle = document.querySelector('#sidebar p');
    if (subtitle) subtitle.textContent = 'Automa\u00e7\u00e3o RJ';
  }

  // So repara nos de texto que realmente tem mojibake. Texto limpo (acentos normais)
  // e deixado intacto — senao o .trim() por-no comeria os espacos entre palavras e
  // elementos inline (ex.: "em <b>Eventos</b> com" viraria "emEventoscom").
  const MOJIBAKE_RE = new RegExp('[\\u00c2\\u00c3\\u00c5\\u00f0\\u0178\\u0080-\\u009f]|\\u00e2[^\\s]{0,3}');
  function fixMainText() {
    const root = document.getElementById('content');
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (!MOJIBAKE_RE.test(node.nodeValue)) continue;
      const fixed = repairText(node.nodeValue);
      if (fixed && fixed !== node.nodeValue) node.nodeValue = fixed;
    }
  }

  function fixVisibleText() {
    fixChromeText();
    fixMainText();
    moveFeedbackPanels();
  }

  function moveFeedbackPanels() {
    const runResults = document.getElementById('results-el');
    if (runResults && runResults.dataset.topMoved !== '1') {
      const page = runResults.closest('.p-6');
      const header = page?.firstElementChild;
      if (page && header) {
        runResults.dataset.topMoved = '1';
        runResults.className = 'space-y-2 hidden sticky top-4 z-20';
        page.insertBefore(runResults, header.nextSibling);
      }
    }

    const settingsFeedback = document.getElementById('s-feedback');
    if (settingsFeedback && settingsFeedback.dataset.topMoved !== '1') {
      const page = settingsFeedback.closest('.p-6');
      const header = page?.firstElementChild;
      if (page && header) {
        settingsFeedback.dataset.topMoved = '1';
        page.insertBefore(settingsFeedback, header.nextSibling);
      }
    }
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
    // ancora no botao Ajuda (o Config foi removido do app)
    const anchor = document.getElementById('nav-help');
    if (!anchor || !anchor.parentNode) return;
    const btn = document.createElement('button');
    btn.onclick = () => window.navigate('logs');
    btn.id = 'nav-logs';
    btn.className = 'nav-item w-full';
    btn.innerHTML = '<span class="nav-icon">🧾</span><span class="nav-label">Logs</span>';
    anchor.parentNode.insertBefore(btn, anchor);
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
          <div class="log-list">
            ${logs.map(log => {
              const st = (log.status || '').toLowerCase();
              const dot = st.includes('confirm') || st === 'ok' ? '#22c55e'
                        : st.includes('pend') ? '#eab308'
                        : st.includes('err') ? '#ef4444' : '#6b7280';
              return `
              <button type="button" class="log-item" onclick="openLogDetail('${safeEsc(log.op_id)}')">
                <span class="log-item-dot" style="background:${dot}"></span>
                <span class="log-item-main">
                  <span class="log-item-top">${safeEsc(log.kind || '-')} · <span style="color:${dot}">${safeEsc(log.status || '-')}</span></span>
                  <span class="log-item-sub">${safeEsc(formatLogDate(log.created_at))}</span>
                </span>
                <span class="log-item-open">Abrir ›</span>
              </button>`;
            }).join('')}
          </div>
          <pre id="log-detail" class="log-box mt-4 min-h-[260px] whitespace-pre-wrap hidden"></pre>
        `}
      </div>
    `;
  }

  window.renderLogsPage = renderLogsPage;
  window.openLogDetail = async function openLogDetail(opId) {
    const box = document.getElementById('log-detail');
    if (!box || !opId) return;
    box.classList.remove('hidden');
    box.textContent = 'Carregando log...';
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

  function injectVividStyles() {
    if (document.getElementById('proeis-vivid-styles')) return;
    const style = document.createElement('style');
    style.id = 'proeis-vivid-styles';
    style.textContent = `
      .btn-primary { background: linear-gradient(135deg,#06b6d4,#10b981) !important; color:#ffffff !important; border:0 !important; box-shadow:0 10px 24px rgba(20,184,166,.22); }
      .btn-primary:hover { filter:brightness(1.12); transform:translateY(-1px); }
      .btn-secondary { background: linear-gradient(135deg,#2563eb,#7c3aed) !important; color:#ffffff !important; border:0 !important; box-shadow:0 10px 24px rgba(37,99,235,.2); }
      .btn-secondary:hover { filter:brightness(1.12); transform:translateY(-1px); }
      .btn-danger { background: linear-gradient(135deg,#ef4444,#f97316) !important; color:#fff7ed !important; border:0 !important; box-shadow:0 10px 24px rgba(239,68,68,.2); }
      .btn-danger:hover { filter:brightness(1.1); transform:translateY(-1px); }
      .btn-default { background: linear-gradient(135deg,#f59e0b,#eab308); color:#111827; font-weight:800; padding:8px 18px; border-radius:8px; border:0; cursor:pointer; font-size:.875rem; min-height:44px; }
      .proeis-logo { background:linear-gradient(135deg,#22d3ee,#14b8a6 55%,#a855f7) !important; color:#08111f !important; font-weight:900; letter-spacing:.02em; box-shadow:0 0 0 1px rgba(255,255,255,.08),0 12px 28px rgba(20,184,166,.25); }
      .settings-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
      .setting-help { display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px; border-radius:999px; background:#1d4ed8; color:#dbeafe; font-size:12px; margin-left:6px; cursor:help; position:relative; text-transform:none; letter-spacing:0; }
      .setting-help:hover::after { content:attr(data-tip); position:absolute; left:50%; bottom:150%; transform:translateX(-50%); width:260px; background:#020617; border:1px solid #38bdf8; color:#e0f2fe; padding:10px; border-radius:8px; font-size:12px; line-height:1.35; text-transform:none; letter-spacing:0; z-index:9999; box-shadow:0 18px 36px rgba(0,0,0,.35); }
      .help-step { border:1px solid #1f2937; background:#111827; border-radius:10px; padding:16px; }
      @media (max-width: 760px) { .settings-grid { grid-template-columns:1fr; } }
    `;
    document.head.appendChild(style);
  }

  function currentDefaults(envDef) {
    const fallback = envDef?.system_defaults || {
      http_attempts: 2,
      connect_timeout: 8,
      read_timeout: 25,
      filter_max_attempts: 8,
      captcha_invalid_retries: 2,
      captcha_refresh_after_invalids: 1,
      gemini_timeout: 30,
      auto_retry_rounds: 0,
      auto_retry_wait_seconds: 300,
    };
    return fallback;
  }

  function settingInput(id, label, value, min, max, tip) {
    return `
      <div class="form-group">
        <label class="form-label">${label}<span class="setting-help" data-tip="${safeEsc(tip)}">?</span></label>
        <input id="${id}" type="number" class="form-input" min="${min}" max="${max}" value="${safeEsc(String(value ?? 0))}">
      </div>
    `;
  }

  window.applySystemDefaults = async function applySystemDefaults() {
    const envDef = await loadEnvDefaults().catch(() => ({}));
    const d = currentDefaults(envDef);
    const map = {
      's-http-attempts': d.http_attempts,
      's-connect-timeout': d.connect_timeout,
      's-read-timeout': d.read_timeout,
      's-filter-attempts': d.filter_max_attempts,
      's-captcha-retries': d.captcha_invalid_retries,
      's-captcha-refresh': d.captcha_refresh_after_invalids,
      's-gemini-timeout': d.gemini_timeout,
      's-auto-retry-rounds': d.auto_retry_rounds,
      's-auto-retry-wait': d.auto_retry_wait_seconds,
    };
    Object.entries(map).forEach(([id, value]) => {
      const el = document.getElementById(id);
      if (el) el.value = String(value);
    });
    showFeedback('Padrao do sistema aplicado. Clique em Salvar para gravar.', 'info');
  };

  window.renderSettingsPage = async function renderSettingsPageV2() {
    let envDef = {};
    try { envDef = await loadEnvDefaults(); } catch { envDef = {}; }
    const saved = storage.getSettings();
    const defaults = currentDefaults(envDef);
    const numKeys = [
      'http_attempts', 'connect_timeout', 'read_timeout', 'filter_max_attempts',
      'captcha_invalid_retries', 'captcha_refresh_after_invalids', 'gemini_timeout',
      'auto_retry_rounds', 'auto_retry_wait_seconds',
    ];
    const s = {};
    for (const k of numKeys) s[k] = saved[k] ?? envDef[k] ?? defaults[k] ?? 0;
    s.login = saved.login || '';
    s.password = saved.password || '';
    s.gemini_api_key = saved.gemini_api_key || '';
    const serverCreds = envDef.has_login && envDef.has_password && envDef.has_gemini_api_key;

    document.getElementById('content').innerHTML = `
      <div class="p-6 max-w-4xl">
        <div class="mb-6">
          <h2 class="text-2xl font-bold text-white">Configuracoes</h2>
          <p class="text-gray-400 text-sm mt-1">Credenciais, captcha, timeouts e retries usados pelo robo no Google Cloud.</p>
        </div>
        <div id="s-feedback" class="mb-4 hidden"></div>

        <div class="card mb-5">
          <h3 class="font-semibold text-white mb-4">Credenciais PROEIS</h3>
          ${serverCreds ? '<div class="result-badge result-info mb-4"><span>!</span><span>Credenciais configuradas no servidor. Voce pode deixar estes campos em branco.</span></div>' : ''}
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div class="form-group">
              <label class="form-label">Login (Matricula)</label>
              <input id="s-login" type="text" class="form-input" placeholder="Ex: 51134322" value="${safeEsc(s.login)}">
            </div>
            <div class="form-group">
              <label class="form-label">Senha</label>
              <input id="s-password" type="password" class="form-input" placeholder="Sua senha" value="${safeEsc(s.password)}">
            </div>
          </div>
        </div>

        <div class="card mb-5">
          <h3 class="font-semibold text-white mb-4">API de IA para CAPTCHA</h3>
          <div class="form-group">
            <label class="form-label">Gemini API Key</label>
            <input id="s-gemini" type="password" class="form-input" placeholder="AIzaSy..." value="${safeEsc(s.gemini_api_key)}">
            <p class="text-xs text-gray-500 mt-1">Se a chave ja estiver no servidor, pode deixar vazio.</p>
          </div>
        </div>

        <div class="card mb-6">
          <div class="flex items-center justify-between gap-3 mb-4">
            <h3 class="font-semibold text-white">Avancado</h3>
            <button type="button" onclick="applySystemDefaults()" class="btn-default">Padrao</button>
          </div>
          <p class="text-xs text-gray-500 mb-4">0 significa usar o valor definido no servidor. O botao Padrao preenche os valores atuais recomendados.</p>
          <div class="settings-grid">
            ${settingInput('s-http-attempts', 'Tent. HTTP', s.http_attempts, 0, 10, 'Numero de tentativas para cada chamada HTTP ao PROEIS. Padrao atual: 2.')}
            ${settingInput('s-connect-timeout', 'Timeout Conn (s)', s.connect_timeout, 0, 60, 'Tempo maximo para abrir conexao com o PROEIS. Padrao atual: 8 segundos.')}
            ${settingInput('s-read-timeout', 'Timeout Read (s)', s.read_timeout, 0, 120, 'Tempo maximo esperando resposta de uma pagina. Padrao atual: 25 segundos.')}
            ${settingInput('s-filter-attempts', 'Max Filtros', s.filter_max_attempts, 0, 30, 'Quantas vezes tenta aplicar filtros de convenio/data/CPA antes de desistir. Padrao atual: 8.')}
            ${settingInput('s-captcha-retries', 'Tent. CAPTCHA', s.captcha_invalid_retries, 0, 10, 'Tentativas extras quando o captcha volta invalido. Padrao atual: 2 extras, total 3 tentativas.')}
            ${settingInput('s-captcha-refresh', 'Trocar CAPTCHA apos', s.captcha_refresh_after_invalids, 0, 5, 'Depois de quantas respostas invalidas troca a imagem do captcha. Padrao atual: 1.')}
            ${settingInput('s-gemini-timeout', 'Timeout Gemini (s)', s.gemini_timeout, 0, 90, 'Tempo maximo esperando resposta do Gemini para resolver captcha. Padrao atual: 30 segundos.')}
            ${settingInput('s-auto-retry-rounds', 'Retry extra', s.auto_retry_rounds, 0, 10, 'Rodadas automaticas extras quando nao consegue confirmar vaga. Padrao atual: 0.')}
            ${settingInput('s-auto-retry-wait', 'Espera Retry (s)', s.auto_retry_wait_seconds, 0, 1800, 'Espera entre rodadas extras de retry automatico. Padrao atual: 300 segundos.')}
          </div>
        </div>

        <div class="flex flex-wrap gap-3">
          <button onclick="saveSettings()" class="btn-primary">Salvar</button>
          <button onclick="testApi()" class="btn-secondary">Testar API</button>
          <button onclick="clearSettings()" class="btn-danger">Limpar</button>
        </div>
      </div>
    `;
    fixVisibleText();
  };

  window.saveSettings = function saveSettingsV2() {
    const readInt = id => parseInt(document.getElementById(id)?.value, 10) || 0;
    const s = {
      ...storage.getSettings(),
      login: document.getElementById('s-login')?.value || '',
      password: document.getElementById('s-password')?.value || '',
      gemini_api_key: document.getElementById('s-gemini')?.value || '',
      http_attempts: readInt('s-http-attempts'),
      connect_timeout: readInt('s-connect-timeout'),
      read_timeout: readInt('s-read-timeout'),
      filter_max_attempts: readInt('s-filter-attempts'),
      captcha_invalid_retries: readInt('s-captcha-retries'),
      captcha_refresh_after_invalids: readInt('s-captcha-refresh'),
      gemini_timeout: readInt('s-gemini-timeout'),
      auto_retry_rounds: readInt('s-auto-retry-rounds'),
      auto_retry_wait_seconds: readInt('s-auto-retry-wait'),
    };
    storage.saveSettings(s);
    showFeedback('Configuracoes salvas com sucesso.', 'ok');
  };

  const originalSettingsToBody = window.settingsToBody;
  window.settingsToBody = function settingsToBodyV2(s) {
    const body = originalSettingsToBody ? originalSettingsToBody(s) : {};
    return {
      ...body,
      login: s.login,
      password: s.password,
      gemini_api_key: s.gemini_api_key,
      http_attempts: s.http_attempts || 0,
      connect_timeout: s.connect_timeout || 0,
      read_timeout: s.read_timeout || 0,
      filter_max_attempts: s.filter_max_attempts || 0,
      captcha_invalid_retries: s.captcha_invalid_retries || 0,
      captcha_refresh_after_invalids: s.captcha_refresh_after_invalids || 0,
      gemini_timeout: s.gemini_timeout || 0,
      auto_retry_rounds: s.auto_retry_rounds || 0,
      auto_retry_wait_seconds: s.auto_retry_wait_seconds || 0,
    };
  };

  window.renderHelpPage = async function renderHelpPageV2() {
    const acc = (icon, title, body) => `
      <details class="mb-2 rounded-xl border border-gray-700 bg-gray-800/40 overflow-hidden group">
        <summary class="cursor-pointer select-none px-4 py-3 flex items-center justify-between gap-3 text-white font-semibold hover:bg-gray-800/70">
          <span class="flex items-center gap-2 min-w-0"><span class="text-lg shrink-0">${icon}</span><span class="truncate">${title}</span></span>
          <span class="text-gray-500 shrink-0 transition-transform duration-200 group-open:rotate-180">&#9662;</span>
        </summary>
        <div class="px-4 pb-4 pt-1 text-sm text-gray-300 leading-relaxed space-y-2">${body}</div>
      </details>`;
    document.getElementById('content').innerHTML = `
      <div class="p-5 md:p-6 max-w-3xl mx-auto pb-16">
        <div class="mb-5">
          <h2 class="text-2xl font-bold text-white">Ajuda &amp; Como usar</h2>
          <p class="text-gray-400 text-sm mt-1">Toque em cada tópico para abrir. Tudo o que o sistema faz por você.</p>
        </div>

        <div class="rounded-xl border border-teal-600/40 bg-gradient-to-br from-teal-500/10 to-cyan-500/5 p-4 mb-5">
          <h3 class="text-white font-bold flex items-center gap-2">🌅 Receita para pegar vaga às 6h</h3>
          <ol class="mt-2 text-sm text-gray-200 space-y-1.5 list-decimal list-inside">
            <li><b>Na véspera:</b> cadastre os eventos em <b>Eventos</b> com a <b>data e hora certas</b> (evita varredura, fica mais rápido).</li>
            <li><b>Uns 5 min antes das 6h:</b> abra o app — ele <b>já loga sozinho</b>.</li>
            <li>Vá em <b>Executar</b> e <b>selecione</b> os eventos do dia.</li>
            <li><b>Toque em Executar ~20–30s ANTES das 6h00.</b> O robô fica martelando e fisga a titular no instante que abre.</li>
            <li>Deixe o <b>app aberto e na tela</b> até terminar.</li>
          </ol>
        </div>

        <h3 class="text-gray-400 text-xs uppercase tracking-wide font-semibold mb-2 mt-4">Telas do app</h3>
        ${acc('🔍', 'Vagas — listar o que está aberto', `
          <p>Escolha <b>convênio</b> e <b>CPA</b> e toque em <b>Listar vagas</b>. Deixe a data em branco para varrer todos os dias, ou informe uma data.</p>
          <p>Ele mostra <b>tudo que está disponível</b> (titular e reserva). Toque no <b>+</b> de uma vaga para já criar o evento com nome, data, hora, tipo e endereço preenchidos.</p>`)}
        ${acc('📋', 'Eventos — cadastrar (Titular ou Reserva)', `
          <p>Cada evento tem um botão <b>Titular</b> / <b>Reserva</b> — <b>você escolhe</b>.</p>
          <ul class="list-disc list-inside space-y-1">
            <li><b>Titular:</b> pega só vaga titular; <b>nunca</b> marca reserva no lugar.</li>
            <li><b>Reserva:</b> pega vaga de reserva (lista de espera).</li>
          </ul>
          <p>Coloque a <b>data e hora certas</b> para o robô ir direto (mais rápido). Sem data, ele varre os dias disponíveis.</p>`)}
        ${acc('▶️', 'Executar — marcar as vagas', `
          <p>Selecione os eventos e toque em <b>Executar</b>. Cada card mostra o <b>nome</b>, data/hora, tipo e endereço, para você diferenciar.</p>
          <p>Ele marca <b>um de cada vez</b>. O log ao vivo mostra o passo a passo e a confirmação. Quando termina, aparece o resultado e a <b>lista das vagas que apareceram no dia</b> (para você conferir nome/endereço).</p>`)}
        ${acc('🧾', 'Logs — conferir o que aconteceu', `
          <p>Mostra as execuções recentes. Toque em <b>Abrir</b> para ver o passo a passo: se marcou, qual vaga escolheu e a mensagem final.</p>
          <p>Também confira na aba <b>Serviços</b> os plantões que já estão marcados na sua conta.</p>`)}

        <h3 class="text-gray-400 text-xs uppercase tracking-wide font-semibold mb-2 mt-5">Por dentro do sistema</h3>
        ${acc('⚡', 'O que o sistema faz por você', `
          <ul class="list-disc list-inside space-y-1">
            <li><b>Login automático</b> ao abrir o app (não precisa clicar).</li>
            <li><b>Modo persistente:</b> martela até a vaga abrir (bom pro rush das 6h).</li>
            <li><b>Resistente à lentidão das 6h:</b> quando o site trava, ele larga e re-tenta rápido.</li>
            <li><b>Confere na lista de marcados</b> se a vaga entrou de verdade.</li>
            <li>Uma <b>operação por vez</b> na conta, sem se atrapalhar.</li>
          </ul>`)}
        ${acc('❓', 'Perguntas frequentes', `
          <p><b>Preciso clicar para logar?</b> Não — abrir o app já loga.</p>
          <p><b>Deu "pendente/erro", perdi a vaga?</b> Confira na aba Serviços; às vezes marcou e o site não avisou. O log final também mostra o que apareceu.</p>
          <p><b>Não achou a vaga?</b> Quase sempre é nome/data/hora do cadastro diferente do site, ou a vaga não abriu/esgotou. Compare com a lista do log.</p>
          <p><b>Deu erro no meio?</b> Deixe o app aberto e na tela durante a execução, e evite abrir outra ação ao mesmo tempo.</p>`)}

        <p class="text-center text-xs text-gray-600 mt-6">Dica: cadastre na véspera e esteja pronto uns segundos antes das 6h. 🍀</p>
      </div>
    `;
    fixVisibleText();
  };

  const originalNavigate = window.navigate;
  window.navigate = function navigateWithLogs(page) {
    let result;
    if (page === 'logs') {
      result = renderLogsPage();
    } else if (page === 'settings') {
      window.state && (window.state.page = 'settings');
      document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
      document.getElementById('nav-settings')?.classList.add('active');
      result = window.renderSettingsPage();
    } else if (page === 'help') {
      window.state && (window.state.page = 'help');
      document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
      document.getElementById('nav-help')?.classList.add('active');
      result = window.renderHelpPage();
    } else {
      result = originalNavigate(page);
    }
    Promise.resolve(result).finally(() => setTimeout(fixVisibleText, 0));
    return result;
  };

  const originalShowFeedback = window.showFeedback;
  if (typeof originalShowFeedback === 'function') {
    window.showFeedback = function showFeedbackTop(msg, type) {
      originalShowFeedback(msg, type);
      moveFeedbackPanels();
      const feedback = document.getElementById('s-feedback');
      if (feedback) feedback.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
  }

  const originalStartRun = window.startRun;
  if (typeof originalStartRun === 'function') {
    window.startRun = async function startRunTopFeedback() {
      moveFeedbackPanels();
      const result = await originalStartRun();
      moveFeedbackPanels();
      const results = document.getElementById('results-el');
      if (results && !results.classList.contains('hidden')) {
        results.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      return result;
    };
  }

  injectVividStyles();
  ensureLogsNav();
  fixVisibleText();
  setTimeout(fixVisibleText, 100);
})();
