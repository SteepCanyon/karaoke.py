#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Караоке-плеер — Windows-сборка.

Функционал полностью взят из macOS-версии (1.0.20):
вкладки «Консоль (Видео)» и «Настройки», параллельный расширенный поиск,
скачивание с кэшем библиотеки, режим обхода 18+ (POT), смена тональности,
окно видео с проектором, автообновление.

Windows-специфичная часть (права администратора, Zapret, логирование
в файл, перехват исключений) — из windows-скрипта.
"""

import os
import sys
import platform
import socket
import ctypes
import json
import re
import shutil
import time
import tempfile
import subprocess
import threading
import logging
import traceback
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
import webbrowser

import requests

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
if IS_WIN:
    LOG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "KaraokePlayer")
else:
    LOG_DIR = os.path.expanduser("~/Library/Logs/KaraokePlayer")

try:
    os.makedirs(LOG_DIR, exist_ok=True)
    LOG_FILE = os.path.join(LOG_DIR, "app.log")
except Exception:
    LOG_FILE = "app.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)


class StreamToLogger:
    def __init__(self, logger, level):
        self.logger = logger
        self.level = level

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            self.logger.log(self.level, line.rstrip())

    def flush(self):
        pass


sys.stdout = StreamToLogger(logging.getLogger("STDOUT"), logging.INFO)
sys.stderr = StreamToLogger(logging.getLogger("STDERR"), logging.ERROR)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_exception
threading.excepthook = lambda args: logging.critical(
    "ОШИБКА В ПОТОКЕ", exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
)

# ============================================================
# DPI (Windows) — чтобы геометрия окон и мониторы совпадали в пикселях
# ============================================================
if IS_WIN:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# ============================================================
# ОКРУЖЕНИЕ VLC
# ============================================================
VLC_FOUND = False

if IS_MAC:
    for _vlc_candidate in (
        "/Applications/VLC.app/Contents/MacOS",
        "/Applications/MacPorts/VLC.app/Contents/MacOS",
    ):
        if os.path.exists(f"{_vlc_candidate}/lib/libvlc.dylib"):
            os.environ["DYLD_LIBRARY_PATH"] = f"{_vlc_candidate}/lib"
            os.environ.setdefault("VLC_PLUGIN_PATH", f"{_vlc_candidate}/plugins")
            VLC_FOUND = True
            break
    if not VLC_FOUND and os.path.exists("/opt/local/libexec/vlc3/lib/libvlc.dylib"):
        os.environ["DYLD_LIBRARY_PATH"] = "/opt/local/libexec/vlc3/lib"
        VLC_FOUND = True

if IS_WIN:
    for _vlc_win in (
        r"C:\Program Files\VideoLAN\VLC",
        r"C:\Program Files (x86)\VideoLAN\VLC",
    ):
        _vlc_dll = os.path.join(_vlc_win, "libvlc.dll")
        if os.path.exists(_vlc_dll):
            try:
                os.add_dll_directory(_vlc_win)
                os.environ["PATH"] = _vlc_win + os.pathsep + os.environ.get("PATH", "")
                logging.info(f"VLC найден: {_vlc_win}")
                VLC_FOUND = True
            except Exception as e:
                logging.error(f"Ошибка add_dll_directory: {e}")
            break
    if not VLC_FOUND:
        logging.warning("VLC не найден в стандартных путях Windows")

try:
    import vlc
    import yt_dlp
except Exception as e:
    logging.critical(f"Ошибка импорта библиотек: {e}")
    messagebox.showerror(
        "Ошибка",
        f"Не установлены библиотеки:\n{e}\n\n"
        "pip install python-vlc yt-dlp requests"
    )
    sys.exit(1)

# ============================================================
# КОНСТАНТЫ
# ============================================================
TPWS_PROXY = "socks5h://127.0.0.1:9876"
TPWS_PORT = 9876
APP_VERSION = "1.0.20"
GITHUB_REPO = "SteepCanyon/karaoke.py"
ZAPRET_DIR = r"C:\Zapret\Dev"
ZAPRET_EXE_TARGET = os.path.join(ZAPRET_DIR, "Zapret.exe")

# Скрываем консольные окна дочерних процессов на Windows.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def find_ffmpeg():
    names = ("ffmpeg.exe", "ffmpeg") if IS_WIN else ("ffmpeg",)
    for n in names:
        bundled = resource_path(os.path.join("ffmpeg", n))
        if os.path.exists(bundled):
            return bundled
    if IS_WIN:
        for p in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
            if os.path.exists(p):
                return p
    for p in ("/opt/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg") or "ffmpeg"


# ============================================================
# WINDOWS: ПРАВА АДМИНИСТРАТОРА И ZAPRET
# ============================================================
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_if_needed():
    """Перезапуск с UAC — Zapret (WinDivert) без администратора не работает."""
    if not IS_WIN or "--elevated" in sys.argv or is_admin():
        return
    logging.info("Запуск с правами администратора...")
    args = [f'"{arg}"' if ' ' in arg else arg for arg in sys.argv[1:]]
    if getattr(sys, "frozen", False):
        # Собранный .exe: параметры передаются без пути к скрипту
        params = " ".join(args + ["--elevated"])
    else:
        script = os.path.abspath(sys.argv[0])
        params = f'"{script}" ' + " ".join(args + ["--elevated"])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        if rc <= 32:
            messagebox.showerror(
                "Права администратора",
                "Запуск с правами администратора отменён.\n\n"
                "Без него Zapret не сможет обходить блокировки YouTube."
            )
    except Exception as e:
        logging.error(f"Не удалось повысить права: {e}")
    sys.exit(0)


elevate_if_needed()


def install_zapret():
    if not IS_WIN:
        return False
    src = resource_path("zapret")
    if not os.path.exists(src):
        logging.error("Папка zapret не найдена рядом с программой")
        return False
    if os.path.exists(ZAPRET_EXE_TARGET):
        return True
    try:
        os.makedirs(ZAPRET_DIR, exist_ok=True)
        for item in os.listdir(src):
            s, d = os.path.join(src, item), os.path.join(ZAPRET_DIR, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        return os.path.exists(ZAPRET_EXE_TARGET)
    except Exception as e:
        logging.error(f"Ошибка установки Zapret: {e}")
        return False


class ZapretProcess:
    def __init__(self, exe_path):
        self.exe_path = exe_path
        self.process = None
        self.ready = False

    def start(self):
        if not os.path.exists(self.exe_path):
            return False
        try:
            self.process = subprocess.Popen(
                [self.exe_path],
                cwd=os.path.dirname(self.exe_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
            time.sleep(1)
            self.ready = True
            logging.info("Zapret успешно запущен")
            return True
        except Exception as e:
            logging.error(f"Ошибка запуска Zapret: {e}")
            return False

    def shutdown(self):
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None
            self.ready = False


class TpwsProcess:
    def __init__(self, binary_path, port=TPWS_PORT, strategy=None):
        self.binary_path = binary_path
        self.port = port
        self.process = None
        self.ready = False
        self.strategy = strategy or ["--split-pos=midsld", "--oob"]

    def start(self):
        if self.ready:
            return True
        if not os.path.exists(self.binary_path):
            return False
        try:
            try:
                os.chmod(self.binary_path, 0o755)
            except Exception:
                pass
            cmd = [self.binary_path, "--socks", "--bind-addr=127.0.0.1", f"--port={self.port}"] + self.strategy
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(40):
                time.sleep(0.1)
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                        self.ready = True
                        return True
                except OSError:
                    continue
            return False
        except Exception:
            return False

    def shutdown(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                pass
            self.process = None
        self.ready = False


# ============================================================
# ЭКРАНЫ ДЛЯ ПРОЕКТОРА
# ============================================================
_libtk_cache = None


def get_nsview(tk_widget):
    """Только macOS: NSView виджета Tk для встраивания VLC."""
    global _libtk_cache
    if not IS_MAC:
        return None
    if _libtk_cache is None:
        try:
            libtk_name = f"libtk{tk.TkVersion}.dylib"
            for path in (f"/opt/local/lib/{libtk_name}", f"/opt/homebrew/lib/{libtk_name}", f"/usr/local/lib/{libtk_name}"):
                if os.path.exists(path):
                    _libtk_cache = ctypes.cdll.LoadLibrary(path)
                    break
        except Exception:
            pass
    if _libtk_cache is None:
        return None
    try:
        f = _libtk_cache.TkMacOSXGetRootControl
        f.restype = ctypes.c_void_p
        f.argtypes = (ctypes.c_void_p,)
        return f(ctypes.c_void_p(tk_widget.winfo_id()))
    except Exception:
        return None


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


def get_windows_screens():
    """Список мониторов Windows в физических пикселях."""
    try:
        user32 = ctypes.windll.user32
        result = []
        proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(_RECT), ctypes.c_ssize_t
        )

        def _cb(hmon, hdc, lprc, data):
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                result.append({
                    'x': int(r.left), 'y': int(r.top),
                    'w': int(r.right - r.left), 'h': int(r.bottom - r.top),
                })
            return 1

        user32.EnumDisplayMonitors(None, None, proc_type(_cb), 0)
        if not result:
            result.append({
                'x': 0, 'y': 0,
                'w': int(user32.GetSystemMetrics(0)),
                'h': int(user32.GetSystemMetrics(1)),
            })
        return result
    except Exception as e:
        logging.error(f"Не удалось получить список мониторов: {e}")
        return None


# ============================================================
# ПРИЛОЖЕНИЕ
# ============================================================
class KaraokeApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Караоке-плеер v{APP_VERSION}")
        self.root.geometry("1200x750")
        self.root.configure(bg="#0a0a0a")
        logging.info("Инициализация приложения...")

        self.proxy_url = None
        self.zapret_process = None
        self.tpws_process = None
        self.node_process = None
        self.projector_mode = False

        # ---------- конфиг ----------
        self.config_file = os.path.join(LOG_DIR, "config.json")
        self.config = self._load_config()

        # PATH для MacPorts
        if IS_MAC:
            os.environ["PATH"] = "/opt/local/bin:/opt/homebrew/bin:" + os.environ.get("PATH", "")

        # ---------- Node.js POT-сервер ----------
        self._start_pot_server()

        # ---------- обход блокировок ----------
        if IS_WIN:
            if not os.path.exists(ZAPRET_EXE_TARGET):
                if not install_zapret():
                    messagebox.showerror("Ошибка", "Не удалось установить Zapret.\nПроверьте папку 'zapret' рядом с программой.")
                    sys.exit(1)
            self.zapret_process = ZapretProcess(ZAPRET_EXE_TARGET)
            self.zapret_process.start()
        else:
            if not os.path.exists("/Applications/VLC.app") and not os.path.exists("/Applications/MacPorts/VLC.app"):
                messagebox.showerror("VLC не найден", "Установите VLC")
                sys.exit(1)
            bundled_tpws = resource_path(os.path.join("zapret", "tpws", "tpws"))
            self.tpws_process = TpwsProcess(bundled_tpws, port=TPWS_PORT, strategy=self.config.get("strategy"))
            if self.tpws_process.start():
                self.proxy_url = TPWS_PROXY
            else:
                self._check_tpws()

        # ---------- VLC ----------
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
            if IS_WIN else
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
        )
        try:
            self.instance = vlc.Instance(f"--http-user-agent={ua}", "--avcodec-hw=none")
            self.player = self.instance.media_player_new()
        except Exception as e:
            logging.critical(f"Ошибка инициализации VLC: {e}")
            messagebox.showerror(
                "VLC не найден",
                "Не удалось запустить VLC.\n\n"
                "Установите VLC с официального сайта и перезапустите программу."
            )
            sys.exit(1)
        if IS_WIN and not VLC_FOUND:
            logging.warning("libvlc.dll не найден — VLC может не инициализироваться")

        # ---------- состояние ----------
        self.current_url = None
        self.current_original_path = None
        self.current_pitch = 0
        self.last_results = []
        self.all_local_files = []
        self.mode = tk.StringVar(value="youtube")

        self.download_folder = os.path.expanduser(
            self.config.get("download_folder") or "~/Music/KaraokePlayer/KaraokeDownloads"
        )
        os.makedirs(self.download_folder, exist_ok=True)

        self.ffmpeg_path = find_ffmpeg()
        self.temp_dir = tempfile.mkdtemp(prefix="karaoke_")
        self.cache_file = os.path.join(self.download_folder, ".cache.json")
        self.video_cache = self._load_cache()
        self.pitch_cache = {}

        self.progress_var = tk.DoubleVar(value=0.0)
        self.stop_progress = threading.Event()
        threading.Thread(target=self.update_progress, daemon=True).start()

        self.search_thread = None
        self.stop_search = threading.Event()

        # ---------- интерфейс ----------
        try:
            self.create_widgets()
            self._create_video_window()
            self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
            threading.Thread(target=self._check_for_updates, daemon=True).start()
            logging.info("Интерфейс успешно создан")
        except Exception as e:
            logging.critical(f"Ошибка создания интерфейса: {e}", exc_info=True)
            messagebox.showerror("Критическая ошибка", f"Не удалось создать интерфейс:\n{e}")

    # ==========================================
    # КОНФИГ / КЭШ
    # ==========================================
    def _load_config(self):
        defaults = {
            "use_pot": False,
            "extended_search": True,
            "strategy": ["--split-pos=midsld", "--oob"],
            "format": "bv*+ba/b",
            "download_folder": "~/Music/KaraokePlayer/KaraokeDownloads",
        }
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in defaults.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            logging.error(f"Ошибка чтения конфига: {e}")
        return defaults

    def _save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка записи конфига: {e}")

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_cache(self):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.video_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _check_tpws(self):
        try:
            with socket.create_connection(("127.0.0.1", TPWS_PORT), timeout=1.0):
                self.proxy_url = TPWS_PROXY
                return True
        except OSError:
            self.proxy_url = None
            return False

    def _start_pot_server(self):
        possible_dirs = [
            resource_path("node_server"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundle_env", "node_server"),
            os.path.join(LOG_DIR, "node_server"),
            os.path.expanduser(r"~\Desktop\bgutil-ytdlp-pot-provider\server"),
            os.path.expanduser("~/Desktop/bgutil-ytdlp-pot-provider/server"),
        ]
        node_server_dir = next(
            (d for d in possible_dirs if os.path.exists(os.path.join(d, "build", "main.js"))), None
        )
        if not node_server_dir:
            logging.info("POT-сервер (node) не найден — режим 18+ будет недоступен")
            return

        node_bin = shutil.which("node") or "node"
        for candidate in ("node.exe", "node", os.path.join("node", "bin", "node")):
            bundled = os.path.join(node_server_dir, candidate)
            if os.path.exists(bundled):
                node_bin = bundled
                try:
                    os.chmod(node_bin, 0o755)
                except Exception:
                    pass
                break

        try:
            self.node_process = subprocess.Popen(
                [node_bin, "build/main.js"],
                cwd=node_server_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
            logging.info(f"[POT] Node.js сервер запущен из: {node_server_dir}")
        except Exception as e:
            logging.error(f"[POT] Ошибка запуска: {e}")
            self.node_process = None

    # ==========================================
    # ИНТЕРФЕЙС
    # ==========================================
    def create_widgets(self):
        bg, fg, accent, entry_bg, list_bg = "#0a0a0a", "#e0e0e0", "#00cc66", "#1a1a1a", "#111111"

        main = tk.Frame(self.root, bg=bg)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.video_placeholder = tk.Frame(self.notebook, bg="black")
        self.notebook.add(self.video_placeholder, text="💻 Консоль (Видео)")

        console_frame = tk.Frame(self.video_placeholder, bg="#111")
        console_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        console_font = ("Consolas", 11) if IS_WIN else ("Menlo", 11)
        self.console_text = tk.Text(
            console_frame, bg="#0a0a0a", fg="#00cc66", font=console_font,
            state=tk.DISABLED, wrap=tk.WORD, highlightthickness=0, bd=0
        )
        self.console_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(console_frame, command=self.console_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_text.config(yscrollcommand=scrollbar.set)

        tk.Label(
            self.video_placeholder, text="Видео открывается в отдельном окне VLC",
            fg="#555", bg="black", font=("Segoe UI", 10)
        ).pack(side=tk.BOTTOM, pady=5)

        self.settings_frame = tk.Frame(self.notebook, bg=bg)
        self.notebook.add(self.settings_frame, text="⚙️ Настройки")
        self._build_settings_ui()

        right = tk.Frame(main, bg=bg, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        mode_frame = tk.Frame(right, bg=bg)
        mode_frame.pack(fill=tk.X, pady=(0, 5))
        tk.Radiobutton(
            mode_frame, text="YouTube", variable=self.mode, value="youtube",
            command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg,
            activebackground=bg, activeforeground=fg
        ).pack(side=tk.LEFT)
        tk.Radiobutton(
            mode_frame, text="Локальные", variable=self.mode, value="local",
            command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg,
            activebackground=bg, activeforeground=fg
        ).pack(side=tk.LEFT, padx=10)

        # Поиск
        self.search_frame = tk.Frame(right, bg=bg)
        tk.Label(self.search_frame, text="🔍 Поиск", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        self.search_entry = tk.Entry(self.search_frame, bg=entry_bg, fg=fg, insertbackground=fg, relief=tk.FLAT)
        self.search_entry.pack(fill=tk.X, ipady=4, pady=5)
        self.search_entry.bind('<Return>', lambda e: self.do_search())
        btn_frame = tk.Frame(self.search_frame, bg=bg)
        btn_frame.pack(fill=tk.X)
        self.search_btn = tk.Button(btn_frame, text="Найти", bg=accent, fg="#000", relief=tk.FLAT, command=self.do_search)
        self.search_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.stop_btn = tk.Button(
            btn_frame, text="⏹ Стоп", bg="#555", fg=fg, relief=tk.FLAT,
            command=self.stop_search_command, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT)

        # Локальный поиск
        self.local_search_frame = tk.Frame(right, bg=bg)
        tk.Label(
            self.local_search_frame, text="🔍 Поиск в локальных",
            fg=fg, bg=bg, font=("Segoe UI", 10, "bold")
        ).pack(anchor=tk.W)
        self.local_search_entry = tk.Entry(
            self.local_search_frame, bg=entry_bg, fg=fg, insertbackground=fg, relief=tk.FLAT
        )
        self.local_search_entry.pack(fill=tk.X, ipady=4, pady=5)
        self.local_search_entry.bind("<KeyRelease>", self._filter_local_files)

        # Статус
        status_frame = tk.Frame(right, bg=bg)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_lbl = tk.Label(status_frame, text="Готов", fg="#888", bg=bg)
        self.status_lbl.pack(side=tk.LEFT)
        self.zapret_status = tk.Label(status_frame, text="", fg=accent, bg=bg)
        self.zapret_status.pack(side=tk.RIGHT)
        self.update_zapret_status()

        # Список
        tk.Label(right, text="🎵 Список", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(10, 0))
        self.results_list = tk.Listbox(
            right, bg=list_bg, fg=fg, relief=tk.FLAT, selectbackground=accent,
            selectforeground="#000", height=8, width=30, highlightthickness=0
        )
        self.results_list.pack(fill=tk.BOTH, expand=True, pady=5)
        self.results_list.bind('<Double-1>', lambda e: self.play_selected())
        self.results_list.bind('<<ListboxSelect>>', self._on_list_select)

        # Кнопки управления
        ctrl_frame = tk.Frame(right, bg=bg)
        ctrl_frame.pack(fill=tk.X, pady=5)
        self.download_btn = tk.Button(
            ctrl_frame, text="📥", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"),
            relief=tk.FLAT, command=self.download_selected, width=4
        )
        self.download_btn.grid(row=0, column=0, padx=2)
        self.play_pause_btn = tk.Button(
            ctrl_frame, text="▶", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"),
            relief=tk.FLAT, command=self.toggle_play_pause, width=4
        )
        self.play_pause_btn.grid(row=0, column=1, padx=2)
        self.stop_play_btn = tk.Button(
            ctrl_frame, text="⏹", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"),
            relief=tk.FLAT, command=self.stop, width=4
        )
        self.stop_play_btn.grid(row=0, column=2, padx=2)
        self.delete_btn = tk.Button(
            ctrl_frame, text="🗑", bg="#cc3333", fg="#fff", font=("Segoe UI", 12, "bold"),
            relief=tk.FLAT, command=self.delete_selected_local, width=4
        )
        self.delete_btn.grid(row=0, column=3, padx=2)
        self.delete_btn.grid_remove()

        # Прогресс
        progress_frame = tk.Frame(right, bg=bg)
        progress_frame.pack(fill=tk.X, pady=5)
        tk.Label(progress_frame, text="Прогресс", fg=fg, bg=bg).pack(anchor=tk.W)
        self.progress_scale = tk.Scale(
            progress_frame, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.progress_var,
            command=self.on_progress_move, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0
        )
        self.progress_scale.pack(fill=tk.X)
        self.progress_scale.bind("<ButtonRelease-1>", self.on_progress_release)

        # Громкость
        vol_frame = tk.Frame(right, bg=bg)
        vol_frame.pack(fill=tk.X, pady=5)
        tk.Label(vol_frame, text="🔊 Громкость", fg=fg, bg=bg).pack(anchor=tk.W)
        self.vol_scale = tk.Scale(
            vol_frame, from_=0, to=200, orient=tk.HORIZONTAL, command=self.on_volume_change,
            bg=bg, fg=fg, troughcolor="#333", highlightthickness=0
        )
        self.vol_scale.set(100)
        self.vol_scale.pack(fill=tk.X)

        # Тональность
        pitch_frame = tk.Frame(right, bg=bg)
        pitch_frame.pack(fill=tk.X, pady=5)
        tk.Label(pitch_frame, text="🎚 Тональность (полутона)", fg=fg, bg=bg).pack(anchor=tk.W)
        self.pitch_scale = tk.Scale(
            pitch_frame, from_=-6, to=6, orient=tk.HORIZONTAL, bg=bg, fg=fg,
            troughcolor="#333", highlightthickness=0, resolution=1
        )
        self.pitch_scale.set(0)
        self.pitch_scale.pack(fill=tk.X)
        self.pitch_val = tk.Label(pitch_frame, text="0", fg=accent, bg=bg)
        self.pitch_val.pack(anchor=tk.E)
        self.pitch_scale.bind("<ButtonRelease-1>", self.on_pitch_release)

        # Нижние кнопки
        self.restart_zapret_btn = tk.Button(
            right, text="🔄 Проверить обход", bg=accent, fg="#000",
            relief=tk.FLAT, command=self.restart_zapret
        )
        self.restart_zapret_btn.pack(fill=tk.X, pady=(5, 2))

        self.projector_btn = tk.Button(
            right, text="📺 На проектор", bg="#555", fg=fg, font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, command=self._toggle_projector
        )
        self.projector_btn.pack(fill=tk.X, pady=(0, 5))

        self.on_mode_change()

    def _build_settings_ui(self):
        bg, fg = "#0a0a0a", "#e0e0e0"
        entry_bg = "#1a1a1a"
        frame = tk.Frame(self.settings_frame, bg=bg, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        self.strategy_var = None
        self.strategy_combo = None
        self.strategies_map = {}

        # Стратегия обхода tpws — только macOS (на Windows работает Zapret)
        if IS_MAC:
            tk.Label(
                frame, text="Стратегия обхода (tpws):", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")
            ).pack(anchor=tk.W, pady=(0, 5))
            self.strategy_var = tk.StringVar()
            strategies = {
                "midsld + oob": ["--split-pos=midsld", "--oob"],
                "disorder + split 1": ["--disorder", "--split-pos=1"],
                "disorder + host + split 1": ["--disorder", "--split-pos=host", "--split-pos=1"],
                "split 1 + oob": ["--split-pos=1", "--oob"],
                "fake + split 1 (Может сработать)": ["--fake", "--split-pos=1"],
                "hostspell + disorder": ["--hostspell=host", "--disorder"],
                "tlsrec + split 2": ["--tlsrec=sni", "--split-pos=2"],
            }
            self.strategy_combo = ttk.Combobox(
                frame, textvariable=self.strategy_var, values=list(strategies.keys()), state="readonly"
            )
            self.strategy_combo.pack(fill=tk.X, pady=(0, 15))
            curr_strategy = self.config.get("strategy", ["--split-pos=midsld", "--oob"])
            for k, v in strategies.items():
                if v == curr_strategy:
                    self.strategy_var.set(k)
                    break
            self.strategies_map = strategies
        else:
            tk.Label(
                frame,
                text=f"Обход блокировок: Zapret ({ZAPRET_EXE_TARGET})",
                fg="#888", bg=bg, font=("Segoe UI", 9)
            ).pack(anchor=tk.W, pady=(0, 15))

        # Формат
        tk.Label(frame, text="Качество видео:", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
        self.format_var = tk.StringVar()
        formats = {
            "Лучшее (bv*+ba/b)": "bv*+ba/b",
            "1080p (bv*[height<=1080]+ba/b)": "bv*[height<=1080]+ba/b",
            "720p (bv*[height<=720]+ba/b)": "bv*[height<=720]+ba/b",
            "480p (bv*[height<=480]+ba/b)": "bv*[height<=480]+ba/b",
            "360p (mp4)": "18",
        }
        self.format_combo = ttk.Combobox(
            frame, textvariable=self.format_var, values=list(formats.keys()), state="readonly"
        )
        self.format_combo.pack(fill=tk.X, pady=(0, 15))
        curr_format = self.config.get("format", "bv*+ba/b")
        for k, v in formats.items():
            if v == curr_format:
                self.format_var.set(k)
                break
        self.formats_map = formats

        # 18+
        self.use_pot_var = tk.BooleanVar(value=self.config.get("use_pot", False))
        tk.Checkbutton(
            frame, text="🔞 Режим обхода 18+ (web_creator)", variable=self.use_pot_var,
            bg=bg, fg=fg, selectcolor=entry_bg, activebackground=bg, activeforeground=fg
        ).pack(anchor=tk.W, pady=(0, 15))
        if not self._pot_available():
            tk.Label(
                frame, text="POT-сервер (node) не найден — режим 18+ не сработает",
                fg="#ffaa00", bg=bg, font=("Segoe UI", 8)
            ).pack(anchor=tk.W, pady=(0, 10))

        # Расширенный поиск
        self.extended_search_var = tk.BooleanVar(value=self.config.get("extended_search", True))
        tk.Checkbutton(
            frame, text="🔎 Расширенный поиск (караоке + karaoke + минус + instrumental)",
            variable=self.extended_search_var,
            bg=bg, fg=fg, selectcolor=entry_bg, activebackground=bg, activeforeground=fg
        ).pack(anchor=tk.W, pady=(0, 15))

        # Папка
        tk.Label(frame, text="Папка для скачивания:", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
        folder_frame = tk.Frame(frame, bg=bg)
        folder_frame.pack(fill=tk.X, pady=(0, 15))
        self.folder_var = tk.StringVar(value=self.download_folder)
        self.folder_entry = tk.Entry(
            folder_frame, textvariable=self.folder_var, bg=entry_bg, fg=fg,
            insertbackground=fg, relief=tk.FLAT
        )
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        tk.Button(folder_frame, text="...", bg="#555", fg=fg, command=self._browse_folder, width=3).pack(side=tk.RIGHT, padx=(5, 0))

        tk.Button(
            frame, text="💾 Сохранить настройки", bg="#00cc66", fg="#000",
            font=("Segoe UI", 10, "bold"), relief=tk.FLAT, command=self._save_settings
        ).pack(fill=tk.X, pady=(10, 20))

        tk.Label(frame, text="Автор: SteepCanyon", fg="#888", bg=bg).pack(anchor=tk.W)
        tk.Label(frame, text=f"Версия: {APP_VERSION}", fg="#888", bg=bg).pack(anchor=tk.W)
        tk.Button(
            frame, text="🌐 GitHub", bg="#333", fg=fg, relief=tk.FLAT,
            command=lambda: webbrowser.open(f"https://github.com/{GITHUB_REPO}")
        ).pack(anchor=tk.W, pady=(5, 0))

    def _pot_available(self):
        return bool(getattr(self, "node_process", None)) and self.node_process.poll() is None

    # ==========================================
    # НАСТРОЙКИ
    # ==========================================
    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get())
        if folder:
            self.folder_var.set(folder)

    def _save_settings(self):
        old_strategy = self.config.get("strategy")
        old_folder = self.download_folder

        self.config["use_pot"] = self.use_pot_var.get()
        self.config["extended_search"] = self.extended_search_var.get()
        if IS_MAC and self.strategy_var is not None:
            self.config["strategy"] = self.strategies_map.get(
                self.strategy_var.get(), ["--split-pos=midsld", "--oob"]
            )
        self.config["format"] = self.formats_map.get(self.format_var.get(), "bv*+ba/b")
        self.config["download_folder"] = self.folder_var.get()

        self._save_config()

        if os.path.expanduser(self.config["download_folder"]) != os.path.expanduser(old_folder):
            self.download_folder = os.path.expanduser(self.config["download_folder"])
            os.makedirs(self.download_folder, exist_ok=True)
            self.cache_file = os.path.join(self.download_folder, ".cache.json")
            self.video_cache = self._load_cache()
            self.load_local_files()

        if IS_MAC and self.tpws_process and self.config["strategy"] != old_strategy:
            bundled_tpws = resource_path(os.path.join("zapret", "tpws", "tpws"))
            self.tpws_process.shutdown()
            self.tpws_process = TpwsProcess(bundled_tpws, port=TPWS_PORT, strategy=self.config["strategy"])
            self.proxy_url = TPWS_PROXY if self.tpws_process.start() else None
            self.update_zapret_status()

        messagebox.showinfo("Настройки", "Настройки сохранены!")

    # ==========================================
    # ОКНО ВИДЕО
    # ==========================================
    def _create_video_window(self):
        self.video_window = tk.Toplevel(self.root)
        self.video_window.title("Karaoke Video")
        self.video_window.configure(bg="black")
        self.video_window.transient(self.root)
        try:
            self.video_window.overrideredirect(True)
        except Exception:
            pass

        self.video_canvas = tk.Canvas(self.video_window, bg="black", highlightthickness=0, bd=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)

        self.video_window.protocol("WM_DELETE_WINDOW", self.video_window.withdraw)

        self.video_placeholder.bind("<Configure>", lambda e: self.root.after(50, self._position_video_window))
        self.root.bind("<Configure>", lambda e: self._position_video_window() if e.widget is self.root else None)
        self.root.bind("<Unmap>", self._on_root_unmap)
        self.root.bind("<Map>", self._on_root_map)
        self.root.after(200, self._position_video_window)
        self.root.after(500, self._attach_vlc)

    def _position_video_window(self):
        if getattr(self, 'projector_mode', False):
            return
        try:
            if not self.video_window.winfo_exists():
                return
            if self.notebook.select() != str(self.video_placeholder):
                self.video_window.withdraw()
                return
            self.root.update_idletasks()
            x, y = self.video_placeholder.winfo_rootx(), self.video_placeholder.winfo_rooty()
            w, h = self.video_placeholder.winfo_width(), self.video_placeholder.winfo_height()
            if w > 10 and h > 10:
                self.video_window.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _attach_vlc(self):
        try:
            if IS_MAC:
                nsview = get_nsview(self.video_canvas)
                if nsview:
                    self.player.set_nsobject(nsview)
            elif IS_WIN:
                self.player.set_hwnd(self.video_canvas.winfo_id())
        except Exception as e:
            logging.error(f"Ошибка привязки VLC: {e}")

    def _on_tab_changed(self, event=None):
        try:
            if self.notebook.select() == str(self.video_placeholder):
                if not self.projector_mode and self.video_window.winfo_exists():
                    self.video_window.deiconify()
                    self.root.after(50, self._position_video_window)
            elif not self.projector_mode:
                self.video_window.withdraw()
        except Exception:
            pass

    def _on_root_unmap(self, event=None):
        if event and event.widget is not self.root:
            return
        if hasattr(self, 'video_window') and self.video_window.winfo_exists():
            self.video_window.withdraw()

    def _on_root_map(self, event=None):
        if event and event.widget is not self.root:
            return
        if not hasattr(self, 'video_window') or not self.video_window.winfo_exists():
            return
        if self.projector_mode or self.notebook.select() == str(self.video_placeholder):
            self.video_window.deiconify()
            self.root.after(50, self._position_video_window)

    # ==========================================
    # ПРОЕКТОР
    # ==========================================
    def _get_screens(self):
        if IS_WIN:
            return get_windows_screens()
        if IS_MAC:
            try:
                from AppKit import NSScreen
                return [
                    {
                        'x': int(s.frame().origin.x), 'y': int(s.frame().origin.y),
                        'w': int(s.frame().size.width), 'h': int(s.frame().size.height),
                    }
                    for s in NSScreen.screens()
                ]
            except ImportError:
                return None
            except Exception:
                return None
        return None

    def _toggle_projector(self):
        if getattr(self, 'projector_mode', False):
            self._return_from_projector()
        else:
            self._send_to_projector()

    def _send_to_projector(self):
        screens = self._get_screens()
        if screens is None:
            messagebox.showerror("Ошибка", "Не удалось получить список мониторов")
            return
        if len(screens) < 2:
            messagebox.showinfo(
                "Второй экран не найден",
                f"Подключите проектор или второй монитор.\nСейчас подключено: {len(screens)}"
            )
            return

        main_idx = 0
        for i, s in enumerate(screens):
            if s['x'] == 0 and s['y'] == 0:
                main_idx = i
                break
        target = next((s for i, s in enumerate(screens) if i != main_idx), None)
        if target is None:
            return

        try:
            self.video_window.attributes("-topmost", False)
            self.video_window.deiconify()
            self.video_window.geometry(f"{target['w']}x{target['h']}+{target['x']}+{target['y']}")
            self.projector_mode = True
            self.projector_btn.config(text="📺 Вернуть на главный", bg="#aa4444")
            self.status_lbl.config(text="Видео на проекторе", fg="#00cc66")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _return_from_projector(self):
        self.projector_mode = False
        try:
            self.projector_btn.config(text="📺 На проектор", bg="#555")
            self.video_window.attributes("-topmost", False)
            self._position_video_window()
            self.status_lbl.config(text="Видео на главном экране", fg="#888")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    # ==========================================
    # ЛОГИКА РАБОТЫ
    # ==========================================
    def _on_list_select(self, event=None):
        sel = self.results_list.curselection()
        if sel and sel[0] < len(self.last_results):
            self.status_lbl.config(text=self.last_results[sel[0]][0][:110], fg="#00cc66")

    def _console(self, message):
        """Только вывод в консоль приложения (без записи в лог)."""
        def _update():
            try:
                self.console_text.config(state=tk.NORMAL)
                self.console_text.insert(tk.END, str(message) + "\n")
                self.console_text.see(tk.END)
                self.console_text.config(state=tk.DISABLED)
            except Exception:
                pass

        try:
            self.root.after(0, _update)
        except Exception:
            pass

    def log_to_ui(self, message):
        logging.info(str(message))
        self._console(message)

    def _filter_local_files(self, event=None):
        self._render_local_files()

    def _render_local_files(self):
        self.results_list.delete(0, tk.END)
        self.last_results = []
        query = self.local_search_entry.get().strip().lower() if hasattr(self, 'local_search_entry') else ""
        count = 0
        for f, path in getattr(self, 'all_local_files', []):
            if query and query not in f.lower():
                continue
            self.results_list.insert(tk.END, f"  {f[:36]}{'...' if len(f) > 36 else ''}")
            self.last_results.append((f, path))
            count += 1
        self.status_lbl.config(text=f"Локальных: {count}", fg="#888")

    def load_local_files(self):
        self.all_local_files = []
        if not os.path.exists(self.download_folder):
            self._render_local_files()
            return
        exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv', '.mpg', '.mpeg', '.ts')
        files = [f for f in os.listdir(self.download_folder) if f.lower().endswith(exts) and not f.startswith('.')]
        for f in sorted(files, key=lambda x: os.path.getmtime(os.path.join(self.download_folder, x)), reverse=True):
            self.all_local_files.append((f, os.path.join(self.download_folder, f)))
        self._render_local_files()

    def on_mode_change(self):
        if self.mode.get() == "youtube":
            self.local_search_frame.pack_forget()
            self.search_frame.pack(fill=tk.X, pady=(0, 5))
            self.download_btn.grid()
            self.delete_btn.grid_remove()
        else:
            self.search_frame.pack_forget()
            self.local_search_frame.pack(fill=tk.X, pady=(0, 5), before=self.results_list)
            self.download_btn.grid_remove()
            self.delete_btn.grid()
            self.load_local_files()

    def delete_selected_local(self):
        if self.mode.get() != "local":
            return
        sel = self.results_list.curselection()
        if not sel:
            return
        fname, path = self.last_results[sel[0]]
        if messagebox.askyesno("Удалить", f"Удалить файл?\n\n{fname}"):
            try:
                os.remove(path)
                for vid, p in list(self.video_cache.items()):
                    if p == path:
                        del self.video_cache[vid]
                self._save_cache()
                self.load_local_files()
            except Exception as e:
                messagebox.showerror("Ошибка", str(e))

    def _get_next_number(self):
        max_num = 0
        if os.path.exists(self.download_folder):
            for f in os.listdir(self.download_folder):
                m = re.match(r'^(\d+)\s*-\s*', f)
                if m:
                    max_num = max(max_num, int(m.group(1)))
        return max_num + 1

    def download_selected(self):
        if self.mode.get() != "youtube":
            return
        sel = self.results_list.curselection()
        if not sel:
            return
        vid = self.last_results[sel[0]][1]
        if not re.match(r'^[A-Za-z0-9_-]{11}$', vid):
            messagebox.showerror("Ошибка", "Некорректный трек. Сделайте новый поиск.")
            return
        if self.video_cache.get(vid) and os.path.exists(self.video_cache[vid]):
            self.status_lbl.config(text="Уже в библиотеке", fg="#00cc66")
            return
        self.status_lbl.config(text="Скачивание...", fg="#00cc66")
        self.download_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._download, args=(vid, False), daemon=True).start()

    def play_selected(self):
        sel = self.results_list.curselection()
        if not sel:
            return
        item = self.last_results[sel[0]][1]
        if self.mode.get() == "youtube":
            if not re.match(r'^[A-Za-z0-9_-]{11}$', item):
                messagebox.showerror("Ошибка", "Некорректный трек. Сделайте новый поиск.")
                return
            cached = self.video_cache.get(item)
            if cached and os.path.exists(cached):
                self.status_lbl.config(text="Из библиотеки", fg="#00cc66")
                self._play_local(cached)
                return
            self.status_lbl.config(text="Загрузка...", fg="#00cc66")
            threading.Thread(target=self._download, args=(item, True), daemon=True).start()
        else:
            self._play_local(item)

    def _resolve_downloaded_path(self, info):
        if info and info.get('requested_downloads'):
            p = info['requested_downloads'][0].get('filepath')
            if p and os.path.exists(p):
                return p
        candidates = []
        if os.path.isdir(self.temp_dir):
            for f in os.listdir(self.temp_dir):
                fp = os.path.join(self.temp_dir, f)
                if os.path.isfile(fp) and f.lower().endswith(('.mp4', '.mkv', '.webm', '.m4a')):
                    candidates.append(fp)
        return max(candidates, key=os.path.getmtime) if candidates else None

    def _download(self, video_id, auto_play):
        url = f"https://youtu.be/{video_id}"

        for f in os.listdir(self.temp_dir):
            try:
                os.remove(os.path.join(self.temp_dir, f))
            except Exception:
                pass

        use_pot = self.config.get("use_pot", False)
        base_opts = {
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'format': self.config.get("format", "bv*+ba/b"),
            'outtmpl': os.path.join(self.temp_dir, '%(title)s.%(ext)s'),
            'nopart': True,
            'ffmpeg_location': self.ffmpeg_path,
            'windowsfilenames': True,
            'progress_hooks': [self._progress_hook],
        }

        if use_pot:
            self.log_to_ui("🔞 [Скачивание] Режим 18+ ВКЛЮЧЕН (web_creator)")
            base_opts.update({
                'remote_components': ['ejs:github'],
                'extractor_args': {'youtube': {'player_client': ['web_creator']}},
                'source_address': '0.0.0.0',
                'http_chunk_size': 10485760,
                'socket_timeout': 60,
                'retries': 20,
                'fragment_retries': 20,
                'retry_sleep': 2,
            })
        else:
            self.log_to_ui("🟢 [Скачивание] Обычный режим (android/web)")
            base_opts.update({
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
            })

        if self.proxy_url:
            base_opts['proxy'] = self.proxy_url.replace('socks5h://', 'socks5://')

        # Первая попытка — с cookies браузера, вторая — без них
        # (Chrome может держать базу cookies заблокированной).
        attempts = [
            dict(base_opts, **{'cookiesfrombrowser': ('chrome',)}),
            dict(base_opts),
        ]

        info = None
        try:
            for idx, opts in enumerate(attempts):
                try:
                    self.log_to_ui(
                        f"📥 [Скачивание] Запуск: {video_id}" + (" (без cookies)" if idx else "")
                    )
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    break
                except Exception as e:
                    if idx == 0:
                        self.log_to_ui(f"⚠️ С cookies не удалось: {e}")
                        continue
                    raise

            self.log_to_ui("📦 [Скачивание] Файл загружен, ищем на диске...")
            temp_path = self._resolve_downloaded_path(info)
            if not temp_path:
                raise Exception("Скачанный файл не найден")

            new_name = f"{self._get_next_number():02d} - {os.path.basename(temp_path)}"
            dest = os.path.join(self.download_folder, new_name)
            shutil.move(temp_path, dest)
            self.video_cache[video_id] = dest
            self._save_cache()

            if auto_play:
                self.root.after(0, lambda: self._play_local(dest))
            else:
                self.root.after(0, lambda: self.status_lbl.config(text=f"Сохранено: {new_name}", fg="#00cc66"))
                self.log_to_ui(f"✅ Сохранено: {new_name}")

        except Exception as e:
            logging.error(f"Ошибка скачивания: {e}", exc_info=True)
            self.log_to_ui(f"❌ ОШИБКА СКАЧИВАНИЯ: {e}")
            self.root.after(0, lambda err=str(e): self.status_lbl.config(text=f"Ошибка: {err}", fg="red"))
        finally:
            self.root.after(0, lambda: self.download_btn.config(state=tk.NORMAL))

    def _progress_hook(self, d):
        if d.get('status') == 'downloading':
            self._console(
                f"📥 [Скачивание] {d.get('_percent_str', '0%').strip()} | {d.get('_speed_str', 'N/A').strip()}"
            )
        elif d.get('status') == 'finished':
            self.log_to_ui("🔗 [Скачивание] Завершено, склеиваем...")

    def _play_local(self, path):
        if not os.path.exists(path):
            return
        try:
            self.current_original_path = path
            self.current_pitch = 0
            self.pitch_scale.set(0)
            self.pitch_val.config(text="0")

            self.player.stop()
            media = self.instance.media_new_path(path)
            self.player.set_media(media)
            self.player.play()
            self.play_pause_btn.config(text="⏸")

            if hasattr(self, 'video_window') and self.video_window.winfo_exists():
                if self.projector_mode or self.notebook.select() == str(self.video_placeholder):
                    self.video_window.deiconify()
                self.root.after(50, self._position_video_window)

            self.status_lbl.config(text="▶ Воспроизведение", fg="#00cc66")
            self.on_volume_change(self.vol_scale.get())
        except Exception as e:
            logging.error(f"Ошибка VLC Play: {e}")

    # ---------- тональность ----------
    def on_pitch_release(self, event):
        semitones = int(self.pitch_scale.get())
        self.pitch_val.config(text=f"{semitones:+d}" if semitones else "0")
        if not self.current_original_path:
            self.status_lbl.config(text="Нет загруженного трека", fg="orange")
            return
        if semitones == self.current_pitch:
            return
        self.current_pitch = semitones
        threading.Thread(target=self._apply_pitch, args=(semitones,), daemon=True).start()

    def _apply_pitch(self, semitones):
        src = self.current_original_path
        if not src or not os.path.exists(src):
            self.root.after(0, lambda: self.status_lbl.config(text="Нет загруженного трека", fg="orange"))
            return

        if semitones == 0:
            self.root.after(0, lambda: self._play_pitched(src, 0))
            return

        key = (src, semitones)
        if key in self.pitch_cache and os.path.exists(self.pitch_cache[key]):
            self.root.after(0, lambda p=self.pitch_cache[key]: self._play_pitched(p, semitones))
            return

        self.root.after(0, lambda: self.status_lbl.config(text=f"Обработка тональности {semitones:+d}...", fg="orange"))

        ratio = 2 ** (semitones / 12.0)
        sr = 44100
        out_path = os.path.join(self.temp_dir, f"pitched_{semitones:+d}.mp4")
        cmd = [
            self.ffmpeg_path, "-y", "-i", src,
            "-filter_complex",
            f"[0:a]asetrate={int(sr * ratio)},aresample={sr},atempo={1.0 / ratio:.6f}[a]",
            "-map", "0:v?", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            out_path,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=180, creationflags=NO_WINDOW
            )
            if result.returncode != 0:
                err = result.stderr.decode(errors="ignore")[:140]
                logging.error(f"ffmpeg pitch error: {err}")
                self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка pitch: {err}", fg="red"))
                return
            self.pitch_cache[key] = out_path
            self.root.after(0, lambda p=out_path: self._play_pitched(p, semitones))
        except Exception as e:
            logging.error(f"Ошибка pitch: {e}")
            self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка pitch: {e}", fg="red"))

    def _play_pitched(self, path, semitones):
        if not os.path.exists(path):
            return
        try:
            pos = self.player.get_position()
            was_playing = self.player.is_playing()
            self.player.stop()
            self.player.set_media(self.instance.media_new_path(path))
            self.player.play()
            if 0 < pos < 1:
                self.root.after(250, lambda: self.player.set_position(pos))
            if not was_playing:
                self.root.after(350, self.player.pause)
            self.status_lbl.config(text=f"▶ Тональность: {semitones:+d}", fg="#00cc66")
        except Exception as e:
            self.status_lbl.config(text=f"Ошибка: {e}", fg="red")

    # ---------- поиск ----------
    def do_search(self):
        q = self.search_entry.get().strip()
        if not q:
            return
        if self.search_thread and self.search_thread.is_alive():
            self.stop_search.set()
            self.search_thread.join(timeout=0.5)
        self.stop_search.clear()
        self.search_btn.config(state=tk.DISABLED, bg="#555")
        self.stop_btn.config(state=tk.NORMAL, bg="#aa0000")
        self.status_lbl.config(text="Поиск...", fg="#00cc66")
        self.results_list.delete(0, tk.END)
        self.last_results = []
        self.search_thread = threading.Thread(target=self._search_task, args=(q,), daemon=True)
        self.search_thread.start()

    def _search_task(self, query):
        try:
            self.log_to_ui(f"🔍 [Поиск] Начинаю поиск: {query}")

            if self.config.get("extended_search", True):
                variants = [
                    f"{query} караоке",
                    f"{query} karaoke",
                    f"{query} минус",
                    f"{query} instrumental",
                ]
            else:
                variants = [f"{query} караоке"]

            base_opts = {
                'quiet': True,
                'extract_flat': True,
                'no_warnings': True,
                'socket_timeout': 15,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            if self.proxy_url:
                base_opts['proxy'] = self.proxy_url.replace('socks5h://', 'socks5://')

            from concurrent.futures import ThreadPoolExecutor, as_completed

            seen_ids = set()
            all_results = []

            def do_one(q):
                try:
                    with yt_dlp.YoutubeDL(dict(base_opts)) as ydl:
                        info = ydl.extract_info(f"ytsearch10:{q}", download=False)
                        return [(e.get('title', ''), e['id']) for e in info.get('entries', []) if e.get('id')]
                except Exception as ex:
                    self.log_to_ui(f"⚠️ Ошибка в '{q}': {ex}")
                    return []

            self.log_to_ui(f"📡 Отправляю {len(variants)} запросов параллельно...")

            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(do_one, v): v for v in variants}
                for fut in as_completed(futures):
                    if self.stop_search.is_set():
                        self.log_to_ui("⏹ Поиск остановлен")
                        break
                    q = futures[fut]
                    try:
                        results = fut.result()
                        added = 0
                        for title, vid in results:
                            if vid not in seen_ids:
                                seen_ids.add(vid)
                                all_results.append((title[:45], vid))
                                added += 1
                        self.log_to_ui(f"✅ '{q}': найдено {len(results)}, новых {added}")
                    except Exception as ex:
                        self.log_to_ui(f"❌ '{q}' упал: {ex}")

            self.log_to_ui(f"📊 Всего уникальных треков: {len(all_results)}")
            self.root.after(0, lambda: self._update_results(all_results))

        except Exception as e:
            logging.error(f"Ошибка поиска: {e}", exc_info=True)
            self.log_to_ui(f"❌ ОШИБКА ПОИСКА: {e}")
            self.root.after(0, lambda err=str(e): self.status_lbl.config(text=f"Ошибка: {err}", fg="red"))
        finally:
            self.root.after(0, lambda: self.search_btn.config(state=tk.NORMAL, bg="#00cc66"))
            self.root.after(0, lambda: self.stop_btn.config(state=tk.DISABLED, bg="#555"))

    def _update_results(self, results):
        self.results_list.delete(0, tk.END)
        self.last_results = results
        for title, vid in results:
            self.results_list.insert(tk.END, f"  {title}")
        self.status_lbl.config(text=f"Найдено {len(results)}", fg="#888")

    def stop_search_command(self):
        self.stop_search.set()
        self.search_btn.config(state=tk.NORMAL, bg="#00cc66")
        self.stop_btn.config(state=tk.DISABLED, bg="#555")
        self.status_lbl.config(text="Поиск остановлен", fg="orange")

    # ---------- воспроизведение ----------
    def toggle_play_pause(self):
        if self.player.is_playing():
            self.player.pause()
            self.play_pause_btn.config(text="▶")
        else:
            sel = self.results_list.curselection()
            if sel:
                self.play_selected()
            elif self.player.get_media():
                self.player.play()
                self.play_pause_btn.config(text="⏸")

    def stop(self):
        self.player.stop()
        self.play_pause_btn.config(text="▶")
        self.current_original_path = None

    def update_progress(self):
        while not self.stop_progress.is_set():
            try:
                if self.player.get_length() > 0:
                    self.progress_var.set(self.player.get_position() * 100)
            except Exception:
                pass
            time.sleep(0.5)

    def on_progress_move(self, val):
        pass

    def on_progress_release(self, event):
        if self.player.get_length() > 0:
            self.player.set_position(self.progress_var.get() / 100.0)

    def on_volume_change(self, val):
        try:
            self.player.audio_set_volume(int(float(val)))
        except Exception:
            pass

    # ---------- обход ----------
    def update_zapret_status(self):
        if IS_WIN:
            on = bool(self.zapret_process and self.zapret_process.ready)
            self.zapret_status.config(text="Zapret ON" if on else "Zapret OFF")
        else:
            self.zapret_status.config(text="tpws ON" if self.proxy_url else "tpws OFF")

    def restart_zapret(self):
        if IS_WIN and self.zapret_process:
            self.zapret_process.shutdown()
            self.zapret_process.start()
        elif IS_MAC:
            if self.tpws_process:
                self.tpws_process.shutdown()
                if self.tpws_process.start():
                    self.proxy_url = TPWS_PROXY
            else:
                self._check_tpws()
        self.update_zapret_status()

    # ---------- обновления ----------
    def _check_for_updates(self):
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            r = requests.get(url, proxies={"https": self.proxy_url} if self.proxy_url else None, timeout=10)
            if r.status_code == 200:
                latest = r.json().get("tag_name", "").lstrip("v")
                if latest and tuple(map(int, latest.split('.'))) > tuple(map(int, APP_VERSION.split('.'))):
                    self.root.after(0, lambda: self.status_lbl.config(
                        text=f"⬆ Доступна версия {latest}", fg="#ffaa00"
                    ))
        except Exception:
            pass

    def on_closing(self):
        logging.info("Закрытие приложения...")
        self.stop_progress.set()
        try:
            self.stop()
        except Exception:
            pass
        if self.zapret_process:
            self.zapret_process.shutdown()
        if self.tpws_process:
            self.tpws_process.shutdown()
        if self.node_process:
            try:
                self.node_process.terminate()
            except Exception:
                pass
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = KaraokeApp(root)
        root.mainloop()
    except Exception as e:
        logging.critical(f"Критический сбой: {e}", exc_info=True)
        messagebox.showerror(
            "Критическая ошибка",
            f"Приложение вылетело:\n{e}\n\nЛог сохранён в:\n{LOG_FILE}"
        )
