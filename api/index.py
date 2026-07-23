from __future__ import annotations

import asyncio
import base64
import json
import os
import re

import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, FastAPI, Request, Response
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
    DEFAULT_URL,
    ASSOCIAR_URL,
    load_env_file,
    login_with_retries,
    normalize_captcha_answer,
    is_valid_captcha_answer,
    reparar_mojibake,
    run_batch_events,
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
    gemini_model: str = ""
    # Parametros do evento
    convenio: str = ""
    data_evento: str = ""
    cpa: str = ""
    disponivel: str = "nao-reserva"
    nome_evento: str = ""
    hora_evento: str = ""
    turno: str = ""
    endereco: str = ""
    dry_run: bool = False
    scan_rounds: int = 1
    # Configuracoes avancadas (0 = usar padrao)
    http_attempts: int = 0
    connect_timeout: int = 0
    read_timeout: int = 0
    filter_max_attempts: int = 0
    captcha_invalid_retries: int = 0
    captcha_refresh_after_invalids: int = 0
    gemini_timeout: int = 0
    auto_retry_rounds: int = 0
    auto_retry_wait_seconds: int = 0

class BatchRunRequest(BaseModel):
    events: list[RunRequest] = []
    fast_mode: bool = True
    batch_window_seconds: int = 0
    batch_repeat_pause_seconds: int = 1
    batch_max_no_action_rounds: int = 2
    recovery_window_seconds: int = 0
    scan_rounds: int = 1
    # Reconexao: quando o stream cai (ex.: timeout do Cloud Run), o painel
    # reenvia a requisicao com o op_id do batch e o indice da ultima linha
    # recebida para reatachar sem iniciar um batch duplicado.
    resume_op_id: str = ""
    resume_from: int = 0

    # Credenciais/configuracoes compartilhadas do lote.
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""
    http_attempts: int = 0
    connect_timeout: int = 0
    read_timeout: int = 0
    filter_max_attempts: int = 0
    captcha_invalid_retries: int = 0
    captcha_refresh_after_invalids: int = 0
    gemini_timeout: int = 0
    auto_retry_rounds: int = 0
    auto_retry_wait_seconds: int = 0

class EventListResponse(BaseModel):
    events: list[dict[str, Any]]

class ListVagasRequest(BaseModel):
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""
    convenio: str = ""
    cpa: str = ""
    data_especifica: str = ""   # dd/mm/yyyy ou yyyy-mm-dd; se vazio, varre todas
    http_attempts: int = 0
    connect_timeout: int = 0
    read_timeout: int = 0
    filter_max_attempts: int = 0
    captcha_invalid_retries: int = 0
    captcha_refresh_after_invalids: int = 0
    gemini_timeout: int = 0

class ServicosRequest(BaseModel):
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""

def _parse_servicos(raw: str) -> list[dict]:
    """Parseia o conteudo do textarea txtEveVoluntario em registros estruturados."""
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
    if getattr(body, "http_attempts", 0) > 0:
        os.environ["PROEIS_HTTP_ATTEMPTS"] = str(body.http_attempts)
    if getattr(body, "connect_timeout", 0) > 0:
        os.environ["PROEIS_CONNECT_TIMEOUT"] = str(body.connect_timeout)
    if getattr(body, "read_timeout", 0) > 0:
        os.environ["PROEIS_READ_TIMEOUT"] = str(body.read_timeout)
    if getattr(body, "filter_max_attempts", 0) > 0:
        os.environ["FILTER_MAX_ATTEMPTS"] = str(body.filter_max_attempts)
    if getattr(body, "captcha_invalid_retries", 0) > 0:
        os.environ["CAPTCHA_INVALID_RETRIES"] = str(body.captcha_invalid_retries)
    if getattr(body, "captcha_refresh_after_invalids", 0) > 0:
        os.environ["CAPTCHA_REFRESH_AFTER_INVALIDS"] = str(body.captcha_refresh_after_invalids)
    if getattr(body, "gemini_timeout", 0) > 0:
        os.environ["GEMINI_TIMEOUT"] = str(body.gemini_timeout)
    if getattr(body, "auto_retry_rounds", 0) > 0:
        os.environ["PROEIS_AUTO_RETRY_ROUNDS"] = str(body.auto_retry_rounds)
    if getattr(body, "auto_retry_wait_seconds", 0) > 0:
        os.environ["PROEIS_AUTO_RETRY_WAIT_SECONDS"] = str(body.auto_retry_wait_seconds)
    if getattr(body, "gemini_model", ""):
        os.environ["GEMINI_MODEL"] = body.gemini_model

_TEXT_FIELDS = (
    "convenio",
    "data_evento",
    "data_especifica",
    "cpa",
    "disponivel",
    "nome_evento",
    "hora_evento",
    "turno",
    "endereco",
)

def _clean_text(value: Any) -> str:
    text = reparar_mojibake(value).strip()
    if any(0x80 <= ord(char) <= 0x9F for char in text):
        try:
            text = text.encode("latin-1").decode("utf-8").strip()
        except UnicodeError:
            pass
    return reparar_mojibake(text).strip()

def _clean_request_text(body: BaseModel) -> None:
    for field in _TEXT_FIELDS:
        if hasattr(body, field):
            setattr(body, field, _clean_text(getattr(body, field, "")))

_env_lock = threading.Lock()
_warmup_lock = threading.Lock()
_warmup_in_progress = False
_warmup_event = threading.Event()
_warmup_event.set()  # começa "livre" (nenhum warmup em andamento)

# Cadeado da conta PROEIS: o site so aceita 1 sessao ativa por conta. Enquanto uma
# operacao (Executar/Listar/Servicos) esta usando a conta, NAO deixamos o login
# automatico (warmup) rodar em paralelo — senao ele derruba a sessao da operacao e
# ela quebra ("erro"). Servico roda com max-instances=1, entao este guard em memoria
# e suficiente.
_account_lock = threading.Lock()
_account_busy = 0

def _account_enter() -> None:
    global _account_busy
    with _account_lock:
        _account_busy += 1

def _account_exit() -> None:
    global _account_busy
    with _account_lock:
        _account_busy = max(0, _account_busy - 1)

def _account_is_busy() -> bool:
    return _account_busy > 0
_LOGS_DIR = ROOT / "logs"
_SSE_PADDING = ":" + (" " * 8192) + "\n\n"
_LOGS_LIMIT = 200

# Thread-local storage para captura por thread (evita mistura de logs em requisicoes concorrentes)
_thread_local = threading.local()

class _RoutingCapture:
    """Thread-safe: encaminha writes para o _Capture do thread atual."""

    def write(self, data: str) -> None:
        cap = getattr(_thread_local, "capture", None)
        if cap is not None:
            cap.write(data)
        else:
            try:
                sys.__stdout__.write(data)
                sys.__stdout__.flush()
            except Exception:
                pass

    def flush(self) -> None:
        cap = getattr(_thread_local, "capture", None)
        if cap is not None:
            cap.flush()
        else:
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

# Instala o roteador uma vez; cada thread registra seu proprio _Capture via _thread_local.capture
_routing_stdout = _RoutingCapture()
sys.stdout = _routing_stdout
sys.stderr = _routing_stdout

def _sse_data(item: dict[str, Any]) -> str:
    return f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

class _Capture:
    """Redireciona stdout/stderr para a fila SSE e para arquivo de log da operacao."""

    def __init__(self, emit_fn, log_file=None, lines: list[str] | None = None) -> None:
        self._emit = emit_fn
        self._log_file = log_file
        self._lines = lines

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
                    if self._lines is not None:
                        self._lines.append(line)
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

def _firestore_db():
    try:
        from google.cloud import firestore
    except ImportError as exc:
        raise AutomationError("Dependencia google-cloud-firestore nao instalada.") from exc
    database = os.getenv("FIRESTORE_DATABASE", "proeis")
    return firestore.Client(database=database)

def _events_collection():
    return _firestore_db().collection(os.getenv("FIRESTORE_EVENTS_COLLECTION", "events"))

def _logs_collection():
    return _firestore_db().collection(os.getenv("FIRESTORE_LOGS_COLLECTION", "operation_logs"))

def _session_collection():
    return _firestore_db().collection(
        os.getenv("FIRESTORE_SESSION_COLLECTION", "proeis_session")
    )

