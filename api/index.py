from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Header, HTTPException, FastAPI
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

_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
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


class SchedulerRunRequest(BaseModel):
    events: list[RunRequest] = []


class EventListResponse(BaseModel):
    events: list[dict[str, Any]]


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


def _apply_runtime_options(body: RunRequest) -> None:
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


def _run_event_once(body: RunRequest) -> dict[str, Any]:
    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
    twocaptcha = body.twocaptcha_key or os.getenv("TWOCAPTCHA_API_KEY", "")

    if not login_val:
        raise AutomationError("Login não configurado.")
    if not password_val:
        raise AutomationError("Senha não configurada.")
    if not gemini_key:
        raise AutomationError("GEMINI_API_KEY não configurada.")
    if not body.convenio:
        raise AutomationError("Convênio não informado.")
    if not body.cpa:
        raise AutomationError("CPA não informado.")

    _apply_runtime_options(body)

    client = ProeisHTTP(
        login=login_val,
        password=password_val,
        twocaptcha_key=twocaptcha,
        gemini_api_key=gemini_key,
    )
    login_with_retries(client, "Login via agendamento Cloud Scheduler")
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
    return {
        "status": "confirmado" if confirmed >= body.quantidade else "pendente",
        "confirmed": confirmed,
        "convenio": body.convenio,
        "cpa": body.cpa,
        "data_evento": body.data_evento,
        "hora_evento": body.hora_evento,
    }


_env_lock = threading.Lock()
_LOGS_DIR = ROOT / "logs"
_SSE_PADDING = ":" + (" " * 2048) + "\n\n"


def _sse_data(item: dict[str, Any]) -> str:
    return f"data: {json.dumps(item, ensure_ascii=False)}\n\n"


class _Capture:
    """Redireciona stdout/stderr para a fila SSE e para arquivo de log da operação."""

    def __init__(self, emit_fn, log_file=None) -> None:
        self._emit = emit_fn
        self._log_file = log_file

    def write(self, data: str) -> None:
        try:
            sys.__stdout__.write(data)
            sys.__stdout__.flush()
        except Exception:
            pass
        if data.strip():
            for line in data.splitlines():
                if line.strip():
                    self._emit({"type": "log", "line": line})
                    if self._log_file:
                        try:
                            self._log_file.write(line + "\n")
                            self._log_file.flush()
                        except Exception:
                            pass

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


