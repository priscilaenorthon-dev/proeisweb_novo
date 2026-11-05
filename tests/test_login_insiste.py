# -*- coding: utf-8 -*-
"""Reproduz a perda de 05/11 e prova que a insistencia no login a evita.

Linha do tempo REAL daquela quinta (Brasilia):
  5h54:29  site saudavel (menu carregando em ~1s)
  5h55:12  site passa a RECUSAR CONEXAO (connect timeout, nem abre TCP)
  5h58:06  robo inicia, janela de 600s, 6 sub-eventos
  5h58:45  checagem de sessao falha
  6h00:04  robo DESISTE: login_with_retries esgotou 3 tentativas e a excecao
           matou a rodada inteira (status=erro, 0/6), com 8 min de janela sobrando
  6h02:21  o site VOLTA — 2min17s depois de o robo ter ido embora

Ou seja: a queda do site foi o gatilho, mas quem perdeu as vagas foi o robo,
por desistir na porta de entrada antes de chegar na marcacao.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proeis_http as p  # noqa: E402

# Segundos, contados do inicio da rodada (5h58:06), em que o site volta.
# 5h58:06 -> 6h02:21 = 255s.
SITE_VOLTA_EM = 255.0
JANELA = 600.0          # janela do robo das 6h
RESERVA = 120.0         # tempo guardado para marcar (SCHEDULED_LOGIN_RESERVE_SECONDS)

LOG = []
p._log = lambda tag, msg="": LOG.append(f"{tag}|{msg}")


class RelogioFalso:
    """Relogio virtual: sleep adianta o tempo em vez de esperar de verdade."""
    def __init__(self):
        self.agora = 0.0

    def monotonic(self):
        return self.agora

    def sleep(self, s):
        self.agora += s


class SiteFalso:
    """Cliente que recusa conexao enquanto o site esta fora do ar.

    Cada tentativa de login consome ~25s de tempo virtual, como no real
    (4 retries HTTP com connect timeout de 5s cada).
    """
    def __init__(self, relogio):
        self.relogio = relogio
        self.tentativas_login = 0
        self.entrou_em = None

    def _fora_do_ar(self):
        return self.relogio.agora < SITE_VOLTA_EM

    def request(self, method, url, **kw):
        self.relogio.agora += 8.0            # connect timeout da checagem de sessao
        if self._fora_do_ar():
            raise p.AutomationError(
                f"Falha de rede acessando {url}: Connection to www.proeis.rj.gov.br timed out."
            )
        return None                           # menu abriu

    def reset_session(self):
        pass

    def login_flow(self):
        self.tentativas_login += 1
        self.relogio.agora += 25.0           # 4 retries HTTP
        if self._fora_do_ar():
            raise p.AutomationError(
                "Falha de rede acessando https://www.proeis.rj.gov.br/Default.aspx: "
                "Connection to www.proeis.rj.gov.br timed out."
            )
        self.entrou_em = self.relogio.agora


# `looks_like_login_page` recebe o soup devolvido pelo request (None aqui).
p.looks_like_login_page = lambda soup: False

ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


# ─── 1. COMPORTAMENTO ANTIGO: login_with_retries(3) mata a rodada ───────────
rel = RelogioFalso()
p.time = rel
site = SiteFalso(rel)
erro_antigo = ""
try:
    p.login_with_retries(site, "Login via robo agendado")   # attempts=3 (como era)
except p.AutomationError as exc:
    erro_antigo = str(exc)
morreu_em = rel.agora

print(f"ANTIGO : desistiu em {morreu_em:.0f}s com {site.tentativas_login} tentativas; "
      f"site so voltaria em {SITE_VOLTA_EM:.0f}s")
check(bool(erro_antigo), "o jeito antigo FALHA (reproduz a perda de 05/11)")
check(morreu_em < SITE_VOLTA_EM,
      f"e desiste ANTES do site voltar ({morreu_em:.0f}s < {SITE_VOLTA_EM:.0f}s)")
check(morreu_em < JANELA * 0.4,
      f"jogando fora a maior parte da janela ({JANELA - morreu_em:.0f}s de {JANELA:.0f}s sem uso)")

# ─── 2. COMPORTAMENTO NOVO: insiste ate sobrar so a reserva ─────────────────
rel2 = RelogioFalso()
p.time = rel2
site2 = SiteFalso(rel2)
janela_login = max(60.0, JANELA - RESERVA - rel2.agora)
erro_novo = ""
try:
    p.recover_until_ready(site2, "Login via robo agendado", int(janela_login))
except p.AutomationError as exc:
    erro_novo = str(exc)
entrou = rel2.agora
sobrou = JANELA - entrou

print(f"NOVO   : entrou em {entrou:.0f}s com {site2.tentativas_login} tentativas; "
      f"sobraram {sobrou:.0f}s para marcar")
print()

check(not erro_novo, f"o jeito novo ENTRA no site (erro='{erro_novo[:60]}')")
check(site2.entrou_em is not None and site2.entrou_em >= SITE_VOLTA_EM,
      "entrou justamente depois de o site voltar")
check(site2.tentativas_login > site.tentativas_login,
      f"insistiu mais que o antigo ({site2.tentativas_login} vs {site.tentativas_login} tentativas)")
check(entrou <= janela_login + 30,
      f"entrou dentro da janela de insistencia ({entrou:.0f}s <= {janela_login:.0f}s)")
check(sobrou >= 60,
      f"sobrou tempo real para marcar ({sobrou:.0f}s) — o que hoje nao houve")

# ─── 3. Site fora do ar a rodada inteira: desiste, mas so no fim da janela ──
rel3 = RelogioFalso()
p.time = rel3
site3 = SiteFalso(rel3)
globals()['SITE_VOLTA_EM'] = 10 ** 9          # nunca volta
erro3 = ""
try:
    p.recover_until_ready(site3, "Login via robo agendado", int(JANELA - RESERVA))
except p.AutomationError as exc:
    erro3 = str(exc)
check(bool(erro3), "site fora do ar a rodada toda: ainda falha (nao trava para sempre)")
check(rel3.agora >= (JANELA - RESERVA) * 0.8,
      f"mas so desiste perto do fim da janela ({rel3.agora:.0f}s), nao em {morreu_em:.0f}s")
check(rel3.agora <= JANELA,
      f"e nunca passa da janela total ({rel3.agora:.0f}s <= {JANELA:.0f}s)")

print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)