def _captcha_collection():
    return _firestore_db().collection(os.getenv("FIRESTORE_CAPTCHA_COLLECTION", "captcha_dataset"))

def _save_captcha_samples(client: ProeisHTTP) -> None:
    """Persiste o dataset de captchas coletado nesta operacao (imagem + resposta +
    veredito do site). Base para medir modelos e treinar um solver proprio.
    Silencioso e defensivo: nunca interrompe a operacao principal."""
    samples = getattr(client, "captcha_samples", None)
    if not samples or os.getenv("CAPTCHA_COLLECT", "1") != "1":
        return
    try:
        col = _captcha_collection()
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        # expire_at: TTL nativo do Firestore apaga o doc sozinho apos N dias. Os
        # captchas verificados ja sao salvos no GCS no retreino semanal, entao nada
        # de treino se perde. (Timestamp -> ativa a politica de TTL do Firestore.)
        expire_at = now_dt + timedelta(days=int(os.getenv("CAPTCHA_TTL_DAYS", "60")))
        batch = _firestore_db().batch()
        for i, s in enumerate(samples[:500]):
            doc = col.document()
            batch.set(doc, {
                "image_b64": s.get("image_b64", ""),
                "answer": s.get("answer", ""),
                "accepted": bool(s.get("accepted", False)),
                "model": s.get("model", ""),
                "preproc": s.get("preproc", ""),
                "created_at": now,
                "expire_at": expire_at,
            })
            if (i + 1) % 400 == 0:
                batch.commit(); batch = _firestore_db().batch()
        batch.commit()
        client.captcha_samples = []
    except Exception as exc:
        print(f"[CAPTCHA] Falha ao salvar dataset (ignorado): {exc}")

def _save_session(client: ProeisHTTP, user_name: str) -> None:
    """Salva cookies e nome do usuario no Firestore para reutilizacao na proxima operacao."""
    try:
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
            for cookie in client.session.cookies
        ]
        _session_collection().document("current").set({
            "cookies": cookies,
            "user_name": user_name,
            "login": client.login,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"[SESSION] Sessao salva (usuario: {user_name or client.login}).")
    except Exception as exc:
        print(f"[SESSION] Aviso: nao foi possivel salvar sessao: {exc}")

def _load_session(client: ProeisHTTP) -> dict:
    """Carrega cookies do Firestore para client.session. Retorna {valid, user_name}."""
    try:
        doc = _session_collection().document("current").get()
        if not doc.exists:
            return {"valid": False, "user_name": ""}
        data = doc.to_dict() or {}
        if data.get("login", "") != client.login:
            return {"valid": False, "user_name": ""}
        cookies = data.get("cookies") or []
        if isinstance(cookies, dict):  # formato antigo: {"ASP.NET_SessionId": "..."}
            for name, value in cookies.items():
                client.session.cookies.set(name, value)
        else:
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    continue
                client.session.cookies.set(
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                    domain=cookie.get("domain") or None,
                    path=cookie.get("path") or "/",
                )
        return {"valid": True, "user_name": data.get("user_name", "")}
    except Exception as exc:
        print(f"[SESSION] Erro ao carregar sessao: {exc}")
        return {"valid": False, "user_name": ""}

def _try_restore_session(client: ProeisHTTP) -> bool:
    """Carrega cookies e verifica se sessao ainda e valida. True = login nao necessario."""
    result = _load_session(client)
    if not result["valid"]:
        return False
    try:
        if client.check_auth():
            user = result["user_name"] or client.login
            print(f"[SESSION] Sessao restaurada para '{user}' (login ignorado).")
            _save_session(client, user)
            return True
        print("[SESSION] Cookies carregados mas sessao invalida no servidor.")
        return False
    except Exception as exc:
        print(f"[SESSION] Erro ao validar sessao restaurada: {exc}")
        return False

def _fetch_user_name_and_save(client: ProeisHTTP) -> None:
    """Apos login novo: busca nome do usuario no menu e persiste sessao no Firestore."""
    try:
        soup = client.request("GET", MENU_URL)
        nome = ""
        for sel in ["#lblNomeVoluntario", "#lblNome", "#lblUsuario",
                    "[id*='lblNome']", "[id*='lblUsuario']"]:
            el = soup.select_one(sel)
            if el:
                nome = el.get_text(strip=True)
                if nome:
                    break
        if not nome:
            txt = soup.get_text(" ", strip=True)
            m = re.search(
                r"(?:bem[- ]?vindo|ol[aá])[,:]?\s+([A-ZÀ-Ú][A-Za-zÀ-ú\s]{2,40})",
                txt, re.IGNORECASE,
            )
            if m:
                nome = m.group(1).strip()
        _save_session(client, nome)
    except Exception as exc:
        print(f"[SESSION] Aviso: nao salvou sessao apos login: {exc}")

def _resave_current_session(client: ProeisHTTP) -> None:
    try:
        doc = _session_collection().document("current").get()
        data = (doc.to_dict() or {}) if doc.exists else {}
        _save_session(client, data.get("user_name", "") or client.login)
    except Exception as exc:
        print(f"[SESSION] Aviso: nao foi possivel atualizar sessao atual: {exc}")

def _event_payload(body: RunRequest) -> dict[str, Any]:
    _clean_request_text(body)
    data = body.model_dump()
    data["scan_rounds"] = int(data.get("scan_rounds") or 1)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return data

def _batch_event_payload(event: RunRequest) -> dict[str, Any]:
    _clean_request_text(event)
    data = event.model_dump()
    data["scan_rounds"] = max(1, int(data.get("scan_rounds") or 1))
    for key in ("convenio", "data_evento", "cpa", "disponivel", "nome_evento", "hora_evento", "turno", "endereco"):
        data[key] = _clean_text(data.get(key, ""))
    return data

def _doc_to_event(doc) -> dict[str, Any]:
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return data

def _stored_events() -> list[dict[str, Any]]:
    query = _events_collection().order_by("created_at")
    return [_doc_to_event(doc) for doc in query.stream()]

def _save_operation_log(
    op_id: str,
    kind: str,
    status: str,
    lines: list[str],
    log_name: str,
    result: dict[str, Any],
) -> None:
    try:
        # Guarda as ultimas N linhas (default 8000, cobre um run inteiro das 6h
        # com todas as re-tentativas) — pra dar pra estudar o log completo.
        # Cap por bytes garante que nao estoura o teto de 1 MiB do doc Firestore.
        max_lines = int(os.getenv("LOG_MAX_LINES", "8000"))
        content = "\n".join(lines[-max_lines:])
        max_bytes = int(os.getenv("LOG_MAX_BYTES", "800000"))
        enc = content.encode("utf-8")
        if len(enc) > max_bytes:
            content = enc[-max_bytes:].decode("utf-8", "ignore")
        now_dt = datetime.now(timezone.utc)
        _logs_collection().document(op_id).set({
            "op_id": op_id,
            "kind": kind,
            "status": status,
            "name": log_name,
            "content": content,
            "line_count": len(lines),
            "size_kb": round(len(content.encode("utf-8")) / 1024, 1),
            "created_at": now_dt.isoformat(),
            # TTL nativo: log antigo se apaga sozinho apos N dias.
            "expire_at": now_dt + timedelta(days=int(os.getenv("LOG_TTL_DAYS", "30"))),
            "result": {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool)) or v is None},
        })
    except Exception:
        pass

def _firestore_logs() -> list[dict[str, Any]]:
    # Le apenas os ultimos _LOGS_LIMIT no servidor (order_by + limit), em vez de
    # baixar a colecao inteira e ordenar em Python. Custo constante conforme cresce.
    try:
        logs = []
        query = _logs_collection().order_by("created_at", direction="DESCENDING").limit(_LOGS_LIMIT)
        for doc in query.stream():
            data = doc.to_dict() or {}
            data.setdefault("op_id", doc.id)
            logs.append(data)
        return logs
    except Exception:
        # Fallback defensivo (ex.: indice ausente): volta ao modo antigo limitado.
        try:
            logs = []
            for doc in _logs_collection().stream():
                data = doc.to_dict() or {}
                data.setdefault("op_id", doc.id)
                logs.append(data)
            return sorted(logs, key=lambda item: item.get("created_at", ""), reverse=True)[:_LOGS_LIMIT]
        except Exception:
            return []