def _scheduled_events_from_env() -> list[RunRequest]:
    raw = os.getenv("SCHEDULED_EVENTS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AutomationError(f"SCHEDULED_EVENTS_JSON inválido: {exc}") from exc
    if isinstance(data, dict) and "events" in data:
        data = data["events"]
    elif isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise AutomationError("SCHEDULED_EVENTS_JSON deve ser um objeto, lista ou {'events': [...] }.")
    return [RunRequest(**item) for item in data]


def _firestore_db():
    try:
        from google.cloud import firestore
    except ImportError as exc:
        raise AutomationError("Dependência google-cloud-firestore não instalada.") from exc
    database = os.getenv("FIRESTORE_DATABASE", "proeis")
    return firestore.Client(database=database)


def _events_collection():
    return _firestore_db().collection(os.getenv("FIRESTORE_EVENTS_COLLECTION", "events"))


def _event_payload(body: RunRequest) -> dict[str, Any]:
    data = body.model_dump()
    data["quantidade"] = int(data.get("quantidade") or 1)
    data["scan_rounds"] = int(data.get("scan_rounds") or 1)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return data


def _doc_to_event(doc) -> dict[str, Any]:
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return data


def _stored_events() -> list[dict[str, Any]]:
    query = _events_collection().order_by("created_at")
    return [_doc_to_event(doc) for doc in query.stream()]


def _scheduled_events_from_firestore() -> list[RunRequest]:
    return [RunRequest(**{k: v for k, v in item.items() if k != "id"}) for item in _stored_events()]


class TestLoginRequest(BaseModel):
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""


@app.get("/api/health")
def health():
    load_env_file()
    checks: dict[str, Any] = {"version": "1.0"}
    checks["scheduler_secret"] = bool(os.getenv("SCHEDULER_SECRET", ""))
    try:
        _events_collection().limit(1).get()
        checks["firestore"] = "ok"
    except Exception as exc:
        checks["firestore"] = f"erro: {exc}"
    all_ok = checks["firestore"] == "ok"
    return {"status": "ok" if all_ok else "degraded", **checks}


@app.post("/api/scheduler/run")
def scheduler_run(
    body: SchedulerRunRequest | None = None,
    x_scheduler_secret: str = Header(default=""),
):
    load_env_file()
    expected_secret = os.getenv("SCHEDULER_SECRET", "")
    if not expected_secret:
        raise HTTPException(status_code=500, detail="SCHEDULER_SECRET não configurado.")
    if not secrets.compare_digest(x_scheduler_secret, expected_secret):
        raise HTTPException(status_code=401, detail="Scheduler não autorizado.")

    try:
        events = body.events if body and body.events else _scheduled_events_from_firestore()
        if not events:
            events = _scheduled_events_from_env()
        if not events:
            raise AutomationError("Nenhum evento informado para o agendamento.")

        results = []
        for index, event in enumerate(events, start=1):
            try:
                result = _run_event_once(event)
                results.append({"index": index, **result})
            except Exception as exc:
                results.append({
                    "index": index,
                    "status": "erro",
                    "message": f"{type(exc).__name__}: {exc}",
                    "convenio": event.convenio,
                    "cpa": event.cpa,
                    "data_evento": event.data_evento,
                    "hora_evento": event.hora_evento,
                })
        ok = any(item.get("status") == "confirmado" for item in results)
        return {"ok": ok, "total": len(results), "results": results}
    except AutomationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        "has_login":           bool(os.getenv("PROEIS_LOGIN", "")),
        "has_password":        bool(os.getenv("PROEIS_PASSWORD", "")),
        "has_gemini_api_key":  bool(os.getenv("GEMINI_API_KEY", "")),
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


@app.get("/api/events", response_model=EventListResponse)
def list_events():
    load_env_file()
    try:
        return {"events": _stored_events()}
    except AutomationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/events")
def create_event(body: RunRequest):
    load_env_file()
    try:
        data = _event_payload(body)
        now = datetime.now(timezone.utc).isoformat()
        data["created_at"] = now
        data["updated_at"] = now
        ref = _events_collection().document()
        ref.set(data)
        return {"ok": True, "event": {"id": ref.id, **data}}
    except AutomationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/events/{event_id}")
def update_event(event_id: str, body: RunRequest):
    load_env_file()
    try:
        ref = _events_collection().document(event_id)
        current = ref.get()
        if not current.exists:
            raise HTTPException(status_code=404, detail="Evento não encontrado.")
        data = _event_payload(body)
        data["created_at"] = (current.to_dict() or {}).get("created_at", datetime.now(timezone.utc).isoformat())
        ref.set(data)
        return {"ok": True, "event": {"id": event_id, **data}}
    except AutomationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    load_env_file()
    try:
        _events_collection().document(event_id).delete()
        return {"ok": True}
    except AutomationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/run")
async def run_automation(body: RunRequest):
    load_env_file()

    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")
    twocaptcha = body.twocaptcha_key or os.getenv("TWOCAPTCHA_API_KEY", "")

    op_id = uuid.uuid4().hex[:8]
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    result: dict[str, Any] = {"status": "pendente", "message": "", "op_id": op_id}
    loop = asyncio.get_event_loop()
    aqueue: asyncio.Queue = asyncio.Queue()

    def emit(msg: Optional[dict]) -> None:
        loop.call_soon_threadsafe(aqueue.put_nowait, msg)

    def _run() -> None:
        old_out = sys.stdout
        old_err = sys.stderr
        _LOGS_DIR.mkdir(exist_ok=True)
        log_path = _LOGS_DIR / f"{ts_str}_{op_id}_run.log"
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        cap = _Capture(emit, log_file)
        sys.stdout = cap
        sys.stderr = cap
        try:
            print(f"[OP] Operação iniciada: id={op_id} | convenio={body.convenio} | cpa={body.cpa} | data={body.data_evento}")
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

            with _env_lock:
                _apply_runtime_options(body)
                client = ProeisHTTP(
                    login=login_val,
                    password=password_val,
                    twocaptcha_key=twocaptcha,
                    gemini_api_key=gemini_key,
                )

            login_with_retries(client, "Login via painel web")

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
            print(f"[OP] Operação encerrada: status={result['status']} | log={log_path.name}")
            sys.stdout = old_out
            sys.stderr = old_err
            try:
                log_file.close()
            except Exception:
                pass
            emit(None)

    async def _stream():
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        yield _SSE_PADDING
        elapsed_idle = 0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(aqueue.get(), timeout=3.0)
                    elapsed_idle = 0
                    if item is None:
                        break
                    yield _sse_data(item)
                except asyncio.TimeoutError:
                    elapsed_idle += 3
                    if elapsed_idle >= 120:
                        aviso = "[AVISO] Operação excedeu 2 min sem resposta. Verifique conexão ou timeouts."
                        yield _sse_data({"type": "log", "line": aviso})
                        break
                    yield ": keep-alive\n\n"
        finally:
            yield _sse_data({"type": "done", **result})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
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

    op_id = uuid.uuid4().hex[:8]
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    result: dict[str, Any] = {"status": "ok", "total": 0, "op_id": op_id}
    loop = asyncio.get_event_loop()
    aqueue: asyncio.Queue = asyncio.Queue()

    def emit(msg: Optional[dict]) -> None:
        loop.call_soon_threadsafe(aqueue.put_nowait, msg)

    def _run() -> None:
        old_out = sys.stdout
        old_err = sys.stderr
        _LOGS_DIR.mkdir(exist_ok=True)
        log_path = _LOGS_DIR / f"{ts_str}_{op_id}_listar.log"
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        cap = _Capture(emit, log_file)
        sys.stdout = cap
        sys.stderr = cap
        try:
            print(f"[OP] Listagem iniciada: id={op_id} | convenio={body.convenio} | cpa={body.cpa} | data={body.data_especifica or 'todas'}")
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

            with _env_lock:
                _apply_runtime_options(body)
                client = ProeisHTTP(
                    login=login_val,
                    password=password_val,
                    twocaptcha_key=twocaptcha,
                    gemini_api_key=gemini_key,
                )

            login_with_retries(client, "Login via painel web (listagem)")
            client.navigate_to_service_page()

            if body.data_especifica:
                from proeis_http import emit_vaga, normalize_date_for_site, norm  # noqa: E402
                data_norm = normalize_date_for_site(body.data_especifica)
                client.fill_filters(body.convenio, data_norm, body.cpa, prefer="qualquer")
                soup = client.require_soup()
                candidates = client.available_candidates(soup, "qualquer")

                reserva = [c for c in candidates if client.matches_preference(norm(c.label), "reserva")]
                titular = [c for c in candidates if not client.matches_preference(norm(c.label), "reserva")]

                for tipo_nome, grupo in [("nao-reserva", titular), ("reserva", reserva)]:
                    if grupo:
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
            print(f"[OP] Listagem encerrada: status={result['status']} | log={log_path.name}")
            sys.stdout = old_out
            sys.stderr = old_err
            try:
                log_file.close()
            except Exception:
                pass
            emit(None)

    async def _stream():
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        yield _SSE_PADDING
        elapsed_idle = 0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(aqueue.get(), timeout=3.0)
                    elapsed_idle = 0
                    if item is None:
                        break
                    yield _sse_data(item)
                except asyncio.TimeoutError:
                    elapsed_idle += 3
                    if elapsed_idle >= 120:
                        aviso = "[AVISO] Operação excedeu 2 min sem resposta. Verifique conexão ou timeouts."
                        yield _sse_data({"type": "log", "line": aviso})
                        break
                    yield ": keep-alive\n\n"
        finally:
            yield _sse_data({"type": "done", **result})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/logs")
def list_logs():
    """Lista as 20 operações mais recentes (arquivos de log)."""
    if not _LOGS_DIR.exists():
        return {"logs": []}
    files = sorted(_LOGS_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:20]
    return {
        "logs": [
            {
                "name": f.name,
                "op_id": f.stem.split("_")[2] if len(f.stem.split("_")) >= 3 else f.stem,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "created_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
            for f in files
        ]
    }


@app.get("/api/logs/{op_id}")
def get_log(op_id: str):
    """Retorna o conteúdo do arquivo de log de uma operação."""
    if not re.match(r"^[a-f0-9]{8}$", op_id):
        raise HTTPException(status_code=400, detail="op_id inválido.")
    if not _LOGS_DIR.exists():
        raise HTTPException(status_code=404, detail="Pasta de logs não encontrada.")
    matches = list(_LOGS_DIR.glob(f"*_{op_id}_*.log"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"Log '{op_id}' não encontrado.")
    log_file = sorted(matches, key=lambda f: f.stat().st_mtime, reverse=True)[0]
    return {"op_id": op_id, "name": log_file.name, "content": log_file.read_text(encoding="utf-8")}


from starlette.staticfiles import StaticFiles

app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="static")

handler = app
