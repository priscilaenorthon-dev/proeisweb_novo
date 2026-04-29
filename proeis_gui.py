import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, IntVar, StringVar, Tk, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from timing_utils import format_elapsed


ROOT = Path(__file__).resolve().parent
OPTIONS_PATH = ROOT / "config" / "proeis_options.json"
SETTINGS_PATH = ROOT / "config" / "local_settings.json"

DEFAULTS = {
    "convenio": "08 BPM - RAS",
    "data_evento": "30/04/2026",
    "cpa": "8o BPM - 6o CPA",
    "disponivel": "reserva",
    "quantidade": 1,
}

DISPONIVEL_LABELS = {
    "reserva": "reserva",
    "nao-reserva": "nao-reserva (Titular)",
}

DISPONIVEL_VALUES = {label: value for value, label in DISPONIVEL_LABELS.items()}


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback


def load_options():
    options = load_json(OPTIONS_PATH, {"convenios": [], "cpas": []})
    convenios = [item["label"] for item in options.get("convenios", [])]
    cpas = [item["label"] for item in options.get("cpas", [])]
    return convenios, cpas


def save_settings(settings: dict):
    SETTINGS_PATH.parent.mkdir(exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def display_disponivel(value: str) -> str:
    return DISPONIVEL_LABELS.get(value, value)


def backend_disponivel(value: str) -> str:
    return DISPONIVEL_VALUES.get(value, value)


def parse_vaga_output(raw: str) -> tuple[str, str, str, str, str, str, str]:
    data_evento = ""
    acao = "Visualizacao"
    label = raw.strip()

    if label.startswith("{"):
        try:
            payload = json.loads(label)
            data_evento = str(payload.get("data", "")).strip()
            acao = str(payload.get("acao", acao)).strip() or acao
            label = str(payload.get("label", "")).strip()
        except json.JSONDecodeError:
            pass

    clean = re.sub(r"\s*Eu\s+Vou\s*$", "", label, flags=re.IGNORECASE).strip()
    m = re.match(
        r"^(.+?)\s+(\d{2}:\d{2}:\d{2})\s+(\d+\s*h)\s+(.+?)\s+(\d+\s*-\s*curso.*|RESERVA.*|DISPONIVEL.*)$",
        clean,
        re.IGNORECASE,
    )
    if m:
        nome, hora, turno, endereco, tipo = (
            m.group(1),
            m.group(2),
            m.group(3).strip(),
            m.group(4),
            m.group(5),
        )
    else:
        nome, hora, turno, endereco, tipo = clean, "", "", "", ""
    return data_evento, nome, hora, turno, endereco, tipo, acao


class ProeisApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("PROEIS - Automacao Local")
        self.root.geometry("1160x760")
        self.root.minsize(980, 620)
        self.root.configure(bg="#f1f5f9")
        self.process: subprocess.Popen | None = None
        self._log_file = None
        self._countdown_active = False
        self._operation_started_at: float | None = None
        self._operation_status_prefix = ""
        self._state_lock = threading.Lock()

        self.convenios, self.cpas = load_options()
        settings = DEFAULTS | load_json(SETTINGS_PATH, {})

        self.convenio        = StringVar(value=settings.get("convenio",    DEFAULTS["convenio"]))
        self.data_evento     = StringVar(value=settings.get("data_evento", DEFAULTS["data_evento"]))
        self.cpa             = StringVar(value=settings.get("cpa",         DEFAULTS["cpa"]))
        self.disponivel      = StringVar(value=display_disponivel(settings.get("disponivel", DEFAULTS["disponivel"])))
        self.quantidade      = IntVar(value=int(settings.get("quantidade", DEFAULTS["quantidade"])))
        self.agendamento     = BooleanVar(value=False)
        self.data_agendamento = StringVar(value="")
        self.hora_agendamento = StringVar(value="")
        self.status          = StringVar(value="Pronto")

        self._configure_style()
        self._build()

    # ── Style ─────────────────────────────────────────────────────────────────

    def _configure_style(self):
        s = ttk.Style()
        s.theme_use("clam")

        bg   = "#f1f5f9"
        card = "#ffffff"
        agenda_bg = "#f0fdf4"

        s.configure("TFrame",        background=bg)
        s.configure("Card.TFrame",   background=card)
        s.configure("Inner.TFrame",  background=card)
        s.configure("Agenda.TFrame", background=agenda_bg)

        s.configure("Header.TLabel",  background=bg,        foreground="#0f172a", font=("Segoe UI", 17, "bold"))
        s.configure("Sub.TLabel",     background=bg,        foreground="#64748b", font=("Segoe UI", 10))
        s.configure("StatusDone.TLabel", background=bg,     foreground="#dc2626", font=("Segoe UI", 10, "bold"))
        s.configure("Field.TLabel",   background=card,      foreground="#374151", font=("Segoe UI", 9,  "bold"))
        s.configure("Hint.TLabel",    background=card,      foreground="#94a3b8", font=("Segoe UI", 8))
        s.configure("TLabel",         background=card,      foreground="#0f172a", font=("Segoe UI", 10))
        s.configure("AgField.TLabel", background=agenda_bg, foreground="#166534", font=("Segoe UI", 9, "bold"))
        s.configure("AgHint.TLabel",  background=agenda_bg, foreground="#4ade80", font=("Segoe UI", 8))
        s.configure("Countdown.TLabel", background=bg, foreground="#16a34a", font=("Segoe UI", 10, "bold"))

        s.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8))
        s.configure("Primary.TButton", background="#2563eb", foreground="#ffffff")
        s.configure("Success.TButton", background="#16a34a", foreground="#ffffff")
        s.configure("Danger.TButton",  background="#dc2626", foreground="#ffffff")
        s.configure("Neutral.TButton", background="#e2e8f0", foreground="#475569")
        s.map("Primary.TButton", background=[("active", "#1d4ed8")])
        s.map("Success.TButton", background=[("active", "#15803d")])
        s.map("Danger.TButton",  background=[("active", "#b91c1c")])
        s.map("Neutral.TButton", background=[("active", "#cbd5e1")])

        s.configure("TCombobox", fieldbackground="#f8fafc", padding=6)
        s.configure("TEntry",    fieldbackground="#f8fafc", padding=8)
        s.configure("AgEntry.TEntry", fieldbackground="#dcfce7", padding=8)
        s.configure("TSeparator", background="#e2e8f0")

        s.configure("TCheckbutton", background=card, foreground="#374151", font=("Segoe UI", 10))
        s.configure("Agenda.TCheckbutton", background=card, foreground="#15803d", font=("Segoe UI", 10, "bold"))

        s.configure("TNotebook", background=bg)
        s.configure("TNotebook.Tab",
                    font=("Segoe UI", 10, "bold"), padding=(18, 9),
                    background="#e2e8f0", foreground="#64748b")
        s.map("TNotebook.Tab",
              background=[("selected", card)],
              foreground=[("selected", "#0f172a")])

        s.configure("Treeview",
                    background=card, foreground="#1e293b",
                    fieldbackground=card, font=("Segoe UI", 10), rowheight=30)
        s.configure("Treeview.Heading",
                    background="#f8fafc", foreground="#64748b",
                    font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
              background=[("selected", "#dbeafe")],
              foreground=[("selected", "#1e3a8a")])

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="PROEIS — Automacao Local", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Configure os filtros e clique em Consultar Filtros para listar vagas, ou Marcar para confirmar a inscricao.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(3, 16))

        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=0)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        # ── Formulário ────────────────────────────────────────────────────────
        form = ttk.Frame(content, style="Card.TFrame", padding=(20, 18))
        form.grid(row=0, column=0, sticky="nsw", padx=(0, 16))

        r = 0

        ttk.Label(form, text="Convenio", style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=(0, 4))
        r += 1
        ttk.Combobox(form, textvariable=self.convenio, values=self.convenios, width=36).grid(row=r, column=0, sticky="ew")
        r += 1

        ttk.Label(form, text="Data do Evento", style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=(14, 4))
        r += 1
        ttk.Entry(form, textvariable=self.data_evento, width=38).grid(row=r, column=0, sticky="ew")
        r += 1
        ttk.Label(form, text="DD/MM/AAAA — deixe vazio para varrer todas as datas", style="Hint.TLabel").grid(
            row=r, column=0, sticky="w", pady=(3, 0)
        )
        r += 1

        ttk.Label(form, text="CPA", style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=(14, 4))
        r += 1
        ttk.Combobox(form, textvariable=self.cpa, values=self.cpas, width=36).grid(row=r, column=0, sticky="ew")
        r += 1

        ttk.Label(form, text="Tipo de vaga", style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=(14, 4))
        r += 1
        ttk.Combobox(
            form, textvariable=self.disponivel,
            values=list(DISPONIVEL_VALUES), width=36, state="readonly",
        ).grid(row=r, column=0, sticky="ew")
        r += 1

        ttk.Label(form, text="Quantidade", style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=(14, 4))
        r += 1
        ttk.Spinbox(form, from_=1, to=20, textvariable=self.quantidade, width=10).grid(row=r, column=0, sticky="w")
        r += 1

        # ── Seção Agendamento ─────────────────────────────────────────────────
        ttk.Separator(form, orient="horizontal").grid(row=r, column=0, sticky="ew", pady=(18, 14))
        r += 1

        ttk.Checkbutton(
            form, text="Agendar execucao automatica",
            variable=self.agendamento, style="Agenda.TCheckbutton",
            command=self._toggle_agenda,
        ).grid(row=r, column=0, sticky="w")
        r += 1

        # Painel de agendamento (oculto por padrão)
        self._agenda_frame = ttk.Frame(form, style="Agenda.TFrame", padding=(12, 10))
        self._agenda_frame.grid(row=r, column=0, sticky="ew", pady=(8, 0))
        self._agenda_frame.grid_remove()
        r += 1

        af = self._agenda_frame
        ttk.Label(af, text="Data de inicio", style="AgField.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 3))
        ttk.Entry(af, textvariable=self.data_agendamento, width=14).grid(row=1, column=0, sticky="w")
        ttk.Label(af, text="DD/MM/AAAA", style="AgHint.TLabel").grid(row=2, column=0, sticky="w", pady=(2, 0))

        ttk.Label(af, text="Horario de inicio", style="AgField.TLabel").grid(row=0, column=1, sticky="w", padx=(16, 0), pady=(0, 3))
        ttk.Entry(af, textvariable=self.hora_agendamento, width=8).grid(row=1, column=1, sticky="w", padx=(16, 0))
        ttk.Label(af, text="HH:MM", style="AgHint.TLabel").grid(row=2, column=1, sticky="w", padx=(16, 0), pady=(2, 0))

        # ── Botões de ação ────────────────────────────────────────────────────
        ttk.Separator(form, orient="horizontal").grid(row=r, column=0, sticky="ew", pady=(18, 16))
        r += 1

        btn_frame = ttk.Frame(form, style="Inner.TFrame")
        btn_frame.grid(row=r, column=0, sticky="ew")
        btn_frame.columnconfigure((0, 1), weight=1)
        r += 1

        ttk.Button(btn_frame, text="Consultar Filtros", style="Primary.TButton", command=self.run_test).grid(
            row=0, column=0, sticky="ew", padx=(0, 5)
        )
        ttk.Button(btn_frame, text="Marcar", style="Success.TButton", command=self.run_real).grid(
            row=0, column=1, sticky="ew", padx=(5, 0)
        )
        ttk.Button(form, text="Listar Vagas", style="Neutral.TButton", command=self.run_list_all).grid(
            row=r, column=0, sticky="ew", pady=(8, 0)
        )
        r += 1
        ttk.Label(
            form,
            text="Consultar Filtros: usa os filtros atuais    Listar Vagas: varre todas as datas",
            style="Hint.TLabel",
        ).grid(row=r, column=0, sticky="w", pady=(6, 0))
        r += 1

        ttk.Button(form, text="Cancelar", style="Danger.TButton", command=self.cancel).grid(
            row=r, column=0, sticky="ew", pady=(12, 0)
        )
        r += 1
        ttk.Button(form, text="Salvar como padrao", style="Neutral.TButton", command=self.save_current).grid(
            row=r, column=0, sticky="ew", pady=(6, 0)
        )

        # ── Painel direito: abas ──────────────────────────────────────────────
        right = ttk.Frame(content)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self.status_label = ttk.Label(right, textvariable=self.status, style="Sub.TLabel")
        self.status_label.grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )

        self.notebook = ttk.Notebook(right)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        # Tab 1 — Vagas Encontradas
        tab_vagas = ttk.Frame(self.notebook, style="Card.TFrame", padding=14)
        self.notebook.add(tab_vagas, text="  Vagas Encontradas  ")
        tab_vagas.rowconfigure(0, weight=1)
        tab_vagas.columnconfigure(0, weight=1)

        cols = ("data", "nome", "hora", "turno", "endereco", "tipo", "acao")
        self.tree = ttk.Treeview(tab_vagas, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("data",     text="Data do Evento")
        self.tree.heading("nome",     text="Nome do Evento")
        self.tree.heading("hora",     text="Hora")
        self.tree.heading("turno",    text="Turno")
        self.tree.heading("endereco", text="Endereco")
        self.tree.heading("tipo",     text="Disponivel")
        self.tree.heading("acao",     text="Acao")
        self.tree.column("data",     width=102, anchor="center", stretch=False)
        self.tree.column("nome",     width=240, stretch=True)
        self.tree.column("hora",     width=72,  anchor="center", stretch=False)
        self.tree.column("turno",    width=62,  anchor="center", stretch=False)
        self.tree.column("endereco", width=180, stretch=True)
        self.tree.column("tipo",     width=126, anchor="center", stretch=False)
        self.tree.column("acao",     width=112, anchor="center", stretch=False)
        self.tree.tag_configure("reserva", foreground="#15803d")
        self.tree.tag_configure("normal",  foreground="#2563eb")

        vsb = ttk.Scrollbar(tab_vagas, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        ttk.Button(tab_vagas, text="Limpar lista", style="Neutral.TButton", command=self.clear_vagas).grid(
            row=1, column=0, columnspan=2, sticky="e", pady=(10, 0)
        )

        # Tab 2 — Log de Execucao
        tab_log = ttk.Frame(self.notebook, style="Card.TFrame", padding=14)
        self.notebook.add(tab_log, text="  Log de Execucao  ")
        tab_log.rowconfigure(0, weight=1)
        tab_log.columnconfigure(0, weight=1)

        self.log = ScrolledText(
            tab_log, wrap="word", font=("Consolas", 9),
            bg="#0f172a", fg="#e2e8f0", insertbackground="#e2e8f0",
            relief="flat", padx=12, pady=12,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        ttk.Button(tab_log, text="Limpar log", style="Neutral.TButton", command=self.clear_log).grid(
            row=1, column=0, sticky="e", pady=(10, 0)
        )

    # ── Agendamento ───────────────────────────────────────────────────────────

    def _toggle_agenda(self):
        if self.agendamento.get():
            self._agenda_frame.grid()
        else:
            self._agenda_frame.grid_remove()

    def _schedule_execution(self):
        data_str = self.data_agendamento.get().strip()
        hora_str = self.hora_agendamento.get().strip()

        if not data_str or not hora_str:
            messagebox.showerror("Campos vazios", "Preencha a data e o horario do agendamento.")
            return

        try:
            target = datetime.strptime(f"{data_str} {hora_str}", "%d/%m/%Y %H:%M")
        except ValueError:
            messagebox.showerror(
                "Formato invalido",
                "Use DD/MM/AAAA para data e HH:MM para o horario.\nExemplo: 30/04/2026  06:00",
            )
            return

        if target <= datetime.now():
            messagebox.showerror("Data no passado", "O agendamento deve ser para um horario futuro.")
            return

        if not messagebox.askyesno(
            "Confirmar agendamento",
            f"A marcacao sera iniciada automaticamente em:\n\n"
            f"  {target.strftime('%d/%m/%Y às %H:%M')}\n\n"
            f"Deixe o computador ligado. Continuar?",
        ):
            return

        self._countdown_active = True
        self.notebook.select(1)
        self.write_log(
            f"Agendamento ativado.\n"
            f"Inicio programado: {target.strftime('%d/%m/%Y as %H:%M')}\n"
            f"Aguardando...\n\n"
        )
        self._tick_countdown(target)

    # Inicia login antecipado 90s antes para estar pronto no horario exato
    _PRE_LOGIN_SECS = 90

    def _tick_countdown(self, target: datetime):
        with self._state_lock:
            if not self._countdown_active:
                return

        total_sec = int((target - datetime.now()).total_seconds())

        if total_sec <= 0:
            # Caso raro: evita disparos duplicados quando o timer atrasar.
            with self._state_lock:
                if not self._countdown_active:
                    return
                self._countdown_active = False
            self.start_process(dry_run=False, scheduled=True)
            return

        if total_sec <= self._PRE_LOGIN_SECS:
            # Dispara o processo agora com --wait-until para que o login
            # seja feito imediatamente enquanto aguarda o horario exato
            with self._state_lock:
                if not self._countdown_active:
                    return
                self._countdown_active = False
            self.status.set(f"Fazendo login antecipado... marcacao em {total_sec}s")
            self.write_log(
                f"Login antecipado iniciado ({total_sec}s antes do horario).\n"
                f"O bot vai fazer login agora e aguardar ate {target.strftime('%H:%M:%S')}.\n\n"
            )
            self.start_process(dry_run=False, wait_until=target, scheduled=True)
            return

        h, rem = divmod(total_sec, 3600)
        m, s   = divmod(rem, 60)
        self.status.set(
            f"Agendado para {target.strftime('%d/%m/%Y %H:%M')}  —  faltam {h:02d}:{m:02d}:{s:02d}"
        )
        self.root.after(1000, self._tick_countdown, target)

    # ── Settings ──────────────────────────────────────────────────────────────

    def current_settings(self):
        return {
            "convenio":    self.convenio.get().strip(),
            "data_evento": self.data_evento.get().strip(),
            "cpa":         self.cpa.get().strip(),
            "disponivel":  backend_disponivel(self.disponivel.get().strip()),
            "quantidade":  int(self.quantidade.get()),
        }

    def save_current(self):
        save_settings(self.current_settings())
        self.write_log("Configuracao salva como padrao.\n")

    # ── Actions ───────────────────────────────────────────────────────────────

    def run_test(self):
        self.start_process(dry_run=True)

    def run_list_all(self):
        self.start_process(dry_run=True, list_all_dates=True)

    def run_real(self):
        if self.agendamento.get():
            self._schedule_execution()
            return
        if not messagebox.askyesno(
            "Confirmar marcacao",
            "Isso vai clicar em Eu Vou se encontrar vaga.\nContinuar?",
        ):
            return
        self.start_process(dry_run=False)

    def start_process(
        self,
        dry_run: bool,
        wait_until: datetime | None = None,
        list_all_dates: bool = False,
        scheduled: bool = False,
    ):
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Em execucao", "A automacao ja esta rodando.")
            return
        settings = self.current_settings()
        if not settings["convenio"] or not settings["cpa"]:
            messagebox.showerror("Campos obrigatorios", "Informe Convenio e CPA.")
            return
        if settings["quantidade"] < 1:
            messagebox.showerror("Quantidade invalida", "A quantidade deve ser 1 ou maior.")
            return

        save_settings(settings)

        args = [
            sys.executable, str(ROOT / "proeis_http.py"),
            "--convenio",   settings["convenio"],
            "--cpa",        settings["cpa"],
            "--disponivel", settings["disponivel"],
            "--quantidade", str(settings["quantidade"]),
        ]
        if settings["data_evento"] and not list_all_dates:
            args.extend(["--data-evento", settings["data_evento"]])
        if dry_run:
            args.append("--dry-run")
        if list_all_dates:
            args.append("--list-all-dates")
        if scheduled and not settings["data_evento"]:
            args.extend(["--scan-rounds", "2"])
        if wait_until:
            args.extend(["--wait-until", wait_until.strftime("%H:%M:%S")])

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"{ts}_gui.log"
        try:
            self._log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        except OSError as exc:
            self._log_file = None
            messagebox.showerror("Erro de log", f"Nao foi possivel criar o arquivo de log:\n{exc}")
            return

        self.clear_log()
        self.clear_vagas()
        self.notebook.select(1)
        self.status_label.configure(style="Sub.TLabel")

        self.write_log(f"[LOG] Arquivo de log: {log_path}\n\n")
        self.write_log(f"Convenio:    {settings['convenio']}\n")
        self.write_log(f"Data Evento: {'(todas as datas)' if list_all_dates else settings['data_evento'] or '(varrer todas)'}\n")
        self.write_log(f"CPA:         {settings['cpa']}\n")
        self.write_log(f"Tipo:        {settings['disponivel']}\n")
        self.write_log(f"Quantidade:  {settings['quantidade']}\n")
        mode = "Listar vagas (todas as datas)" if list_all_dates else ("Teste (sem confirmar)" if dry_run else "MARCACAO REAL")
        self.write_log(f"Modo:        {mode}\n\n")
        if not dry_run and not list_all_dates and settings["quantidade"] > 1:
            self.write_log(
                "Observacao: apos confirmar uma vaga, o bot avanca para a proxima data,\n"
                "pois o CPROEIS pode ocultar os demais servicos do mesmo dia.\n\n"
            )
        if scheduled and not settings["data_evento"]:
            self.write_log("Agendamento sem data: varredura em 2 rodadas.\n\n")

        self._operation_started_at = time.perf_counter()
        start_line = f"Inicio:      {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        self.write_log(start_line)
        if self._log_file:
            self._log_file.write(start_line)

        self._operation_status_prefix = "Listando vagas" if list_all_dates else ("Testando" if dry_run else "Executando marcacao")
        self.status.set(f"{self._operation_status_prefix}... tempo: 0.0s")
        try:
            self.process = subprocess.Popen(
                args, cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
        except OSError as exc:
            if self._log_file:
                self._log_file.write(f"[Erro ao iniciar subprocesso: {exc}]\n")
                self._log_file.close()
                self._log_file = None
            self.status.set("Falha ao iniciar")
            messagebox.showerror("Falha ao iniciar", f"Nao foi possivel iniciar a automacao:\n{exc}")
            return
        self._tick_operation_timer()
        threading.Thread(target=self._read_output, daemon=True).start()

    # ── Output ────────────────────────────────────────────────────────────────

    def _read_output(self):
        assert self.process is not None
        for line in self.process.stdout or []:
            if self._log_file:
                self._log_file.write(line)
                self._log_file.flush()
            stripped = line.rstrip("\n")
            if stripped.startswith("[VAGA]"):
                vaga = stripped[6:].lstrip()
                self.root.after(0, self.write_log, f"  -> {vaga}\n")
                self.root.after(0, self._add_vaga_row, vaga)
            elif stripped.startswith("[VAGAS]"):
                msg = stripped[7:].lstrip()
                self.root.after(0, self.write_log, f"{msg}\n")
            else:
                self.root.after(0, self.write_log, line)
        code = self.process.wait()
        self.root.after(0, self._finish, code)

    def _tick_operation_timer(self):
        if self._operation_started_at is None:
            return
        if self.process and self.process.poll() is None:
            elapsed = format_elapsed(time.perf_counter() - self._operation_started_at)
            self.status.set(f"{self._operation_status_prefix}... tempo: {elapsed}")
            self.root.after(1000, self._tick_operation_timer)

    def _add_vaga_row(self, label: str):
        try:
            data_evento, nome, hora, turno, endereco, tipo, acao = parse_vaga_output(label)
            tag = "reserva" if "reserva" in tipo.lower() else "normal"
            self.tree.insert("", "end", values=(data_evento, nome, hora, turno, endereco, tipo, acao), tags=(tag,))
            self.notebook.select(0)
        except Exception as exc:
            self.write_log(f"[WARN] Falha ao adicionar vaga na tabela: {exc}\n")

    def _finish(self, code: int):
        elapsed = None
        if self._operation_started_at is not None:
            elapsed = format_elapsed(time.perf_counter() - self._operation_started_at)
            self._operation_started_at = None

        if code == 0:
            status = "Finalizado com sucesso"
        else:
            status = f"Finalizado com erro (codigo {code})"

        if elapsed:
            self.status_label.configure(style="StatusDone.TLabel")
            self.status.set(f"{status} - tempo: {elapsed}")
            self.write_log(f"\nTempo total da operacao: {elapsed}\n")
        else:
            self.status_label.configure(style="StatusDone.TLabel")
            self.status.set(status)

        if code == 0:
            self.write_log("Finalizado com sucesso.\n")
        else:
            self.write_log(f"Finalizado com erro. Codigo: {code}\n")
        if self._log_file:
            if elapsed:
                self._log_file.write(f"\n[Tempo total da operacao: {elapsed}]\n")
            self._log_file.write(f"\n[Codigo de saida: {code}]\n")
            self._log_file.close()
            self._log_file = None

    def cancel(self):
        with self._state_lock:
            if self._countdown_active:
                self._countdown_active = False
                self.status.set("Agendamento cancelado")
                self.write_log("Agendamento cancelado pelo usuario.\n")

        proc = self.process
        if proc and proc.poll() is None:
            proc.terminate()
            self.write_log("Processo cancelado.\n")
            self.status.set("Cancelado")

            def _ensure_stopped() -> None:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        return

            threading.Thread(target=_ensure_stopped, daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def write_log(self, text: str):
        self.log.insert("end", text)
        self.log.see("end")

    def clear_log(self):
        self.log.delete("1.0", "end")

    def clear_vagas(self):
        for item in self.tree.get_children():
            self.tree.delete(item)


def main():
    root = Tk()
    ProeisApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
