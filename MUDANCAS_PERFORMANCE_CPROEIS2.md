# Mudancas significativas para marcar vaga com velocidade parecida ao CPROEIS2

Data da analise: 2026-06-08

Referencia analisada:

- `https://automacao5.deploy.app.br/cproeis2.html`
- `ANALISE_AUTOMACAO5_CPROEIS2.md`
- `proeis_http.py`
- `api/index.py`
- `web/app.js`
- Deploy atual: `https://proeisweb-novo-1082055415046.southamerica-east1.run.app/api/health` respondeu `status=ok`

Observacao de seguranca: credenciais reais nao devem ser gravadas neste arquivo, em logs, em commits, em `localStorage` ou em `sessionStorage`.

## Diagnostico principal

O CPROEIS2 parece muito rapido porque a marcacao critica nao comeca do zero. Pelo fluxo do JavaScript publico, a automacao agenda a conexao antes do horario alvo, abre o stream com antecedencia e deixa o backend trabalhar antes do momento da marcacao.

O ponto importante: "menos de 2 segundos" provavelmente mede o trecho depois que a automacao ja esta logada, posicionada e pronta. Nao e o tempo total incluindo login, navegacao, captcha e preparo.

Nosso sistema ainda coloca trabalho demais no caminho critico:

- O frontend executa eventos em loop, chamando `/api/run` uma vez por evento.
- Cada `/api/run` cria um novo `ProeisHTTP`.
- Cada execucao espera `_warmup_event.wait(timeout=30)`.
- Cada execucao chama `_try_restore_session()`, que faz GET em `FrmEventoAssociar.aspx` para validar sessao.
- Depois chama `mark_scanning_dates()`, que chama `navigate_to_service_page()`.
- Se o evento nao tiver data especifica, ainda faz `dates_for_convenio()` e varre datas.
- O captcha de filtro fica no caminho critico da marcacao.
- Quando existem varios eventos, sessao, navegacao, filtros e captcha podem ser repetidos demais.

## Mudanca 1: criar endpoint de execucao em lote rapida

Criar um endpoint novo:

```text
POST /api/run-batch-fast
```

Entrada sugerida:

```json
{
  "events": [
    {
      "convenio": "...",
      "cpa": "...",
      "data_evento": "dd/mm/yyyy",
      "hora_evento": "08:00",
      "nome_evento": "...",
      "endereco": "",
      "disponivel": "nao-reserva",
      "quantidade": 1
    }
  ],
  "horario": "",
  "fast_mode": true,
  "batch_window_seconds": 0,
  "batch_repeat_pause_seconds": 1
}
```

Comportamento:

- Criar apenas um `ProeisHTTP` para todos os eventos.
- Restaurar ou fazer login uma unica vez.
- Reutilizar cookies, pagina atual e filtros enquanto a execucao durar.
- Ordenar eventos por grupo: `convenio + data_evento + cpa + disponivel`.
- Para eventos do mesmo grupo, filtrar uma vez e clicar nas linhas correspondentes sem refazer login/navegacao.
- Usar a logica ja existente de `run_batch_events()` como base, mas expor isso pela API em SSE.

Motivo:

Hoje `web/app.js` executa um evento por vez chamando `/api/run`. Isso multiplica overhead. O CPROEIS2 manda a lista de eventos para uma unica stream de automacao.

## Mudanca 2: nao iniciar a preparacao no horario alvo

Criar modo "armado" para agendamento rapido.

Endpoint sugerido:

```text
POST /api/run-batch-fast
```

Quando `horario` for informado:

- Abrir a stream imediatamente.
- Restaurar sessao imediatamente.
- Validar login antes do horario alvo.
- A partir de T-300s, manter a conexao ativa.
- A partir de T-30s, navegar para `FrmEventoAssociar.aspx`.
- A partir de T-30s, pre-selecionar convenio.
- Se a data alvo ja existir, deixar o select de datas pronto.
- No horario alvo, fazer apenas o minimo necessario: consultar disponibilidade e clicar em "Eu Vou".

O arquivo `proeis_http.py` ja tem uma base importante:

- `wait_for_target_time()`
- `_try_prefill_convenio()`
- `fill_filters()` ja detecta convenio pre-selecionado e pula o POST de convenio.

Essa capacidade existe no CLI, mas nao esta exposta corretamente no fluxo web/API.

## Mudanca 3: mover captcha para o pre-armamento sempre que possivel

Hoje o tempo do captcha entra no caminho critico:

```text
horario alvo -> resolver captcha -> consultar vagas -> escolher linha -> clicar Eu Vou
```

Para chegar perto de 2 segundos depois do horario alvo, o ideal e:

```text
antes do horario -> login + navegacao + convenio + data + montar payload + resolver captcha
horario alvo -> POST pesquisa + parse candidatos + clique Eu Vou
```

Implementacao sugerida:

