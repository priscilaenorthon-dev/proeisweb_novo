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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    import io as _io
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import cv2 as _cv2
    import numpy as _np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False


def _clean_captcha_cv2(image_bytes: bytes) -> bytes | None:
    """Limpeza avançada via OpenCV: remove bolinhas coloridas (alta saturação),
    realça o texto fraco (CLAHE) e reduz ruído. Retorna PNG em bytes ou None."""
    if not _CV2_AVAILABLE:
        return None
    try:
        arr = _np.frombuffer(image_bytes, _np.uint8)
        img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
        if img is None:
            return None
        img = _cv2.resize(img, None, fx=3, fy=3, interpolation=_cv2.INTER_CUBIC)
        hsv = _cv2.cvtColor(img, _cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1]
        img[sat > 110] = (255, 255, 255)  # bolinhas coloridas -> branco
        gray = _cv2.cvtColor(img, _cv2.COLOR_BGR2GRAY)
        clahe = _cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        gray = _cv2.medianBlur(gray, 3)
        ok, buf = _cv2.imencode(".png", gray)
        return buf.tobytes() if ok else None
    except Exception:
        return None


def _otsu_threshold(gray: "Image.Image") -> int:
    """Calcula o limiar de Otsu a partir do histograma (sem numpy)."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(i * hist[i] for i in range(256))
    sum_b = 0.0
    w_b = 0
    max_var = -1.0
    threshold = 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > max_var:
            max_var = var_between
            threshold = t
    return threshold


def _preprocess_captcha_image(image_bytes: bytes, mode: str | None = None) -> bytes:
    """Pré-processa a imagem do captcha para melhorar a leitura pelo OCR/Gemini.

    Modos (env CAPTCHA_PREPROCESS, padrão 'v1'):
      - 'v1'       : original — cinza + autocontraste + sharpen 2x + upscale 2x
      - 'denoise'  : cinza + autocontraste + median denoise + upscale 3x + sharpen
      - 'binarize' : denoise + binarização por Otsu (texto preto / fundo branco)
      - 'clean'    : OpenCV — remove bolinhas por saturação + CLAHE (melhor leitura)
      - 'off'      : não altera
    """
    mode = (mode or os.getenv("CAPTCHA_PREPROCESS", "off")).strip().lower()
    if mode == "off":
        return image_bytes
    if mode == "clean":
        cleaned = _clean_captcha_cv2(image_bytes)
        if cleaned is not None:
            return cleaned
        mode = "binarize"  # fallback quando OpenCV indisponível
    if not _PIL_AVAILABLE:
        return image_bytes
    try:
        img = Image.open(_io.BytesIO(image_bytes))
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        else:
            img = img.convert("RGB")
        img = img.convert("L")

        if mode == "v1":
            img = ImageOps.autocontrast(img, cutoff=5)
            img = img.filter(ImageFilter.SHARPEN)
            img = img.filter(ImageFilter.SHARPEN)
            w, h = img.size
            if w < 300:
                img = img.resize((w * 2, h * 2), Image.LANCZOS)
        else:
            # denoise / binarize compartilham a base de limpeza de ruído colorido
            img = ImageOps.autocontrast(img, cutoff=2)
            img = img.filter(ImageFilter.MedianFilter(size=3))  # remove pontinhos
            w, h = img.size
            scale = 3 if w < 260 else 2
            img = img.resize((w * scale, h * scale), Image.LANCZOS)
            if mode == "binarize":
                thr = _otsu_threshold(img)
                img = img.point(lambda p, t=thr: 0 if p < t else 255, mode="L")
            else:  # denoise
                img = img.filter(ImageFilter.SHARPEN)

        img = img.convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return image_bytes


try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass

LOG_DIR = Path("logs")
_LOG_FILE_HANDLE = None


# â"€â"€ Logger â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

_OP = threading.local()


def _op_start() -> None:
    _OP.t = time.monotonic()


def _op_elapsed() -> str:
    t = getattr(_OP, "t", None)
    if t is None:
        return ""
    return f" +{time.monotonic() - t:.1f}s"


def _log(tag: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}]{_op_elapsed()} [{tag:<9}] {msg}")


def _step(current: int, total: int, tag: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}]{_op_elapsed()} [{tag:<9}] Etapa {current}/{total}: {msg}")


def _phase(name: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}]{_op_elapsed()} [{'─'*9}] ══ {name} ══")


# â"€â"€ Tee (stdout + arquivo) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

class _Tee:
    """Escreve simultaneamente em mÃºltiplos streams (ex: stdout + arquivo de log)."""

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


# â"€â"€ URLs â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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
        super().__init__(f"Resposta invalida para captcha de 6 caracteres: {answer or raw_answer!r}")


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
    confidence: float | None = None


def norm(value: str | None) -> str:
    value = value or ""
    value = value.replace("Âº", "o").replace("Â°", "o")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value.lower()).strip()


def norm_match(value: str | None) -> str:
    text = norm(value)
    if not text:
        return ""

    text = re.sub(r"[\.,;:/\\\-]", " ", text)
    words = text.split()
    alias = {
        "av": "avenida",
        "avn": "avenida",
        "r": "rua",
        "rod": "rodovia",
        "estr": "estrada",
        "trav": "travessa",
        "tv": "travessa",
        "pca": "praca",
    }
    normalized_words = [alias.get(word, word) for word in words]
    return " ".join(normalized_words)


def _time_tokens(value: str | None) -> set[str]:
    text = norm(value)
    if not text:
        return set()
    tokens: set[str] = set()
    for h, m, _ in re.findall(r"\b([0-2]?\d):([0-5]\d):([0-5]\d)\b", text):
        tokens.add(f"{int(h):02d}:{int(m):02d}")
    for h, m in re.findall(r"\b([0-2]?\d)[:h]([0-5]\d)\b", text):
        tokens.add(f"{int(h):02d}:{int(m):02d}")
    return tokens


def normalize_captcha_answer(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def is_valid_captcha_answer(value: str) -> bool:
    return bool(re.fullmatch(r"[A-F0-9]{6}", normalize_captcha_answer(value)))


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


MOJIBAKE_MARKERS = ("Ãƒ", "Ã‚", "Ã¢", "Ã°Å¸", "Æ’")


def reparar_mojibake(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    for _ in range(2):
        if not any(marker in text for marker in MOJIBAKE_MARKERS):
            break
        try:
            repaired = text.encode("cp1252").decode("utf-8")
        except UnicodeError:
            break
        if repaired == text:
            break
        text = repaired
    return text


def _parse_vaga_label(label: str) -> tuple[str, str]:
    """Extrai (nome_evento, endereco) do label concatenado da linha HTML do PROEIS."""
    # Remove texto do botão e colunas de disponibilidade que o get_text() inclui
    clean = re.sub(r"\s+(?:disponivel\s+)?reserva\s*[-–]\s*curso.*$", "", label, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s+\d+\s*[-–]\s*curso.*$", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s+eu\s+vou\s*$", "", clean, flags=re.IGNORECASE).strip()
    # Separa nome (antes do horário HH:MM ou HHhMM) do endereço (após)
    m = re.match(r"^(.+?)\s+\d{1,2}:\d{2}(?::\d{2})?\s+(.*)$", clean)
    if m:
        return m.group(1).rstrip("-– ").strip(), m.group(2).strip()
    m2 = re.match(r"^(.+?)\s+\d{1,2}h\d{2}\s*(.*)$", clean, re.IGNORECASE)
    if m2:
        return m2.group(1).strip(), m2.group(2).strip()
    return clean, ""


def emit_vaga(label: str, data_evento: str = "", acao: str = "Visualizacao") -> None:
    nome, endereco = _parse_vaga_label(label)
    print("[VAGA] " + json.dumps({
        "data": display_date_for_log(data_evento),
        "acao": acao,
        "label": reparar_mojibake(label),
        "nome": reparar_mojibake(nome),
        "endereco": reparar_mojibake(endereco),
    }, ensure_ascii=False))


# â"€â"€ Cliente HTTP â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

class ProeisHTTP:
    def __init__(self, login: str, password: str, gemini_api_key: str = "", debug: bool = True):
        self.login = login
        self.password = password
        self.gemini_api_key = gemini_api_key
        self.debug = debug
        self.bad_captcha_reports = 0
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
        self.consecutive_site_rejections = 0
        # Coleta de dataset de captcha: cada resolucao guarda (imagem crua, resposta,
        # modelo); quando o site aceita/recusa, o resultado e anexado. Vira dataset
        # rotulado (site = rotulador gratuito) para medir modelos e treinar solver proprio.
        self._pending_captcha: dict | None = None
        self.captcha_samples: list[dict] = []
        _op_start()
        _model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite") if gemini_api_key else "nenhum"
        _log("INFO", f"Solver ativo: {_model}")

    # â"€â"€ HTTP â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    def reset_session(self) -> None:
        _log("HTTP", "Recriando sessao HTTP local.")
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
        self.soup = None

    def check_auth(self) -> bool:
        """Verifica se a sessao atual esta autenticada via MENU_URL.

        FrmEventoAssociar.aspx requer estado de navegacao e redireciona para
        Default.aspx mesmo com sessao valida. MENU_URL e acessivel diretamente
        com qualquer sessao autenticada e e o verificador correto.
        """
        t0 = time.monotonic()
        try:
            response = self.session.request(
                "GET", MENU_URL,
                timeout=(int(os.getenv("PROEIS_CONNECT_TIMEOUT", "8")), 10),
                allow_redirects=True,
            )
            self.site_elapsed_seconds += time.monotonic() - t0
            final_url = response.url
            soup_check = BeautifulSoup(response.text, "html.parser")
            # Detecta tela de login pelo URL ou pelos elementos reais (nao busca textual)
            if "Default.aspx" in final_url or soup_check.select_one("#txtSenha") or soup_check.select_one("#btnEntrar"):
                _log("SESSION", "Sessao expirada (tela de login detectada).")
                return False
            self.soup = soup_check
            self.last_url = final_url
            _log("SESSION", "Sessao ativa — menu carregado com sucesso.")
            return True
        except Exception as exc:
            self.site_elapsed_seconds += time.monotonic() - t0
            _log("SESSION", f"Erro ao verificar sessao: {exc}")
            return False

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

    # â"€â"€ Login â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    def login_flow(self) -> None:
        _phase("FASE 1/4: LOGIN")
        _log("LOGIN", "Iniciando autenticacao no PROEIS...")
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
                    "btnEntrar": self.input_value(soup, "btnEntrar") or "Avançar",
                }
            )
            soup = self.post_form(payload, DEFAULT_URL)
            page_text = norm(soup.get_text(" ", strip=True))

            if not soup.select_one("#txtSenha") and not soup.select_one("#TextCaptcha"):
                _step(5, 5, "LOGIN", "Login realizado com sucesso. Sessao autenticada.")
                self.report_good_captcha()
                return

            if "senha invalida" in page_text:
                raise AutomationError("Login recusado: senha invalida.")

            if "login e senha nao conferem" in page_text:
                _log(
                    "LOGIN",
                    f"PROEIS retornou 'login e senha nao conferem' (tentativa {attempt}/{max_attempts}); "
                    "tratando como falha recuperavel de login/captcha e tentando novamente...",
                )
                continue

            if "erro ao confirmar imagem" in page_text:
                _log("LOGIN", f"Captcha recusado pelo site (tentativa {attempt}/{max_attempts}); reportando erro e tentando novamente...")
                self.report_bad_captcha()
                continue

            if soup.select_one("#txtSenha") or soup.select_one("#TextCaptcha"):
                snippet = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:500]
                _log("LOGIN", f"Login permaneceu na tela inicial (tentativa {attempt}/{max_attempts}). Retorno: {snippet}")
                continue

            snippet = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:500]
            raise AutomationError(f"Login nao saiu da tela inicial. Retorno do site: {snippet}")

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

    # â"€â"€ Captcha â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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
        attempts = int(os.getenv("CAPTCHA_INVALID_RETRIES", "2")) + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                return self.solve_captcha_once(image)
            except AutomationError as exc:
                last_error = str(exc)
                if "resposta invalida" not in last_error or attempt == attempts:
                    raise
                _log("CAPTCHA", f"Resposta fora do padrao; reenviando captcha (tentativa {attempt}/{attempts}).")
        raise AutomationError(last_error or "Solver nao retornou uma resposta valida.")

    def solve_page_captcha(self, soup: BeautifulSoup, refresh_after_invalids: int | None = None) -> tuple[BeautifulSoup, str]:
        max_attempts = int(os.getenv("CAPTCHA_INVALID_RETRIES", "2")) + 1
        refresh_after_invalids = refresh_after_invalids or int(os.getenv("CAPTCHA_REFRESH_AFTER_INVALIDS", "1"))
        refresh_after_invalids = max(1, refresh_after_invalids)
        invalid_streak = 0
        last_error = ""
        current_soup = soup

        for attempt in range(1, max_attempts + 1):
            _log("CAPTCHA", f"Tentativa de resolucao {attempt}/{max_attempts}...")
            try:
                captcha = self.extract_captcha_image(current_soup)
                text = self.solve_captcha_once(captcha)
                self._remember_captcha(captcha, text)
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

                _log("CAPTCHA", f"Reenviando imagem atual para solver (tentativa {attempt}/{max_attempts}).")
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

                _log("CAPTCHA", f"Reenviando imagem atual para solver (tentativa {attempt}/{max_attempts}).")

        raise AutomationError(last_error or "Solver nao retornou uma resposta valida.")

    def solve_captcha_once(self, image: bytes) -> str:
        """Envia imagem ao Gemini e retorna a resposta valida."""
        t0 = time.monotonic()
        try:
            return self._solve_parallel_all_solvers(image)
        finally:
            self.captcha_elapsed_seconds += time.monotonic() - t0

    def _solve_parallel_all_solvers(self, image: bytes) -> str:
        """Envia o captcha para dois modelos Gemini em paralelo; vence quem responder primeiro."""
        if not self.gemini_api_key:
            raise AutomationError("Nenhum solver de captcha configurado (GEMINI_API_KEY).")

        primary = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
        fallback_after = int(os.getenv("GEMINI_PRO_FALLBACK_AFTER_REJECTS", "1"))
        fallback_model = os.getenv("GEMINI_PRO_FALLBACK_MODEL", "gemini-2.5-flash").strip()
        if fallback_model and self.bad_captcha_reports >= fallback_after:
            if primary != fallback_model:
                _log(
                    "CAPTCHA",
                    f"Site recusou {self.bad_captcha_reports} captcha(s); usando fallback de maior acerto: {fallback_model}.",
                )
            primary = fallback_model

        secondary = os.getenv("GEMINI_MODEL_PARALLEL", "").strip()
        if not secondary:
            secondary = primary

        # Escala para thinking mode apos N rejeicoes consecutivas do site.
        # GEMINI_THINKING_ESCALATION_BUDGET=0 (padrao) desativa; defina >0 para ativar.
        thinking_budget = 0
        escalation_budget = int(os.getenv("GEMINI_THINKING_ESCALATION_BUDGET", "0"))
        if escalation_budget > 0:
            escalation_threshold = int(os.getenv("CAPTCHA_ESCALATION_AFTER_REJECTIONS", "4"))
            if self.consecutive_site_rejections >= escalation_threshold:
                thinking_budget = escalation_budget
                _log("CAPTCHA", f"[Escalacao] Usando thinkingBudget={thinking_budget} apos {self.consecutive_site_rejections} rejeicao(oes) do site.")

        if primary == secondary:
            result = self._solve_via_gemini_result(image, model=primary, thinking_budget=thinking_budget)
            text = normalize_captcha_answer(result.text)
            self.last_captcha_id = result.captcha_id
            _log("CAPTCHA", f"Vencedor: {primary} | resposta={text}")
            return text

        stop_event = threading.Event()
        winner: list[CaptchaSubmission | None] = [None]
        primary_error:   list[Exception] = []
        secondary_errors: list[Exception] = []
        lock = threading.Lock()

        def run(model: str, is_primary: bool) -> None:
            try:
                result = self._solve_via_gemini_result(image, stop_event=stop_event, model=model, thinking_budget=thinking_budget)
                with lock:
                    if winner[0] is None:
                        winner[0] = result
                        stop_event.set()
                        _log("CAPTCHA", f"Vencedor: {model} | resposta={normalize_captcha_answer(result.text)}")
            except Exception as exc:
                msg = str(exc)
                # Modelo descontinuado pelo Google — ignorar silenciosamente
                if "no longer available" in msg or "HTTP 404" in msg:
                    _log("CAPTCHA", f"[{model}] Modelo indisponivel (ignorando): {msg[:80]}")
                    return
                with lock:
                    if is_primary:
                        primary_error.append(exc)
                    else:
                        secondary_errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(primary,   True),  daemon=True),
            threading.Thread(target=run, args=(secondary, False), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if winner[0]:
            text = normalize_captcha_answer(winner[0].text)
            self.last_captcha_id = winner[0].captcha_id
            return text

        # Propaga erro do modelo principal; ignora falhas do secundário
        if primary_error:
            raise primary_error[0]
        if secondary_errors:
            raise secondary_errors[0]
        raise AutomationError("Todos os solvers Gemini falharam sem retornar erro.")

    def _solve_via_gemini_result(
        self,
        image: bytes,
        stop_event: threading.Event | None = None,
        model: str | None = None,
        thinking_budget: int = 0,
        preprocess: str | None = None,
    ) -> CaptchaSubmission:
        """Resolve captcha usando Gemini via visao computacional."""
        if stop_event and stop_event.is_set():
            raise AutomationError("resolucao paralela cancelada apos vencedor")

        model = model or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
        processed = _preprocess_captcha_image(image, mode=preprocess)
        if len(processed) != len(image):
            _log("CAPTCHA", f"[Gemini] Preprocessamento: {len(image)}B -> {len(processed)}B")
        parts: list[dict[str, Any]] = [
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(image).decode("ascii"),
                }
            },
        ]
        if processed != image:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": base64.b64encode(processed).decode("ascii"),
                    }
                }
            )
        parts.append(
            {
                "text": (
                    "Read this PROEIS CAPTCHA. It has EXACTLY 6 characters, each a hexadecimal "
                    "digit: only 0 1 2 3 4 5 6 7 8 9 A B C D E F (uppercase).\n"
                    "The characters are colored and overlaid with random colored dots and thin "
                    "gray lines - ignore the dots and lines, read only the 6 aligned characters "
                    "left to right.\n"
                    "If a second image is present, it is the same captcha enhanced for OCR; "
                    "use both to decide the best reading.\n"
                    "Common confusions: O/Q -> 0, I/L -> 1, S -> 5, G -> 6, Z -> 2, B vs 8, D vs 0.\n"
                    "Never answer with placeholders like ABCDEF. Never add spaces, punctuation, JSON or explanation.\n"
                    "Return ONLY the 6 characters."
                )
            }
        )
        _log("CAPTCHA", f"[Gemini] Enviando imagem para {model}...")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        # thinking_budget parameter (>0) overrides env-based budget (for escalation after rejections)
        model_lc = model.lower()
        env_budget: int | None = None
        if "flash" in model_lc:
            env_budget = int(os.getenv("GEMINI_FLASH_THINKING_BUDGET", "0"))
        elif os.getenv("GEMINI_PRO_THINKING_BUDGET"):
            env_budget = int(os.getenv("GEMINI_PRO_THINKING_BUDGET", "0"))
        effective_budget = thinking_budget if thinking_budget > 0 else (env_budget or 0)
        # gemini-2.5-pro nao aceita thinkingBudget=0 (HTTP 400 "Budget 0 is invalid");
        # quando nenhum budget foi configurado para um modelo pro, usa o minimo aceito.
        if "2.5-pro" in model_lc and effective_budget <= 0:
            effective_budget = int(os.getenv("GEMINI_PRO_THINKING_BUDGET", "128"))
        max_output_tokens = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "32"))
        if effective_budget and "GEMINI_MAX_OUTPUT_TOKENS" not in os.environ:
            max_output_tokens = effective_budget + 64
        elif "2.5-pro" in model_lc and "GEMINI_MAX_OUTPUT_TOKENS" not in os.environ:
            max_output_tokens = int(os.getenv("GEMINI_PRO_MAX_OUTPUT_TOKENS", "2048"))
        payload = {
            "contents": [{
                "parts": parts
            }],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": max_output_tokens,
            },
        }
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": effective_budget}
        if effective_budget:
            _log("CAPTCHA", f"[Gemini] thinkingBudget={effective_budget} ativado para {model}.")

        timeout = int(os.getenv("GEMINI_TIMEOUT", "30"))
        max_429_retries = 2
        max_5xx_retries = int(os.getenv("GEMINI_5XX_RETRIES", "4"))
        retry_wait_429 = int(os.getenv("GEMINI_429_RETRY_WAIT", "62"))
        retry_wait_5xx = int(os.getenv("GEMINI_5XX_WAIT", "8"))  # initial backoff for 5xx; doubles each attempt

        total_attempts = max(max_429_retries, max_5xx_retries)
        _5xx_attempt = 0
        _429_attempt = 0

        for attempt in range(1, total_attempts + 1):
            if stop_event and stop_event.is_set():
                raise AutomationError("resolucao paralela cancelada apos vencedor")
            try:
                resp = requests.post(url, params={"key": self.gemini_api_key}, json=payload, timeout=timeout)
            except requests.RequestException as exc:
                raise AutomationError(f"Gemini: erro de rede: {exc}") from exc

            if stop_event and stop_event.is_set():
                raise AutomationError("resolucao paralela cancelada apos vencedor")

            if resp.status_code == 429:
                _429_attempt += 1
                if _429_attempt < max_429_retries:
                    _log("CAPTCHA", f"[Gemini] Rate limit (429); aguardando {retry_wait_429}s para reset da janela de 1min ({_429_attempt}/{max_429_retries})...")
                    self._sleep_or_cancel_parallel_captcha(retry_wait_429, stop_event)
                    continue
                raise AutomationError(f"Gemini: rate limit atingido apos {max_429_retries} tentativas")

            if resp.status_code >= 500:
                _5xx_attempt += 1
                wait = retry_wait_5xx * (2 ** (_5xx_attempt - 1))
                if _5xx_attempt < max_5xx_retries:
                    _log("CAPTCHA", f"[Gemini] Servico indisponivel (HTTP {resp.status_code}); aguardando {wait}s e tentando novamente ({_5xx_attempt}/{max_5xx_retries})...")
                    self._sleep_or_cancel_parallel_captcha(wait, stop_event)
                    continue
                raise AutomationError(f"Gemini: servico indisponivel apos {max_5xx_retries} tentativas (ultimo: HTTP {resp.status_code})")

            if resp.status_code != 200:
                raise AutomationError(f"Gemini: HTTP {resp.status_code}: {resp.text[:200]}")

            break

        data = resp.json()
        try:
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            raise CaptchaInvalidAnswerError("", f"Gemini sem texto: {data}") from exc

        text = normalize_captcha_answer(raw_text)
        _log("CAPTCHA", f"[Gemini] Resposta recebida: '{raw_text}' -> '{text}'")

        if not is_valid_captcha_answer(text):
            raise CaptchaInvalidAnswerError(text, raw_text)

        _log("CAPTCHA", f"[Gemini] Captcha valido: {text}")
        return CaptchaSubmission(text=text, captcha_id=None, solver_index=0, confidence=1.0)


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

    def _remember_captcha(self, image: bytes, answer: str) -> None:
        """Guarda a ultima resolucao (imagem crua + resposta) ate o site julgar."""
        if os.getenv("CAPTCHA_COLLECT", "1") != "1":
            return
        try:
            self._pending_captcha = {
                "image_b64": base64.b64encode(image).decode("ascii"),
                "answer": answer,
                "model": os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
                "preproc": os.getenv("CAPTCHA_PREPROCESS", "off"),
            }
        except Exception:
            self._pending_captcha = None

    def _label_pending_captcha(self, accepted: bool) -> None:
        """Anexa o veredito do site (aceito/recusado) a ultima resolucao pendente."""
        sample = self._pending_captcha
        self._pending_captcha = None
        if not sample:
            return
        sample["accepted"] = accepted
        # cap defensivo para nao estourar memoria/payload numa varredura longa
        if len(self.captcha_samples) < 500:
            self.captcha_samples.append(sample)

    def report_bad_captcha(self) -> None:
        self.bad_captcha_reports += 1
        self.consecutive_site_rejections += 1
        self._label_pending_captcha(False)

    def report_bad_captcha_id(self, captcha_id: str | None) -> None:
        self.bad_captcha_reports += 1
        self.consecutive_site_rejections += 1
        self._label_pending_captcha(False)

    def report_good_captcha(self) -> None:
        self.consecutive_site_rejections = 0
        self._label_pending_captcha(True)

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

    # â"€â"€ NavegaÃ§Ã£o â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    def _try_navigate_to_service_page(self) -> bool:
        _phase("FASE 2/4: NAVEGAÇÃO")
        _log("NAV", f"Acessando menu e navegando para tela de serviços ({MENU_URL.split('/')[-1]})...")
        menu_filename = MENU_URL.split("/")[-1]
        if self.soup is not None and self.last_url and menu_filename in self.last_url:
            _log("NAV", "Reutilizando pagina do menu ja carregada (sem nova requisicao).")
            soup = self.soup
        else:
            soup = self.request("GET", MENU_URL)

        if soup.select_one("#btnEscala") or "btnEscala" in str(soup):
            _log("NAV", "Botao 'Escala' encontrado. Clicando via postback...")
            soup = self.postback("btnEscala")

        if self.has_service_fields(soup):
            _log("NAV", "Tela de associar voluntario encontrada (campos de convenio/CPA presentes).")
            return True

        _log("NAV", "Procurando link 'Nova Inscricao'...")
        new_subscription = self.find_action_by_text(soup, ("nova inscricao", "nova inscriÃ§Ã£o"))
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
                return True

        keywords = (
            "inscricao", "inscrever", "servico", "servicos",
            "evento", "eventos", "escala", "minhas inscricoes",
        )
        for nav_step in range(1, 7):
            soup = self.require_soup()
            if self.has_service_fields(soup):
                _log("NAV", f"Tela de servico encontrada (etapa de navegacao {nav_step}).")
                return True
            candidate = self.best_navigation_link(soup, keywords)
            if not candidate:
                _log("NAV", "Nenhum link de navegacao encontrado.")
                break
            _log("NAV", f"Navegando para: '{candidate.label}' (score={candidate.score}, etapa {nav_step}/6)...")
            if candidate.action == "postback":
                self.postback(candidate.payload["target"], candidate.payload.get("argument", ""))
            else:
                self.request("GET", candidate.action)

        return False

    def navigate_to_service_page(self) -> None:
        # Se ja estamos na tela de filtros (ex: apos um "Eu Vou" confirmado),
        # nao volta ao menu - evita passar por FrmVoluntarioInscricoesConsultar
        # que altera o VIEWSTATE e faz o site retornar 0 resultados.
        if self.soup is not None and self.has_service_fields(self.soup):
            _log("NAV", "Ja na tela de servicos (FrmEventoAssociar). Reutilizando para proxima marcacao.")
            return

        if self._try_navigate_to_service_page():
            return

        # Navegacao falhou - pagina em estado inconsistente. Refaz login e tenta novamente.
        _log("NAV", "Navegacao falhou; refazendo login para limpar estado da sessao...")
        self.login_flow()
        if self._try_navigate_to_service_page():
            return

        raise AutomationError("Nao encontrei a tela de marcacao pelo fluxo de navegacao disponivel.")

    def reset_navigation_state(self) -> None:
        """Volta ao menu para limpar estado da sessao antes de proxima navegacao.
        Util para resetar estado quando grupo (convenio/data/cpa) muda entre eventos.
        """
        _log("NAV", "Limpando estado da navegacao: voltando ao menu...")
        try:
            soup = self.request("GET", MENU_URL)
            # Nao guarda o soup do menu para forcar re-navegacao na proxima chamada
            self.soup = None
            _log("NAV", "Retornado ao menu com sucesso.")
        except Exception as exc:
            _log("NAV", f"Nao consegui voltar ao menu: {exc}. Continuando mesmo assim...")
            self.soup = None

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
        return ("convenio" in text or "convÃªnio" in text) and ("cpa" in text or "data do evento" in text)

    # â"€â"€ Filtros â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    def fill_filters(self, convenio: str, data_evento: str, cpa: str, prefer: str = "nao-reserva") -> None:
        _phase("FASE 3/4: FILTROS")
        _log("FILTRO", f"Convênio='{convenio}' | Data='{data_evento}' | CPA='{cpa}' | Prefer='{prefer}'")
        soup = self.require_soup()
        fields = self.find_fields(soup)

        # Se _try_prefill_convenio ja fez o POST de convenio e as datas estao carregadas,
        # pula essa etapa para chegar direto na resolucao do captcha no horario critico.
        date_field = fields.get("data")
        convenio_field = fields.get("convenio")
        convenio_prefilled = False
        if date_field and convenio_field:
            date_select = soup.select_one(f'select[name="{date_field}"]')
            convenio_select = soup.select_one(f'select[name="{convenio_field}"]')
            if date_select:
                real_options = [
                    o for o in date_select.select("option")
                    if o.get("value", "") not in {"", "0"} and norm(o.get_text(" ", strip=True)) != "selecione"
                ]
                selected_ok = False
                if convenio_select:
                    selected_convenio = convenio_select.select_one("option[selected]")
                    if selected_convenio is None:
                        selected_list = convenio_select.select("option")
                        selected_convenio = selected_list[0] if selected_list else None
                    if selected_convenio is not None:
                        desired = norm(convenio)
                        selected_text = norm(selected_convenio.get_text(" ", strip=True))
                        selected_value = norm(selected_convenio.get("value", ""))
                        selected_ok = desired in {selected_text, selected_value} or desired in selected_text

                if real_options and selected_ok:
                    convenio_prefilled = True

        if convenio_prefilled:
            _log("FILTRO", "Convenio ja pre-selecionado. Indo direto para captcha.")
        else:
            _log("FILTRO", f"Selecionando convenio '{convenio}' (campo: {fields.get('convenio')})...")
            payload = self.form_payload(soup)
            self.set_field(payload, fields, "convenio", convenio)
            payload["__EVENTTARGET"] = fields["convenio"]
            payload["__EVENTARGUMENT"] = ""
            soup = self.post_form(payload)

        max_filter_attempts = int(os.getenv("FILTER_MAX_ATTEMPTS", "8"))
        for attempt in range(1, max_filter_attempts + 1):
            _log("FILTRO", f"Preenchendo data='{data_evento}' e cpa='{cpa}' (tentativa {attempt}/{max_filter_attempts})...")
            payload = self.form_payload(soup)
            fields = self.find_fields(soup)
            self.set_field(payload, fields, "data", normalize_date_for_site(data_evento))
            self.set_field(payload, fields, "cpa", cpa)
            _log("FILTRO", "Resolvendo captcha do formulario de filtro...")
            self.fill_page_captcha(soup, payload)
            self.set_reserva_checkbox(soup, payload, norm(prefer) == "reserva")
            submit = self.find_submit(soup, ("pesquisar", "buscar", "consultar", "filtrar", "listar", "avancar"))
            if submit:
                payload[submit] = self.input_value(soup, submit)
                _log("FILTRO", f"Botao de submit: '{submit}'")
            _log("FILTRO", "Consultando disponibilidade...")
            soup = self.post_form(payload)
            if "erro ao confirmar imagem" in norm(str(soup)):
                _log("FILTRO", f"Captcha de filtro recusado pelo site (tentativa {attempt}/{max_filter_attempts}); tentando novamente...")
                self.report_bad_captcha()
                refreshed = self.refresh_page_captcha(soup)
                if refreshed is not None:
                    soup = refreshed
                    _log("CAPTCHA", "Nova imagem de captcha obtida apos rejeicao do filtro.")
                continue
            _log("FILTRO", "Filtros aplicados com sucesso.")
            self.report_good_captcha()
            return
        raise AutomationError("Captcha do filtro falhou em todas as tentativas.")

    def fill_filters_first_matching_date(self, convenio: str, cpa: str, prefer: str, scan_rounds: int = 1) -> str:
        _log("FILTRO", f"=== Varredura de datas: convenio='{convenio}' cpa='{cpa}' prefer='{prefer}' rounds={scan_rounds} ===")
        max_filter_attempts = int(os.getenv("FILTER_MAX_ATTEMPTS", "8"))
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
                for attempt in range(1, max_filter_attempts + 1):
                    payload = self.form_payload(soup)
                    fields = self.find_fields(soup)
                    payload[fields["data"]] = value
                    self.set_field(payload, fields, "cpa", cpa)
                    self.fill_page_captcha(soup, payload)
                    self.set_reserva_checkbox(soup, payload, True)
                    submit = self.find_submit(soup, ("pesquisar", "buscar", "consultar", "filtrar", "listar", "avancar"))
                    if submit:
                        payload[submit] = self.input_value(soup, submit)
                    _log("FILTRO", f"Consultando vagas para '{label}' (tentativa {attempt}/{max_filter_attempts})...")
                    result_soup = self.post_form(payload)
                    if "erro ao confirmar imagem" in norm(str(result_soup)):
                        _log("FILTRO", f"Captcha recusado para data '{label}' (tentativa {attempt}/{max_filter_attempts}); tentando novamente...")
                        self.report_bad_captcha()
                        soup = result_soup
                        continue
                    self.report_good_captcha()
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
        _phase("FASE 4/4: MARCAÇÃO DE VAGA")
        _log("VAGA", f"Meta={quantidade} vaga(s) | Prefer='{prefer}' | Rounds={scan_rounds} | Data inicial='{start_date}'")
        self.navigate_to_service_page()

        confirmed = 0
        scan_rounds = coerce_scan_rounds(scan_rounds)

        if start_date:
            # Data especifica conhecida: pula dates_for_convenio() e re-navegacao.
            # fill_filters fara o POST de convenio por conta propria.
            # Economiza ~400ms (1 POST de EVENTTARGET + navegacao extra).
            normalized_start = normalize_date_for_site(start_date)
            dates: list[tuple[str, str]] = [(None, normalized_start)]  # type: ignore[list-item]
            _log("VAGA", f"Data especifica: buscando apenas em '{normalized_start}' (sem varredura de datas).")
            print(f"[VAGAS] Data especifica: '{normalized_start}'.")
            start_index, end_index = 0, 1
        else:
            # Modo varredura: enumera todas as datas disponiveis no convenio.
            # Reset de soup necessario pois dates_for_convenio usa postback parcial
            # (EVENTTARGET=ddlConvenios) que deixa ViewState invalido para captcha.
            dates = self.dates_for_convenio(convenio)
            print(f"[VAGAS] Marcacao por varredura iniciada: {len(dates)} data(s) disponivel(is).")
            self.soup = None
            start_index = first_scan_date_index(dates, "")
            end_index = len(dates)

        for scan_round in range(1, scan_rounds + 1):
            if confirmed >= quantidade:
                break
            if scan_rounds > 1:
                _log("VAGA", f"Rodada de varredura {scan_round}/{scan_rounds}.")

            date_index = start_index if scan_round == 1 else 0
            scan_end = end_index
            while date_index < scan_end and confirmed < quantidade:
                _, label = dates[date_index]
                _log("VAGA", f"Testando data: '{label}' ({date_index + 1}/{len(dates)})...")
                self.navigate_to_service_page()
                self.fill_filters(convenio, label, cpa, prefer=prefer)
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
        max_filter_attempts = int(os.getenv("FILTER_MAX_ATTEMPTS", "8"))
        disponibilidade_runs = [
            ("reserva", True),
            ("nao-reserva", False),
        ]
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
            date_seen_actions: set[tuple[str, str]] = set()
            date_total = 0
            for disponibilidade_nome, reserva_checked in disponibilidade_runs:
                for attempt in range(1, max_filter_attempts + 1):
                    payload = self.form_payload(soup)
                    fields = self.find_fields(soup)
                    payload[fields["data"]] = value
                    self.set_field(payload, fields, "cpa", cpa)
                    self.fill_page_captcha(soup, payload)
                    self.set_reserva_checkbox(soup, payload, reserva_checked)
                    submit = self.find_submit(soup, ("pesquisar", "buscar", "consultar", "filtrar", "listar", "avancar"))
                    if submit:
                        payload[submit] = self.input_value(soup, submit)
                    _log(
                        "VAGA",
                        f"Consultando disponibilidade {disponibilidade_nome} para '{label}' "
                        f"(tentativa {attempt}/{max_filter_attempts})...",
                    )
                    result_soup = self.post_form(payload)
                    if "erro ao confirmar imagem" in norm(str(result_soup)):
                        _log(
                            "VAGA",
                            f"Captcha recusado em '{label}' ({disponibilidade_nome}) "
                            f"(tentativa {attempt}/{max_filter_attempts}); tentando novamente...",
                        )
                        self.report_bad_captcha()
                        soup = result_soup
                        continue

                    candidates = self.available_candidates(result_soup, "qualquer")
                    new_candidates: list[Candidate] = []
                    for candidate in candidates:
                        action_key = (
                            candidate.action,
                            json.dumps(candidate.payload, sort_keys=True, ensure_ascii=False),
                        )
                        if action_key in date_seen_actions:
                            continue
                        date_seen_actions.add(action_key)
                        new_candidates.append(candidate)

                    self.report_good_captcha()
                    if new_candidates:
                        _log("VAGA", f"{len(new_candidates)} vaga(s) encontrada(s) em '{label}' ({disponibilidade_nome}):")
                        print(f"[VAGAS] {len(new_candidates)} vaga(s) encontrada(s) em {label} ({disponibilidade_nome}):")
                        for candidate in new_candidates:
                            emit_vaga(candidate.label, data_evento=label, acao="Visualizacao")
                        total += len(new_candidates)
                        date_total += len(new_candidates)
                    else:
                        _log("VAGA", f"Nenhuma vaga nova encontrada em '{label}' ({disponibilidade_nome}).")
                    soup = result_soup
                    break

            if date_total == 0:
                _log("VAGA", f"Nenhuma vaga encontrada em '{label}'.")

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
            if ("convenio" in key or "convÃªnio" in key) and "convenio" not in fields:
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

    # â"€â"€ Escolha de vagas â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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
        reserva_vaga = "reserva" in norm(chosen.label)
        if chosen.action == "postback":
            # Resolve captcha novo tambem aqui (o site exige no clique de participar).
            payload = self.form_payload(soup)
            self.fill_page_captcha(soup, payload)
            if reserva_vaga:
                self.set_reserva_checkbox(soup, payload, True)
            payload["__EVENTTARGET"] = chosen.payload["target"]
            payload["__EVENTARGUMENT"] = chosen.payload.get("argument", "")
            soup = self.post_form(payload)
        elif chosen.action == "submit":
            payload = dict(chosen.payload)
            self.fill_page_captcha(soup, payload)
            if reserva_vaga:
                self.set_reserva_checkbox(soup, payload, True)
            soup = self.post_form(payload)
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
        end_index = (start_index + 1) if start_date else len(dates)

        for scan_round in range(1, coerce_scan_rounds(scan_rounds) + 1):
            date_index = start_index if scan_round == 1 else 0
            scan_end = end_index if start_date else len(dates)
            while date_index < scan_end and simulated < quantidade:
                _, label = dates[date_index]
                self.navigate_to_service_page()
                self.fill_filters(convenio, label, cpa, prefer=prefer)
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
        prefer_norm = norm(prefer)
        should_try_fallback = prefer_norm in {"nao-reserva", "sem-reserva", "normal"}

        # Carrega todos os candidatos uma unica vez; evita parse duplo da mesma pagina
        all_candidates = self.available_candidates(soup, "qualquer")
        candidates = (
            [c for c in all_candidates if self.matches_preference(norm(c.label), prefer_norm)]
            if should_try_fallback
            else all_candidates
        )
        prefer_used = prefer
        filters = {"nome": nome_evento, "hora": hora_evento, "turno": turno, "endereco": endereco}
        active_filters = {k: v for k, v in filters.items() if v}
        _log("VAGA", f"{len(candidates)} candidato(s) antes dos filtros. Filtros ativos: {active_filters}")
        filtered = [
            candidate for candidate in candidates
            if self.event_matches(candidate.label, nome_evento, hora_evento, turno, endereco)
        ]

        if not filtered and should_try_fallback:
            any_filtered = [
                candidate
                for candidate in all_candidates
                if self.event_matches(candidate.label, nome_evento, hora_evento, turno, endereco)
            ]
            if any_filtered:
                _log(
                    "VAGA",
                    "Fallback de disponibilidade: nao encontrei linha exata em 'nao-reserva'; "
                    "continuando com candidatos de 'qualquer' para nao perder vaga compativel.",
                )
                candidates = all_candidates
                filtered = any_filtered
                prefer_used = "qualquer"

        filtered = sorted(
            filtered,
            key=lambda candidate: (
                self.event_match_score(candidate.label, nome_evento, hora_evento, turno, endereco),
                candidate.score,
            ),
            reverse=True,
        )
        if not filtered:
            _log("VAGA", f"Nenhum candidato passou pelos filtros. Opcoes disponiveis ({len(candidates)}):")
            if dry_run:
                print(f"[VAGAS] {len(candidates)} opcao(oes) disponivel(is) encontrada(s):")
                for c in candidates[:20]:
                    emit_vaga(c.label, data_evento=data_evento, acao="Visualizacao")
            else:
                for c in candidates[:20]:
                    _log("VAGA", f"  opcao ignorada pelos filtros: {c.label[:240]}")
            if not candidates:
                self.log_unmatched_action_rows(soup, prefer)
            # Mensagem de erro mais informativa quando o filtro de hora eliminou tudo.
            # As horas so sao sugeridas a partir de vagas do MESMO evento (filtro de
            # nome); listar horas de outros eventos da data induzia a "corrigir" um
            # cadastro que estava certo quando a vaga apenas nao existia na data.
            detail = ""
            if hora_evento and active_filters.get("hora") and candidates:
                from re import findall as _findall
                mesmo_evento = [
                    c for c in candidates
                    if self.event_matches(c.label, nome_evento, "", "", "")
                ] if nome_evento else candidates
                horas_disp = sorted({
                    m for c in mesmo_evento
                    for m in _findall(r'\b\d{1,2}:\d{2}(?::\d{2})?\b', c.label)
                })
                if horas_disp:
                    detail = f" Horas disponíveis desse evento nessa data: {', '.join(horas_disp)}. Ajuste a hora no cadastro do evento."
                elif nome_evento:
                    detail = " Nenhuma vaga desse evento nessa data (esgotada ou nao publicada); o cadastro pode estar certo."
            raise AutomationError(f"Nao encontrei a linha exata do evento solicitado.{detail}")
        chosen = filtered[0]
        for pos, candidate in enumerate(filtered[:5], 1):
            match_score = self.event_match_score(candidate.label, nome_evento, hora_evento, turno, endereco)
            _log("VAGA", f"Candidato filtrado #{pos}: match={match_score} vaga={candidate.score} | {candidate.label[:140]}")
        _log("VAGA", f"{len(filtered)} vaga(s) apos filtros (prefer usado='{prefer_used}'). Escolhida: '{chosen.label[:80]}'")
        if dry_run:
            print(f"[VAGAS] {len(filtered)} vaga(s) encontrada(s):")
            for c in filtered:
                emit_vaga(c.label, data_evento=data_evento, acao="Visualizacao")
            _log("VAGA", "dry_run=true: nao cliquei em Eu Vou.")
            return False
        _log("VAGA", f"Clicando em 'Eu Vou' (acao={chosen.action})...")
        # Vaga RESERVA exige o checkbox 'Aceita vaga reserva' marcado no clique.
        reserva_vaga = "reserva" in norm(chosen.label)
        if chosen.action == "postback":
            # O PROEIS exige um captcha NOVO tambem no clique de "Eu Vou"/participar.
            # Sem isso, o site rejeita e volta para a tela de filtros (marcacao nao confirma).
            payload = self.form_payload(soup)
            self.fill_page_captcha(soup, payload)
            if reserva_vaga:
                self.set_reserva_checkbox(soup, payload, True)
            payload["__EVENTTARGET"] = chosen.payload["target"]
            payload["__EVENTARGUMENT"] = chosen.payload.get("argument", "")
            soup = self.post_form(payload)
        elif chosen.action == "submit":
            payload = dict(chosen.payload)
            self.fill_page_captcha(soup, payload)
            if reserva_vaga:
                self.set_reserva_checkbox(soup, payload, True)
            soup = self.post_form(payload)
        else:
            soup = self.request("GET", chosen.action)
        success = self.confirm_if_needed(soup)
        if success:
            self.report_good_captcha()
            emit_vaga(chosen.label, data_evento=data_evento, acao="Clicado Eu vou")
        return success

    def event_matches(self, label: str, nome_evento: str, hora_evento: str, turno: str, endereco: str) -> bool:
        label_norm = norm_match(label)
        label_words = {word for word in label_norm.split() if len(word) >= 2}

        def _word_overlap_ratio(raw_value: str, min_len: int = 3) -> float:
            words = [w for w in norm_match(raw_value).split() if len(w) >= min_len]
            if not words:
                return 1.0
            hits = sum(1 for w in words if w in label_words)
            return hits / max(1, len(words))

        def nome_matches(nome: str) -> bool:
            nome_norm = norm_match(nome)
            if not nome_norm:
                return True
            return nome_norm in label_norm

        def hora_matches(hora: str) -> bool:
            if not hora:
                return True
            wanted = _time_tokens(hora)
            if not wanted:
                return norm_match(hora) in label_norm
            available = _time_tokens(label)
            return bool(wanted & available)

        def endereco_matches(raw_endereco: str) -> bool:
            if not raw_endereco:
                return True
            endereco_norm = norm_match(raw_endereco)
            if endereco_norm in label_norm:
                return True
            return _word_overlap_ratio(raw_endereco, min_len=3) >= 0.6

        checks = [
            (nome_evento, nome_matches(nome_evento)),
            (hora_evento, hora_matches(hora_evento)),
            (turno, norm_match(turno) in label_norm),
            (endereco, endereco_matches(endereco)),
        ]
        active = [matches for raw, matches in checks if raw]
        return all(active) if active else True

    def event_match_score(self, label: str, nome_evento: str, hora_evento: str, turno: str, endereco: str) -> int:
        label_norm = norm_match(label)
        label_words = {word for word in label_norm.split() if len(word) >= 2}
        score = 0
        nome_norm = norm_match(nome_evento)
        if nome_norm:
            if nome_norm in label_norm:
                score += 1000
            words = nome_norm.split()
            score += sum(20 for word in words if re.search(rf"\b{re.escape(word)}\b", label_norm))
            long_words = [w for w in words if len(w) >= 3]
            if long_words:
                overlap = sum(1 for word in long_words if word in label_words)
                score += int((overlap / len(long_words)) * 300)
            title_match = re.search(r"\b\d+\s*bpm\s+(.+?)\s+\d{2}:\d{2}:\d{2}\b", label_norm)
            if title_match:
                event_title = title_match.group(1).strip()
                if event_title == nome_norm:
                    score += 600
                elif event_title.endswith(f" {nome_norm}"):
                    score += 150
        if hora_evento:
            wanted = _time_tokens(hora_evento)
            available = _time_tokens(label)
            if wanted and available and (wanted & available):
                score += 300
            elif norm_match(hora_evento) in label_norm:
                score += 180
        if turno and norm_match(turno) in label_norm:
            score += 100
        if endereco:
            endereco_norm = norm_match(endereco)
            if endereco_norm in label_norm:
                score += 400
            else:
                end_words = [w for w in endereco_norm.split() if len(w) >= 3]
                if end_words:
                    overlap = sum(1 for word in end_words if word in label_words)
                    score += int((overlap / len(end_words)) * 240)
        return score

    def log_unmatched_action_rows(self, soup: BeautifulSoup, prefer: str, limit: int = 12) -> None:
        rows = []
        for row in soup.select("tr"):
            text = row.get_text(" ", strip=True)
            text_norm = norm(text)
            if not text_norm or "eu vou" not in text_norm:
                continue
            rows.append(text[:240])
            if len(rows) >= limit:
                break
        if not rows:
            _log("VAGA", "Diagnostico: nenhuma linha com 'Eu Vou' apareceu no HTML retornado.")
            return
        _log("VAGA", f"Diagnostico: {len(rows)} linha(s) com 'Eu Vou' ignoradas por prefer='{norm(prefer)}':")
        for row in rows:
            _log("VAGA", f"  linha ignorada: {row}")

    def available_candidates(self, soup: BeautifulSoup, prefer: str) -> list[Candidate]:
        prefer_norm = norm(prefer)
        debug_matches = os.getenv("PROEIS_DEBUG_MATCHES") == "1"
        candidates: list[Candidate] = []
        seen_actions: set[tuple[str, str]] = set()
        for row in soup.select("tr"):
            text = row.get_text(" ", strip=True)
            text_norm = norm(text)
            if not self.matches_preference(text_norm, prefer_norm):
                continue
            if debug_matches:
                _log("DEBUG", f"[MATCH] prefer='{prefer}' | texto='{text[:120]}'")
            action = self.row_action(row)
            if action:
                action_key = (action[0], json.dumps(action[1], sort_keys=True, ensure_ascii=False))
                if action_key not in seen_actions:
                    seen_actions.add(action_key)
                    candidates.append(Candidate(text[:240], action[0], action[1], self.preference_score(text_norm, prefer_norm)))
            else:
                controls = [
                    f"{c.name}|{norm(c.get_text(' ', strip=True))[:40]}|{norm(c.get('href', c.get('onclick', '')))[:40]}"
                    for c in row.select("a[href], a[onclick], input[type=submit], button")
                ]
                if debug_matches:
                    _log("DEBUG", f"[SEM ACAO] controls={controls}")
        for link in soup.select("a[href], a[onclick]"):
            if link.find_parent("tr") is not None:
                continue
            text = link.get_text(" ", strip=True)
            text_norm = norm(text)
            if text_norm in {"", "eu vou"}:
                continue
            if self.matches_preference(text_norm, prefer_norm):
                action = self.link_action(link)
                if action:
                    action_key = (action[0], json.dumps(action[1], sort_keys=True, ensure_ascii=False))
                    if action_key not in seen_actions:
                        seen_actions.add(action_key)
                        candidates.append(Candidate(text[:240], action[0], action[1], self.preference_score(text_norm, prefer_norm)))
        result = sorted(candidates, key=lambda item: item.score, reverse=True)
        if result:
            _log("VAGA", f"available_candidates(prefer='{prefer}'): {len(result)} candidato(s) encontrado(s).")
        if not result:
            rows_text = [norm(r.get_text(" ", strip=True))[:100] for r in soup.select("tr") if r.get_text(strip=True)]
            if debug_matches:
                _log("DEBUG", f"[ZERO] prefer='{prefer}' | {len(rows_text)} linhas na pagina:")
                for t in rows_text[:30]:
                    _log("DEBUG", f"  ROW: {t}")
        return result

    def matches_preference(self, text: str, prefer: str) -> bool:
        is_reserva_spot = bool(re.search(r"\b(?:disponivel\s+)?reserva\s*-\s*curso", text))
        has_titular_slot = re.search(r"\b\d+\s*-\s*curso", text) is not None
        if prefer in {"nao-reserva", "sem-reserva", "normal"}:
            return has_titular_slot
        if prefer in {"qualquer", "any", ""}:
            return "disponivel" in text or "reserva" in text or has_titular_slot
        if prefer == "reserva":
            return is_reserva_spot
        return re.search(rf"\b{re.escape(prefer)}\b", text) is not None or f"disponivel {prefer}" in text

    def preference_score(self, text: str, prefer: str) -> int:
        is_reserva_spot = bool(re.search(r"\b(?:disponivel\s+)?reserva\s*-\s*curso", text))
        if prefer in {"nao-reserva", "sem-reserva", "normal"} and not is_reserva_spot:
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
        for control in row.select("a[href], a[onclick], input[type=submit], button"):
            text = norm(
                " ".join(
                    [
                        control.get_text(" ", strip=True),
                        control.get("value", ""),
                        control.get("id", ""),
                        control.get("name", ""),
                        control.get("href", ""),
                        control.get("onclick", ""),
                    ]
                )
            )
            if not any(
                word in text
                for word in ("eu vou", "inscrever", "marcar", "reservar", "confirmar", "selecionar", "participar")
            ):
                continue
            if control.name == "a":
                return self.link_action(control)
            payload = self.form_payload()
            submit_name = control.get("name") or control.get("id")
            if not submit_name:
                continue
            payload[submit_name] = control.get("value", control.get_text(" ", strip=True))
            return ("submit", payload)

        for clickable in [row, *row.select("[onclick]")]:
            script = clickable.get("onclick", "")
            postback = self.parse_postback_script(script)
            if postback:
                return ("postback", postback)
        return None

    def link_action(self, link) -> tuple[str, dict[str, str]] | None:
        href = link.get("href", "")
        onclick = link.get("onclick", "")
        script = href or onclick
        postback = self.parse_postback_script(script)
        if postback:
            return ("postback", postback)
        if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
            return (urljoin(self.last_url, href), {})
        return None

    def parse_postback_script(self, script: str) -> dict[str, str] | None:
        if not script:
            return None
        direct = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", script)
        if direct:
            return {"target": direct.group(1), "argument": direct.group(2)}
        webform = re.search(r'WebForm_PostBackOptions\("([^"]+)"\s*,\s*"([^"]*)"', script)
        if webform:
            return {"target": webform.group(1), "argument": webform.group(2)}
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


# â"€â"€ Helpers globais â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AutomationError(f"Variavel/secret obrigatorio ausente: {name}")
    return value


def load_env_file(path: Path | None = None) -> None:
    paths: list[Path] = []
    if path is not None:
        paths.append(path)
    else:
        cwd_env = Path.cwd() / ".env"
        script_env = Path(__file__).resolve().parent / ".env"
        paths.append(cwd_env)
        if script_env != cwd_env:
            paths.append(script_env)

    for env_path in paths:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
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
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
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


# â"€â"€ CLI â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automacao HTTP puro PROEIS.")
    parser.add_argument("--convenio")
    parser.add_argument("--data-evento", default="")
    parser.add_argument("--cpa")
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
    parser.add_argument("--wait-until", default="", help="HH:MM:SS - aguarda ate esse horario para iniciar login/captcha e marcar.")
    parser.add_argument("--batch-events", default="", help="JSON com a lista de eventos preparada pela interface.")
    parser.add_argument(
        "--batch-independent",
        action="store_true",
        help="Processa cada evento do batch em sessao isolada (mais estavel, porem mais lento).",
    )
    parser.add_argument(
        "--recovery-window-seconds",
        type=int,
        default=int(os.getenv("PROEIS_RECOVERY_WINDOW_SECONDS", "0")),
        help="Tempo maximo para recuperar site/sessao antes de desistir.",
    )
    parser.add_argument(
        "--batch-window-seconds",
        type=int,
        default=int(os.getenv("PROEIS_BATCH_WINDOW_SECONDS", "0")),
        help="Tempo maximo para repetir rodadas dos eventos nao confirmados.",
    )
    parser.add_argument(
        "--batch-repeat-pause-seconds",
        type=int,
        default=int(os.getenv("PROEIS_BATCH_REPEAT_PAUSE_SECONDS", "30")),
        help="Pausa curta entre rodadas de eventos pendentes.",
    )
    parser.add_argument(
        "--batch-max-no-action-rounds",
        type=int,
        default=int(os.getenv("PROEIS_BATCH_MAX_NO_ACTION_ROUNDS", "2")),
        help="Maximo de rodadas consecutivas sem 'Eu Vou' antes de encerrar pendencias (0=sem limite).",
    )
    parser.add_argument(
        "--auto-retry-rounds",
        type=int,
        default=int(os.getenv("PROEIS_AUTO_RETRY_ROUNDS", "0")),
        help="Numero de rodadas automaticas extras apos a janela principal esgotar (0=sem retry).",
    )
    parser.add_argument(
        "--auto-retry-wait-seconds",
        type=int,
        default=int(os.getenv("PROEIS_AUTO_RETRY_WAIT_SECONDS", "300")),
        help="Espera em segundos entre rodadas automaticas de retry.",
    )
    return parser.parse_args()


def load_batch_events(path: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise AutomationError("--batch-events deve apontar para um JSON contendo uma lista.")

    events = []
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise AutomationError(f"Evento agrupado #{index} invalido: esperado objeto JSON.")
        settings = {
            "convenio": reparar_mojibake(item.get("convenio", "")).strip(),
            "data_evento": reparar_mojibake(item.get("data_evento", "")).strip(),
            "cpa": reparar_mojibake(item.get("cpa", "")).strip(),
            "disponivel": reparar_mojibake(item.get("disponivel", "nao-reserva")).strip() or "nao-reserva",
            "quantidade": int(item.get("quantidade") or 1),
            "nome_evento": reparar_mojibake(item.get("nome_evento", "")).strip(),
            "hora_evento": reparar_mojibake(item.get("hora_evento", "")).strip(),
            "turno": reparar_mojibake(item.get("turno", "")).strip(),
            "endereco": reparar_mojibake(item.get("endereco", "")).strip(),
        }
        if not settings["convenio"] or not settings["cpa"]:
            raise AutomationError(f"Evento agrupado #{index} sem convenio ou CPA.")
        if settings["disponivel"] not in {"reserva", "nao-reserva"}:
            raise AutomationError(f"Evento agrupado #{index} com tipo de vaga invalido: {settings['disponivel']}.")
        if settings["quantidade"] < 1:
            raise AutomationError(f"Evento agrupado #{index} com quantidade invalida.")
        events.append(settings)
    if not events:
        raise AutomationError("--batch-events nao contem eventos.")
    return events


def _try_prefill_convenio(client: ProeisHTTP, convenio: str, check_date: str) -> bool:
    """Navega ate a tela de filtros e faz o POST de convenio antecipado.

    Retorna True apenas se as datas do servidor ja incluem check_date, indicando
    que fill_filters pode pular o POST de convenio no horario critico.
    Se as datas ainda nao estiverem disponiveis, o soup fica na tela base (sem convenio
    selecionado) e fill_filters roda normalmente.
    """
    client.navigate_to_service_page()
    soup = client.require_soup()
    fields = client.find_fields(soup)
    if not fields.get("convenio"):
        raise AutomationError("Campo de convenio nao encontrado para pre-selecao.")
    payload = client.form_payload(soup)
    client.set_field(payload, fields, "convenio", convenio)
    payload["__EVENTTARGET"] = fields["convenio"]
    payload["__EVENTARGUMENT"] = ""
    result_soup = client.post_form(payload)

    fields2 = client.find_fields(result_soup)
    date_field = fields2.get("data")
    if not date_field:
        return False
    date_select = result_soup.select_one(f'select[name="{date_field}"]')
    if not date_select:
        return False
    real_options = [
        o for o in date_select.select("option")
        if o.get("value", "") not in {"", "0"} and norm(o.get_text(" ", strip=True)) != "selecione"
    ]
    if not real_options:
        _log("INFO", "Pre-selecao de convenio: ainda sem datas disponiveis no servidor.")
        return False

    date_labels = [o.get_text(" ", strip=True) for o in real_options]
    if check_date and not any(check_date in lbl or lbl in check_date for lbl in date_labels):
        _log("INFO", f"Pre-selecao: data alvo '{check_date}' ainda nao disponivel. Datas no servidor: {date_labels}")
        return False

    # Datas OK — mantém o soup com convenio ja selecionado para fill_filters reutilizar.
    client.soup = result_soup
    _log("INFO", f"Convenio '{convenio}' pre-selecionado. Datas disponiveis: {date_labels}. Pulando POST de convenio no horario.")
    return True


def wait_for_target_time(
    wait_until: str,
    client: ProeisHTTP | None = None,
    prefill_convenio: str = "",
    prefill_date: str = "",
) -> bool:
    if not wait_until:
        return False
    try:
        t = datetime.strptime(wait_until, "%H:%M:%S")
    except ValueError:
        t = datetime.strptime(wait_until, "%H:%M")
    now = datetime.now()
    target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    wait_secs = int((target - datetime.now()).total_seconds())
    _log("INFO", f"Agendamento ativo. Aguardando ate {target.strftime('%H:%M:%S')} ({wait_secs}s)...")
    print(f"Agendamento ativo. Iniciando login/marcacao em {wait_secs}s (horario: {target.strftime('%H:%M:%S')})...")
    PRE_NAV_SECONDS = 30
    pre_navigated = False
    pre_nav_attempted = False
    while True:
        remaining = int((target - datetime.now()).total_seconds())
        if remaining <= 0:
            break
        if not pre_nav_attempted and remaining <= PRE_NAV_SECONDS and client:
            pre_nav_attempted = True
            try:
                if prefill_convenio:
                    _log("INFO", f"Pre-selecionando convenio '{prefill_convenio}' ({remaining}s antes do horario)...")
                    pre_navigated = _try_prefill_convenio(client, prefill_convenio, prefill_date)
                    if not pre_navigated:
                        # Datas ainda nao disponiveis: ao menos posiciona na tela de filtros.
                        client.navigate_to_service_page()
                        pre_navigated = True
                else:
                    _log("INFO", f"Pre-navegando para tela de servicos ({remaining}s antes do horario)...")
                    client.navigate_to_service_page()
                    pre_navigated = True
                _log("INFO", "Bot posicionado. Aguardando horario para busca.")
            except Exception as exc:
                _log("WARN", f"Pre-navegacao falhou ({exc}); continuando aguardando normalmente.")
        if remaining % 10 == 0 or remaining <= 5:
            _log("INFO", f"Aguardando horario... {remaining}s restantes.")
        time.sleep(1)
    _log("INFO", f"Horario {target.strftime('%H:%M:%S.%f')} atingido.")
    return pre_navigated


def login_with_retries(client: ProeisHTTP, reason: str, attempts: int = 3) -> None:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                client.reset_session()
            _log("LOGIN", f"{reason} (tentativa {attempt}/{attempts})")
            client.login_flow()
            return
        except AutomationError as exc:
            last_error = str(exc)
            if attempt == attempts:
                break
            wait_seconds = min(10, 2 * attempt)
            _log("LOGIN", f"Falha no login: {exc}. Nova tentativa em {wait_seconds}s.")
            time.sleep(wait_seconds)
    raise AutomationError(f"Login falhou apos {attempts} tentativa(s): {last_error}")


def looks_like_login_page(soup: BeautifulSoup) -> bool:
    text = norm(soup.get_text(" ", strip=True))
    return bool(soup.select_one("#txtSenha") or soup.select_one("#TextCaptcha") or "tipo de acesso" in text)


def ensure_session_ready(client: ProeisHTTP, reason: str = "Validando sessao") -> None:
    _log("LOGIN", f"{reason}: checando acesso ao menu antes da marcacao.")
    try:
        soup = client.request("GET", MENU_URL)
    except AutomationError as exc:
        _log("LOGIN", f"Nao consegui acessar o menu ({exc}); refazendo login.")
        login_with_retries(client, "Relogin apos falha ao abrir o PROEIS")
        return

    if looks_like_login_page(soup):
        _log("LOGIN", "Sessao caiu ou voltou para a tela de login; refazendo login.")
        login_with_retries(client, "Relogin por sessao expirada")
        return

    _log("LOGIN", "Sessao validada.")


def is_recoverable_error(exc: AutomationError) -> bool:
    message = norm(str(exc))
    if "nao encontrei a linha exata do evento solicitado" in message:
        return False
    if "nenhuma opcao disponivel" in message:
        return False
    if "nao encontrada no select" in message:
        return False
    markers = (
        "falha de rede",
        "sessao",
        "login",
        "acessando",
        "nao encontrei a tela de marcacao",
        "nenhuma pagina carregada",
        "opcao",
        "captcha",
    )
    return any(marker in message for marker in markers)


def is_no_action_available_error(message: str) -> bool:
    message_norm = norm(message)
    return (
        "nao encontrei a linha exata do evento solicitado" in message_norm
        or "nenhuma opcao disponivel" in message_norm
        or "nenhuma linha com eu vou" in message_norm
    )


def recover_until_ready(client: ProeisHTTP, reason: str, window_seconds: int) -> None:
    if window_seconds <= 0:
        ensure_session_ready(client, reason)
        return

    deadline = time.monotonic() + window_seconds
    attempt = 1
    last_error = ""
    _log("LOGIN", f"{reason}: janela de recuperacao ativada por ate {window_seconds}s.")

    while True:
        try:
            ensure_session_ready(client, f"{reason} tentativa {attempt}")
            return
        except AutomationError as exc:
            last_error = str(exc)
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                break
            wait_seconds = min(10, max(2, remaining))
            _log("LOGIN", f"PROEIS ainda indisponivel/sessao invalida: {exc}. Nova tentativa em {wait_seconds}s; restam {remaining}s.")
            time.sleep(wait_seconds)
            attempt += 1

    raise AutomationError(f"Nao foi possivel recuperar site/sessao dentro de {window_seconds}s: {last_error}")


def run_one_batch_event(
    client: ProeisHTTP,
    event: dict[str, Any],
    index: int,
    total: int,
    dry_run: bool,
    scan_rounds: int,
    recovery_window_seconds: int,
    last_group: tuple[str, str, str],
) -> tuple[bool, bool, tuple[str, str, str], str]:
    group = (event["convenio"], event["data_evento"], event["cpa"])
    _log(
        "INFO",
        f"--- Evento agrupado {index}/{total}: "
        f"nome='{event['nome_evento']}' data='{event['data_evento']}' hora='{event['hora_evento']}' ---",
    )
    last_error = ""

    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            if attempt == 2:
                _log("INFO", f"Regra 1: retry imediato do evento {index}/{total} apos clique sem confirmacao.")
                client.soup = None
                last_group = ("", "", "")
            elif attempt == 3:
                _log("INFO", f"Regra 2: retry do evento {index}/{total} apos logout/login.")
                try:
                    client.session.cookies.clear()
                except Exception:
                    pass
                client.soup = None
                client.login_flow()
                last_group = ("", "", "")

            if group != last_group:
                # OPCAO 2: Limpar estado do navegador voltando ao menu quando grupo mudar
                client.reset_navigation_state()
                client.navigate_to_service_page()
                if event["data_evento"]:
                    client.fill_filters(event["convenio"], event["data_evento"], event["cpa"], prefer=event["disponivel"])
                else:
                    event["data_evento"] = client.fill_filters_first_matching_date(
                        event["convenio"], event["cpa"], event["disponivel"], scan_rounds=scan_rounds,
                    )
                last_group = group
            else:
                _log("INFO", "Reutilizando filtros da combinacao anterior.")

            success = client.choose_target_event(
                event["disponivel"],
                dry_run,
                data_evento=event["data_evento"],
                nome_evento=event["nome_evento"],
                hora_evento=event["hora_evento"],
                turno=event["turno"],
                endereco=event["endereco"],
            )
            if success:
                _log(
                    "INFO",
                    f"Evento {index}/{total} confirmado na tentativa {attempt}/{max_attempts}; sem retries adicionais para este evento.",
                )
                last_group = ("", "", "")
                return True, True, last_group, ""
            if dry_run:
                _log("INFO", f"Evento {index}/{total}: dry_run ativo; pulando retries de confirmacao.")
                last_group = ("", "", "")
                return True, False, last_group, ""
            last_error = "Clique executado, mas o site nao confirmou a marcacao."
            _log("INFO", f"Evento {index}/{total} segue sem confirmacao apos o clique.")
            if attempt < max_attempts:
                _log("INFO", f"Tentarei novamente este mesmo evento ({attempt + 1}/{max_attempts}).")
                continue
            _log("INFO", "Regra 3: esgotadas as tentativas deste evento; seguindo para o proximo.")
            _log("INFO", "Resetando estado para forcar nova navegacao no proximo evento.")
            last_group = ("", "", "")
            return True, False, last_group, last_error
        except AutomationError as exc:
            last_error = str(exc)
            _log("ERRO", f"Evento {index}/{total} falhou na tentativa {attempt}/{max_attempts}: {exc}")
            if not is_no_action_available_error(last_error):
                last_group = ("", "", "")
            else:
                if attempt < 2:
                    _log(
                        "INFO",
                        f"Evento {index}/{total}: sem linha/botao 'Eu Vou'. Vou tentar mais uma vez antes de desistir.",
                    )
                    last_group = ("", "", "")
                    client.soup = None
                    time.sleep(1)
                    continue
                _log("INFO", f"Evento {index}/{total}: falha sem acao disponivel; nao vou repetir tentativa imediata.")
                break
            if attempt < max_attempts:
                if is_recoverable_error(exc):
                    try:
                        recover_until_ready(client, "Recuperacao apos falha recuperavel", recovery_window_seconds)
                    except AutomationError as recover_exc:
                        last_error = str(recover_exc)
                        _log("ERRO", f"Recuperacao falhou para evento {index}: {recover_exc}")
                        break
                else:
                    time.sleep(1)

    return False, False, last_group, last_error


def run_batch_events(
    client: ProeisHTTP,
    events: list[dict[str, Any]],
    dry_run: bool,
    scan_rounds: int,
    recovery_window_seconds: int = 0,
    batch_window_seconds: int = 0,
    batch_repeat_pause_seconds: int = 30,
    batch_max_no_action_rounds: int = 2,
) -> int:
    _log("INFO", f"=== FASE 2: Marcacao agrupada ({len(events)} combinacao(oes)) ===")
    confirmed_total = 0
    no_action_total = 0
    last_group = ("", "", "")
    # Salva a data original de cada evento para resetar a cada nova rodada.
    # Eventos sem data usam varredura de datas; sem o reset, a data encontrada
    # na rodada anterior ficaria gravada e o bot nunca varreria outros dias.
    original_data: dict[int, str] = {i: e["data_evento"] for i, e in enumerate(events, 1)}
    pending: list[tuple[int, dict[str, Any]]] = list(enumerate(events, 1))
    deadline = time.monotonic() + batch_window_seconds if batch_window_seconds > 0 else None
    round_index = 1
    confirmed_per_event: dict[int, int] = {}  # indice do evento -> quantas vezes marcou
    event_status: dict[int, str] = {}
    event_last_error: dict[int, str] = {}
    event_attempts: dict[int, int] = {}
    consecutive_no_action_rounds = 0

    while pending:
        confirmed_this_round = 0
        no_action_this_round = 0
        if round_index > 1:
            _log("INFO", f"=== Nova varredura {round_index}: {len(pending)} evento(s) ainda pendente(s) ===")
            for index, event in pending:
                if not original_data[index]:
                    event["data_evento"] = ""

        next_pending: list[tuple[int, dict[str, Any]]] = []
        for index, event in pending:
            nome = event.get("nome_evento", "")
            quantidade = int(event.get("quantidade", 1))
            event_attempts[index] = event_attempts.get(index, 0) + 1

            if confirmed_per_event.get(index, 0) >= quantidade:
                _log("INFO", f"Pulando evento {index} '{nome}': quantidade {quantidade} ja atingida.")
                continue

            event_ok, success, last_group, last_error = run_one_batch_event(
                client,
                event,
                index,
                len(events),
                dry_run,
                scan_rounds,
                recovery_window_seconds,
                last_group,
            )
            if success:
                confirmed_total += 1
                confirmed_this_round += 1
                confirmed_per_event[index] = confirmed_per_event.get(index, 0) + 1
                event_status[index] = "confirmado"
                event_last_error.pop(index, None)
                _log("INFO", f"Evento {index} '{nome}': {confirmed_per_event[index]}/{quantidade} marcacao(oes) concluida(s).")
                if confirmed_per_event[index] < quantidade:
                    next_pending.append((index, event))
                continue
            if event_ok and dry_run:
                event_status[index] = "simulado"
                continue

            if last_error:
                event_last_error[index] = last_error
            if is_no_action_available_error(last_error):
                no_action_this_round += 1
                remaining = int(deadline - time.monotonic()) if deadline is not None else 0
                if deadline is not None and remaining > 0:
                    event_status[index] = "pendente"
                    next_pending.append((index, event))
                    _log(
                        "INFO",
                        f"Evento {index}/{len(events)} ainda nao apareceu com a linha exata; "
                        f"sera repetido na proxima varredura. Restam {remaining}s de janela.",
                    )
                    continue
                no_action_total += 1
                event_status[index] = "sem Eu Vou"
                _log(
                    "INFO",
                    f"Evento {index}/{len(events)} nao sera repetido agora: site nao exibiu linha/botao 'Eu Vou'. "
                    "Use Listar Vagas para confirmar se a vaga ainda aparece.",
                )
                continue

            event_status[index] = "pendente"
            next_pending.append((index, event))
            if last_error:
                _log(
                    "ERRO",
                    f"Evento {index}/{len(events)} segue pendente: "
                    f"nome='{event['nome_evento']}' data='{event['data_evento']}' hora='{event['hora_evento']}' erro='{last_error}'",
                )

        pending = next_pending
        if not pending:
            break
        if deadline is None:
            break

        if no_action_this_round > 0 and confirmed_this_round == 0:
            consecutive_no_action_rounds += 1
        else:
            consecutive_no_action_rounds = 0

        if (
            batch_max_no_action_rounds > 0
            and consecutive_no_action_rounds >= batch_max_no_action_rounds
            and pending
        ):
            _log(
                "INFO",
                f"Encerrando varredura: atingido limite de {batch_max_no_action_rounds} rodada(s) consecutiva(s) sem acao.",
            )
            break

        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            _log("INFO", "Janela de varredura encerrada; nao ha tempo para nova rodada.")
            break

        wait_seconds = min(max(0, batch_repeat_pause_seconds), remaining)
        if wait_seconds > 0:
            _log("INFO", f"Aguardando {wait_seconds}s antes da proxima varredura dos pendentes; restam {remaining}s de janela.")
            time.sleep(wait_seconds)
        round_index += 1

    _log("INFO", "=== Resumo por evento ===")
    for index, event in enumerate(events, 1):
        status = event_status.get(index, "pendente" if any(item[0] == index for item in pending) else "nao confirmado")
        error = event_last_error.get(index, "")
        detail = f" | erro='{error}'" if error else ""
        _log(
            "INFO",
            f"Evento {index}/{len(events)} [{status}] tentativas={event_attempts.get(index, 0)} "
            f"data='{event['data_evento']}' hora='{event['hora_evento']}' nome='{event['nome_evento']}'{detail}",
        )
    _log("INFO", f"Marcacao agrupada finalizada. Confirmadas: {confirmed_total}/{len(events)}. Sem Eu Vou: {no_action_total}. Pendentes nao marcados: {len(pending)}.")
    return confirmed_total


def run_batch_events_independent(
    client: ProeisHTTP,
    events: list[dict[str, Any]],
    dry_run: bool,
    scan_rounds: int,
    recovery_window_seconds: int = 0,
    batch_window_seconds: int = 0,
    batch_repeat_pause_seconds: int = 30,
    batch_max_no_action_rounds: int = 2,
) -> int:
    _log("INFO", f"=== FASE 2: Marcacao independente ({len(events)} evento(s)) ===")
    confirmed_total = 0
    expected_total = sum(max(1, int(event.get("quantidade", 1))) for event in events)

    for index, event in enumerate(events, 1):
        if index > 1:
            _log("INFO", f"Isolando sessao para evento {index}/{len(events)}: novo login.")
            try:
                client.session.cookies.clear()
            except Exception:
                pass
            client.soup = None
            login_with_retries(client, f"Login isolado para evento {index}/{len(events)}")
        else:
            _log("INFO", "Modo independente: primeiro evento usa a sessao ja autenticada.")

        confirmed_event = run_batch_events(
            client,
            [event],
            dry_run,
            scan_rounds,
            recovery_window_seconds,
            batch_window_seconds,
            batch_repeat_pause_seconds,
            batch_max_no_action_rounds,
        )
        confirmed_total += confirmed_event

    _log(
        "INFO",
        f"Marcacao independente finalizada. Confirmadas: {confirmed_total}/{expected_total} marcacao(oes) esperada(s).",
    )
    return confirmed_total


def main() -> int:
    load_env_file()
    log_path = _setup_log()
    _log("INFO", f"Arquivo de log: {log_path}")
    print(f"[LOG] Arquivo de log: {log_path}")
    args = parse_args()

    _log("INFO", "=== PROEIS Automacao HTTP ===")
    _log("INFO", f"convenio='{args.convenio or ''}' data='{args.data_evento}' cpa='{args.cpa or ''}'")
    _log("INFO", f"disponivel='{args.disponivel}' quantidade={args.quantidade} dry_run={args.dry_run}")
    if args.batch_events:
        _log("INFO", f"batch_independent={args.batch_independent}")

    batch_events = load_batch_events(args.batch_events) if args.batch_events else []
    if not batch_events and (not args.convenio or not args.cpa):
        raise AutomationError("Informe --convenio e --cpa, ou use --batch-events.")
    if args.quantidade < 1:
        raise AutomationError("--quantidade deve ser 1 ou maior.")
    if args.dry_run and args.quantidade != 1:
        _log("INFO", "dry_run=true: forcando quantidade=1 para teste rapido.")
        args.quantidade = 1

    client = ProeisHTTP(
        login=required_env("PROEIS_LOGIN"),
        password=required_env("PROEIS_PASSWORD"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        debug=not args.no_debug,
    )
    if not client.gemini_api_key:
        raise AutomationError("Configure a chave: GEMINI_API_KEY.")
    operation_started = time.monotonic()
    atexit.register(print_timing_summary, client, operation_started)

    if args.wait_until:
        _log("INFO", "=== FASE 1: Agendamento ===")
        wait_for_target_time(args.wait_until)
        _log("INFO", "Horario agendado atingido. Iniciando login/captcha.")

    _log("INFO", "=== FASE 2: Login ===")
    login_with_retries(client, "Login inicial")

    if batch_events:
        original_data_evento = [e.get("data_evento", "") for e in batch_events]
        total_events = len(batch_events)
        runner = run_batch_events_independent if args.batch_independent else run_batch_events
        for retry_round in range(1 + args.auto_retry_rounds):
            if retry_round > 0:
                wait = args.auto_retry_wait_seconds
                _log("INFO", f"=== Retry automatico {retry_round}/{args.auto_retry_rounds}: aguardando {wait}s antes de nova rodada ===")
                time.sleep(wait)
                for event, orig in zip(batch_events, original_data_evento):
                    if not orig:
                        event["data_evento"] = ""
                ensure_session_ready(client, f"Inicio do retry automatico {retry_round}")
            confirmed = runner(
                client,
                batch_events,
                args.dry_run,
                args.scan_rounds,
                args.recovery_window_seconds,
                args.batch_window_seconds,
                args.batch_repeat_pause_seconds,
                args.batch_max_no_action_rounds,
            )
            if confirmed >= total_events:
                _log("INFO", "Todos os eventos confirmados.")
                break
            if retry_round < args.auto_retry_rounds:
                _log("INFO", f"Retry {retry_round + 1}/{args.auto_retry_rounds}: {total_events - confirmed} evento(s) pendente(s) apos a janela.")
        return 0

    if args.list_all_dates:
        _log("INFO", "=== FASE 2: Listagem de todas as datas ===")
        client.navigate_to_service_page()
        client.list_all_available_dates(args.convenio, args.cpa)
        return 0

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
                    client.fill_filters(args.convenio, args.data_evento, args.cpa, prefer=args.disponivel)
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
