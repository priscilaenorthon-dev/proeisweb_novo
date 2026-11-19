# -*- coding: utf-8 -*-
"""Reproduz as perdas de 12/11 e prova as tres mudancas no clique.

(Em 19/11 descobriu-se que "Acao nao permitida" significa sessao que ja marcou:
esse caso especifico reloga em vez de repetir — ver test_uma_marcacao_por_login.
Aqui a repeticao na mesma tela e testada com uma recusa generica do site.)

Naquela quinta o site respondeu "Acao nao permitida" a 5 cliques em 'Eu Vou'.
A pagina devolvida AINDA trazia a vaga, mas o robo voltava ao menu e refazia o
filtro (~1s). Nesse segundo outro voluntario levou as duas MOTO PATRULHA de 1
posto (18/11 e 20/11). Em 3 das 5 recusas o captcha do clique tinha confianca
0,54-0,61. E o diagnostico dizia "a vaga sumiu" quando ela estava na tela.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bs4 import BeautifulSoup  # noqa: E402
import proeis_http as p  # noqa: E402

LOG = []
p._log = lambda tag, msg="": LOG.append(f"{tag}|{msg}")
p.emit_vaga = lambda *a, **k: None

ok = True


def check(cond, msg):
    global ok
    print(("PASSOU  " if cond else "FALHOU  ") + msg)
    if not cond:
        ok = False


LABEL = "MOTO PATRULHA SÃO FIDÉLIS 13:00:00 8 h AVENIDA EMYDIO MAIA SANTOS 1 - curso: N Eu Vou"


def tela(com_vaga=True, alerta=False, sucesso=False, texto_alerta="Nao foi possivel concluir a operacao"):
    """Tela de vagas como a do site: cada coluna numa celula separada."""
    linha = ("<tr><td>MOTO PATRULHA SÃO FIDÉLIS</td><td>13:00:00</td><td>8 h</td>"
             "<td>AVENIDA EMYDIO MAIA SANTOS</td><td>1 - curso: N</td>"
             "<td><a href=\"javascript:__doPostBack('gv$ctl02$lnk','')\">Eu Vou</a></td></tr>") if com_vaga else ""
    outra = ("<tr><td>RAS CAMPOS 12H</td><td>07:00:00</td><td>12 h</td><td>RUA X</td>"
             "<td>2 - curso: N</td><td><a href=\"javascript:__doPostBack('gv$ctl03$lnk','')\">Eu Vou</a></td></tr>")
    script = f"<script>alert('{texto_alerta}');</script>" if alerta else ""
    msg = "<span>Confirmacao no evento foi incluida com sucesso</span>" if sucesso else ""
    return BeautifulSoup(
        "<html><body><select id='ddlDataEvento' name='ddlDataEvento'>"
        "<option value='270' selected>2026-11-20</option></select>"
        f"{msg}<table>{linha}{outra}</table>{script}</body></html>", "html.parser")


# ─── 1. diagnostico: a vaga na tela tem de ser reconhecida ──────────────────
fb = p.ProeisHTTP.extract_site_feedback(tela(com_vaga=True, alerta=True, texto_alerta="Ação não permitida"), LABEL)
check(fb["alvo_presente"] is True, "recusa com a vaga na tela -> diagnostico diz que ela CONTINUA (antes dizia 'sumiu')")
fb2 = p.ProeisHTTP.extract_site_feedback(tela(com_vaga=False, alerta=True), LABEL)
check(fb2["alvo_presente"] is False, "vaga realmente fora da tela -> diagnostico diz que sumiu")
check(any("permitida" in a for a in fb["alertas"]), "o alert 'Ação não permitida' e capturado")


def novo_cliente(respostas, confs):
    """Cliente cujo site devolve `respostas` em sequencia a cada clique."""
    c = p.ProeisHTTP(login="x", password="y", gemini_api_key="", debug=False)
    c.posts = []
    c.refreshes = 0
    c.menu_gets = 0
    fila_conf = list(confs)

    def fill(soup, payload):
        c.last_captcha_conf = fila_conf.pop(0) if fila_conf else 1.0
        c._pending_captcha = {"image_b64": "aW1n", "answer": "ABC123", "model": "ocr-local", "conf": c.last_captcha_conf}
        payload["TextCaptcha"] = "ABC123"

    def post(payload):
        c.posts.append(dict(payload))
        return respostas.pop(0)

    def refresh(soup):
        c.refreshes += 1
        return tela(com_vaga=True)

    c.form_payload = lambda soup: {}
    c.fill_page_captcha = fill
    c.set_reserva_checkbox = lambda soup, payload, on: None
    c.post_form = post
    c.refresh_page_captcha = refresh
    c.audit_marking = lambda *a, **k: ""

    def verify(*a, **k):
        c.menu_gets += 1
        return False
    c.verify_marking_in_menu = verify
    c.soup = tela(com_vaga=True)
    return c


def marcar(c):
    return c.choose_target_event(
        "nao-reserva", False, data_evento="2026-11-20",
        nome_evento="MOTO PATRULHA SÃO FIDÉLIS", hora_evento="13:00", turno="", endereco="")


# ─── 2. recusa com a vaga na tela -> clica de novo NA MESMA TELA ────────────
LOG.clear()
c = novo_cliente([tela(True, alerta=True), tela(True, alerta=True), tela(False, sucesso=True)], [1.0, 1.0, 1.0])
res = marcar(c)
txt = "\n".join(LOG)
check(res is True, "duas recusas seguidas e a terceira entra: vaga MARCADA")
check(len(c.posts) == 3, f"foram 3 cliques na mesma tela ({len(c.posts)})")
check(c.menu_gets == 0, "sem voltar ao menu entre os cliques (era o ~1s que custou as vagas)")
check("CONTINUA na tela" in txt, "o log registra que repetiu o clique na mesma tela")
ctx = [s_.get("context") for s_ in c.captcha_samples]
check(ctx.count("clique-recusado") == 2 and ctx.count("clique-aceito") == 1,
      f"captchas dos cliques guardados com o veredito ({ctx})")
check(all(s_.get("image_b64") for s_ in c.captcha_samples), "a IMAGEM do captcha recusado fica guardada para conferencia")

# ─── 3. recusa e a vaga some -> para e refaz o filtro (nao insiste no vazio) ─
LOG.clear()
c = novo_cliente([tela(False, alerta=True)], [1.0])
res = marcar(c)
check(res is False and len(c.posts) == 1, "vaga sumiu da tela devolvida: 1 clique so, devolve para o lote refiltrar")
check("NAO esta mais na tela" in "\n".join(LOG), "o log diz que a vaga saiu da tela")

# ─── 4. limite de cliques na mesma tela ─────────────────────────────────────
os.environ["CLICK_SAME_PAGE_ATTEMPTS"] = "4"
c = novo_cliente([tela(True, alerta=True) for _ in range(10)], [1.0] * 10)
res = marcar(c)
check(res is False and len(c.posts) == 4, f"site recusando sempre: para em 4 cliques ({len(c.posts)}), nao trava")

# ─── 5. captcha com confianca baixa -> troca a imagem ANTES de clicar ───────
LOG.clear()
c = novo_cliente([tela(False, sucesso=True)], [0.54, 0.97])
res = marcar(c)
check(res is True and c.refreshes == 1 and len(c.posts) == 1,
      f"confianca 0,54: pediu outra imagem ({c.refreshes}x) e so entao clicou ({len(c.posts)} clique)")
check("confianca baixa" in "\n".join(LOG), "o log registra a troca por confianca baixa")

c = novo_cliente([tela(False, sucesso=True)], [0.98])
res = marcar(c)
check(res is True and c.refreshes == 0, "confianca alta (0,98): clica direto, sem troca")

c = novo_cliente([tela(False, sucesso=True)], [0.50, 0.55, 0.60])
res = marcar(c)
check(res is True and c.refreshes == 2 and len(c.posts) == 1,
      f"OCR inseguro 3 vezes: troca no maximo 2 ({c.refreshes}) e clica assim mesmo")

# ─── 6. se trocar a imagem apagar a lista, o recurso se desliga ─────────────
c = novo_cliente([tela(False, sucesso=True)], [0.50, 0.50, 0.50])
c.refresh_page_captcha = lambda soup: tela(com_vaga=False)
erro = ""
try:
    marcar(c)
except p.AutomationError as exc:
    erro = str(exc)
check("refazendo o filtro" in erro and not c.posts, "troca apagou a vaga: nao clica no escuro, refaz o filtro")
check(c._captcha_refresh_broken is True, "e desliga a troca de captcha pelo resto da rodada")
c.soup = tela(com_vaga=True)
res = marcar(c)
check(res is True and len(c.posts) == 1, "na tentativa seguinte clica direto, mesmo com confianca baixa")

print()
print("=== RESULTADO:", "TODOS OS TESTES PASSARAM ===" if ok else "HOUVE FALHA ===")
sys.exit(0 if ok else 1)
