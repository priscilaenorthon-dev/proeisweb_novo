import argparse
import atexit
import base64
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from twocaptcha import TwoCaptcha
    _TWOCAPTCHA_SDK = True
except ImportError:
    _TWOCAPTCHA_SDK = False

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass


try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass

LOG_DIR = Path("logs")
_LOG_FILE_HANDLE = None


# ── Logger ────────────────────────────────────────────────────────────────────

def _log(tag: str, msg: str) -> None:
    """Imprime linha de log com timestamp e tag categorizadora."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{tag:<9}] {msg}")


def _step(current: int, total: int, tag: str, msg: str) -> None:
    """Imprime etapa numerada de um fluxo."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{tag:<9}] Etapa {current}/{total}: {msg}")


# ── Tee (stdout + arquivo) ────────────────────────────────────────────────────

class _Tee:
    """Escreve simultaneamente em múltiplos streams (ex: stdout + arquivo de log)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def _setup_log() -> Path:
    global _LOG_FILE_HANDLE
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{ts}_http.log"
    log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    _LOG_FILE_HANDLE = log_file
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)
    return log_path


def _close_log_file() -> None:
    global _LOG_FILE_HANDLE
    if _LOG_FILE_HANDLE is None:
        return
    try:
        _LOG_FILE_HANDLE.flush()
        _LOG_FILE_HANDLE.close()
    except Exception:
        pass
    finally:
        _LOG_FILE_HANDLE = None


atexit.register(_close_log_file)


# ── URLs ──────────────────────────────────────────────────────────────────────

BASE_URL = "https://www.proeis.rj.gov.br/"
DEFAULT_URL = urljoin(BASE_URL, "Default.aspx")
MENU_URL = urljoin(BASE_URL, "FrmMenuVoluntario.aspx")
INSCRICOES_URL = urljoin(BASE_URL, "FrmVoluntarioInscricoesConsultar.aspx")
ASSOCIAR_URL = urljoin(BASE_URL, "FrmEventoAssociar.aspx")


class AutomationError(RuntimeError):
    pass


class CaptchaInvalidAnswerError(AutomationError):
    def __init__(self, answer: str, raw_answer: str):
        self.answer = answer
        self.raw_answer = raw_answer
        super().__init__(f"2captcha retornou resposta invalida para captcha de 6 caracteres: {answer or raw_answer!r}")


@dataclass
class Candidate:
    label: str
    action: str
    payload: dict[str, str]
    score: int


@dataclass
class CaptchaSubmission:
    text: str
    captcha_id: str | None
    solver_index: int


def norm(value: str | None) -> str:
    value = value or ""
    value = value.replace("º", "o").replace("°", "o")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value.lower()).strip()


def normalize_captcha_answer(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def is_valid_captcha_answer(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{6}", normalize_captcha_answer(value)))


def coerce_scan_rounds(value: int) -> int:
    return max(1, int(value))


def next_scan_date_index(current_index: int, found_candidate: bool) -> int:
    return current_index + 1


def first_scan_date_index(dates: list[tuple[str, str]], start_date: str = "") -> int:
    if not start_date:
        return 0

    normalized_start = normalize_date_for_site(start_date)
    for index, (_, label) in enumerate(dates):
        if normalize_date_for_site(label) == normalized_start:
            return index

    try:
        start_dt = datetime.strptime(normalized_start, "%Y-%m-%d").date()
    except ValueError:
        return 0

    for index, (_, label) in enumerate(dates):
        try:
            if datetime.strptime(normalize_date_for_site(label), "%Y-%m-%d").date() >= start_dt:
                return index
        except ValueError:
            continue
    return len(dates)


def display_date_for_log(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return normalize_date_for_site(value)


def emit_vaga(label: str, data_evento: str = "", acao: str = "Visualizacao") -> None:
    print("[VAGA] " + json.dumps({"data": display_date_for_log(data_evento), "acao": acao, "label": label}, ensure_ascii=False))


# ── Cliente HTTP ──────────────────────────────────────────────────────────────

class ProeisHTTP:
    def __init__(self, login: str, password: str, captcha_key: str, debug: bool = True):
        self.login = login
        self.password = password
        self.captcha_key = captcha_key
        self.debug = debug
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.7,en;q=0.6",
                "Origin": BASE_URL.rstrip("/"),
                "Referer": DEFAULT_URL,
            }
        )
        self.last_url = DEFAULT_URL
        self.soup: BeautifulSoup | None = None
        self.last_captcha_id: str | None = None
        self.site_elapsed_seconds = 0.0
        self.captcha_elapsed_seconds = 0.0
        _log("INFO", f"SDK 2captcha-python {'disponivel' if _TWOCAPTCHA_SDK else 'NAO instalada — usando HTTP direto (instale: pip install 2captcha-python)'}")

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def request(self, method: str, url: str, **kwargs) -> BeautifulSoup:
        last_error: Exception | None = None
        max_attempts = int(os.getenv("PROEIS_HTTP_ATTEMPTS", "2"))
        connect_timeout = int(os.getenv("PROEIS_CONNECT_TIMEOUT", "8"))
        read_timeout = int(os.getenv("PROEIS_READ_TIMEOUT", "25"))
        short_url = url.split("/")[-1] or url
        for attempt in range(1, max_attempts + 1):
            t0 = time.monotonic()
            counted_elapsed = False
            try:
                _log("HTTP", f"{method} {short_url} (tentativa {attempt}/{max_attempts})...")
                response = self.session.request(method, url, timeout=(connect_timeout, read_timeout), **kwargs)
                elapsed = time.monotonic() - t0
                elapsed_ms = int(elapsed * 1000)
                self.site_elapsed_seconds += elapsed
                counted_elapsed = True
                response.raise_for_status()
                self.last_url = response.url
                self.soup = BeautifulSoup(response.text, "html.parser")
                _log("HTTP", f"{method} {short_url} -> {response.status_code} ({elapsed_ms}ms, {len(response.text)} chars)")
                return self.soup
            except requests.RequestException as exc:
                if not counted_elapsed:
                    self.site_elapsed_seconds += time.monotonic() - t0
                last_error = exc
                if attempt == max_attempts:
                    break
                wait = 2
                _log("HTTP", f"Falha de rede em {short_url}; nova tentativa em {wait}s ({attempt}/{max_attempts}): {exc}")
                time.sleep(wait)
        raise AutomationError(f"Falha de rede acessando {url}: {last_error}")

    def form_payload(self, soup: BeautifulSoup | None = None) -> dict[str, str]:
        soup = soup or self.require_soup()
        payload: dict[str, str] = {}
        for tag in soup.select("input[name], select[name], textarea[name]"):
            name = tag.get("name")
            if not name:
                continue
            if tag.name == "select":
                selected = tag.select_one("option[selected]")
                option = selected or tag.select_one("option")
                payload[name] = option.get("value", option.get_text(strip=True)) if option else ""
            elif tag.name == "textarea":
                payload[name] = tag.get_text()
            elif tag.get("type") in {"checkbox", "radio"}:
                if tag.has_attr("checked"):
                    payload[name] = tag.get("value", "on")
            elif tag.get("type") not in {"submit", "button", "image", "file"}:
                payload[name] = tag.get("value", "")
        vs_keys = [k for k in payload if k.startswith("__VIEWSTATE")]
        _log("FORM", f"Payload extraido: {len(payload)} campo(s); ViewState keys: {vs_keys}")
        return payload

    def require_soup(self) -> BeautifulSoup:
        if self.soup is None:
            raise AutomationError("Nenhuma pagina carregada.")
        return self.soup

    def post_form(self, payload: dict[str, str], url: str | None = None) -> BeautifulSoup:
        target = url or self.last_url
        safe_fields = {k: v for k, v in payload.items() if "senha" not in k.lower() and "password" not in k.lower()}
        _log("FORM", f"POST -> {target.split('/')[-1]} | {len(payload)} campo(s) | Captcha: {'TextCaptcha' in payload}")
        return self.request("POST", target, data=payload)

    def postback(self, target: str, argument: str = "") -> BeautifulSoup:
        _log("FORM", f"PostBack: target='{target}' argument='{argument}'")
        payload = self.form_payload()
        payload["__EVENTTARGET"] = target
        payload["__EVENTARGUMENT"] = argument
        return self.post_form(payload)

    # ── Login ─────────────────────────────────────────────────────────────────

    def login_flow(self) -> None:
        _log("LOGIN", "=== Iniciando fluxo de login ===")
        max_attempts = 6

        _step(1, 5, "LOGIN", f"Carregando pagina inicial: {DEFAULT_URL}")
        soup = self.request("GET", DEFAULT_URL)
        payload = self.form_payload(soup)

        _step(2, 5, "LOGIN", "Selecionando tipo de acesso: ID Funcional (ddlTipoAcesso=ID)")
        payload["ddlTipoAcesso"] = "ID"
        payload["__EVENTTARGET"] = "ddlTipoAcesso"
        soup = self.post_form(payload, DEFAULT_URL)

        for attempt in range(1, max_attempts + 1):
            _step(3, 5, "LOGIN", f"Resolvendo captcha da tela de login (tentativa {attempt}/{max_attempts})...")
            soup, captcha_text = self.solve_page_captcha(soup)

            _step(4, 5, "LOGIN", f"Submetendo credenciais (login={self.login}, captcha={captcha_text})...")
            payload = self.form_payload(soup)
            payload.update(
                {
                    "ddlTipoAcesso": "ID",
                    "txtLogin": self.login,
                    "txtSenha": self.password_for_form(soup),
                    "TextCaptcha": captcha_text,
                    "btnEntrar": "Avançar",
                }
            )
            soup = self.post_form(payload, DEFAULT_URL)
            page_text = norm(soup.get_text(" ", strip=True))

            if not soup.select_one("#txtSenha") and not soup.select_one("#TextCaptcha"):
                _step(5, 5, "LOGIN", "Login realizado com sucesso. Sessao autenticada.")
                return

            if "senha invalida" in page_text:
                raise AutomationError("Login recusado: senha invalida.")

            if "erro ao confirmar imagem" in page_text:
                _log("LOGIN", f"Captcha recusado pelo site (tentativa {attempt}/{max_attempts}); reportando erro e tentando novamente...")
                self.report_bad_captcha()
                continue

            raise AutomationError("Login nao saiu da tela inicial. Verifique a mensagem retornada pelo site no log.")

        raise AutomationError("Captcha de login falhou em todas as tentativas.")

    def password_for_form(self, soup: BeautifulSoup) -> str:
        field = soup.select_one("#txtSenha") or soup.select_one('input[name="txtSenha"]')
        max_length = field.get("maxlength") if field else None
        if max_length and max_length.isdigit():
            limit = int(max_length)
            if limit > 0 and len(self.password) > limit:
                _log("LOGIN", f"Senha maior que maxlength={limit}; truncando como o navegador faria.")
                return self.password[:limit]
        return self.password

    # ── Captcha ───────────────────────────────────────────────────────────────

    def extract_captcha_image(self, soup: BeautifulSoup) -> bytes:
        _log("CAPTCHA", "Procurando imagem de captcha no HTML da pagina...")
        html = str(soup)
        match = re.search(
            r"background:\s*url\(['\"]data:image/png;base64,([^'\"]+)['\"]",
            html,
        )
        if not match:
            # fallback: qualquer data:image/png;base64 inline
            match = re.search(r"data:image/png;base64,([^'\";)]+)", html)
        if not match:
            raise AutomationError("Nao encontrei a imagem do captcha no HTML.")
        raw_b64 = match.group(1).strip()
        image_bytes = base64.b64decode(raw_b64)
        _log("CAPTCHA", f"Imagem extraida: {len(raw_b64)} chars base64 -> {len(image_bytes)} bytes PNG")
        return image_bytes

    def solve_captcha(self, image: bytes) -> str:
        attempts = int(os.getenv("TWOCAPTCHA_INVALID_RETRIES", "2")) + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                return self.solve_captcha_once(image)
            except AutomationError as exc:
                last_error = str(exc)
                if "resposta invalida" not in last_error or attempt == attempts:
                    raise
                _log("CAPTCHA", f"Resposta fora do padrao; reenviando captcha (tentativa {attempt}/{attempts}).")
        raise AutomationError(last_error or "2captcha nao retornou uma resposta valida.")

    def solve_page_captcha(self, soup: BeautifulSoup, refresh_after_invalids: int | None = None) -> tuple[BeautifulSoup, str]:
        max_attempts = int(os.getenv("TWOCAPTCHA_INVALID_RETRIES", "2")) + 1
        refresh_after_invalids = refresh_after_invalids or int(os.getenv("TWOCAPTCHA_REFRESH_AFTER_INVALIDS", "2"))
        refresh_after_invalids = max(1, refresh_after_invalids)
        invalid_streak = 0
        last_error = ""
        current_soup = soup

        for attempt in range(1, max_attempts + 1):
            _log("CAPTCHA", f"Tentativa de resolucao {attempt}/{max_attempts}...")
            try:
                captcha = self.extract_captcha_image(current_soup)
                text = self.solve_captcha_once(captcha)
                return current_soup, text
            except CaptchaInvalidAnswerError as exc:
                last_error = str(exc)
                if attempt == max_attempts:
                    raise

                invalid_streak += 1
                answer_len = len(exc.answer)
                refresh_now = answer_len in {4, 5}
                if refresh_now:
                    _log("CAPTCHA", f"Resposta invalida com {answer_len} caracteres ({exc.answer!r}); trocando imagem agora.")
                else:
                    _log("CAPTCHA", f"Resposta invalida (streak={invalid_streak}, limite={refresh_after_invalids}).")

                if refresh_now or invalid_streak >= refresh_after_invalids:
                    _log("CAPTCHA", "Solicitando nova imagem de captcha ao PROEIS...")
                    refreshed = self.refresh_page_captcha(current_soup)
                    if refreshed is not None:
                        current_soup = refreshed
                        invalid_streak = 0
                        _log("CAPTCHA", "Nova imagem obtida. Reiniciando resolucao.")
                        continue

                _log("CAPTCHA", f"Reenviando imagem atual para 2captcha (tentativa {attempt}/{max_attempts}).")
            except AutomationError as exc:
                last_error = str(exc)
                if "resposta invalida" not in last_error or attempt == max_attempts:
                    raise

                invalid_streak += 1
                _log("CAPTCHA", f"Resposta invalida (streak={invalid_streak}, limite={refresh_after_invalids}).")
                if invalid_streak >= refresh_after_invalids:
                    _log("CAPTCHA", "Solicitando nova imagem de captcha ao PROEIS...")
                    refreshed = self.refresh_page_captcha(current_soup)
                    if refreshed is not None:
                        current_soup = refreshed
                        invalid_streak = 0
                        _log("CAPTCHA", "Nova imagem obtida. Reiniciando resolucao.")
                        continue

                _log("CAPTCHA", f"Reenviando imagem atual para 2captcha (tentativa {attempt}/{max_attempts}).")

        raise AutomationError(last_error or "2captcha nao retornou uma resposta valida.")

    def solve_captcha_once(self, image: bytes) -> str:
        """Envia imagem ao 2captcha e retorna o texto resolvido."""
        t0 = time.monotonic()
        try:
            parallel_solves = max(1, int(os.getenv("TWOCAPTCHA_PARALLEL_SOLVES", "2")))
            if parallel_solves == 1:
                return self._solve_single_captcha_submission(image, 1)
            return self._solve_parallel_captcha_submissions(image, parallel_solves)
        finally:
            self.captcha_elapsed_seconds += time.monotonic() - t0

    def _solve_single_captcha_submission(self, image: bytes, solver_index: int) -> str:
        result = self._solve_single_captcha_submission_result(image, solver_index)
        self.last_captcha_id = result.captcha_id
        return result.text

    def _solve_single_captcha_submission_result(
        self,
        image: bytes,
        solver_index: int,
        stop_event: threading.Event | None = None,
    ) -> CaptchaSubmission:
        _log("CAPTCHA", f"Resolucao {solver_index}: enviando captcha.")
        if _TWOCAPTCHA_SDK:
            return self._solve_via_sdk_result(image, solver_index)
        return self._solve_via_http_result(image, solver_index, stop_event)

    def _solve_parallel_captcha_submissions(self, image: bytes, parallel_solves: int) -> str:
        _log("CAPTCHA", f"Captcha paralelo ativado: {parallel_solves} envio(s) simultaneo(s).")
        errors: list[str] = []
        stop_event = threading.Event()
        executor = ThreadPoolExecutor(max_workers=parallel_solves)
        futures = [
            executor.submit(self._solve_single_captcha_submission_result, image, solver_index, stop_event)
            for solver_index in range(1, parallel_solves + 1)
        ]
        try:
            for future in as_completed(futures):
                try:
                    result = future.result()
                except CaptchaInvalidAnswerError as exc:
                    errors.append(str(exc))
                    _log("CAPTCHA", f"Resposta paralela invalida: {exc}")
                    continue
                except AutomationError as exc:
                    errors.append(str(exc))
                    _log("CAPTCHA", f"Resolucao paralela falhou: {exc}")
                    continue

                text = normalize_captcha_answer(result.text)
                if is_valid_captcha_answer(text):
                    self.last_captcha_id = result.captcha_id
                    stop_event.set()
                    _log(
                        "CAPTCHA",
                        f"Captcha paralelo vencedor: envio {result.solver_index}, id={result.captcha_id}, resposta={text}",
                    )
                    executor.shutdown(wait=False, cancel_futures=True)
                    return text

                errors.append(f"envio {result.solver_index}: resposta invalida {result.text!r}")
                _log("CAPTCHA", f"Envio {result.solver_index} retornou formato invalido: {result.text!r}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        raise CaptchaInvalidAnswerError("", "; ".join(errors) or "sem resposta valida")

    def _solve_via_sdk(self, image: bytes) -> str:
        result = self._solve_via_sdk_result(image, 1)
        self.last_captcha_id = result.captcha_id
        return result.text

    def _solve_via_sdk_result(self, image: bytes, solver_index: int) -> CaptchaSubmission:
        """Resolve captcha usando o SDK oficial 2captcha-python (v2.x)."""
        max_wait = int(os.getenv("TWOCAPTCHA_MAX_WAIT", "75"))
        poll_interval = float(os.getenv("TWOCAPTCHA_POLL_INTERVAL", "1.5"))
        _log("CAPTCHA", f"[SDK] Inicializando TwoCaptcha (pollingInterval={poll_interval}s, timeout={max_wait}s)...")
        solver = TwoCaptcha(
            os.environ["TWOCAPTCHA_API_KEY"],
            pollingInterval=poll_interval,
            defaultTimeout=max_wait,
        )
        b64 = base64.b64encode(image).decode("ascii")
        # get_method() do SDK v2 detecta base64 por: sem '.' e len>50
        _log("CAPTCHA", f"[SDK] Enviando imagem ({len(b64)} chars base64) com: numeric=0, minLen=5, maxLen=6, caseSensitive=0, polling={poll_interval}s...")
        t0 = time.monotonic()
        try:
            result = solver.normal(
                b64,
                numeric=0,
                minLen=5,
                maxLen=6,
                caseSensitive=0,
            )
        except Exception as exc:
            raise AutomationError(f"2captcha SDK erro ao resolver captcha: {exc}")

        elapsed = int((time.monotonic() - t0) * 1000)
        captcha_id = result.get("captchaId") or result.get("id") or ""
        captcha_id = str(captcha_id) if captcha_id else None
        # No SDK v2, result['code'] é a string bruta retornada pela API
        raw_code = str(result.get("code", ""))
        text = normalize_captcha_answer(raw_code)
        _log("CAPTCHA", f"[SDK] Resposta em {elapsed}ms: '{raw_code}' -> normalizado: '{text}' (captchaId={captcha_id})")

        if not is_valid_captcha_answer(text):
            self.report_bad_captcha_id(captcha_id)
            raise CaptchaInvalidAnswerError(text, raw_code)

        _log("CAPTCHA", f"[SDK] Captcha resolvido com sucesso: {text}")
        return CaptchaSubmission(text=text, captcha_id=captcha_id, solver_index=solver_index)

    def _solve_via_http(self, image: bytes) -> str:
        result = self._solve_via_http_result(image, 1)
        self.last_captcha_id = result.captcha_id
        return result.text

    def _solve_via_http_result(
        self,
        image: bytes,
        solver_index: int,
        stop_event: threading.Event | None = None,
    ) -> CaptchaSubmission:
        """Resolve captcha via HTTP direto (fallback sem SDK)."""
        _log("CAPTCHA", "[HTTP] Enviando captcha para 2captcha via HTTP direto (SDK nao instalada)...")
        submit_timeout = int(os.getenv("TWOCAPTCHA_SUBMIT_TIMEOUT", "45"))
        result_timeout = int(os.getenv("TWOCAPTCHA_RESULT_TIMEOUT", "30"))
        response = requests.post(
            "https://2captcha.com/in.php",
            data={
                "key": self.captcha_key,
                "method": "base64",
                "body": base64.b64encode(image).decode("ascii"),
                "json": 1,
                "regsense": 1,
                "numeric": 4,
                "min_len": 6,
                "max_len": 6,
            },
            timeout=submit_timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != 1:
            raise AutomationError(f"2captcha recusou envio: {data}")
        captcha_id = data["request"]
        _log("CAPTCHA", f"[HTTP] Captcha enviado. captchaId={captcha_id}")

        initial_wait = float(os.getenv("TWOCAPTCHA_INITIAL_WAIT", "5"))
        poll_interval = float(os.getenv("TWOCAPTCHA_POLL_INTERVAL", "3"))
        max_wait = float(os.getenv("TWOCAPTCHA_MAX_WAIT", "75"))
        deadline = time.monotonic() + max_wait
        _log("CAPTCHA", f"[HTTP] Aguardando resultado (inicial={initial_wait}s, polling={poll_interval}s, max={max_wait}s)...")
        self._sleep_or_cancel_parallel_captcha(initial_wait, stop_event)
        poll_count = 0
        while time.monotonic() < deadline:
            if stop_event and stop_event.is_set():
                raise AutomationError("resolucao paralela cancelada apos vencedor")
            poll_count += 1
            result = requests.get(
                "https://2captcha.com/res.php",
                params={"key": self.captcha_key, "action": "get", "id": captcha_id, "json": 1},
                timeout=result_timeout,
            )
            if result.status_code >= 500:
                _log("CAPTCHA", f"[HTTP] Poll {poll_count}: erro {result.status_code} no servidor 2captcha, tentando novamente...")
                self._sleep_or_cancel_parallel_captcha(poll_interval, stop_event)
                continue
            result.raise_for_status()
            solved = result.json()
            if solved.get("status") == 1:
                text = normalize_captcha_answer(solved["request"])
                _log("CAPTCHA", f"[HTTP] Resolvido apos {poll_count} poll(s): '{solved['request']}' -> '{text}'")
                if not is_valid_captcha_answer(text):
                    self.report_bad_captcha_id(captcha_id)
                    raise CaptchaInvalidAnswerError(text, str(solved.get("request", "")))
                _log("CAPTCHA", f"[HTTP] Captcha valido: {text}")
                return CaptchaSubmission(text=text, captcha_id=captcha_id, solver_index=solver_index)
            if solved.get("request") != "CAPCHA_NOT_READY":
                raise AutomationError(f"2captcha retornou erro: {solved}")
            _log("CAPTCHA", f"[HTTP] Poll {poll_count}: ainda processando... (restam {int(deadline - time.monotonic())}s)")
            self._sleep_or_cancel_parallel_captcha(poll_interval, stop_event)
        raise AutomationError("2captcha nao respondeu em tempo util.")

    def _sleep_or_cancel_parallel_captcha(self, seconds: float, stop_event: threading.Event | None = None) -> None:
        if not stop_event:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise AutomationError("resolucao paralela cancelada apos vencedor")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def normalize_captcha_answer(self, value: str) -> str:
        return normalize_captcha_answer(value)

    def report_bad_captcha(self) -> None:
        self.report_bad_captcha_id(self.last_captcha_id)

    def report_bad_captcha_id(self, captcha_id: str | None) -> None:
        if not captcha_id:
            return
        _log("CAPTCHA", f"Reportando captcha incorreto ao 2captcha (id={captcha_id})...")
        try:
            requests.get(
                "https://2captcha.com/res.php",
                params={"key": self.captcha_key, "action": "reportbad", "id": captcha_id, "json": 1},
                timeout=15,
            )
            _log("CAPTCHA", "Captcha incorreto reportado com sucesso.")
        except requests.RequestException as exc:
            _log("CAPTCHA", f"Nao foi possivel reportar captcha incorreto: {exc}")

    def refresh_page_captcha(self, soup: BeautifulSoup) -> BeautifulSoup | None:
        _log("CAPTCHA", "Procurando controle para gerar nova imagem de captcha...")
        control = soup.select_one("#lnkNewCaptcha, [name=lnkNewCaptcha]")
        if not control:
            for candidate in soup.select("a[href], input[type=submit][name], button[name]"):
                text = norm(
                    " ".join(
                        [
                            candidate.get_text(" ", strip=True),
                            candidate.get("value", ""),
                            candidate.get("id", ""),
                            candidate.get("name", ""),
                            candidate.get("href", ""),
                        ]
                    )
                )
                if "gerar nova imagem" in text or "newcaptcha" in text:
                    control = candidate
                    break
        if not control:
            _log("CAPTCHA", "Controle de nova imagem nao encontrado; mantendo imagem atual.")
            return None

        _log("CAPTCHA", f"Controle encontrado: id='{control.get('id')}' name='{control.get('name')}'")
        payload = self.form_payload(soup)
        name = control.get("name") or control.get("id")
        href = control.get("href", "")
        postback = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
        if postback:
            payload["__EVENTTARGET"] = postback.group(1)
            payload["__EVENTARGUMENT"] = postback.group(2)
        elif name:
            payload["__EVENTTARGET"] = name
            payload["__EVENTARGUMENT"] = ""
            if control.name in {"input", "button"} and control.get("name"):
                payload[control.get("name")] = control.get("value", "")
        else:
            _log("CAPTCHA", "Controle sem acao identificavel; abortando refresh.")
            return None

        _log("CAPTCHA", "Solicitando nova imagem via postback...")
        return self.post_form(payload)

    # ── Navegação ─────────────────────────────────────────────────────────────

    def navigate_to_service_page(self) -> None:
        # Se ja estamos na tela de filtros (ex: apos um "Eu Vou" confirmado),
        # nao volta ao menu — evita passar por FrmVoluntarioInscricoesConsultar
        # que altera o VIEWSTATE e faz o site retornar 0 resultados.
        if self.soup is not None and self.has_service_fields(self.soup):
            _log("NAV", "Ja na tela de servicos (FrmEventoAssociar). Reutilizando para proxima marcacao.")
            return
        _log("NAV", "=== Navegando para tela de servicos ===")
        _log("NAV", f"GET {MENU_URL.split('/')[-1]}...")
        soup = self.request("GET", MENU_URL)

        if soup.select_one("#btnEscala") or "btnEscala" in str(soup):
            _log("NAV", "Botao 'Escala' encontrado. Clicando via postback...")
            soup = self.postback("btnEscala")

        if self.has_service_fields(soup):
            _log("NAV", "Tela de associar voluntario encontrada (campos de convenio/CPA presentes).")
            return

        _log("NAV", "Procurando link 'Nova Inscricao'...")
        new_subscription = self.find_action_by_text(soup, ("nova inscricao", "nova inscrição"))
        if new_subscription:
            _log("NAV", f"Link encontrado: '{new_subscription.label}' -> acao='{new_subscription.action}'")
            if new_subscription.action == "postback":
                soup = self.postback(new_subscription.payload["target"], new_subscription.payload.get("argument", ""))
            elif new_subscription.action == "submit":
                soup = self.post_form(new_subscription.payload)
            else:
                soup = self.request("GET", new_subscription.action)
            if self.has_service_fields(soup):
                _log("NAV", "Tela de associar voluntario encontrada apos clicar em Nova Inscricao.")
                return

        keywords = (
            "inscricao", "inscrever", "servico", "servicos",
            "evento", "eventos", "escala", "minhas inscricoes",
        )
        for nav_step in range(1, 7):
            soup = self.require_soup()
            if self.has_service_fields(soup):
                _log("NAV", f"Tela de servico encontrada (etapa de navegacao {nav_step}).")
                return
            candidate = self.best_navigation_link(soup, keywords)
            if not candidate:
                _log("NAV", "Nenhum link de navegacao encontrado.")
                break
            _log("NAV", f"Navegando para: '{candidate.label}' (score={candidate.score}, etapa {nav_step}/6)...")
            if candidate.action == "postback":
                self.postback(candidate.payload["target"], candidate.payload.get("argument", ""))
            else:
                self.request("GET", candidate.action)

        raise AutomationError("Nao encontrei a tela de marcacao pelo fluxo de navegacao disponivel.")

    def find_action_by_text(self, soup: BeautifulSoup, keywords: Iterable[str]) -> Candidate | None:
        for link in soup.select("a[href]"):
            label = link.get_text(" ", strip=True) or link.get("title") or link.get("id") or ""
            if any(keyword in norm(label) for keyword in keywords):
                action = self.link_action(link)
                if action:
                    return Candidate(label, action[0], action[1], 100)
        for control in soup.select("input[type=submit][name], button[name]"):
            label = " ".join([control.get_text(" ", strip=True), control.get("value", ""), control.get("id", ""), control.get("name", "")])
            if any(keyword in norm(label) for keyword in keywords):
                payload = self.form_payload(soup)
                payload[control.get("name")] = control.get("value", "")
                return Candidate(label, "submit", payload, 100)
        return None

    def best_navigation_link(self, soup: BeautifulSoup, keywords: Iterable[str]) -> Candidate | None:
        candidates: list[Candidate] = []
        for link in soup.select("a[href]"):
            label = link.get_text(" ", strip=True) or link.get("title") or link.get("id") or ""
            label_norm = norm(label + " " + (link.get("href") or ""))
            score = sum(10 for kw in keywords if kw in label_norm)
            if score == 0:
                continue
            href = link["href"]
            postback = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
            if postback:
                candidates.append(
                    Candidate(label, "postback", {"target": postback.group(1), "argument": postback.group(2)}, score)
                )
            elif not href.startswith("#") and not href.lower().startswith("javascript:"):
                candidates.append(Candidate(label, urljoin(self.last_url, href), {}, score))
        return sorted(candidates, key=lambda item: item.score, reverse=True)[0] if candidates else None

    def has_service_fields(self, soup: BeautifulSoup) -> bool:
        text = norm(soup.get_text(" ", strip=True))
        return ("convenio" in text or "convênio" in text) and ("cpa" in text or "data do evento" in text)

    # ── Filtros ───────────────────────────────────────────────────────────────

    def fill_filters(self, convenio: str, data_evento: str, cpa: str) -> None:
        _log("FILTRO", f"=== Preenchendo filtros: convenio='{convenio}' data='{data_evento}' cpa='{cpa}' ===")
        soup = self.require_soup()
        fields = self.find_fields(soup)

        _log("FILTRO", f"Selecionando convenio '{convenio}' (campo: {fields.get('convenio')})...")
        payload = self.form_payload(soup)
        self.set_field(payload, fields, "convenio", convenio)
        payload["__EVENTTARGET"] = fields["convenio"]
        payload["__EVENTARGUMENT"] = ""
        soup = self.post_form(payload)

        for attempt in range(1, 7):
            _log("FILTRO", f"Preenchendo data='{data_evento}' e cpa='{cpa}' (tentativa {attempt}/6)...")
            payload = self.form_payload(soup)
            fields = self.find_fields(soup)
            self.set_field(payload, fields, "data", normalize_date_for_site(data_evento))
            self.set_field(payload, fields, "cpa", cpa)
            _log("FILTRO", "Resolvendo captcha do formulario de filtro...")
            self.fill_page_captcha(soup, payload)
            self.set_reserva_checkbox(soup, payload, True)
            submit = self.find_submit(soup, ("pesquisar", "buscar", "consultar", "filtrar", "listar", "avancar"))
            if submit:
                payload[submit] = self.input_value(soup, submit)
                _log("FILTRO", f"Botao de submit: '{submit}'")
            _log("FILTRO", "Consultando disponibilidade...")
            soup = self.post_form(payload)
            if "erro ao confirmar imagem" in norm(str(soup)):
                _log("FILTRO", f"Captcha de filtro recusado pelo site (tentativa {attempt}/6); reportando e tentando novamente...")
                self.report_bad_captcha()
                continue
            _log("FILTRO", "Filtros aplicados com sucesso.")
            return
        raise AutomationError("Captcha do filtro falhou em todas as tentativas.")

    def fill_filters_first_matching_date(self, convenio: str, cpa: str, prefer: str, scan_rounds: int = 1) -> str:
        _log("FILTRO", f"=== Varredura de datas: convenio='{convenio}' cpa='{cpa}' prefer='{prefer}' rounds={scan_rounds} ===")
        soup = self.require_soup()
        fields = self.find_fields(soup)

        _log("FILTRO", f"Selecionando convenio '{convenio}'...")
        payload = self.form_payload(soup)
        self.set_field(payload, fields, "convenio", convenio)
        payload["__EVENTTARGET"] = fields["convenio"]
        payload["__EVENTARGUMENT"] = ""
        soup = self.post_form(payload)

        fields = self.find_fields(soup)
        date_field = fields.get("data")
        if not date_field:
            raise AutomationError("Nao encontrei o campo de data para varredura.")

        date_select = soup.select_one(f'select[name="{date_field}"]')
        if not date_select:
            raise AutomationError("Campo de data nao e um select; informe --data-evento manualmente.")

        dates = [
            (option.get("value", ""), option.get_text(" ", strip=True))
            for option in date_select.select("option")
            if option.get("value", "") not in {"", "0"} and norm(option.get_text(" ", strip=True)) != "selecione"
        ]
        if not dates:
            raise AutomationError("Nenhuma data disponivel no select.")

        _log("FILTRO", f"{len(dates)} data(s) disponivel(is) no select: {[lbl for _, lbl in dates]}")
        scan_rounds = coerce_scan_rounds(scan_rounds)

        for scan_round in range(1, scan_rounds + 1):
            if scan_rounds > 1:
                _log("FILTRO", f"Rodada de varredura {scan_round}/{scan_rounds}.")
            for value, label in dates:
                _log("FILTRO", f"Testando data: '{label}' (value='{value}')...")
                for attempt in range(1, 7):
                    payload = self.form_payload(soup)
                    fields = self.find_fields(soup)
                    payload[fields["data"]] = value
                    self.set_field(payload, fields, "cpa", cpa)
                    self.fill_page_captcha(soup, payload)
                    self.set_reserva_checkbox(soup, payload, True)
                    submit = self.find_submit(soup, ("pesquisar", "buscar", "consultar", "filtrar", "listar", "avancar"))
                    if submit:
                        payload[submit] = self.input_value(soup, submit)
                    _log("FILTRO", f"Consultando vagas para '{label}' (tentativa {attempt}/6)...")
                    result_soup = self.post_form(payload)
                    if "erro ao confirmar imagem" in norm(str(result_soup)):
                        _log("FILTRO", f"Captcha recusado para data '{label}' (tentativa {attempt}/6); tentando novamente...")
                        self.report_bad_captcha()
                        soup = result_soup
                        continue
                    if self.available_candidates(result_soup, prefer):
                        _log("FILTRO", f"Vagas encontradas para: '{label}'")
                        return label
                    _log("FILTRO", f"Nenhuma vaga do tipo '{prefer}' em '{label}'.")
                    self.navigate_to_service_page()
                    soup = self.require_soup()
                    payload = self.form_payload(soup)
                    fields = self.find_fields(soup)
                    self.set_field(payload, fields, "convenio", convenio)
                    payload["__EVENTTARGET"] = fields["convenio"]
                    payload["__EVENTARGUMENT"] = ""
                    soup = self.post_form(payload)
                    break

        raise AutomationError("Nenhuma data disponivel tinha vaga do tipo solicitado.")

    def dates_for_convenio(self, convenio: str) -> list[tuple[str, str]]:
        _log("FILTRO", f"Buscando datas disponiveis para convenio '{convenio}'...")
        soup = self.require_soup()
        fields = self.find_fields(soup)

        payload = self.form_payload(soup)
        self.set_field(payload, fields, "convenio", convenio)
        payload["__EVENTTARGET"] = fields["convenio"]
        payload["__EVENTARGUMENT"] = ""
        soup = self.post_form(payload)

        dates = self.available_date_options(soup)
        if not dates:
            raise AutomationError("Nenhuma data disponivel no select.")
        _log("FILTRO", f"{len(dates)} data(s) encontrada(s): {[lbl for _, lbl in dates]}")
        return dates

    def mark_scanning_dates(
        self,
        convenio: str,
        cpa: str,
        prefer: str,
        quantidade: int,
        scan_rounds: int = 1,
        start_date: str = "",
        nome_evento: str = "",
        hora_evento: str = "",
        turno: str = "",
        endereco: str = "",
    ) -> int:
        _log("VAGA", f"=== Marcacao por varredura: quantidade={quantidade}, prefer='{prefer}', rounds={scan_rounds}, data_inicial='{start_date}' ===")
        self.navigate_to_service_page()
        dates = self.dates_for_convenio(convenio)
        print(f"[VAGAS] Marcacao por varredura iniciada: {len(dates)} data(s) disponivel(is).")

        confirmed = 0
        scan_rounds = coerce_scan_rounds(scan_rounds)
        start_index = first_scan_date_index(dates, start_date)
        if start_index >= len(dates):
            raise AutomationError("Nenhuma data disponivel igual ou posterior a data inicial informada.")
        if start_date:
            _log("VAGA", f"Varredura iniciando em '{dates[start_index][1]}' para respeitar a data inicial.")
        for scan_round in range(1, scan_rounds + 1):
            if confirmed >= quantidade:
                break
            if scan_rounds > 1:
                _log("VAGA", f"Rodada de varredura {scan_round}/{scan_rounds}.")

            date_index = start_index if scan_round == 1 else 0
            while date_index < len(dates) and confirmed < quantidade:
                _, label = dates[date_index]
                _log("VAGA", f"Testando data: '{label}' (indice {date_index + 1}/{len(dates)})...")
                self.navigate_to_service_page()
                self.fill_filters(convenio, label, cpa)
                candidates = self.available_candidates(self.require_soup(), prefer)
                if not candidates:
                    _log("VAGA", f"Nenhuma vaga do tipo '{prefer}' em '{label}'.")
                    date_index = next_scan_date_index(date_index, found_candidate=False)
                    continue

                _log("VAGA", f"{len(candidates)} vaga(s) encontrada(s) em '{label}'. Tentativa de marcacao {confirmed + 1}/{quantidade}.")
                success = self.choose_target_event(
                    prefer, False,
                    data_evento=label,
                    nome_evento=nome_evento,
                    hora_evento=hora_evento,
                    turno=turno,
                    endereco=endereco,
                )
                if not success:
                    raise AutomationError("Clique executado, mas nao encontrei confirmacao de sucesso no retorno do site.")
                confirmed += 1
                _log("VAGA", f"Marcacoes confirmadas: {confirmed}/{quantidade}.")
                _log("VAGA", "CPROEIS limita a exibicao apos uma marcacao no mesmo dia; avancando para a proxima data.")
                date_index = next_scan_date_index(date_index, found_candidate=True)

        return confirmed

    def list_all_available_dates(self, convenio: str, cpa: str) -> int:
        _log("VAGA", f"=== Listando vagas de todas as datas: convenio='{convenio}' cpa='{cpa}' ===")
        soup = self.require_soup()
        fields = self.find_fields(soup)

        payload = self.form_payload(soup)
        self.set_field(payload, fields, "convenio", convenio)
        payload["__EVENTTARGET"] = fields["convenio"]
        payload["__EVENTARGUMENT"] = ""
        soup = self.post_form(payload)

        dates = self.available_date_options(soup)
        print(f"[VAGAS] Varredura iniciada: {len(dates)} data(s) disponivel(is).")

        total = 0
        for i, (value, label) in enumerate(dates, 1):
            _log("VAGA", f"Listando data {i}/{len(dates)}: '{label}' (value='{value}')...")
            for attempt in range(1, 7):
                payload = self.form_payload(soup)
                fields = self.find_fields(soup)
                payload[fields["data"]] = value
                self.set_field(payload, fields, "cpa", cpa)
                self.fill_page_captcha(soup, payload)
                self.set_reserva_checkbox(soup, payload, True)
                submit = self.find_submit(soup, ("pesquisar", "buscar", "consultar", "filtrar", "listar", "avancar"))
                if submit:
                    payload[submit] = self.input_value(soup, submit)
                _log("VAGA", f"Consultando disponibilidade para '{label}' (tentativa {attempt}/6)...")
                result_soup = self.post_form(payload)
                if "erro ao confirmar imagem" in norm(str(result_soup)):
                    _log("VAGA", f"Captcha recusado para '{label}' (tentativa {attempt}/6); tentando novamente...")
                    self.report_bad_captcha()
                    soup = result_soup
                    continue

                candidates = self.available_candidates(result_soup, "qualquer")
                if candidates:
                    _log("VAGA", f"{len(candidates)} vaga(s) encontrada(s) em '{label}':")
                    print(f"[VAGAS] {len(candidates)} vaga(s) encontrada(s) em {label}:")
                    for candidate in candidates:
                        emit_vaga(candidate.label, data_evento=label, acao="Visualizacao")
                    total += len(candidates)
                else:
                    _log("VAGA", f"Nenhuma vaga encontrada em '{label}'.")
                break

            _log("NAV", f"Retornando a tela de servicos para proxima data ({i}/{len(dates)})...")
            self.navigate_to_service_page()
            soup = self.require_soup()
            payload = self.form_payload(soup)
            fields = self.find_fields(soup)
            self.set_field(payload, fields, "convenio", convenio)
            payload["__EVENTTARGET"] = fields["convenio"]
            payload["__EVENTARGUMENT"] = ""
            soup = self.post_form(payload)

        _log("VAGA", f"Varredura concluida: {total} vaga(s) encontrada(s) no total.")
        print(f"[VAGAS] Varredura concluida: {total} vaga(s) encontrada(s) no total.")
        return total

    def available_date_options(self, soup: BeautifulSoup) -> list[tuple[str, str]]:
        fields = self.find_fields(soup)
        date_field = fields.get("data")
        if not date_field:
            raise AutomationError("Nao encontrei o campo de data para varredura.")
        date_select = soup.select_one(f'select[name="{date_field}"]')
        if not date_select:
            raise AutomationError("Campo de data nao e um select.")
        dates = [
            (option.get("value", ""), option.get_text(" ", strip=True))
            for option in date_select.select("option")
            if option.get("value", "") not in {"", "0"} and norm(option.get_text(" ", strip=True)) != "selecione"
        ]
        if not dates:
            raise AutomationError("Nenhuma data disponivel no select.")
        return dates

    def fill_page_captcha(self, soup: BeautifulSoup, payload: dict[str, str]) -> None:
        captcha_field = self.find_captcha_field(soup)
        if not captcha_field:
            _log("CAPTCHA", "Nenhum campo de captcha encontrado na pagina; pulando etapa de captcha.")
            return
        _log("CAPTCHA", f"Campo de captcha encontrado: '{captcha_field}'")
        original_payload = payload.copy()
        final_soup, captcha_text = self.solve_page_captcha(soup)
        if final_soup is not soup:
            _log("CAPTCHA", "Pagina foi atualizada durante resolucao; sincronizando payload com novo VIEWSTATE...")
            payload.clear()
            payload.update(self.form_payload(final_soup))
            for name, value in original_payload.items():
                if not name.startswith("__"):
                    payload[name] = value
            captcha_field = self.find_captcha_field(final_soup) or captcha_field
        payload[captcha_field] = captcha_text
        _log("CAPTCHA", f"Campo '{captcha_field}' preenchido com: '{captcha_text}'")

    def find_captcha_field(self, soup: BeautifulSoup) -> str | None:
        for control in soup.select("input[name]"):
            name = control.get("name", "")
            text = norm(
                " ".join(
                    [
                        name,
                        control.get("id", ""),
                        control.get("placeholder", ""),
                        self.label_for(soup, control.get("id")),
                        self.near_text(control),
                    ]
                )
            )
            if "caracteres da imagem" in text or "captcha" in text:
                return name
        return None

    def set_reserva_checkbox(self, soup: BeautifulSoup, payload: dict[str, str], enabled: bool) -> None:
        for checkbox in soup.select('input[type="checkbox"][name]'):
            text = norm(
                " ".join(
                    [
                        checkbox.get("name", ""),
                        checkbox.get("id", ""),
                        self.label_for(soup, checkbox.get("id")),
                        self.near_text(checkbox),
                    ]
                )
            )
            if "reserva" in text:
                if enabled:
                    payload[checkbox["name"]] = checkbox.get("value", "on")
                    _log("FORM", f"Checkbox de reserva '{checkbox['name']}' marcado.")
                else:
                    payload.pop(checkbox["name"], None)
                    _log("FORM", f"Checkbox de reserva '{checkbox['name']}' desmarcado.")

    def find_fields(self, soup: BeautifulSoup) -> dict[str, str]:
        fields: dict[str, str] = {}
        known_ids = {
            "ddlConvenios": "convenio",
            "ddlDataEvento": "data",
            "ddlCPAS": "cpa",
        }
        for control_id, logical in known_ids.items():
            control = soup.select_one(f"#{control_id}")
            if control and control.get("name"):
                fields[logical] = control["name"]
        controls = soup.select("input[name], select[name], textarea[name]")
        for control in controls:
            name = control.get("name")
            if not name:
                continue
            text = " ".join(
                [
                    control.get("id", ""),
                    name,
                    control.get("placeholder", ""),
                    control.get("aria-label", ""),
                    self.label_for(soup, control.get("id")),
                    self.near_text(control),
                ]
            )
            key = norm(text)
            if ("convenio" in key or "convênio" in key) and "convenio" not in fields:
                fields["convenio"] = name
            elif ("data do evento" in key or ("data" in key and "evento" in key)) and "data" not in fields:
                fields["data"] = name
            elif re.search(r"\bcpa\b", key) and "cpa" not in fields:
                fields["cpa"] = name
        _log("FORM", f"Campos identificados no formulario: {fields}")
        return fields

    def label_for(self, soup: BeautifulSoup, control_id: str | None) -> str:
        if not control_id:
            return ""
        label = soup.select_one(f'label[for="{control_id}"]')
        return label.get_text(" ", strip=True) if label else ""

    def near_text(self, tag) -> str:
        texts = []
        parent = tag.parent
        for _ in range(3):
            if not parent:
                break
            texts.append(parent.get_text(" ", strip=True)[:200])
            parent = parent.parent
        return " ".join(texts)

    def set_field(self, payload: dict[str, str], fields: dict[str, str], logical: str, desired: str) -> None:
        name = fields.get(logical)
        if not name:
            raise AutomationError(f"Nao encontrei o campo {logical} no formulario atual.")
        soup = self.require_soup()
        select = soup.select_one(f'select[name="{name}"]')
        if select:
            chosen = self.option_value(select, desired)
            _log("FORM", f"Campo '{logical}' ({name}): '{desired}' -> opcao selecionada: '{chosen}'")
            payload[name] = chosen
        else:
            _log("FORM", f"Campo '{logical}' ({name}): '{desired}' (input livre)")
            payload[name] = desired

    def option_value(self, select, desired: str) -> str:
        desired_norm = norm(desired)
        options = select.select("option")
        for option in options:
            text = norm(option.get_text(" ", strip=True))
            value = norm(option.get("value", ""))
            if desired_norm == text or desired_norm == value:
                return option.get("value", option.get_text(strip=True))
        for option in options:
            text = norm(option.get_text(" ", strip=True))
            value = norm(option.get("value", ""))
            if desired_norm in text or desired_norm in value:
                return option.get("value", option.get_text(strip=True))
        raise AutomationError(f"Opcao '{desired}' nao encontrada no select {select.get('name')}.")

    def find_submit(self, soup: BeautifulSoup, keywords: Iterable[str]) -> str | None:
        for tag in soup.select("input[type=submit][name], button[name]"):
            text = norm(" ".join([tag.get("value", ""), tag.get_text(" ", strip=True), tag.get("id", ""), tag.get("name", "")]))
            if any(keyword in text for keyword in keywords):
                return tag.get("name")
        return None

    def input_value(self, soup: BeautifulSoup, name: str) -> str:
        tag = soup.select_one(f'[name="{name}"]')
        return tag.get("value", "") if tag else ""

    # ── Escolha de vagas ──────────────────────────────────────────────────────

    def choose_available(self, prefer: str, dry_run: bool, data_evento: str = "") -> None:
        soup = self.require_soup()
        candidates = self.available_candidates(soup, prefer)
        if not candidates:
            raise AutomationError("Nenhuma opcao disponivel/reserva encontrada para os filtros.")
        chosen = candidates[0]
        _log("VAGA", f"{min(len(candidates), 10)} vaga(s) encontrada(s). Escolhida: '{chosen.label[:80]}'")
        print(f"[VAGAS] {min(len(candidates), 10)} vaga(s) encontrada(s):")
        for c in candidates[:10]:
            emit_vaga(c.label, data_evento=data_evento, acao="Visualizacao")
        if dry_run:
            _log("VAGA", "dry_run=true: marcacao nao confirmada.")
            return
        _log("VAGA", f"Clicando em 'Eu Vou': '{chosen.label[:80]}' (acao={chosen.action})...")
        if chosen.action == "postback":
            soup = self.postback(chosen.payload["target"], chosen.payload.get("argument", ""))
        elif chosen.action == "submit":
            soup = self.post_form(chosen.payload)
        else:
            soup = self.request("GET", chosen.action)
        self.confirm_if_needed(soup)

    def simulate_target_events(
        self,
        prefer: str,
        quantidade: int,
        data_evento: str = "",
        nome_evento: str = "",
        hora_evento: str = "",
        turno: str = "",
        endereco: str = "",
    ) -> list[Candidate]:
        soup = self.require_soup()
        candidates = self.available_candidates(soup, prefer)
        filtered = [
            candidate for candidate in candidates
            if self.event_matches(candidate.label, nome_evento, hora_evento, turno, endereco)
        ]
        selected = filtered[: max(1, quantidade)]
        if not selected:
            _log("VAGA", f"Simulacao: nenhum candidato passou pelos filtros para prefer='{prefer}'.")
            raise AutomationError("Simulacao nao encontrou vaga compativel.")
        _log("VAGA", f"Simulacao: {len(selected)}/{quantidade} vaga(s) seriam clicadas.")
        print(f"[SIMULATE] Marcacoes simuladas: {len(selected)}/{quantidade}")
        for candidate in selected:
            emit_vaga(candidate.label, data_evento=data_evento, acao="Simulacao Eu vou")
        return selected

    def simulate_mark_scanning_dates(
        self,
        convenio: str,
        cpa: str,
        prefer: str,
        quantidade: int,
        scan_rounds: int = 1,
        start_date: str = "",
        nome_evento: str = "",
        hora_evento: str = "",
        turno: str = "",
        endereco: str = "",
    ) -> int:
        _log("VAGA", f"=== Simulacao por varredura: quantidade={quantidade}, prefer='{prefer}', data_inicial='{start_date}' ===")
        self.navigate_to_service_page()
        dates = self.dates_for_convenio(convenio)
        simulated = 0
        start_index = first_scan_date_index(dates, start_date)
        if start_index >= len(dates):
            raise AutomationError("Nenhuma data disponivel igual ou posterior a data inicial informada.")

        for scan_round in range(1, coerce_scan_rounds(scan_rounds) + 1):
            date_index = start_index if scan_round == 1 else 0
            while date_index < len(dates) and simulated < quantidade:
                _, label = dates[date_index]
                self.navigate_to_service_page()
                self.fill_filters(convenio, label, cpa)
                remaining = quantidade - simulated
                try:
                    selected = self.simulate_target_events(
                        prefer, 1, data_evento=label,
                        nome_evento=nome_evento,
                        hora_evento=hora_evento,
                        turno=turno,
                        endereco=endereco,
                    )
                    simulated += min(len(selected), remaining)
                except AutomationError:
                    _log("VAGA", f"Simulacao: nenhuma vaga do tipo '{prefer}' em '{label}'.")
                date_index = next_scan_date_index(date_index, found_candidate=True)

        print(f"[SIMULATE] Total simulado: {simulated}/{quantidade}")
        return simulated

    def choose_target_event(
        self,
        prefer: str,
        dry_run: bool,
        data_evento: str = "",
        nome_evento: str = "",
        hora_evento: str = "",
        turno: str = "",
        endereco: str = "",
    ) -> bool:
        soup = self.require_soup()
        candidates = self.available_candidates(soup, prefer)
        filters = {"nome": nome_evento, "hora": hora_evento, "turno": turno, "endereco": endereco}
        active_filters = {k: v for k, v in filters.items() if v}
        _log("VAGA", f"{len(candidates)} candidato(s) antes dos filtros. Filtros ativos: {active_filters}")
        filtered = [
            candidate for candidate in candidates
            if self.event_matches(candidate.label, nome_evento, hora_evento, turno, endereco)
        ]
        if not filtered:
            _log("VAGA", f"Nenhum candidato passou pelos filtros. Opcoes disponiveis ({len(candidates)}):")
            print(f"[VAGAS] {len(candidates)} opcao(oes) disponivel(is) encontrada(s):")
            for c in candidates[:20]:
                emit_vaga(c.label, data_evento=data_evento, acao="Visualizacao")
            raise AutomationError("Nao encontrei a linha exata do evento solicitado.")
        chosen = filtered[0]
        _log("VAGA", f"{len(filtered)} vaga(s) apos filtros. Escolhida: '{chosen.label[:80]}'")
        print(f"[VAGAS] {len(filtered)} vaga(s) encontrada(s):")
        if dry_run:
            for c in filtered:
                emit_vaga(c.label, data_evento=data_evento, acao="Visualizacao")
            _log("VAGA", "dry_run=true: nao cliquei em Eu Vou.")
            return False
        _log("VAGA", f"Clicando em 'Eu Vou' (acao={chosen.action})...")
        if chosen.action == "postback":
            soup = self.postback(chosen.payload["target"], chosen.payload.get("argument", ""))
        elif chosen.action == "submit":
            soup = self.post_form(chosen.payload)
        else:
            soup = self.request("GET", chosen.action)
        success = self.confirm_if_needed(soup)
        if success:
            emit_vaga(chosen.label, data_evento=data_evento, acao="Clicado Eu vou")
        return success

    def event_matches(self, label: str, nome_evento: str, hora_evento: str, turno: str, endereco: str) -> bool:
        label_norm = norm(label)
        checks = [
            (nome_evento, norm(nome_evento) in label_norm),
            (hora_evento, norm(hora_evento) in label_norm),
            (turno, norm(turno) in label_norm),
            (endereco, norm(endereco) in label_norm),
        ]
        active = [matches for raw, matches in checks if raw]
        return all(active) if active else True

    def available_candidates(self, soup: BeautifulSoup, prefer: str) -> list[Candidate]:
        prefer_norm = norm(prefer)
        candidates: list[Candidate] = []
        for row in soup.select("tr"):
            text = row.get_text(" ", strip=True)
            text_norm = norm(text)
            if not self.matches_preference(text_norm, prefer_norm):
                continue
            action = self.row_action(row)
            if action:
                candidates.append(Candidate(text[:240], action[0], action[1], self.preference_score(text_norm, prefer_norm)))
        for link in soup.select("a[href]"):
            text = link.get_text(" ", strip=True)
            text_norm = norm(text)
            if self.matches_preference(text_norm, prefer_norm):
                action = self.link_action(link)
                if action:
                    candidates.append(Candidate(text[:240], action[0], action[1], self.preference_score(text_norm, prefer_norm)))
        result = sorted(candidates, key=lambda item: item.score, reverse=True)
        if result:
            _log("VAGA", f"available_candidates(prefer='{prefer}'): {len(result)} candidato(s) encontrado(s).")
        return result

    def matches_preference(self, text: str, prefer: str) -> bool:
        if prefer in {"nao-reserva", "sem-reserva", "normal"}:
            return "reserva" not in text and re.search(r"\b\d+\s*-\s*curso", text) is not None
        if prefer in {"qualquer", "any", ""}:
            return "disponivel" in text or "reserva" in text or re.search(r"\b\d+\s*-\s*curso", text) is not None
        if prefer == "reserva":
            return "reserva" in text
        return re.search(rf"\b{re.escape(prefer)}\b", text) is not None or f"disponivel {prefer}" in text

    def preference_score(self, text: str, prefer: str) -> int:
        if prefer in {"nao-reserva", "sem-reserva", "normal"} and "reserva" not in text:
            match = re.search(r"\b(\d+)\s*-\s*curso", text)
            return 100 - int(match.group(1)) if match else 80
        if prefer == "reserva" and "reserva" in text:
            return 100
        if prefer not in {"qualquer", "any", ""} and re.search(rf"\b{re.escape(prefer)}\b", text):
            return 100
        if "disponivel" in text:
            return 50
        if "reserva" in text:
            return 40
        return 10

    def row_action(self, row) -> tuple[str, dict[str, str]] | None:
        for control in row.select("a[href], input[type=submit][name], button[name]"):
            text = norm(" ".join([control.get_text(" ", strip=True), control.get("value", ""), control.get("id", ""), control.get("name", "")]))
            if not any(
                word in text
                for word in ("eu vou", "inscrever", "marcar", "reservar", "confirmar", "selecionar", "participar")
            ):
                continue
            if control.name == "a":
                return self.link_action(control)
            payload = self.form_payload()
            payload[control.get("name")] = control.get("value", "")
            return ("submit", payload)
        return None

    def link_action(self, link) -> tuple[str, dict[str, str]] | None:
        href = link.get("href", "")
        postback = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
        if postback:
            return ("postback", {"target": postback.group(1), "argument": postback.group(2)})
        if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
            return (urljoin(self.last_url, href), {})
        return None

    def confirm_if_needed(self, soup: BeautifulSoup) -> bool:
        _log("VAGA", "Verificando se ha tela de confirmacao...")
        text = norm(soup.get_text(" ", strip=True))
        if any(word in text for word in ("confirmar", "confirma", "deseja")):
            submit = self.find_submit(soup, ("confirmar", "sim", "concluir", "finalizar"))
            if submit:
                _log("VAGA", f"Tela de confirmacao detectada. Clicando em '{submit}'...")
                payload = self.form_payload(soup)
                payload[submit] = self.input_value(soup, submit)
                soup = self.post_form(payload)
        final_text = soup.get_text(" ", strip=True)
        page_text = norm(str(soup))
        success = any(
            term in page_text
            for term in (
                "confirmacao no evento foi incluida com sucesso",
                "confirmacao no evento foi incluida",
                "incluida com sucesso",
                "incluido com sucesso",
            )
        )
        _log("VAGA", "Resposta final do site:")
        print("Retorno final:")
        print(re.sub(r"\s+", " ", final_text)[:1200])
        if success:
            _log("VAGA", "*** MARCACAO CONFIRMADA PELO SITE ***")
        else:
            _log("VAGA", "Confirmacao de sucesso NAO encontrada na resposta do site.")
        return success


# ── Helpers globais ───────────────────────────────────────────────────────────

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AutomationError(f"Variavel/secret obrigatorio ausente: {name}")
    return value


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def normalize_date_for_site(value: str) -> str:
    value = value.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    return f"{seconds:.1f}s"


def print_timing_summary(client: ProeisHTTP, started_at: float) -> None:
    total = time.monotonic() - started_at
    captcha = client.captcha_elapsed_seconds
    site = client.site_elapsed_seconds
    other = max(0.0, total - captcha - site)
    _log(
        "RESUMO",
        "Tempos: "
        f"total={format_elapsed(total)} | "
        f"captcha={format_elapsed(captcha)} | "
        f"site={format_elapsed(site)} | "
        f"outros={format_elapsed(other)}",
    )
    print(
        "[RESUMO] "
        f"Tempo total: {format_elapsed(total)} | "
        f"captcha: {format_elapsed(captcha)} | "
        f"site: {format_elapsed(site)} | "
        f"outros: {format_elapsed(other)}"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automacao HTTP puro PROEIS.")
    parser.add_argument("--convenio", required=True)
    parser.add_argument("--data-evento", default="")
    parser.add_argument("--cpa", required=True)
    parser.add_argument("--disponivel", choices=["reserva", "nao-reserva"], default="nao-reserva")
    parser.add_argument("--quantidade", type=int, default=1, help="Quantidade de vagas para tentar marcar.")
    parser.add_argument("--nome-evento", default="")
    parser.add_argument("--hora-evento", default="")
    parser.add_argument("--turno", default="")
    parser.add_argument("--endereco", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--simulate-mark", action="store_true", help="Segue a logica de marcacao, mas apenas lista as vagas que seriam clicadas.")
    parser.add_argument("--list-all-dates", action="store_true", help="Lista vagas de todas as datas disponiveis para o convenio/CPA, sem clicar em Eu Vou.")
    parser.add_argument("--no-debug", action="store_true")
    parser.add_argument("--scan-rounds", type=int, default=1, help="Quantidade de rodadas de varredura quando a data do evento estiver vazia.")
    parser.add_argument("--wait-until", default="", help="HH:MM:SS — faz login imediatamente e aguarda ate esse horario para marcar.")
    return parser.parse_args()


def main() -> int:
    load_env_file()
    log_path = _setup_log()
    _log("INFO", f"Arquivo de log: {log_path}")
    print(f"[LOG] Arquivo de log: {log_path}")
    args = parse_args()

    _log("INFO", "=== PROEIS Automacao HTTP ===")
    _log("INFO", f"convenio='{args.convenio}' data='{args.data_evento}' cpa='{args.cpa}'")
    _log("INFO", f"disponivel='{args.disponivel}' quantidade={args.quantidade} dry_run={args.dry_run}")

    if args.quantidade < 1:
        raise AutomationError("--quantidade deve ser 1 ou maior.")
    if args.dry_run and args.quantidade != 1:
        _log("INFO", "dry_run=true: forcando quantidade=1 para teste rapido.")
        args.quantidade = 1

    client = ProeisHTTP(
        login=required_env("PROEIS_LOGIN"),
        password=required_env("PROEIS_PASSWORD"),
        captcha_key=required_env("TWOCAPTCHA_API_KEY"),
        debug=not args.no_debug,
    )
    operation_started = time.monotonic()
    atexit.register(print_timing_summary, client, operation_started)

    _log("INFO", "=== FASE 1: Login ===")
    client.login_flow()

    if args.list_all_dates:
        _log("INFO", "=== FASE 2: Listagem de todas as datas ===")
        client.navigate_to_service_page()
        client.list_all_available_dates(args.convenio, args.cpa)
        return 0

    if args.wait_until:
        try:
            t = datetime.strptime(args.wait_until, "%H:%M:%S")
        except ValueError:
            t = datetime.strptime(args.wait_until, "%H:%M")
        now = datetime.now()
        target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_secs = int((target - datetime.now()).total_seconds())
        _log("INFO", f"Login antecipado concluido. Aguardando ate {target.strftime('%H:%M:%S')} ({wait_secs}s)...")
        print(f"Login antecipado concluido. Iniciando marcacao em {wait_secs}s (horario: {target.strftime('%H:%M:%S')})...")
        while True:
            remaining = int((target - datetime.now()).total_seconds())
            if remaining <= 0:
                break
            if remaining % 10 == 0:
                _log("INFO", f"Aguardando horario... {remaining}s restantes.")
            time.sleep(1)
        _log("INFO", "Horario atingido — iniciando marcacao.")

    _log("INFO", "=== FASE 2: Marcacao ===")

    if args.simulate_mark:
        simulated = client.simulate_mark_scanning_dates(
            args.convenio, args.cpa, args.disponivel, args.quantidade,
            scan_rounds=args.scan_rounds,
            start_date=args.data_evento,
            nome_evento=args.nome_evento,
            hora_evento=args.hora_evento,
            turno=args.turno,
            endereco=args.endereco,
        )
        if simulated < args.quantidade:
            _log("INFO", f"Simulacao encontrou apenas {simulated}/{args.quantidade} marcacao(oes).")
        return 0

    if not args.data_evento and not args.dry_run:
        confirmed = client.mark_scanning_dates(
            args.convenio, args.cpa, args.disponivel, args.quantidade,
            scan_rounds=args.scan_rounds,
            nome_evento=args.nome_evento,
            hora_evento=args.hora_evento,
            turno=args.turno,
            endereco=args.endereco,
        )
        if confirmed < args.quantidade:
            _log("INFO", f"Sem mais vagas. Marcacoes confirmadas: {confirmed}/{args.quantidade}.")
        return 0

    if args.data_evento and not args.dry_run and args.quantidade > 1:
        _log("INFO", "Quantidade maior que 1 com data inicial: apos cada marcacao, a varredura avanca para datas posteriores.")
        confirmed = client.mark_scanning_dates(
            args.convenio, args.cpa, args.disponivel, args.quantidade,
            scan_rounds=args.scan_rounds,
            start_date=args.data_evento,
            nome_evento=args.nome_evento,
            hora_evento=args.hora_evento,
            turno=args.turno,
            endereco=args.endereco,
        )
        if confirmed < args.quantidade:
            _log("INFO", f"Sem mais vagas. Marcacoes confirmadas: {confirmed}/{args.quantidade}.")
        return 0

    confirmed = 0
    selected_date = args.data_evento
    skip_filter = False  # True quando a pagina atual ja tem candidatos visiveis

    for index in range(1, args.quantidade + 1):
        _log("INFO", f"--- Marcacao {index}/{args.quantidade} ---")
        try:
            if not skip_filter:
                # navigate_to_service_page ja retorna cedo se estivermos em FrmEventoAssociar
                client.navigate_to_service_page()
                if args.data_evento:
                    client.fill_filters(args.convenio, args.data_evento, args.cpa)
                else:
                    selected_date = client.fill_filters_first_matching_date(
                        args.convenio, args.cpa, args.disponivel, scan_rounds=args.scan_rounds,
                    )
            else:
                _log("INFO", "Pagina atual ainda tem candidatos; pulando re-filtro.")
            skip_filter = False

            success = client.choose_target_event(
                args.disponivel, args.dry_run,
                data_evento=selected_date,
                nome_evento=args.nome_evento,
                hora_evento=args.hora_evento,
                turno=args.turno,
                endereco=args.endereco,
            )
        except AutomationError:
            if confirmed:
                _log("INFO", f"Sem mais vagas. Marcacoes confirmadas: {confirmed}/{args.quantidade}.")
                return 0
            raise

        if args.dry_run:
            _log("INFO", "dry_run=true: teste encerrado apos localizar a primeira opcao.")
            return 0
        if not success:
            raise AutomationError("Clique executado, mas nao encontrei confirmacao de sucesso no retorno do site.")
        confirmed += 1
        _log("INFO", f"Marcacoes confirmadas: {confirmed}/{args.quantidade}.")

        # Verifica se a pagina atual (pos-"Eu Vou") ja tem mais candidatos visiveis.
        # Se sim, evita re-filtrar e re-resolver captcha desnecessariamente.
        if confirmed < args.quantidade:
            try:
                remaining = client.available_candidates(client.require_soup(), args.disponivel)
                if remaining:
                    _log("INFO", f"{len(remaining)} candidato(s) ainda visivel(is) na pagina atual. Proximo clique direto.")
                    skip_filter = True
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        _log("ERRO", str(exc))
        print(f"ERRO: {exc}", file=sys.stderr)
        raise SystemExit(2)
