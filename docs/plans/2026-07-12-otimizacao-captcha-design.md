# Design — Otimização de velocidade do PROEIS Bot (captcha + varredura)

Data: 2026-07-12
Status: Aprovado pela usuária

## Problema

Listar/marcar vagas está muito lento (~5 min para listar 7 datas). Medição real
de uma execução de `Listar Vagas` (08 BPM - RAS / 8º BPM - 6º CPA, todas as datas):

- 59 chamadas ao Gemini para captcha
- 40 recusadas pelo PROEIS (**68% de erro**)
- ~6 s por captcha
- Tempo total: **~296 s**

## Diagnóstico

Dois fatores que se multiplicam:

1. **`thinkingBudget=1024`** (default no código, `proeis_http.py:779`,
   `GEMINI_FLASH_THINKING_BUDGET`) faz cada captcha levar ~6 s em vez de ~1 s.
   Foi ligado no commit `ec676d9` ("perf: mudar thinking budget padrão para 1024").
2. **68% de recusa** do captcha → cada erro força nova tentativa (até
   `FILTER_MAX_ATTEMPTS=8` por data), cada uma mais um Gemini de ~6 s.

### Hipótese central

O captcha do PROEIS (ASP.NET) expira rápido. Com o solve levando ~6 s
(thinking=1024), a resposta chega **após** o captcha vencer no servidor → recusa.
Logo, `thinkingBudget=1024` pode causar **lentidão E recusa** ao mesmo tempo.
Reduzir para 0 pode melhorar os dois de uma vez.

## Referência (sistema CPROEIS em automacao4.deploy.app.br)

Sistema "primo" (mesma origem, backend PHP). Diferenças que o deixam mais rápido:

- Marca em **datas/horários específicos** escolhidos no cadastro do evento, em vez
  de varrer todas as datas → muito menos captchas.
- Casamento de nome/endereço com **curinga `*`** (ex.: `RUA*CENTRO`).
- Campo **Tolerância (minutos)** no horário.
- Pré-resolve o captcha do login em segundo plano (endpoint dedicado).
- Solver de captcha é server-side (PHP) — não observável.

## Plano aprovado

### Abordagem A (primeiro — experimento reversível, medir)
Trocar o default de `GEMINI_FLASH_THINKING_BUDGET` de `1024` → `0` no código.
Fazer deploy e **re-rodar a mesma listagem** para comparar tempo e taxa de erro.
Critério de sucesso: tempo por captcha cai para ~1 s e/ou a taxa de recusa cai.

### Abordagem C (depois — reduzir quantidade de captchas)
Trazer do sistema de referência:
- Suporte a **datas específicas** e **horários específicos** por evento (menos
  datas varridas).
- Casamento com **curinga `*`** em nome e endereço.

### Abordagem B (fallback — só se A não bastar)
Ajustes no código do captcha: prompt/pré-processamento, modelo alternativo,
lógica de retry/escalação.

## Sequência de execução

1. A: mudar default para 0, deploy, medir (self-verifica pelo log `thinkingBudget=`).
2. Avaliar resultado de A.
3. C: implementar datas/horários específicos + curinga.
4. B apenas se necessário.

## Riscos / reversão

- A é reversível (voltar o default para 1024 ou setar env var).
- Todo deploy é via push para `main` (auto-deploy no Cloud Run).
- Se após o deploy o log ainda mostrar `thinkingBudget=1024`, há um env var no
  Cloud Run sobrescrevendo → ajustar via gcloud.
