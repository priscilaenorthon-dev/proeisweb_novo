from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Adiciona a raiz do projeto ao path para importar proeis_http.py
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from proeis_http import (  # noqa: E402
    AutomationError,
    ProeisHTTP,
    MENU_URL,
    load_env_file,
    login_with_retries,
    reparar_mojibake,
)

app = FastAPI(title="PROEIS Bot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    # Credenciais
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""
    twocaptcha_key: str = ""
    gemini_model: str = ""
    # Parâmetros do evento
    convenio: str = ""
    data_evento: str = ""
    cpa: str = ""
    disponivel: str = "nao-reserva"
    quantidade: int = 1
    nome_evento: str = ""
    hora_evento: str = ""
    turno: str = ""
    endereco: str = ""
    dry_run: bool = False
    scan_rounds: int = 1
    # Configurações avançadas (0 = usar padrão)
    http_attempts: int = 0
    connect_timeout: int = 0
    read_timeout: int = 0
    filter_max_attempts: int = 0


class ListVagasRequest(BaseModel):
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""
    twocaptcha_key: str = ""
    gemini_model: str = ""
    convenio: str = ""
    cpa: str = ""
    data_especifica: str = ""   # dd/mm/yyyy ou yyyy-mm-dd — se vazio, varre todas
    http_attempts: int = 0
    connect_timeout: int = 0
    read_timeout: int = 0
    filter_max_attempts: int = 0


class ServicosRequest(BaseModel):
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""


def _parse_servicos(raw: str) -> list[dict]:
    """Parseia o conteúdo do textarea txtEveVoluntario em registros estruturados."""
    servicos: list[dict] = []
    for block in re.split(r"={4,}", raw):
        block = reparar_mojibake(block.strip())
        if not block:
            continue
        rec: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("="):
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                val = val.strip()
                k = key.strip().lower()
                if "conv" in k:
                    rec["convenio"] = val
                elif "evento" in k:
                    rec["evento"] = val
                elif "data" in k or "hora" in k:
                    rec["data_hora"] = val
                elif "ponto" in k:
                    rec["ponto_encontro"] = val
                elif "endere" in k:
                    rec["endereco"] = val
                elif "comple" in k:
                    rec["complemento"] = val
                elif "tipo" in k:
                    rec["tipo_vaga"] = val
        if rec.get("data_hora") or rec.get("evento"):
            servicos.append(rec)
    return servicos


class TestLoginRequest(BaseModel):
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0"}


@app.post("/api/test-login")
async def test_login(body: TestLoginRequest):
    """Faz login real e retorna o nome do usuário autenticado."""
    load_env_file()
    login_val  = body.login       or os.getenv("PROEIS_LOGIN", "")
    pwd_val    = body.password    or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    if not login_val or not pwd_val or not gemini_key:
        return {"ok": False, "message": "Preencha login, senha e Gemini API Key."}

    try:
        client = ProeisHTTP(
            login=login_val,
            password=pwd_val,
            twocaptcha_key="",
            gemini_api_key=gemini_key,
            debug=False,
        )
        login_with_retries(client, "Teste de login via painel web")

        # Busca o nome do usuário no menu pós-login
        from proeis_http import MENU_URL  # noqa: E402
        soup = client.request("GET", MENU_URL)

        nome = ""
        # Tenta seletores comuns de portais ASP.NET do governo
        for sel in [
            "#lblNomeVoluntario", "#lblNome", "#lblUsuario",
            "[id*='lblNome']", "[id*='lblUsuario']", "[id*='nomeUsuario']",
            ".nome-usuario", ".nomeVoluntario",
        ]:
            el = soup.select_one(sel)
            if el:
                nome = el.get_text(strip=True)
                if nome:
                    break

        # Fallback: procura padrão "Bem-vindo, NOME" ou "Olá, NOME"
        if not nome:
            import re as _re
            txt = soup.get_text(" ", strip=True)
            m = _re.search(r"(?:bem[- ]?vindo|ol[aá])[,:]?\s+([A-ZÀ-Ú][A-Za-zÀ-ú\s]{2,40})", txt, _re.IGNORECASE)
            if m:
                nome = m.group(1).strip()

        # Último recurso: pega o primeiro heading da página
        if not nome:
            h = soup.select_one("h1, h2, h3")
            if h:
                nome = h.get_text(strip=True)[:80]

        return {
            "ok": True,
            "login": login_val,
            "nome": nome or "(nome não encontrado na página)",
        }

    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@app.post("/api/servicos-marcados")
def get_servicos_marcados(body: ServicosRequest):
    load_env_file()
    login_val  = body.login       or os.getenv("PROEIS_LOGIN", "")
    pwd_val    = body.password    or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    if not login_val or not pwd_val or not gemini_key:
        return {"ok": False, "message": "Credenciais não configuradas.", "servicos": [], "nome": ""}

    try:
        client = ProeisHTTP(
            login=login_val, password=pwd_val,
            twocaptcha_key="", gemini_api_key=gemini_key, debug=False,
        )
        login_with_retries(client, "Buscar serviços marcados")
        soup = client.request("GET", MENU_URL)

        # Nome do usuário
        nome = ""
        for sel in ["#lblNomeVoluntario", "#lblNome", "[id*='lblNome']"]:
            el = soup.select_one(sel)
            if el:
                nome = el.get_text(strip=True)
                if nome:
                    break

        # Serviços agendados
        ta = soup.select_one("#txtEveVoluntario")
        raw = ta.get_text("\n", strip=True) if ta else ""
        servicos = _parse_servicos(raw)

        return {"ok": True, "nome": nome, "servicos": servicos}

    except Exception as exc:
        return {"ok": False, "message": str(exc), "servicos": [], "nome": ""}


@app.get("/api/env-defaults")
def get_env_defaults():
    load_env_file()
    def _int(name: str, default: int = 0) -> int:
        try: return int(os.getenv(name, str(default)))
        except ValueError: return default
    return {
        "login":               os.getenv("PROEIS_LOGIN", ""),
        "password":            os.getenv("PROEIS_PASSWORD", ""),
        "gemini_api_key":      os.getenv("GEMINI_API_KEY", ""),
        "http_attempts":       _int("PROEIS_HTTP_ATTEMPTS"),
        "connect_timeout":     _int("PROEIS_CONNECT_TIMEOUT"),
        "read_timeout":        _int("PROEIS_READ_TIMEOUT"),
        "filter_max_attempts": _int("FILTER_MAX_ATTEMPTS"),
    }


@app.get("/api/options")
def get_options():
    options_path = ROOT / "config" / "proeis_options.json"
    data = json.loads(options_path.read_text(encoding="utf-8"))
    return data


@app.post("/api/run")
async def run_automation(body: RunRequest):
    load_env_file()

    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
    twocaptcha = body.twocaptcha_key or os.getenv("TWOCAPTCHA_API_KEY", "")

    result: dict[str, Any] = {"status": "pendente", "message": ""}
    loop = asyncio.get_event_loop()
    aqueue: asyncio.Queue = asyncio.Queue()

    def emit(msg: Optional[dict]) -> None:
        loop.call_soon_threadsafe(aqueue.put_nowait, msg)

    class _Capture:
        """Redireciona stdout/stderr para a fila SSE."""

        def write(self, data: str) -> None:
            try:
                sys.__stdout__.write(data)
                sys.__stdout__.flush()
            except Exception:
                pass
            if data.strip():
                for line in data.splitlines():
                    if line.strip():
                        emit({"type": "log", "line": line})

        def flush(self) -> None:
            try:
                sys.__stdout__.flush()
            except Exception:
                pass

        def reconfigure(self, **_: Any) -> None:
            pass

        def fileno(self) -> int:
            try:
                return sys.__stdout__.fileno()
            except Exception:
                return -1

    def _run() -> None:
        old_out = sys.stdout
        old_err = sys.stderr
        sys.stdout = _Capture()
        sys.stderr = _Capture()
        try:
            if not login_val:
                raise AutomationError("Login não configurado. Vá em Configurações.")
            if not password_val:
                raise AutomationError("Senha não configurada. Vá em Configurações.")
            if not gemini_key:
                raise AutomationError("GEMINI_API_KEY não configurada. Vá em Configurações.")
            if not body.convenio:
                raise AutomationError("Convênio não informado.")
            if not body.cpa:
                raise AutomationError("CPA não informado.")

            # Aplica configurações avançadas como variáveis de ambiente
            # (seguro em serverless porque cada invocação é isolada)
            if body.http_attempts > 0:
                os.environ["PROEIS_HTTP_ATTEMPTS"] = str(body.http_attempts)
            if body.connect_timeout > 0:
                os.environ["PROEIS_CONNECT_TIMEOUT"] = str(body.connect_timeout)
            if body.read_timeout > 0:
                os.environ["PROEIS_READ_TIMEOUT"] = str(body.read_timeout)
            if body.filter_max_attempts > 0:
                os.environ["FILTER_MAX_ATTEMPTS"] = str(body.filter_max_attempts)
            if body.gemini_model:
                os.environ["GEMINI_MODEL"] = body.gemini_model

            client = ProeisHTTP(
                login=login_val,
                password=password_val,
                twocaptcha_key=twocaptcha,
                gemini_api_key=gemini_key,
            )

            login_with_retries(client, "Login via painel web")

            # Usa sempre mark_scanning_dates — é o caminho robusto do CLI.
            # Quando data_evento está definida, start_date limita o início da varredura.
            confirmed = client.mark_scanning_dates(
                body.convenio,
                body.cpa,
                body.disponivel,
                body.quantidade,
                scan_rounds=body.scan_rounds,
                start_date=body.data_evento,
                nome_evento=body.nome_evento,
                hora_evento=body.hora_evento,
                turno=body.turno,
                endereco=body.endereco,
            )
            result["status"] = "confirmado" if confirmed >= body.quantidade else "pendente"
            result["confirmed"] = confirmed

        except AutomationError as exc:
            result["status"] = "erro"
            result["message"] = str(exc)
        except Exception as exc:
            result["status"] = "erro"
            result["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
            emit(None)  # sentinel: sinaliza fim da execução

    async def _stream():
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(aqueue.get(), timeout=55.0)
                    if item is None:
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    aviso = (
                        "[AVISO] Operação excedeu 55s. "
                        "Verifique sua conexão ou use timeouts menores nas Configurações."
                    )
                    yield (
                        f"data: {json.dumps({'type': 'log', 'line': aviso}, ensure_ascii=False)}\n\n"
                    )
                    break
        finally:
            yield f"data: {json.dumps({'type': 'done', **result}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/list-vagas")
async def list_vagas(body: ListVagasRequest):
    load_env_file()

    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
    twocaptcha = body.twocaptcha_key or os.getenv("TWOCAPTCHA_API_KEY", "")

    result: dict[str, Any] = {"status": "ok", "total": 0}
    loop = asyncio.get_event_loop()
    aqueue: asyncio.Queue = asyncio.Queue()

    def emit(msg: Optional[dict]) -> None:
        loop.call_soon_threadsafe(aqueue.put_nowait, msg)

    class _Capture:
        def write(self, data: str) -> None:
            try:
                sys.__stdout__.write(data)
                sys.__stdout__.flush()
            except Exception:
                pass
            if data.strip():
                for line in data.splitlines():
                    if line.strip():
                        emit({"type": "log", "line": line})

        def flush(self) -> None:
            try:
                sys.__stdout__.flush()
            except Exception:
                pass

        def reconfigure(self, **_: Any) -> None:
            pass

        def fileno(self) -> int:
            try:
                return sys.__stdout__.fileno()
            except Exception:
                return -1

    def _run() -> None:
        old_out = sys.stdout
        old_err = sys.stderr
        sys.stdout = _Capture()
        sys.stderr = _Capture()
        try:
            if not login_val:
                raise AutomationError("Login não configurado. Vá em Configurações.")
            if not password_val:
                raise AutomationError("Senha não configurada. Vá em Configurações.")
            if not gemini_key:
                raise AutomationError("GEMINI_API_KEY não configurada. Vá em Configurações.")
            if not body.convenio:
                raise AutomationError("Convênio não informado.")
            if not body.cpa:
                raise AutomationError("CPA não informado.")

            if body.http_attempts > 0:
                os.environ["PROEIS_HTTP_ATTEMPTS"] = str(body.http_attempts)
            if body.connect_timeout > 0:
                os.environ["PROEIS_CONNECT_TIMEOUT"] = str(body.connect_timeout)
            if body.read_timeout > 0:
                os.environ["PROEIS_READ_TIMEOUT"] = str(body.read_timeout)
            if body.filter_max_attempts > 0:
                os.environ["FILTER_MAX_ATTEMPTS"] = str(body.filter_max_attempts)
            if body.gemini_model:
                os.environ["GEMINI_MODEL"] = body.gemini_model

            client = ProeisHTTP(
                login=login_val,
                password=password_val,
                twocaptcha_key=twocaptcha,
                gemini_api_key=gemini_key,
            )

            login_with_retries(client, "Login via painel web (listagem)")
            client.navigate_to_service_page()

            if body.data_especifica:
                # Data específica: usa fill_filters e separa candidatos por tipo
                # para que o frontend detecte corretamente Titular/Reserva
                from proeis_http import emit_vaga, normalize_date_for_site, norm  # noqa: E402
                data_norm = normalize_date_for_site(body.data_especifica)
                client.fill_filters(body.convenio, data_norm, body.cpa, prefer="qualquer")
                soup = client.require_soup()
                candidates = client.available_candidates(soup, "qualquer")

                # Separa por tipo usando matches_preference (mesma lógica do proeis_http)
                reserva = [c for c in candidates if client.matches_preference(norm(c.label), "reserva")]
                titular = [c for c in candidates if not client.matches_preference(norm(c.label), "reserva")]

                for tipo_nome, grupo in [("nao-reserva", titular), ("reserva", reserva)]:
                    if grupo:
                        # Emite cabeçalho que o frontend lê para detectar o tipo
                        print(f"[VAGAS] {len(grupo)} vaga(s) encontrada(s) em {data_norm} ({tipo_nome}):")
                        for cand in grupo:
                            emit_vaga(cand.label, data_evento=data_norm, acao="Visualizacao")

                total = len(candidates)
            else:
                total = client.list_all_available_dates(body.convenio, body.cpa)

            result["total"] = total

        except AutomationError as exc:
            result["status"] = "erro"
            result["message"] = str(exc)
        except Exception as exc:
            result["status"] = "erro"
            result["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
            emit(None)

    async def _stream():
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(aqueue.get(), timeout=55.0)
                    if item is None:
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield (
                        f"data: {json.dumps({'type': 'log', 'line': '[AVISO] Operação excedeu 55s.'})}\n\n"
                    )
                    break
        finally:
            yield f"data: {json.dumps({'type': 'done', **result}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# Serve arquivos estáticos em desenvolvimento local (Vercel gerencia isso em produção)
if not os.getenv("VERCEL"):
    from starlette.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="static")

# Expõe o app ASGI para o runtime Vercel Python
handler = app