class TestLoginRequest(BaseModel):
    login: str = ""
    password: str = ""
    gemini_api_key: str = ""

@app.get("/api/health")
def health():
    load_env_file()
    checks: dict[str, Any] = {"version": "1.0"}
    try:
        _events_collection().limit(1).get()
        checks["firestore"] = "ok"
    except Exception as exc:
        checks["firestore"] = f"erro: {exc}"
    all_ok = checks["firestore"] == "ok"
    return {"status": "ok" if all_ok else "degraded", **checks}

@app.post("/api/test-login")
async def test_login(body: TestLoginRequest):
    """Faz login real e retorna o nome do usuario autenticado."""
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
            gemini_api_key=gemini_key,
            debug=False,
        )
        login_with_retries(client, "Teste de login via painel web")

        from proeis_http import MENU_URL  # noqa: E402
        soup = client.request("GET", MENU_URL)

        nome = ""
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

        if not nome:
            import re as _re
            txt = soup.get_text(" ", strip=True)
            m = _re.search(r"(?:bem[- ]?vindo|ol[aá])[,:]?\s+([A-ZÀ-Ú][A-Za-zÀ-ú\s]{2,40})", txt, _re.IGNORECASE)
            if m:
                nome = m.group(1).strip()

        if not nome:
            h = soup.select_one("h1, h2, h3")
            if h:
                nome = h.get_text(strip=True)[:80]

        _save_session(client, nome or login_val)

        return {
            "ok": True,
            "login": login_val,
            "nome": nome or "(nome nao encontrado na pagina)",
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
        return {"ok": False, "message": "Credenciais nao configuradas.", "servicos": [], "nome": ""}

    try:
        client = ProeisHTTP(
            login=login_val, password=pwd_val,
            gemini_api_key=gemini_key, debug=False,
        )
        session_restored = _try_restore_session(client)
        if not session_restored:
            login_with_retries(client, "Buscar servicos marcados")

        # Se sessao foi restaurada, soup ja e o menu (do check_auth). Senao, busca agora.
        if session_restored and client.soup is not None:
            soup = client.soup
        else:
            soup = client.request("GET", MENU_URL)

        nome = ""
        for sel in ["#lblNomeVoluntario", "#lblNome", "[id*='lblNome']"]:
            el = soup.select_one(sel)
            if el:
                nome = el.get_text(strip=True)
                if nome:
                    break

        _save_session(client, nome)

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
        "captcha_invalid_retries": _int("CAPTCHA_INVALID_RETRIES"),
        "captcha_refresh_after_invalids": _int("CAPTCHA_REFRESH_AFTER_INVALIDS"),
        "gemini_timeout": _int("GEMINI_TIMEOUT"),
        "auto_retry_rounds": _int("PROEIS_AUTO_RETRY_ROUNDS"),
        "auto_retry_wait_seconds": _int("PROEIS_AUTO_RETRY_WAIT_SECONDS"),
        "system_defaults": {
            "http_attempts": 2,
            "connect_timeout": 8,
            "read_timeout": 25,
            "filter_max_attempts": 8,
            "captcha_invalid_retries": 2,
            "captcha_refresh_after_invalids": 1,
            "gemini_timeout": 30,
            "auto_retry_rounds": 0,
            "auto_retry_wait_seconds": 300,
        },
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
            raise HTTPException(status_code=404, detail="Evento nao encontrado.")
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
    _clean_request_text(body)

    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    op_id = uuid.uuid4().hex[:8]
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    result: dict[str, Any] = {"status": "pendente", "message": "", "op_id": op_id}
    loop = asyncio.get_event_loop()
    aqueue: asyncio.Queue = asyncio.Queue()

    def emit(msg: Optional[dict]) -> None:
        loop.call_soon_threadsafe(aqueue.put_nowait, msg)

    def _run() -> None:
        _LOGS_DIR.mkdir(exist_ok=True)
        log_path = _LOGS_DIR / f"{ts_str}_{op_id}_run.log"
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        log_lines: list[str] = []
        cap = _Capture(emit, log_file, log_lines)
        _thread_local.capture = cap
        _account_enter()  # segura a conta (bloqueia login automatico paralelo) ate o finally
        try:
            print(f"[OP] Operacao iniciada: id={op_id} | convenio={body.convenio} | cpa={body.cpa} | data={body.data_evento}")
            print(
                "[OP] Alvo: "
                f"nome={body.nome_evento or '-'} | hora={body.hora_evento or '-'} | "
                f"tipo={body.disponivel or '-'} | endereco={body.endereco or '-'}"
            )
            if not login_val:
                raise AutomationError("Login nao configurado. Va em Configuracoes.")
            if not password_val:
                raise AutomationError("Senha nao configurada. Va em Configuracoes.")
            if not gemini_key:
                raise AutomationError("GEMINI_API_KEY nao configurada. Va em Configuracoes.")
            if not body.convenio:
                raise AutomationError("Convenio nao informado.")
            if not body.cpa:
                raise AutomationError("CPA nao informado.")

            with _env_lock:
                _apply_runtime_options(body)
                client = ProeisHTTP(
                    login=login_val,
                    password=password_val,
                    gemini_api_key=gemini_key,
                )

            # Aguarda warmup de background (abertura do painel) antes de verificar sessao.
            # Evita login duplicado caso o usuario clique antes do warmup terminar.
            _warmup_event.wait(timeout=30)
            if not _try_restore_session(client):
                login_with_retries(client, "Login via painel web")
                _fetch_user_name_and_save(client)

            confirmed = client.mark_scanning_dates(
                body.convenio,
                body.cpa,
                body.disponivel,
                1,  # sempre 1 vaga por evento
                scan_rounds=body.scan_rounds,
                start_date=body.data_evento,
                nome_evento=body.nome_evento,
                hora_evento=body.hora_evento,
                turno=body.turno,
                endereco=body.endereco,
            )
            _resave_current_session(client)
            _save_captcha_samples(client)
            result["status"] = "confirmado" if confirmed >= 1 else "pendente"
            result["confirmed"] = confirmed

        except AutomationError as exc:
            result["status"] = "erro"
            result["message"] = str(exc)
        except Exception as exc:
            result["status"] = "erro"
            result["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            _account_exit()  # libera a conta
            print(f"[OP] Operacao encerrada: status={result['status']} | log={log_path.name}")
            _save_operation_log(op_id, "run", result["status"], log_lines, log_path.name, result)
            _thread_local.capture = None
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
        emitted = 0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(aqueue.get(), timeout=3.0)
                    elapsed_idle = 0
                    if item is None:
                        break
                    yield _sse_data(item)
                    # Re-priming periodico: forca o flush contra proxies/navegadores
                    # que voltam a bufferizar depois do pacote inicial (desktop/web).
                    emitted += 1
                    if emitted % 6 == 0:
                        yield ": flush\n\n"
                except asyncio.TimeoutError:
                    elapsed_idle += 3
                    if elapsed_idle >= 120:
                        aviso = "[AVISO] Operacao excedeu 2 min sem resposta. Verifique conexao ou timeouts."
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

# ── Batch unico com reconexao ───────────────────────────────────────────────
# Apenas um batch rapido roda por vez nesta instancia. Se o stream do painel
# cair (ex.: timeout de requisicao do Cloud Run), o painel reenvia a chamada e
# reataca ao batch em andamento em vez de iniciar um processo duplicado —
# batches simultaneos na mesma conta do PROEIS derrubam a sessao um do outro.
_batch_guard = threading.Lock()
_current_batch: dict[str, Any] = {"run": None}

class _BatchRun:
    def __init__(self, op_id: str, total: int) -> None:
        self.op_id = op_id
        self.lock = threading.Lock()
        self.messages: list[dict[str, Any]] = []
        self.done = threading.Event()
        self.result: dict[str, Any] = {
            "status": "pendente",
            "message": "",
            "op_id": op_id,
            "confirmed": 0,
            "total": total,
        }
        self.perf: dict[str, int] = {}

    def emit(self, msg: Optional[dict]) -> None:
        if msg is None:
            self.done.set()
            return
        with self.lock:
            msg["i"] = len(self.messages)
            self.messages.append(msg)

async def _stream_batch(run: _BatchRun, start_index: int, attached: bool):
    yield _SSE_PADDING
    yield _sse_data({
        "type": "start",
        "op_id": run.op_id,
        "attached": attached,
        "total": run.result.get("total", 0),
    })
    idx = start_index
    idle = 0.0
    while True:
        with run.lock:
            new = run.messages[idx:]
        if new:
            idx += len(new)
            idle = 0.0
            for msg in new:
                yield _sse_data(msg)
            continue
        if run.done.is_set():
            break
        await asyncio.sleep(0.5)
        idle += 0.5
        if idle >= 15:
            idle = 0.0
            yield ": keep-alive\n\n"
    yield _sse_data({"type": "done", **run.result, "perf": run.perf})

def _sse_response(gen) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/api/run-batch-fast")
async def run_batch_fast(body: BatchRunRequest):
    load_env_file()

    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    with _batch_guard:
        active = _current_batch.get("run")
        if active is not None and not active.done.is_set():
            # Ja existe batch rodando: reataca a ele em vez de iniciar outro.
            if body.resume_op_id == active.op_id:
                start_index = max(0, min(body.resume_from, len(active.messages)))
            else:
                start_index = 0
                active.emit({
                    "type": "log",
                    "line": f"[AVISO] Batch {active.op_id} ja em andamento; reconectando ao processo existente em vez de iniciar outro.",
                })
            return _sse_response(_stream_batch(active, start_index, attached=True))
        if body.resume_op_id:
            # Reconexao tardia: nunca inicia um batch novo a partir de um resume.
            if active is not None and active.op_id == body.resume_op_id:
                # Batch terminou enquanto o painel estava desconectado; entrega o final.
                start_index = max(0, min(body.resume_from, len(active.messages)))
                return _sse_response(_stream_batch(active, start_index, attached=True))

            async def _gone():
                yield _SSE_PADDING
                yield _sse_data({
                    "type": "log",
                    "line": f"[AVISO] Batch {body.resume_op_id} nao encontrado (a instancia pode ter reiniciado). Nenhum batch novo foi iniciado; confira o historico de logs.",
                })
                yield _sse_data({
                    "type": "done",
                    "status": "pendente",
                    "message": "Conexao com o batch original foi perdida. Confira o historico de logs.",
                    "op_id": body.resume_op_id,
                    "confirmed": 0,
                    "total": 0,
                    "perf": {},
                })
            return _sse_response(_gone())

        op_id = uuid.uuid4().hex[:8]
        run = _BatchRun(op_id, len(body.events))
        _current_batch["run"] = run

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = run.result
    perf = run.perf
    emit = run.emit

    def _run() -> None:
        _LOGS_DIR.mkdir(exist_ok=True)
        log_path = _LOGS_DIR / f"{ts_str}_{op_id}_run_batch_fast.log"
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        log_lines: list[str] = []
        cap = _Capture(emit, log_file, log_lines)
        _thread_local.capture = cap
        t_total = time.monotonic()
        _account_enter()  # segura a conta (bloqueia login automatico paralelo) ate o finally
        try:
            if not body.events:
                raise AutomationError("Nenhum evento informado para execucao em lote.")
            if not login_val:
                raise AutomationError("Login nao configurado. Va em Configuracoes.")
            if not password_val:
                raise AutomationError("Senha nao configurada. Va em Configuracoes.")
            if not gemini_key:
                raise AutomationError("GEMINI_API_KEY nao configurada. Va em Configuracoes.")

            events = [_batch_event_payload(event) for event in body.events]
            missing = [
                i + 1
                for i, event in enumerate(events)
                if not event.get("convenio") or not event.get("cpa")
            ]
            if missing:
                raise AutomationError(f"Eventos sem convenio/CPA: {missing}.")

            events.sort(key=lambda ev: (
                ev.get("convenio", ""),
                ev.get("data_evento", ""),
                ev.get("cpa", ""),
                ev.get("disponivel", ""),
                ev.get("hora_evento", ""),
                ev.get("nome_evento", ""),
            ))

            print(f"[OP] Batch rapido iniciado: id={op_id} | eventos={len(events)}")
            complete = sum(1 for ev in events if ev.get("data_evento"))
            print(f"[INFO] Modo rapido: {complete}/{len(events)} evento(s) com data definida; eventos sem data podem cair em varredura.")

            with _env_lock:
                _apply_runtime_options(body)
                client = ProeisHTTP(
                    login=login_val,
                    password=password_val,
                    gemini_api_key=gemini_key,
                )

            t_session = time.monotonic()
            _warmup_event.wait(timeout=30)
            if not _try_restore_session(client):
                login_with_retries(client, "Login via batch rapido")
                _fetch_user_name_and_save(client)
            perf["session_ms"] = int((time.monotonic() - t_session) * 1000)
            print(f"[PERF] session_ms={perf['session_ms']}")

            t_batch = time.monotonic()
            confirmed = run_batch_events(
                client,
                events,
                dry_run=False,
                scan_rounds=max(1, int(body.scan_rounds or 1)),
                recovery_window_seconds=max(0, int(body.recovery_window_seconds or 0)),
                batch_window_seconds=max(0, int(body.batch_window_seconds or 0)),
                batch_repeat_pause_seconds=max(0, int(body.batch_repeat_pause_seconds or 1)),
                batch_max_no_action_rounds=max(0, int(body.batch_max_no_action_rounds or 2)),
            )
            perf["batch_ms"] = int((time.monotonic() - t_batch) * 1000)
            print(f"[PERF] batch_ms={perf['batch_ms']}")

            _resave_current_session(client)
            _save_captcha_samples(client)
            result["confirmed"] = confirmed
            result["status"] = "confirmado" if confirmed > 0 else "pendente"
            result["message"] = ""

        except AutomationError as exc:
            result["status"] = "erro"
            result["message"] = str(exc)
        except Exception as exc:
            result["status"] = "erro"
            result["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            _account_exit()  # libera a conta
            # Cada passo do encerramento e isolado: se um falhar, os demais rodam
            # e o emit(None) SEMPRE acontece — sem ele o batch nunca e marcado
            # como terminado e a trava de batch unico ficaria presa para sempre.
            try:
                perf["total_ms"] = int((time.monotonic() - t_total) * 1000)
                print(f"[PERF] total_ms={perf['total_ms']}")
                print(f"[OP] Batch rapido encerrado: status={result['status']} | log={log_path.name}")
            except BaseException:
                pass
            try:
                _save_operation_log(op_id, "run_batch_fast", result["status"], log_lines, log_path.name, result)
            except BaseException:
                pass
            _thread_local.capture = None
            try:
                log_file.close()
            except Exception:
                pass
            emit(None)

    def _run_safe() -> None:
        try:
            _run()
        finally:
            run.emit(None)  # garantia extra; done.set() e idempotente

    threading.Thread(target=_run_safe, daemon=True).start()
    return _sse_response(_stream_batch(run, 0, attached=False))


def _iso_or_none(value: str) -> Optional[str]:
    """Normaliza data para 'yyyy-mm-dd'. Aceita 'yyyy-mm-dd' ou 'dd/mm/yyyy'."""
    s = (value or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def _scheduled_run_requests() -> list[RunRequest]:
    """Le os eventos cadastrados no Firestore e os expande em RunRequests
    (uma por data x hora), igual ao expandEventForRun do front. Mantem apenas
    datas de hoje em diante (fuso de Brasilia, UTC-3), pra nao tentar data passada.
    Credenciais ficam vazias -> o endpoint usa as variaveis de ambiente."""
    tz = timezone(timedelta(hours=-3))
    today = datetime.now(tz).date().isoformat()
    # O robo casa a vaga por NOME + HORA (unicos por evento). O endereco NAO entra
    # no casamento: um errinho de digitacao no endereco faria a vaga ser PULADA
    # mesmo estando aberta (event_matches exige que todos os filtros ativos batam).
    # Como nome+hora ja identificam a vaga, ignorar o endereco so aumenta a chance
    # de marcar. Para reativar, defina SCHEDULED_MATCH_ENDERECO=1.
    use_endereco = os.getenv("SCHEDULED_MATCH_ENDERECO", "0") in ("1", "true", "yes")
    reqs: list[RunRequest] = []
    for ev in _stored_events():
        dates = [d.strip() for d in str(ev.get("data_evento", "")).split(",") if d.strip()]
        times = [t.strip() for t in str(ev.get("hora_evento", "")).split(",") if t.strip()] or [""]
        for raw_date in dates:
            iso = _iso_or_none(raw_date)
            if iso is None or iso < today:
                continue
            for hora in times:
                reqs.append(RunRequest(
                    convenio=str(ev.get("convenio", "")),
                    cpa=str(ev.get("cpa", "")),
                    disponivel=str(ev.get("disponivel", "") or "nao-reserva"),
                    nome_evento=str(ev.get("nome_evento", "")),
                    endereco=str(ev.get("endereco", "")) if use_endereco else "",
                    data_evento=iso,
                    hora_evento=hora,
                    scan_rounds=1,
                ))
    return reqs


@app.post("/api/run-scheduled")
async def run_scheduled(request: Request):
    """Robo agendado (Cloud Scheduler dispara ~5h58 de quinta). Autentica pelo
    header X-Scheduler-Secret, busca os eventos cadastrados sozinho, e roda o
    mesmo batch persistente titular-only do botao Executar. Segura a conexao ate
    o fim (mantem a CPU ativa no Cloud Run) e devolve um resumo em JSON."""
    secret = os.getenv("SCHEDULER_SECRET", "")
    provided = request.headers.get("X-Scheduler-Secret", "") or request.query_params.get("secret", "")
    if not secret or provided != secret:
        raise HTTPException(status_code=403, detail="Segredo do agendador invalido.")
    load_env_file()

    login_val = os.getenv("PROEIS_LOGIN", "")
    password_val = os.getenv("PROEIS_PASSWORD", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not (login_val and password_val and gemini_key):
        raise HTTPException(status_code=500, detail="Credenciais PROEIS/GEMINI nao configuradas no servico.")

    reqs = _scheduled_run_requests()
    if not reqs:
        return {"ok": True, "status": "sem-eventos", "confirmed": 0, "total": 0,
                "message": "Nenhum evento com data futura cadastrado."}

    # Modo teste (dry): ?dry=1 loga e varre mas NAO marca nada, e usa janela curta.
    # Serve pra validar o pipeline (login + varredura) sem risco de marcar vaga.
    dry_run = request.query_params.get("dry", "") in ("1", "true", "yes")
    # Janela generosa: dispara 5h58 e fica insistindo (re-varrendo) ate ~6h08,
    # cobrindo a abertura das 6h. batch_max_no_action_rounds=0 => nao desiste.
    if dry_run:
        window = max(30, int(os.getenv("SCHEDULED_DRY_WINDOW", "45")))
    else:
        window = max(60, int(os.getenv("SCHEDULED_BATCH_WINDOW", "600")))

    with _batch_guard:
        active = _current_batch.get("run")
        if active is not None and not active.done.is_set():
            return {"ok": True, "status": "ja-rodando", "op_id": active.op_id, "confirmed": 0,
                    "total": active.result.get("total", 0),
                    "message": "Ja existe um batch em andamento; nada iniciado."}
        op_id = uuid.uuid4().hex[:8]
        run = _BatchRun(op_id, len(reqs))
        _current_batch["run"] = run

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = run.result
    perf = run.perf
    emit = run.emit
    events = [_batch_event_payload(r) for r in reqs]

    def _run() -> None:
        _LOGS_DIR.mkdir(exist_ok=True)
        log_path = _LOGS_DIR / f"{ts_str}_{op_id}_run_scheduled.log"
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        log_lines: list[str] = []
        cap = _Capture(emit, log_file, log_lines)
        _thread_local.capture = cap
        t_total = time.monotonic()
        _account_enter()  # segura a conta ate o finally (bloqueia login paralelo)
        try:
            print(f"[OP] Robo agendado iniciado: id={op_id} | sub-eventos={len(events)} | janela={window}s | dry_run={dry_run}")
            events.sort(key=lambda ev: (
                ev.get("convenio", ""), ev.get("data_evento", ""), ev.get("cpa", ""),
                ev.get("disponivel", ""), ev.get("hora_evento", ""), ev.get("nome_evento", ""),
            ))
            with _env_lock:
                client = ProeisHTTP(
                    login=login_val, password=password_val, gemini_api_key=gemini_key,
                )
            t_session = time.monotonic()
            _warmup_event.wait(timeout=30)
            if not _try_restore_session(client):
                login_with_retries(client, "Login via robo agendado")
                _fetch_user_name_and_save(client)
            perf["session_ms"] = int((time.monotonic() - t_session) * 1000)
            print(f"[PERF] session_ms={perf['session_ms']}")

            t_batch = time.monotonic()
            confirmed = run_batch_events(
                client, events, dry_run=dry_run, scan_rounds=1,
                recovery_window_seconds=0, batch_window_seconds=window,
                batch_repeat_pause_seconds=1, batch_max_no_action_rounds=0,
            )
            perf["batch_ms"] = int((time.monotonic() - t_batch) * 1000)
            print(f"[PERF] batch_ms={perf['batch_ms']}")

            _resave_current_session(client)
            _save_captcha_samples(client)
            result["confirmed"] = confirmed
            result["status"] = "confirmado" if confirmed > 0 else "pendente"
            result["message"] = ""
        except AutomationError as exc:
            result["status"] = "erro"
            result["message"] = str(exc)
        except Exception as exc:
            result["status"] = "erro"
            result["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            _account_exit()
            try:
                perf["total_ms"] = int((time.monotonic() - t_total) * 1000)
                print(f"[OP] Robo agendado encerrado: status={result['status']} | confirmadas={result['confirmed']}/{result['total']}")
            except BaseException:
                pass
            try:
                _save_operation_log(op_id, "run_scheduled", result["status"], log_lines, log_path.name, result)
            except BaseException:
                pass
            _thread_local.capture = None
            try:
                log_file.close()
            except Exception:
                pass
            emit(None)

    def _run_safe() -> None:
        try:
            _run()
        finally:
            run.emit(None)

    threading.Thread(target=_run_safe, daemon=True).start()

    # Segura a requisicao ate o batch terminar (CPU ativa) com teto de seguranca.
    max_wait = window + 120
    waited = 0.0
    while not run.done.is_set() and waited < max_wait:
        await asyncio.sleep(1.0)
        waited += 1.0
    return {"ok": True, "op_id": op_id, "status": result["status"],
            "confirmed": result["confirmed"], "total": result["total"],
            "message": result.get("message", ""), "perf": perf}


@app.post("/api/list-vagas")
async def list_vagas(body: ListVagasRequest):
    load_env_file()
    _clean_request_text(body)
    if body.filter_max_attempts < 8:
        body.filter_max_attempts = 8

    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    op_id = uuid.uuid4().hex[:8]
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    result: dict[str, Any] = {"status": "ok", "total": 0, "op_id": op_id}
    loop = asyncio.get_event_loop()
    aqueue: asyncio.Queue = asyncio.Queue()

    def emit(msg: Optional[dict]) -> None:
        loop.call_soon_threadsafe(aqueue.put_nowait, msg)

    def _run() -> None:
        _LOGS_DIR.mkdir(exist_ok=True)
        log_path = _LOGS_DIR / f"{ts_str}_{op_id}_listar.log"
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        log_lines: list[str] = []
        cap = _Capture(emit, log_file, log_lines)
        _thread_local.capture = cap
        _account_enter()  # segura a conta (bloqueia login automatico paralelo) ate o finally
        try:
            print(f"[OP] Listagem iniciada: id={op_id} | convenio={body.convenio} | cpa={body.cpa} | data={body.data_especifica or 'todas'}")
            if not login_val:
                raise AutomationError("Login nao configurado. Va em Configuracoes.")
            if not password_val:
                raise AutomationError("Senha nao configurada. Va em Configuracoes.")
            if not gemini_key:
                raise AutomationError("GEMINI_API_KEY nao configurada. Va em Configuracoes.")
            if not body.convenio:
                raise AutomationError("Convenio nao informado.")
            if not body.cpa:
                raise AutomationError("CPA nao informado.")

            with _env_lock:
                _apply_runtime_options(body)
                client = ProeisHTTP(
                    login=login_val,
                    password=password_val,
                    gemini_api_key=gemini_key,
                )

            # Aguarda warmup de background antes de verificar sessao.
            _warmup_event.wait(timeout=30)
            if not _try_restore_session(client):
                login_with_retries(client, "Login via painel web (listagem)")
                _fetch_user_name_and_save(client)
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

            _resave_current_session(client)
            result["total"] = total

            # Coleta extra de captchas verificados para treinar a IA. Roda DEPOIS de as
            # vagas ja terem sido exibidas ao vivo, entao nao atrasa o resultado. Barato:
            # nao exige CPU sempre-ligada (roda dentro do request). Best-effort, nunca quebra.
            try:
                n_harvest = int(os.getenv("CAPTCHA_HARVEST_AFTER_LISTING", "20"))
                if n_harvest > 0 and body.convenio and body.cpa:
                    client.harvest_captchas(body.convenio, body.cpa, n_harvest)
            except Exception as exc:
                print(f"[COLETA] Coleta pos-listagem ignorada: {exc}")

            _save_captcha_samples(client)

        except AutomationError as exc:
            result["status"] = "erro"
            result["message"] = str(exc)
        except Exception as exc:
            result["status"] = "erro"
            result["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            _account_exit()  # libera a conta
            print(f"[OP] Listagem encerrada: status={result['status']} | log={log_path.name}")
            _save_operation_log(op_id, "listar", result["status"], log_lines, log_path.name, result)
            _thread_local.capture = None
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
        emitted = 0
        try:
            while True:
                try:
                    item = await asyncio.wait_for(aqueue.get(), timeout=3.0)
                    elapsed_idle = 0
                    if item is None:
                        break
                    yield _sse_data(item)
                    # Re-priming periodico: forca o flush contra proxies/navegadores
                    # que voltam a bufferizar depois do pacote inicial (desktop/web).
                    emitted += 1
                    if emitted % 6 == 0:
                        yield ": flush\n\n"
                except asyncio.TimeoutError:
                    elapsed_idle += 3
                    if elapsed_idle >= 120:
                        aviso = "[AVISO] Operacao excedeu 2 min sem resposta. Verifique conexao ou timeouts."
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
def list_logs(kind: str = ""):
    """Lista as operacoes mais recentes. Filtro opcional: ?kind=agendamento|run|listar"""
    stored = _firestore_logs()
    if kind:
        stored = [item for item in stored if item.get("kind", "") == kind]
    if stored:
        return {
            "logs": [
                {
                    "name": item.get("name") or f"{item.get('op_id', '')}.log",
                    "op_id": item.get("op_id", ""),
                    "kind": item.get("kind", ""),
                    "status": item.get("status", ""),
                    "size_kb": item.get("size_kb", 0),
                    "created_at": item.get("created_at", ""),
                    "line_count": item.get("line_count", 0),
                }
                for item in stored
            ]
        }
    if not _LOGS_DIR.exists():
        return {"logs": []}
    files = sorted(_LOGS_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:_LOGS_LIMIT]
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

@app.get("/api/log-content/{op_id}")
def get_persisted_log(op_id: str):
    if not re.match(r"^[a-f0-9]{8}$", op_id):
        raise HTTPException(status_code=400, detail="op_id invalido.")
    try:
        doc = _logs_collection().document(op_id).get()
        if doc.exists:
            data = doc.to_dict() or {}
            return {
                "op_id": op_id,
                "name": data.get("name", f"{op_id}.log"),
                "kind": data.get("kind", ""),
                "status": data.get("status", ""),
                "created_at": data.get("created_at", ""),
                "content": data.get("content", ""),
            }
    except Exception:
        pass
    if _LOGS_DIR.exists():
        matches = list(_LOGS_DIR.glob(f"*_{op_id}_*.log"))
        if matches:
            log_file = sorted(matches, key=lambda f: f.stat().st_mtime, reverse=True)[0]
            return {"op_id": op_id, "name": log_file.name, "content": log_file.read_text(encoding="utf-8")}
    raise HTTPException(status_code=404, detail=f"Log '{op_id}' nao encontrado.")

@app.get("/api/logs/{op_id}")
def get_log(op_id: str):
    """Retorna o conteudo do arquivo de log de uma operacao."""
    if not re.match(r"^[a-f0-9]{8}$", op_id):
        raise HTTPException(status_code=400, detail="op_id invalido.")
    if not _LOGS_DIR.exists():
        raise HTTPException(status_code=404, detail="Pasta de logs nao encontrada.")
    matches = list(_LOGS_DIR.glob(f"*_{op_id}_*.log"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"Log '{op_id}' nao encontrado.")
    log_file = sorted(matches, key=lambda f: f.stat().st_mtime, reverse=True)[0]
    return {"op_id": op_id, "name": log_file.name, "content": log_file.read_text(encoding="utf-8")}

def _trigger_warmup_login() -> None:
    """Dispara login em background se nao houver sessao valida e credenciais estiverem configuradas.
    Usa _warmup_lock para evitar logins redundantes e _warmup_event para sincronizar com run/listar."""
    global _warmup_in_progress
    login_val = os.getenv("PROEIS_LOGIN", "")
    password_val = os.getenv("PROEIS_PASSWORD", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not (login_val and password_val and gemini_key):
        return
    # Nao logar em paralelo se uma operacao esta usando a conta (derrubaria a sessao dela).
    if _account_is_busy():
        return
    with _warmup_lock:
        if _warmup_in_progress:
            return
        _warmup_in_progress = True
        _warmup_event.clear()  # sinaliza "login em andamento"

    def _do_warmup() -> None:
        global _warmup_in_progress
        try:
            client = ProeisHTTP(
                login=login_val,
                password=password_val,
                gemini_api_key=gemini_key,
            )
            if not _try_restore_session(client):
                login_with_retries(client, "Warmup automatico (abertura do painel)")
                _fetch_user_name_and_save(client)
        except Exception:
            pass
        finally:
            with _warmup_lock:
                _warmup_in_progress = False
            _warmup_event.set()  # sinaliza "login concluido (ou falhou)"

    threading.Thread(target=_do_warmup, daemon=True).start()

@app.get("/api/session-status")
def session_status():
    """Retorna status da sessao persistida: {logged_in, user_name, saved_at}.
    Se houver sessao salva, valida no PROEIS para manter a sessao viva enquanto o painel estiver aberto.
    Se nao houver sessao valida, dispara login em background para pre-aquecer."""
    load_env_file()
    try:
        doc = _session_collection().document("current").get()
        if not doc.exists:
            _trigger_warmup_login()
            return {"logged_in": False, "user_name": "", "saved_at": ""}
        data = doc.to_dict() or {}
        login_val = os.getenv("PROEIS_LOGIN", "")
        password_val = os.getenv("PROEIS_PASSWORD", "")
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        if not (login_val and password_val and gemini_key):
            return {
                "logged_in": bool(data.get("cookies")),
                "user_name": data.get("user_name", ""),
                "saved_at": str(data.get("saved_at") or ""),
            }

        client = ProeisHTTP(
            login=login_val,
            password=password_val,
            gemini_api_key=gemini_key,
            debug=False,
        )
        if _try_restore_session(client):
            user_name = data.get("user_name", "") or login_val
            saved_at = datetime.now(timezone.utc).isoformat()
            _session_collection().document("current").update({"saved_at": saved_at})
            return {"logged_in": True, "user_name": user_name, "saved_at": saved_at}

        _trigger_warmup_login()
        return {"logged_in": False, "user_name": "", "saved_at": ""}
    except Exception:
        return {"logged_in": False, "user_name": "", "saved_at": ""}

@app.post("/api/session-logout")
def session_logout():
    """Apaga a sessao persistida do Firestore, forcando novo login na proxima operacao."""
    load_env_file()
    try:
        _session_collection().document("current").delete()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

@app.post("/api/session-keepalive-web")
def session_keepalive_web(body: TestLoginRequest):
    """Mantem a sessao ativa a partir do painel web usando as credenciais da tela de login."""
    load_env_file()
    login_val = body.login or os.getenv("PROEIS_LOGIN", "")
    password_val = body.password or os.getenv("PROEIS_PASSWORD", "")
    gemini_key = body.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    if not login_val or not password_val or not gemini_key:
        return {"ok": False, "status": "sem_credenciais"}

    # Se uma operacao esta usando a conta, ela ja mantem a sessao viva. Nao mexer
    # (um login de keepalive aqui derrubaria a sessao da operacao — regra 1-sessao do PROEIS).
    if _account_is_busy():
        return {"ok": True, "status": "ocupada"}

    # Segura a conta durante o restore/login: o warmup paralelo (do session-status)
    # pula enquanto isso, evitando dois logins simultaneos na abertura do app.
    _account_enter()
    try:
        with _env_lock:
            _apply_runtime_options(body)
            client = ProeisHTTP(
                login=login_val,
                password=password_val,
                gemini_api_key=gemini_key,
                debug=False,
            )

        if _try_restore_session(client):
            doc = _session_collection().document("current").get()
            user_name = (doc.to_dict() or {}).get("user_name", login_val) if doc.exists else login_val
            _session_collection().document("current").update({
                "saved_at": datetime.now(timezone.utc).isoformat(),
            })
            return {"ok": True, "status": "ativa", "user_name": user_name}

        login_with_retries(client, "Keepalive via painel web")
        _fetch_user_name_and_save(client)
        doc = _session_collection().document("current").get()
        user_name = (doc.to_dict() or {}).get("user_name", login_val) if doc.exists else login_val
        return {"ok": True, "status": "renovada", "user_name": user_name}
    except Exception as exc:
        print(f"[KEEPALIVE] Erro no keepalive web: {exc}")
        return {"ok": False, "status": "erro", "message": str(exc)}
    finally:
        _account_exit()

from starlette.staticfiles import StaticFiles


@app.get("/api/captcha-dump")
def captcha_dump(n: int = 10, source: str = "login"):
    """Ferramenta de laboratorio: coleta N imagens de captcha reais e devolve
    em base64 (sem resolver). Usado pelos testes em captcha_tests/. Somente
    imagens anonimas de captcha; nao expoe dados sensiveis."""
    load_env_file()
    n = max(1, min(int(n), 200))
    login_val = os.getenv("PROEIS_LOGIN", "")
    password_val = os.getenv("PROEIS_PASSWORD", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")

    client = ProeisHTTP(login=login_val, password=password_val, gemini_api_key=gemini_key, debug=False)
    images: list[str] = []
    errors: list[str] = []

    try:
        if source == "filter":
            if not _try_restore_session(client):
                login_with_retries(client, "captcha-dump")
            client.navigate_to_service_page()
            soup = client.require_soup()
        else:
            base_soup = client.request("GET", DEFAULT_URL)
            payload = client.form_payload(base_soup)
            payload["ddlTipoAcesso"] = "ID"
            payload["__EVENTTARGET"] = "ddlTipoAcesso"
            soup = client.post_form(payload, DEFAULT_URL)

        for i in range(n):
            try:
                img = client.extract_captcha_image(soup)
                images.append(base64.b64encode(img).decode("ascii"))
            except Exception as exc:
                errors.append(f"#{i}: {exc}")
            if i < n - 1:
                refreshed = client.refresh_page_captcha(soup)
                if refreshed:
                    soup = refreshed
                elif source == "filter":
                    client.navigate_to_service_page()
                    soup = client.require_soup()
                else:
                    base_soup = client.request("GET", DEFAULT_URL)
                    payload = client.form_payload(base_soup)
                    payload["ddlTipoAcesso"] = "ID"
                    payload["__EVENTTARGET"] = "ddlTipoAcesso"
                    soup = client.post_form(payload, DEFAULT_URL)
    except Exception as exc:
        errors.append(f"fatal: {exc}")

    return {"ok": bool(images), "count": len(images), "images": images, "errors": errors}


@app.get("/api/captcha-dataset/stats")
def captcha_dataset_stats():
    """Estatisticas do dataset de captchas coletado do uso real (site = rotulador).
    Mostra quantas amostras temos e a taxa de acerto por modelo — base para
    medir melhorias e treinar um solver proprio."""
    # Total exato via agregacao (o servidor conta sem baixar doc a doc — custo baixo).
    total_exact = None
    try:
        total_exact = int(_captcha_collection().count().get()[0][0].value)
    except Exception:
        total_exact = None
    # Acuracia/por-modelo a partir de uma AMOSTRA recente limitada (custo constante,
    # nao cresce com a colecao). Evita ler a colecao inteira a cada chamada.
    sample_n = int(os.getenv("CAPTCHA_STATS_SAMPLE", "3000"))
    try:
        docs = list(
            _captcha_collection()
            .select(["accepted", "model", "created_at"])
            .order_by("created_at", direction="DESCENDING")
            .limit(sample_n)
            .stream()
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc), "total": total_exact or 0}
    sample = len(docs)
    accepted = 0
    by_model: dict[str, dict[str, int]] = {}
    for d in docs:
        v = d.to_dict() or {}
        ok = bool(v.get("accepted"))
        accepted += 1 if ok else 0
        m = v.get("model", "?")
        by_model.setdefault(m, {"total": 0, "accepted": 0})
        by_model[m]["total"] += 1
        by_model[m]["accepted"] += 1 if ok else 0
    for m, st in by_model.items():
        st["accuracy_pct"] = round(st["accepted"] / max(1, st["total"]) * 100, 1)
    return {
        "ok": True,
        "total": total_exact if total_exact is not None else sample,
        "sample": sample,
        "accepted": accepted,
        "accuracy_pct": round(accepted / max(1, sample) * 100, 1),
        "by_model": by_model,
    }


@app.get("/api/captcha-dataset/export")
def captcha_dataset_export(limit: int = 500, only_accepted: bool = True):
    """Exporta amostras do dataset (imagem base64 + resposta confirmada) para
    treinar/avaliar um solver offline. only_accepted=1 traz so os confirmados
    pelo site (rotulos corretos)."""
    limit = max(1, min(int(limit), 2000))
    try:
        q = _captcha_collection()
        if only_accepted:
            q = q.where("accepted", "==", True)
        docs = list(q.limit(limit).stream())
    except Exception as exc:
        return {"ok": False, "message": str(exc), "samples": []}
    samples = []
    for d in docs:
        v = d.to_dict() or {}
        samples.append({
            "image_b64": v.get("image_b64", ""),
            "answer": v.get("answer", ""),
            "accepted": bool(v.get("accepted")),
            "model": v.get("model", ""),
        })
    return {"ok": True, "count": len(samples), "samples": samples}


@app.post("/api/captcha-bench")
async def captcha_bench(request: Request):
    """
    Benchmark de resolucao de captcha: coleta imagens reais do PROEIS (login + filtro)
    e testa multiplos modelos/budgets Gemini. Retorna SSE com progresso e stats finais.
    """
    load_env_file()
    body = await request.json()
    n_login  = min(int(body.get("n_login", 15)), 25)
    n_filter = min(int(body.get("n_filter", 15)), 25)
    configs  = body.get("configs") or [
        {"model": "gemini-2.5-flash", "budget": 0,    "label": "flash-0"},
        {"model": "gemini-2.5-flash", "budget": 1024, "label": "flash-1024"},
        {"model": "gemini-2.5-flash", "budget": 4096, "label": "flash-4096"},
        {"model": "gemini-2.5-pro",   "budget": 1024, "label": "pro-1024"},
    ]

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    login_val  = os.getenv("PROEIS_LOGIN", "")
    pwd_val    = os.getenv("PROEIS_PASSWORD", "")

    if not gemini_key:
        return {"ok": False, "message": "GEMINI_API_KEY nao configurada."}

    async def generate():
        q: asyncio.Queue = asyncio.Queue()

        def run() -> None:
            def emit(msg: str, **kw) -> None:
                q.put_nowait({"type": "log", "line": msg, **kw})

            try:
                client = ProeisHTTP(
                    login=login_val, password=pwd_val,
                    gemini_api_key=gemini_key, debug=False,
                )

                # ── Collect login captchas ───────────────────────────────────────
                # O captcha de login aparece APÓS o postback ddlTipoAcesso=ID,
                # não no GET inicial da página.
                login_images: list[bytes] = []
                emit(f"[LOGIN] Coletando {n_login} imagens da tela de login (Default.aspx)...")
                try:
                    base_soup = client.request("GET", DEFAULT_URL)
                    payload = client.form_payload(base_soup)
                    payload["ddlTipoAcesso"] = "ID"
                    payload["__EVENTTARGET"] = "ddlTipoAcesso"
                    soup = client.post_form(payload, DEFAULT_URL)
                    for i in range(n_login):
                        try:
                            img = client.extract_captcha_image(soup)
                            login_images.append(img)
                            emit(f"[LOGIN] #{i+1}/{n_login}: {len(img)}B extraidos")
                            if i < n_login - 1:
                                refreshed = client.refresh_page_captcha(soup)
                                if refreshed:
                                    soup = refreshed
                                else:
                                    base_soup = client.request("GET", DEFAULT_URL)
                                    payload = client.form_payload(base_soup)
                                    payload["ddlTipoAcesso"] = "ID"
                                    payload["__EVENTTARGET"] = "ddlTipoAcesso"
                                    soup = client.post_form(payload, DEFAULT_URL)
                        except Exception as exc:
                            emit(f"[LOGIN] #{i+1}: erro ao extrair - {exc}")
                except Exception as exc:
                    emit(f"[LOGIN] Falha ao acessar Default.aspx: {exc}")

                # ── Collect filter captchas ──────────────────────────────────────
                # FrmEventoAssociar requer navegacao via menu apos login.
                filter_images: list[bytes] = []
                if login_val and pwd_val:
                    emit(f"[FILTRO] Coletando {n_filter} imagens da tela de filtro (FrmEventoAssociar)...")
                    try:
                        if not _try_restore_session(client):
                            emit("[FILTRO] Sessao expirada; fazendo login...")
                            login_with_retries(client, "benchmark-captcha")
                        client.navigate_to_service_page()
                        fsoup = client.require_soup()
                        for i in range(n_filter):
                            try:
                                img = client.extract_captcha_image(fsoup)
                                filter_images.append(img)
                                emit(f"[FILTRO] #{i+1}/{n_filter}: {len(img)}B extraidos")
                                if i < n_filter - 1:
                                    refreshed = client.refresh_page_captcha(fsoup)
                                    if refreshed:
                                        fsoup = refreshed
                                    else:
                                        client.navigate_to_service_page()
                                        fsoup = client.require_soup()
                            except Exception as exc:
                                emit(f"[FILTRO] #{i+1}: erro ao extrair - {exc}")
                    except Exception as exc:
                        emit(f"[FILTRO] Nao foi possivel coletar: {exc}")
                else:
                    emit("[FILTRO] Credenciais ausentes; pulando captchas de filtro.")

                # ── Analyse image characteristics ────────────────────────────────
                login_sizes  = [len(b) for b in login_images]
                filter_sizes = [len(b) for b in filter_images]
                emit(f"\n[INFO] Login:  {len(login_images)} imagens | tamanho médio {round(sum(login_sizes)/max(1,len(login_sizes)))}B")
                emit(f"[INFO] Filtro: {len(filter_images)} imagens | tamanho médio {round(sum(filter_sizes)/max(1,len(filter_sizes)))}B")
                same_size = (
                    login_sizes and filter_sizes and
                    abs(sum(login_sizes)/len(login_sizes) - sum(filter_sizes)/len(filter_sizes)) < 200
                )
                emit(f"[INFO] Captchas login vs filtro parecem {'IGUAIS (mesmo gerador)' if same_size else 'DIFERENTES (geradores distintos)'}")

                # ── Solve each image with each config ────────────────────────────
                all_items = [("login", b) for b in login_images] + [("filter", b) for b in filter_images]
                total = len(all_items)
                emit(f"\n[BENCH] {total} imagens x {len(configs)} configs = {total*len(configs)} chamadas Gemini")

                all_results: list[dict] = []

                # Limit 429 wait to 10s inside benchmark to avoid stalling
                _prev_429_wait = os.environ.get("GEMINI_429_RETRY_WAIT")
                os.environ["GEMINI_429_RETRY_WAIT"] = "10"

                for idx, (source, img) in enumerate(all_items):
                    row: dict = {"source": source, "i": idx + 1, "configs": {}}
                    # inclui a imagem crua (base64) das primeiras para leitura de gabarito
                    if idx < int(body.get("return_images", 8)):
                        row["img_b64"] = base64.b64encode(img).decode("ascii")
                    for cfg in configs:
                        model  = cfg.get("model", "gemini-2.5-flash")
                        budget = int(cfg.get("budget", 0))
                        preprocess = cfg.get("preprocess")
                        label  = str(cfg.get("label") or f"{model.split('gemini-')[-1]}-b{budget}")
                        t0 = time.monotonic()
                        try:
                            res = client._solve_via_gemini_result(img, model=model, thinking_budget=budget, preprocess=preprocess)
                            elapsed = round(time.monotonic() - t0, 2)
                            raw = res.text.strip()
                            norm = normalize_captcha_answer(raw)
                            valid = is_valid_captcha_answer(norm)
                            row["configs"][label] = {"ok": True, "answer": norm, "valid": valid, "elapsed": elapsed}
                            emit(f"[{idx+1}/{total}] {source} | {label}: {norm} {'✓' if valid else '✗'} {elapsed}s")
                        except Exception as exc:
                            elapsed = round(time.monotonic() - t0, 2)
                            row["configs"][label] = {"ok": False, "error": str(exc)[:120], "elapsed": elapsed}
                            emit(f"[{idx+1}/{total}] {source} | {label}: ERRO {elapsed}s — {str(exc)[:80]}")
                    all_results.append(row)

                if _prev_429_wait is not None:
                    os.environ["GEMINI_429_RETRY_WAIT"] = _prev_429_wait
                else:
                    os.environ.pop("GEMINI_429_RETRY_WAIT", None)

                # ── Aggregate stats ──────────────────────────────────────────────
                stats: dict = {}
                for cfg in configs:
                    label = str(cfg.get("label") or f"{cfg.get('model','?').split('gemini-')[-1]}-b{cfg.get('budget',0)}")
                    rows  = [r["configs"][label] for r in all_results if label in r["configs"]]
                    ok    = [r for r in rows if r.get("ok")]
                    valid = [r for r in ok if r.get("valid")]
                    times = [r["elapsed"] for r in ok]
                    import statistics as _st
                    stats[label] = {
                        "n": len(rows),
                        "ok_pct":    round(len(ok)    / max(1, len(rows)) * 100, 1),
                        "valid_pct": round(len(valid) / max(1, len(rows)) * 100, 1),
                        "mean_s":    round(sum(times) / max(1, len(times)), 2),
                        "median_s":  round(_st.median(times) if times else 0, 2),
                        "min_s":     round(min(times) if times else 0, 2),
                        "max_s":     round(max(times) if times else 0, 2),
                    }

                # per-source valid_pct breakdown
                source_stats: dict = {}
                for src in ("login", "filter"):
                    src_rows = [r for r in all_results if r["source"] == src]
                    if not src_rows:
                        continue
                    src_stats: dict = {}
                    for cfg in configs:
                        label = str(cfg.get("label") or f"{cfg.get('model','?').split('gemini-')[-1]}-b{cfg.get('budget',0)}")
                        rows  = [r["configs"][label] for r in src_rows if label in r["configs"]]
                        valid = [r for r in rows if r.get("ok") and r.get("valid")]
                        times = [r["elapsed"] for r in rows if r.get("ok")]
                        src_stats[label] = {
                            "valid_pct": round(len(valid) / max(1, len(rows)) * 100, 1),
                            "mean_s":    round(sum(times) / max(1, len(times)), 2) if times else 0,
                        }
                    source_stats[src] = src_stats

                q.put_nowait({"type": "done", "stats": stats, "source_stats": source_stats, "detail": all_results})

            except Exception as exc:
                q.put_nowait({"type": "error", "message": str(exc)})
            finally:
                q.put_nowait(None)

        loop = asyncio.get_event_loop()
        threading.Thread(target=run, daemon=True).start()

        while True:
            item = await q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── Frontend com versao automatica de cache ─────────────────────────────────
# O index.html e servido dinamicamente com ?v=<hash do conteudo dos assets>.
# Quando um deploy muda app.js/styles.css, as URLs mudam e o navegador baixa a
# versao nova sozinho — sem precisar de recarregamento forcado no celular.
def _asset_version() -> str:
    import hashlib
    h = hashlib.md5()
    for name in ("index.html", "app.js", "styles.css", "tailwind.css", "proeis-fixes.js", "favicon.svg", "manifest.webmanifest"):
        try:
            h.update((ROOT / "web" / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


_INDEX_HTML = re.sub(
    rb"\?v=[0-9A-Za-z._-]+",
    f"?v={_asset_version()}".encode(),
    (ROOT / "web" / "index.html").read_bytes(),
)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page():
    return Response(
        content=_INDEX_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="static")

handler = app