- Criar uma funcao `prepare_filter_payload(convenio, data_evento, cpa, prefer)`.
- Essa funcao navega, seleciona convenio, seleciona data/CPA, resolve captcha e guarda o payload pronto.
- Executar essa preparacao poucos segundos antes do alvo, por exemplo T-3s ou T-2s.
- No horario alvo, enviar o POST imediatamente.
- Se o site recusar captcha por expiracao, cair para o fluxo normal como fallback.

Risco:

- O captcha pode expirar ou ser invalidado se resolvido cedo demais.
- Por isso, preparar muito perto do horario alvo e manter fallback normal.

## Mudanca 4: exigir dados completos no modo rapido

O modo rapido nao deve varrer tudo.

Para performance, cada evento rapido deve ter:

- `convenio`
- `cpa`
- `data_evento`
- `hora_evento` ou `nome_evento` bem especifico
- `disponivel`

Se `data_evento` estiver vazio, o sistema cai em varredura:

- `dates_for_convenio()`
- multiplas datas
- multiplos captchas
- multiplas navegacoes

Isso nunca vai parecer com o CPROEIS2 em menos de 2 segundos.

Mudanca no frontend:

- Na tela principal, criar um aviso: "Modo rapido exige data definida".
- Quando o usuario criar evento a partir de "Listar Vagas", salvar data/hora exatas automaticamente.
- No botao "Rodar Automacao Agora", usar modo rapido apenas para eventos completos.
- Eventos incompletos devem ir para modo normal/varredura.

## Mudanca 5: reduzir validacoes repetidas de sessao durante execucao

Hoje cada `/api/run` faz:

- criar client
- aguardar warmup
- restaurar sessao
- validar no PROEIS
- navegar

No modo batch/armado:

- Validar sessao no inicio da stream.
- Se a sessao for valida, manter o mesmo client vivo.
- Nao chamar `_try_restore_session()` entre eventos do mesmo lote.
- Se cair sessao durante execucao, relogar uma vez e retomar pendentes.

Importante:

- Nao remover validacao de sessao globalmente.
- Apenas evitar validacao repetida dentro da mesma execucao ja autenticada.

## Mudanca 6: reusar filtros por grupo

O gargalo do PROEIS e:

- postback de convenio
- captcha de filtro
- POST de pesquisa
- parse da tabela

Se varios eventos compartilham `convenio + data_evento + cpa + disponivel`, nao devemos repetir filtro.

Implementacao:

- Agrupar eventos antes de executar.
- Para cada grupo:
  - navegar uma vez;
  - selecionar convenio/data/CPA uma vez;
  - resolver captcha uma vez;
  - consultar uma vez;
  - parsear candidatos uma vez;
  - tentar clicar nos eventos desse grupo.

O `run_batch_events()` ja tenta reutilizar `last_group`, mas o frontend atual nao usa esse caminho porque chama `/api/run` separado por evento.

## Mudanca 7: criar logs de tempo por fase

Sem telemetria por fase, fica dificil saber se o gargalo e login, captcha, navegacao ou clique.

Adicionar no resultado SSE e no log:

```text
[PERF] session_restore_ms=...
[PERF] navigate_ms=...
[PERF] convenio_post_ms=...
[PERF] captcha_solve_ms=...
[PERF] filter_post_ms=...
[PERF] parse_candidates_ms=...
[PERF] click_ms=...
[PERF] confirm_ms=...
[PERF] total_after_target_ms=...
```

Tambem retornar no `done`:

```json
{
  "type": "done",
  "status": "confirmado",
  "confirmed": 1,
  "op_id": "...",
  "perf": {
    "captcha_ms": 900,
    "site_ms": 800,
    "after_target_ms": 1700
  }
}
```

Meta:

- Separar tempo total da operacao do tempo depois do horario alvo.
- Comparar justamente com o CPROEIS2.

## Mudanca 8: ajustar timeouts e retries para caminho critico

No caminho normal, retry e bom.

No caminho critico de horario, retry com espera longa pode matar a vaga.

Recomendacao:

- Criar perfil `fast_mode`.
- Em `fast_mode`:
  - `PROEIS_HTTP_ATTEMPTS=1` durante o clique/pesquisa critica.
  - Sem `time.sleep(1)` antes de retry imediato no trecho critico.
  - Retry so depois que uma tentativa rapida falhar.
  - Fallback normal fora da janela critica.

Nao aplicar isso globalmente, apenas dentro do endpoint rapido.

## Mudanca 9: manter Cloud Run quente e sessao viva

O deploy atual respondeu `/api/health` com sucesso, mas a velocidade depende de:

- Cloud Run sem cold start.
- Sessao PROEIS viva.
- Firestore acessivel.
- Solver de captcha rapido.

Confirmar no deploy:

- `min-instances=1`.
- Cloud Scheduler chamando keepalive a cada 10 minutos.
- `/api/session-keepalive` funcionando.
- `/api/session-status` nao sendo chamado em excesso pelo frontend durante execucao.

