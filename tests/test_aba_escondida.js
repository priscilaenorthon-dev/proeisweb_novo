// Prova que a aba em segundo plano para de bater no servidor.
//
// Medido em 03/11: o app aberto numa aba fazia ~1.500 requisicoes/dia em
// /api/session-status (ping de 45s do startSessionKeepAlive), mantendo a
// instancia do Cloud Run acordada 24h por dia — ~R$29/mes num projeto cujo
// robo sozinho custa ~R$2,40.
//
// Roda o app.js de verdade num navegador real, com fetch e timers falsos
// para nao depender do servidor nem esperar 45s por tique.
//
// Uso: node tests/test_aba_escondida.js
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const APP = path.join(__dirname, '..', 'web', 'app.js');

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.goto('about:blank');

  // Extrai so o bloco do keepalive do app.js real (sem o resto do app, que
  // depende de DOM/rotas). Se o nome da funcao mudar, o teste falha alto.
  const fonte = fs.readFileSync(APP, 'utf8');
  const ini = fonte.indexOf('let _sessionPollTimer');
  const fim = fonte.indexOf('async function checkApiStatus');
  if (ini < 0 || fim < 0 || fim <= ini) {
    console.error('FALHOU  nao achei o bloco do keepalive em web/app.js');
    process.exit(1);
  }
  const bloco = fonte.slice(ini, fim);

  const r = await page.evaluate(async (src) => {
    // --- dublês ---
    let chamadas = [];
    let visivel = true;
    Object.defineProperty(document, 'hidden', { get: () => !visivel, configurable: true });
    window.fetch = async (u) => {
      chamadas.push(String(u));
      return { ok: true, status: 200, json: async () => ({ logged_in: true, user_name: 'TESTE' }) };
    };
    // timers controlados: guardamos o callback do setInterval para "disparar" na mao
    let tick = null;
    const realInterval = window.setInterval;
    window.setInterval = (fn) => { tick = fn; return 1; };
    window.clearInterval = () => {};
    window.setTimeout = () => 0;   // evita o polling de 2s entrar no meio
    window.api = { get: async (u) => { chamadas.push(u); return { logged_in: true, user_name: 'TESTE' }; },
                   post: async (u) => { chamadas.push(u); return { ok: true, user_name: 'TESTE' }; } };
    window.storage = { getSettings: () => ({ reconnect_auto: false }) };
    window.state = { page: 'events' };
    window.navigate = async () => {};
    window._applySessionStatus = () => {};

    // carrega o bloco real do app.js
    // eslint-disable-next-line no-eval
    eval(src + '\nwindow.startSessionKeepAlive = startSessionKeepAlive;');

    const conta = () => chamadas.length;
    const zera = () => { chamadas = []; };

    // 1) aba VISIVEL: os tiques batem no servidor
    visivel = true;
    window.startSessionKeepAlive(false);
    zera();
    for (let i = 0; i < 5; i++) await tick();
    const visiveis = conta();

    // 2) aba ESCONDIDA: os tiques nao batem
    visivel = false;
    zera();
    for (let i = 0; i < 20; i++) await tick();
    const escondidas = conta();

    // 3) ao VOLTAR para a aba: atualiza na hora, sem esperar 45s
    zera();
    visivel = true;
    document.dispatchEvent(new Event('visibilitychange'));
    await new Promise((res) => realInterval.call(window, res, 50));
    const aoVoltar = conta();

    // 4) evento de visibilidade com a aba ainda escondida nao dispara nada
    zera();
    visivel = false;
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
    await new Promise((res) => realInterval.call(window, res, 50));
    const escondidaComEvento = conta();

    // 5) chamar startSessionKeepAlive varias vezes nao empilha listeners
    visivel = true;
    window.startSessionKeepAlive(false);
    window.startSessionKeepAlive(false);
    window.startSessionKeepAlive(false);
    zera();
    document.dispatchEvent(new Event('visibilitychange'));
    await new Promise((res) => realInterval.call(window, res, 50));
    const aposTresRegistros = conta();

    return { visiveis, escondidas, aoVoltar, escondidaComEvento, aposTresRegistros };
  }, bloco);

  await browser.close();

  let ok = true;
  const check = (cond, msg) => {
    console.log((cond ? 'PASSOU  ' : 'FALHOU  ') + msg);
    if (!cond) ok = false;
  };

  console.log(`aba visivel  : ${r.visiveis} chamada(s) em 5 tiques`);
  console.log(`aba escondida: ${r.escondidas} chamada(s) em 20 tiques`);
  console.log('');

  check(r.visiveis === 5, `aba em primeiro plano continua atualizando (${r.visiveis} de 5 tiques)`);
  check(r.escondidas === 0, `aba escondida NAO bate no servidor (${r.escondidas} chamadas em 20 tiques)`);
  check(r.aoVoltar >= 1, `ao voltar para a aba, atualiza na hora (${r.aoVoltar} chamada)`);
  check(r.escondidaComEvento === 0, 'evento de visibilidade com a aba escondida nao dispara nada');
  check(r.aposTresRegistros === 1,
    `3 chamadas de startSessionKeepAlive nao empilham listeners (${r.aposTresRegistros} chamada, nao ${r.aposTresRegistros > 1 ? r.aposTresRegistros : 3})`);

  console.log('');
  console.log('=== RESULTADO:', ok ? 'TODOS OS TESTES PASSARAM ===' : 'HOUVE FALHA ===');
  process.exit(ok ? 0 : 1);
})();
