# Laboratório de Captcha — Resultados

Objetivo: aumentar o acerto de leitura dos captchas do PROEIS (6 caracteres
hexadecimais coloridos, com bolinhas e linhas de ruído).

## Metodologia

1. **28 imagens reais** coletadas do PROEIS (2 lotes de 14: login + filtro),
   via endpoint `GET /api/captcha-dump`. Salvas em `images/` e `images_val/`.
2. **Gabarito por consenso**: painel de 7-8 modelos fortes (gemini-2.5-pro,
   3.1-pro, 3.5-flash, pro-latest, 3-flash) sobre imagem crua + variantes
   pré-processadas + leitura humana. Rótulo = resposta com ≥5 votos concordantes.
   Resultado: 25/28 rótulos de alta confiança.
3. **Grade de teste**: cada modelo rápido × pré-processamento × thinking budget,
   medindo **acerto exato da string de 6 chars** (não só formato válido) e latência.
   Harness em `bench.py`.

## Descobertas

- **O pré-processamento atrapalha.** Imagem crua (`off`) supera todas as variantes
  com OpenCV/Pillow (limpeza, binarização, remoção de linhas). O modelo lê melhor
  os caracteres coloridos originais do que versões "limpas" que distorcem traços.
- **O modelo é o fator dominante.** Trocar o modelo praticamente dobrou o acerto.
- **`thinkingBudget=0` deixa o flash rápido (~2s) sem perder acerto.**
- **Consenso de 3 vias não superou o modelo único** no conjunto de 28 (86% vs 86%),
  então não compensa o custo/latência extra.

## Acerto medido (28 captchas, string exata)

| Config | Acerto | Latência |
|---|---|---|
| **gemini-2.5-flash (produção antiga)** | **46%** (13/28) | ~2s |
| gemini-3.5-flash + upscale 3x | 82% (23/28) | ~2s |
| **gemini-3.5-flash + imagem crua (NOVO PADRÃO)** | **86%** (24/28) | ~2s |
| consenso 3 vias (3.5-flash off/upscale + flash-latest) | 86% (24/28) | ~2s (paralelo) |

## Decisão implementada

Padrões novos no código (`proeis_http.py`), sem custo/latência extra:

- `GEMINI_MODEL` padrão → **`gemini-3.5-flash`** (era `gemini-2.5-flash`)
- `CAPTCHA_PREPROCESS` padrão → **`off`** (era `clean`)
- `thinkingBudget=0` para modelos flash (já era o padrão)
- Prompt refinado: instrui a ignorar bolinhas/linhas e lista mais confusões (B/8, D/0)

Ganho esperado: acerto de captcha ~46% → ~86% por tentativa, mantendo ~2s.
Como o site rejeita respostas erradas e o robô re-tenta, quase dobrar o acerto
por tentativa reduz muito o número de captchas gastos por vaga.

## Reproduzir

```bash
python captcha_tests/bench.py            # sanity (conta imagens + chave)
# scripts de grade/painel: ver histórico no RESULTADOS e nos *.json salvos
```

## Atualização — modelo de produção e coleta de dataset

**Disponibilidade dos modelos (chave com billing pago):**
- gemini-3.5-flash e gemini-flash-latest: retornam **503/indisponível** — o Google
  faz *throttle* de modelos preview independentemente de pagamento. Inviáveis em
  produção (validação ao vivo com 3.5-flash deu **0 vagas**).
- **gemini-3.1-flash-lite: confiável** (100% nos testes), rápido (~1s).

**Escolha final: gemini-3.1-flash-lite** (não o 3.5-flash, apesar do acerto offline
maior, por ser instável). Validação ao vivo:
- Aceitação do site: **~30% (2.5-flash antigo) → 40% (3.1-flash-lite)**
- 28 vagas encontradas, 0 erros de rede, ~181s.

**Coleta de dataset (base de aprendizado real):**
Cada captcha resolvido é gravado no Firestore (`captcha_dataset`) com imagem crua +
resposta + veredito do site (aceito/recusado). O site é um rotulador gratuito.
Confirmado: 53 amostras rotuladas gravadas em uma única listagem.

- `GET /api/captcha-dataset/stats` — acerto por modelo, total de amostras
- `GET /api/captcha-dataset/export` — exporta amostras confirmadas p/ treino

Isso NÃO "treina o Gemini" (não é possível via API; cada chamada é stateless).
É a base para: (a) medir automaticamente qual modelo/pré-processamento acerta mais
conforme os dados crescem; (b) futuramente treinar um solver OCR próprio (CRNN) nos
dados confirmados — esse sim roda de graça e melhora a cada uso.