## Mudanca 10: criar tela operacional parecida com CPROEIS2, mas com modo rapido real

Interface principal:

- Loading: "Verificando sessao..."
- Login/credenciais.
- Dashboard com eventos.
- Botoes:
  - Exportar
  - Importar
  - Cadastrar
  - Agendar Automacao
  - Rodar Automacao Agora
- Tela de terminal com logs ao vivo.

Mas a diferenca tecnica importante:

- "Rodar Agora" deve chamar `/api/run-batch-fast`.
- "Agendar Automacao" deve abrir a stream antes do horario alvo e deixar o backend armado.

Nao repetir o desenho atual de executar um evento por request.

## Fluxo alvo para ficar rapido

### Rodar agora, evento completo

```text
Frontend
  -> POST /api/run-batch-fast com lista de eventos

Backend
  -> cria 1 ProeisHTTP
  -> restaura sessao 1 vez
  -> navega para tela de servico
  -> agrupa eventos
  -> para cada grupo:
       -> seleciona convenio/data/CPA
       -> resolve captcha
       -> consulta disponibilidade
       -> parseia candidatos
       -> clica Eu Vou
  -> streama logs ate done
```

### Agendado rapido

```text
Frontend
  -> usuario informa HH:MM:SS
  -> abre stream agora, nao no horario

Backend
  -> login/sessao antes do horario
  -> T-300s: conexao ativa e logs vivos
  -> T-30s: navegar e pre-selecionar convenio/data
  -> T-3s: montar payload e tentar resolver captcha
  -> T: enviar POST de pesquisa
  -> T+0.x: parsear candidatos
  -> T+1.x: clicar Eu Vou
  -> finalizar com confirmado/pendente/erro
```

## Mudancas por arquivo

### `api/index.py`

- Adicionar modelo `BatchRunRequest`.
- Adicionar endpoint `POST /api/run-batch-fast`.
- Reusar estrutura SSE de `/api/run`.
- Criar apenas um client por lote.
- Chamar funcao batch no backend.
- Incluir `perf` no evento `done`.
- Expor opcao de horario agendado com pre-armamento.

### `proeis_http.py`

- Reaproveitar `run_batch_events()` no endpoint web.
- Extrair preparacao de filtro em funcao reutilizavel.
- Adicionar preparo de payload/captcha perto do horario alvo.
- Adicionar timers por fase.
- Evitar reset/navegacao quando grupo for o mesmo.
- Garantir que modo rapido nao faca varredura de datas sem necessidade.

### `web/app.js`

- Trocar loop de varios `/api/run` por uma chamada unica a `/api/run-batch-fast`.
- Ordenar/agrupar eventos antes de enviar.
- Mostrar quais eventos entram em modo rapido e quais cairao no modo normal.
- No agendamento, abrir stream imediatamente e deixar backend aguardar horario.
- Exibir metricas de performance no terminal.

### `web/index.html` e `web/styles.css`

- Ajustar fluxo visual parecido com CPROEIS2.
- Dashboard direto de eventos.
- Tela de execucao em terminal.
- Sem impacto direto na velocidade, mas melhora operacao.

## Criterios de aceite de performance

Para evento completo com sessao viva:

- Nao fazer login no horario critico.
- Nao chamar `/api/run` por evento.
- Nao varrer datas.
- Nao repetir postback de convenio para eventos do mesmo grupo.
- Tempo depois do horario alvo deve ser medido e exibido.
- Meta inicial: `after_target_ms <= 2500` para evento com data conhecida e captcha preparado perto do alvo.
- Meta secundaria: reduzir tempo total de evento unico quente para menos de 5 segundos.

## Ordem recomendada de implementacao

1. Adicionar telemetria `[PERF]` no fluxo atual.
2. Criar `/api/run-batch-fast` usando um unico `ProeisHTTP`.
3. Alterar frontend para usar batch em vez de loop de `/api/run`.
4. Exigir/indicar dados completos para modo rapido.
5. Expor agendamento armado pela API.
6. Implementar preparacao de filtro/captcha perto do horario.
7. Comparar logs antes/depois.

## O que nao fazer

- Nao trocar Python agora.
- Nao usar Playwright/browser completo para marcar vaga antes de esgotar otimizacoes HTTP.
- Nao salvar senha no navegador.
- Nao copiar o EventSource com JSON gigante na query string.
- Nao remover Firestore/sessao persistente.
- Nao prometer 2 segundos quando o evento nao tem data definida ou exige varredura.

## Resumo executivo

Para marcar tao rapido quanto o CPROEIS2, nosso sistema precisa parar de executar "um evento = uma request = uma sessao/navegacao/filtro". O caminho correto e uma execucao em lote, com um unico client HTTP, sessao restaurada antes, pagina pre-navegada, convenio/data pre-selecionados e captcha preparado o mais perto possivel do horario alvo.

O maior ganho esperado nao vem de trocar linguagem. Vem de tirar login, validacao, navegacao, varredura e parte do captcha do caminho critico.
