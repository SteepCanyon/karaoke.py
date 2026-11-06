#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
import webbrowser
import requests
import logging

# ==========================================
# НАСТРОЙКА ОКРУЖЕНИЯ
# ==========================================
IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"

# Логирование
if IS_WIN:
    LOG_DIR = r"C:\Users\Suncho\Desktop\karaoke_player_you_tube"
else:
    LOG_DIR = os.path.expanduser("~/Desktop/karaoke_player_you_tube")

try:
    os.makedirs(LOG_DIR, exist_ok=True)
    LOG_FILE = os.path.join(LOG_DIR, "app.log")
except Exception:
    LOG_FILE = "app.log"

logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s', encoding='utf-8')

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception

if IS_MAC:
    for _vlc_candidate in ("/Applications/VLC.app/Contents/MacOS", "/Applications/MacPorts/VLC.app/Contents/MacOS"):
        if os.path.exists(f"{_vlc_candidate}/lib/libvlc.dylib"):
            os.environ["DYLD_LIBRARY_PATH"] = f"{_vlc_candidate}/lib"
            os.environ.setdefault("VLC_PLUGIN_PATH", f"{_vlc_candidate}/plugins")
            break

if IS_WIN:
    for _vlc_win in (r"C:\Program Files\VideoLAN\VLC", r"C:\Program Files (x86)\VideoLAN\VLC"):
        if os.path.exists(os.path.join(_vlc_win, "libvlc.dll")):
            try: os.add_dll_directory(_vlc_win)
            except Exception: pass
            break

try:
    import vlc
    import yt_dlp
except Exception as e:
    logging.critical(f"Ошибка импорта VLC или yt-dlp: {e}")
    sys.exit(1)

# ==========================================
# КОНСТАНТЫ
# ==========================================
TPWS_PROXY = "socks5h://127.0.0.1:9876"
TPWS_PORT = 9876
APP_VERSION = "1.0.19"
GITHUB_REPO = "SteepCanyon/karaoke.py"
ZAPRET_DIR = r"C:\Zapret\Dev"
ZAPRET_EXE_TARGET = os.path.join(ZAPRET_DIR, "Zapret.exe")

def resource_path(relative_path):
    if getattr(sys, 'frozen', False): return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

def find_ffmpeg():
    names = ("ffmpeg.exe", "ffmpeg") if IS_WIN else ("ffmpeg",)
    for n in names:
        bundled = resource_path(os.path.join("ffmpeg", n))
        if os.path.exists(bundled): return bundled
    if IS_WIN:
        for p in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
            if os.path.exists(p): return p
    for p in ("/opt/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p): return p
    return shutil.which("ffmpeg") or "ffmpeg"

# ==========================================
# WINDOWS ADMIN & ZAPRET
# ==========================================
def is_admin():
    try: return ctypes.windll.shell32.IsUserAnAdmin()
    except: return False

if IS_WIN and not is_admin():
    script = os.path.abspath(sys.argv[0])
    params = ' '.join([f'"{arg}"' if ' ' in arg else arg for arg in sys.argv[1:]])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}', None, 1)
    sys.exit(0)

def install_zapret():
    if not IS_WIN: return False
    src = resource_path("zapret")
    if not os.path.exists(src): return False
    if os.path.exists(ZAPRET_EXE_TARGET): return True
    try:
        os.makedirs(ZAPRET_DIR, exist_ok=True)
        for item in os.listdir(src):
            s, d = os.path.join(src, item), os.path.join(ZAPRET_DIR, item)
            if os.path.isdir(s): shutil.copytree(s, d, dirs_exist_ok=True)
            else: shutil.copy2(s, d)
        return os.path.exists(ZAPRET_EXE_TARGET)
    except Exception: return False

class ZapretProcess:
    def __init__(self, exe_path):
        self.exe_path, self.process, self.ready = exe_path, None, False
    def start(self):
        if not os.path.exists(self.exe_path): return False
        try:
            self.process = subprocess.Popen([self.exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
            time.sleep(1)
            self.ready = True
            return True
        except Exception: return False
    def shutdown(self):
        if self.process:
            self.process.terminate(); self.process = None; self.ready = False

class TpwsProcess:
    def __init__(self, binary_path, port=TPWS_PORT, strategy=None):
        self.binary_path, self.port, self.process, self.ready = binary_path, port, None, False
        self.strategy = strategy or ["--split-pos=midsld", "--oob"]
    def start(self):
        if self.ready: return True
        if not os.path.exists(self.binary_path): return False
        try:
            # Выдаем права на запуск (macOS часто снимает их при переносе папок)
            try: os.chmod(self.binary_path, 0o755)
            except Exception: pass
            
            cmd = [self.binary_path, "--socks", "--bind-addr=127.0.0.1", f"--port={self.port}"] + self.strategy
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(40):
                time.sleep(0.1)
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                        self.ready = True
                        return True
                except OSError: continue
            return False
        except Exception: return False
    def shutdown(self):
        if self.process:
            try:
                self.process.terminate(); self.process.wait(timeout=3)
            except Exception: pass
            self.process = None
        self.ready = False

_libtk_cache = None
def get_nsview(tk_widget):
    global _libtk_cache
    if not IS_MAC: return None
    if _libtk_cache is None:
        try:
            libtk_name = f"libtk{tk.TkVersion}.dylib"
            for path in [f"/opt/local/lib/{libtk_name}", f"/opt/homebrew/lib/{libtk_name}", f"/usr/local/lib/{libtk_name}"]:
                if os.path.exists(path):
                    _libtk_cache = ctypes.cdll.LoadLibrary(path)
                    break
        except Exception: pass
    if _libtk_cache is None: return None
    try:
        f = _libtk_cache.TkMacOSXGetRootControl
        f.restype = ctypes.c_void_p
        f.argtypes = (ctypes.c_void_p,)
        return f(ctypes.c_void_p(tk_widget.winfo_id()))
    except Exception: return None

# ==========================================
# ГЛАВНЫЙ КЛАСС ПРИЛОЖЕНИЯ
# ==========================================
class KaraokeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Караоке-плеер")
        self.root.geometry("1200x750")
        self.root.configure(bg="#0a0a0a")

        # Конфиг
        self.config_file = os.path.expanduser("~/.karaoke_config.json")
        self.config = self._load_config()

        # PATH для MacPorts
        os.environ["PATH"] = "/opt/local/bin:/opt/homebrew/bin:" + os.environ.get("PATH", "")

        # Запуск Node.js POT сервера
        self.node_process = None
        possible_dirs = [
            resource_path("node_server"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundle_env", "node_server"),
            os.path.expanduser("~/Desktop/bgutil-ytdlp-pot-provider/server")
        ]
        node_server_dir = next((d for d in possible_dirs if os.path.exists(os.path.join(d, "build", "main.js"))), None)
        
        if node_server_dir:
            try:
                node_bin = "/usr/local/bin/node"
                bundled_node = os.path.join(node_server_dir, "node")
                if os.path.exists(bundled_node): 
                    node_bin = bundled_node
                    try: os.chmod(node_bin, 0o755)
                    except Exception: pass
                elif not os.path.exists(node_bin): 
                    node_bin = "node"

                self.node_process = subprocess.Popen([node_bin, "build/main.js"], cwd=node_server_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"[POT] Node.js сервер запущен из: {node_server_dir}")
            except Exception as e:
                print(f"[POT] ОШИБКА: {e}")

        # Запуск Zapret/Tpws
        self.zapret_process = None
        self.tpws_process = None
        self.proxy_url = None
        self.projector_mode = False

        if IS_WIN:
            if not os.path.exists(ZAPRET_EXE_TARGET):
                if not install_zapret(): sys.exit(1)
            self.zapret_process = ZapretProcess(ZAPRET_EXE_TARGET)
            self.zapret_process.start()
        else:
            if not os.path.exists("/Applications/VLC.app") and not os.path.exists("/Applications/MacPorts/VLC.app"):
                messagebox.showerror("VLC не найден", "Установите VLC")
                sys.exit(1)
            bundled_tpws = resource_path(os.path.join("zapret", "tpws", "tpws"))
            self.tpws_process = TpwsProcess(bundled_tpws, port=TPWS_PORT, strategy=self.config.get("strategy"))
            if self.tpws_process.start(): self.proxy_url = TPWS_PROXY
            else: self._check_tpws()

        # VLC
        try:
            self.instance = vlc.Instance("--http-user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "--avcodec-hw=none")
            self.player = self.instance.media_player_new()
        except Exception as e:
            logging.critical(f"Ошибка VLC: {e}")
            sys.exit(1)

        # Переменные
        self.current_url = None
        self.current_original_path = None
        self.current_pitch = 0
        self.last_results = []
        self.all_local_files = []
        self.mode = tk.StringVar(value="youtube")

        self.download_folder = os.path.expanduser(self.config.get("download_folder", "~/Music/KaraokePlayer/KaraokeDownloads"))
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

        # Сборка UI
        self.create_widgets()
        self._create_video_window()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        threading.Thread(target=self._check_for_updates, daemon=True).start()

    def _load_config(self):
        defaults = {
            "use_pot": False,
            "extended_search": True,
            "strategy": ["--split-pos=midsld", "--oob"],
            "format": "bv*+ba/b",
            "download_folder": "~/Music/KaraokePlayer/KaraokeDownloads"
        }
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in defaults.items():
                        if k not in data: data[k] = v
                    return data
        except Exception: pass
        return defaults

    def _save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception: pass

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
        return {}

    def _save_cache(self):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f: json.dump(self.video_cache, f, ensure_ascii=False, indent=2)
        except Exception: pass

    def _check_tpws(self):
        try:
            with socket.create_connection(("127.0.0.1", TPWS_PORT), timeout=1.0):
                self.proxy_url = TPWS_PROXY
                return True
        except OSError:
            self.proxy_url = None
            return False

    # ==========================================
    # ИНТЕРФЕЙС
    # ==========================================
    def create_widgets(self):
        bg, fg, accent, entry_bg, list_bg = "#0a0a0a", "#e0e0e0", "#00cc66", "#1a1a1a", "#111111"

        main = tk.Frame(self.root, bg=bg)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        self.video_placeholder = tk.Frame(self.notebook, bg="black")
        self.notebook.add(self.video_placeholder, text="💻 Консоль (Видео)")

        # Консоль внутри вкладки
        console_frame = tk.Frame(self.video_placeholder, bg="#111")
        console_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.console_text = tk.Text(console_frame, bg="#0a0a0a", fg="#00cc66", font=("Menlo", 11), 
                                    state=tk.DISABLED, wrap=tk.WORD, highlightthickness=0)
        self.console_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = tk.Scrollbar(console_frame, command=self.console_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_text.config(yscrollcommand=scrollbar.set)
        
        # Инструкция для видео
        tk.Label(self.video_placeholder, text="Видео откроется в отдельном окне VLC", 
                 fg="#555", bg="black", font=("Segoe UI", 10)).pack(side=tk.BOTTOM, pady=5)

        self.settings_frame = tk.Frame(self.notebook, bg=bg)
        self.notebook.add(self.settings_frame, text="⚙️ Настройки")

        self._build_settings_ui()

        right = tk.Frame(main, bg=bg, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        # Режимы
        mode_frame = tk.Frame(right, bg=bg)
        mode_frame.pack(fill=tk.X, pady=(0, 5))
        tk.Radiobutton(mode_frame, text="YouTube", variable=self.mode, value="youtube", command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg).pack(side=tk.LEFT)
        tk.Radiobutton(mode_frame, text="Локальные", variable=self.mode, value="local", command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg).pack(side=tk.LEFT, padx=10)

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
        self.stop_btn = tk.Button(btn_frame, text="⏹ Стоп", bg="#555", fg=fg, relief=tk.FLAT, command=self.stop_search_command, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        # Локальный поиск
        self.local_search_frame = tk.Frame(right, bg=bg)
        tk.Label(self.local_search_frame, text="🔍 Поиск в локальных", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        self.local_search_entry = tk.Entry(self.local_search_frame, bg=entry_bg, fg=fg, insertbackground=fg, relief=tk.FLAT)
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
        self.results_list = tk.Listbox(right, bg=list_bg, fg=fg, relief=tk.FLAT, selectbackground=accent, selectforeground="#000", height=8, width=30)
        self.results_list.pack(fill=tk.BOTH, expand=True, pady=5)
        self.results_list.bind('<Double-1>', lambda e: self.play_selected())
        self.results_list.bind('<<ListboxSelect>>', self._on_list_select)

        # Кнопки управления
        ctrl_frame = tk.Frame(right, bg=bg)
        ctrl_frame.pack(fill=tk.X, pady=5)
        self.download_btn = tk.Button(ctrl_frame, text="📥", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.download_selected, width=4)
        self.download_btn.grid(row=0, column=0, padx=2)
        self.play_pause_btn = tk.Button(ctrl_frame, text="▶", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.toggle_play_pause, width=4)
        self.play_pause_btn.grid(row=0, column=1, padx=2)
        self.stop_play_btn = tk.Button(ctrl_frame, text="⏹", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.stop, width=4)
        self.stop_play_btn.grid(row=0, column=2, padx=2)
        self.delete_btn = tk.Button(ctrl_frame, text="🗑", bg="#cc3333", fg="#fff", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.delete_selected_local, width=4)
        self.delete_btn.grid(row=0, column=3, padx=2)
        self.delete_btn.grid_remove()

        # Прогресс
        progress_frame = tk.Frame(right, bg=bg)
        progress_frame.pack(fill=tk.X, pady=5)
        self.progress_scale = tk.Scale(progress_frame, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.progress_var, command=self.on_progress_move, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0)
        self.progress_scale.pack(fill=tk.X)
        self.progress_scale.bind("<ButtonRelease-1>", self.on_progress_release)

        # Громкость
        vol_frame = tk.Frame(right, bg=bg)
        vol_frame.pack(fill=tk.X, pady=5)
        tk.Label(vol_frame, text="🔊 Громкость", fg=fg, bg=bg).pack(anchor=tk.W)
        self.vol_scale = tk.Scale(vol_frame, from_=0, to=200, orient=tk.HORIZONTAL, command=self.on_volume_change, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0)
        self.vol_scale.set(100)
        self.vol_scale.pack(fill=tk.X)

        # Тональность
        pitch_frame = tk.Frame(right, bg=bg)
        pitch_frame.pack(fill=tk.X, pady=5)
        tk.Label(pitch_frame, text="🎚 Тональность", fg=fg, bg=bg).pack(anchor=tk.W)
        self.pitch_scale = tk.Scale(pitch_frame, from_=-6, to=6, orient=tk.HORIZONTAL, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0, resolution=1)
        self.pitch_scale.pack(fill=tk.X)
        self.pitch_scale.bind("<ButtonRelease-1>", self.on_pitch_release)

        # Нижние кнопки
        self.restart_zapret_btn = tk.Button(right, text="🔄 Проверить обход", bg=accent, fg="#000", relief=tk.FLAT, command=self.restart_zapret)
        self.restart_zapret_btn.pack(fill=tk.X, pady=(5, 2))
        
        self.projector_btn = tk.Button(right, text="📺 На проектор", bg="#555", fg=fg, font=("Segoe UI", 9, "bold"), relief=tk.FLAT, command=self._toggle_projector)
        self.projector_btn.pack(fill=tk.X, pady=(0, 5))

        self.on_mode_change()

    def _build_settings_ui(self):
        bg, fg = "#0a0a0a", "#e0e0e0"
        entry_bg = "#1a1a1a"
        frame = tk.Frame(self.settings_frame, bg=bg, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        # Стратегия
        tk.Label(frame, text="Стратегия обхода (tpws):", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
        self.strategy_var = tk.StringVar()
        strategies = {
            "midsld + oob": ["--split-pos=midsld", "--oob"],
            "disorder + split 1": ["--disorder", "--split-pos=1"],
            "disorder + host + split 1": ["--disorder", "--split-pos=host", "--split-pos=1"],
            "split 1 + oob": ["--split-pos=1", "--oob"],
            "fake + split 1 (Might work)": ["--fake", "--split-pos=1"],
            "hostspell + disorder": ["--hostspell=host", "--disorder"],
            "tlsrec + split 2": ["--tlsrec=sni", "--split-pos=2"]
        }
        self.strategy_combo = ttk.Combobox(frame, textvariable=self.strategy_var, values=list(strategies.keys()), state="readonly")
        self.strategy_combo.pack(fill=tk.X, pady=(0, 15))
        
        curr_strategy = self.config.get("strategy", ["--split-pos=midsld", "--oob"])
        for k, v in strategies.items():
            if v == curr_strategy:
                self.strategy_var.set(k)
                break
        self.strategies_map = strategies

        # Формат
        tk.Label(frame, text="Качество видео:", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
        self.format_var = tk.StringVar()
        formats = {
            "Лучшее (bv*+ba/b)": "bv*+ba/b",
            "1080p (bv*[height<=1080]+ba/b)": "bv*[height<=1080]+ba/b",
            "720p (bv*[height<=720]+ba/b)": "bv*[height<=720]+ba/b",
            "480p (bv*[height<=480]+ba/b)": "bv*[height<=480]+ba/b",
            "360p (mp4)": "18"
        }
        self.format_combo = ttk.Combobox(frame, textvariable=self.format_var, values=list(formats.keys()), state="readonly")
        self.format_combo.pack(fill=tk.X, pady=(0, 15))
        
        curr_format = self.config.get("format", "bv*+ba/b")
        for k, v in formats.items():
            if v == curr_format:
                self.format_var.set(k)
                break
        self.formats_map = formats

        # 18+
        self.use_pot_var = tk.BooleanVar(value=self.config.get("use_pot", False))
        tk.Checkbutton(frame, text="🔞 Режим обхода 18+ (web_creator)", variable=self.use_pot_var, 
                       bg=bg, fg=fg, selectcolor=entry_bg, activebackground=bg, activeforeground=fg).pack(anchor=tk.W, pady=(0, 15))

        # Расширенный поиск
        self.extended_search_var = tk.BooleanVar(value=self.config.get("extended_search", True))
        tk.Checkbutton(frame, text="🔎 Расширенный поиск (караоке + karaoke + минус + instrumental)", 
                       variable=self.extended_search_var, 
                       bg=bg, fg=fg, selectcolor=entry_bg, activebackground=bg, activeforeground=fg).pack(anchor=tk.W, pady=(0, 15))

        # Папка
        tk.Label(frame, text="Папка для скачивания:", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
        folder_frame = tk.Frame(frame, bg=bg)
        folder_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.folder_var = tk.StringVar(value=self.download_folder)
        self.folder_entry = tk.Entry(folder_frame, textvariable=self.folder_var, bg=entry_bg, fg=fg, insertbackground=fg, relief=tk.FLAT)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        
        tk.Button(folder_frame, text="...", bg="#555", fg=fg, command=self._browse_folder, width=3).pack(side=tk.RIGHT, padx=(5, 0))

        # Сохранить
        tk.Button(frame, text="💾 Сохранить настройки", bg="#00cc66", fg="#000", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, command=self._save_settings).pack(fill=tk.X, pady=(10, 20))

        # Инфа
        tk.Label(frame, text="Автор: SteepCanyon", fg="#888", bg=bg).pack(anchor=tk.W)
        tk.Label(frame, text=f"Версия: {APP_VERSION}", fg="#888", bg=bg).pack(anchor=tk.W)
        tk.Button(frame, text="🌐 GitHub", bg="#333", fg=fg, relief=tk.FLAT, command=lambda: webbrowser.open(f"https://github.com/{GITHUB_REPO}")).pack(anchor=tk.W, pady=(5, 0))

    # ==========================================
    # МЕТОДЫ НАСТРОЕК
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
        self.config["strategy"] = self.strategies_map.get(self.strategy_var.get(), ["--split-pos=midsld", "--oob"])
        self.config["format"] = self.formats_map.get(self.format_var.get(), "bv*+ba/b")
        self.config["download_folder"] = self.folder_var.get()

        self._save_config()

        if self.config["download_folder"] != old_folder:
            self.download_folder = os.path.expanduser(self.config["download_folder"])
            os.makedirs(self.download_folder, exist_ok=True)
            self.cache_file = os.path.join(self.download_folder, ".cache.json")
            self.video_cache = self._load_cache()
            self.load_local_files()

        if IS_MAC and self.config["strategy"] != old_strategy:
            bundled_tpws = resource_path(os.path.join("zapret", "tpws", "tpws"))
            self.tpws_process.shutdown()
            self.tpws_process = TpwsProcess(bundled_tpws, port=TPWS_PORT, strategy=self.config["strategy"])
            if self.tpws_process.start(): self.proxy_url = TPWS_PROXY
            else: self.proxy_url = None
            self.update_zapret_status()

        messagebox.showinfo("Настройки", "Настройки сохранены!")

    # ==========================================
    # ВИДЕО ОКНО
    # ==========================================
    def _create_video_window(self):
        self.video_window = tk.Toplevel(self.root)
        self.video_window.title("Karaoke Video")
        self.video_window.configure(bg="black")
        self.video_window.transient(self.root)
        
        self.video_canvas = tk.Canvas(self.video_window, bg="black", highlightthickness=0, bd=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)
        
        self.video_window.protocol("WM_DELETE_WINDOW", self.video_window.withdraw)

        self.video_placeholder.bind("<Configure>", lambda e: self.root.after(50, self._position_video_window))
        self.root.bind("<Unmap>", self._on_root_unmap)
        self.root.bind("<Map>", self._on_root_map)
        self.root.after(200, self._position_video_window)
        self.root.after(500, self._attach_vlc)

    def _position_video_window(self):
        if getattr(self, 'projector_mode', False): return
        try:
            if not self.video_window.winfo_exists(): return
            self.root.update_idletasks()
            x, y = self.video_placeholder.winfo_rootx(), self.video_placeholder.winfo_rooty()
            w, h = self.video_placeholder.winfo_width(), self.video_placeholder.winfo_height()
            if w > 10 and h > 10:
                self.video_window.geometry(f"{w}x{h}+{x}+{y}")
        except Exception: pass

    def _attach_vlc(self):
        try:
            if IS_MAC:
                nsview = get_nsview(self.video_canvas)
                if nsview: self.player.set_nsobject(nsview)
            elif IS_WIN:
                self.player.set_hwnd(self.video_canvas.winfo_id())
        except Exception: pass

    def _on_root_unmap(self, event=None):
        if event and event.widget is not self.root: return
        if hasattr(self, 'video_window') and self.video_window.winfo_exists(): self.video_window.withdraw()

    def _on_root_map(self, event=None):
        if event and event.widget is not self.root: return
        if hasattr(self, 'video_window') and self.video_window.winfo_exists():
            self.video_window.deiconify()
            self.root.after(50, self._position_video_window)

    # ==========================================
    # ПРОЕКТОР
    # ==========================================
    def _get_screens(self):
        if not IS_MAC: return None
        try:
            from AppKit import NSScreen
            return [{'x': int(s.frame().origin.x), 'y': int(s.frame().origin.y), 'w': int(s.frame().size.width), 'h': int(s.frame().size.height)} for s in NSScreen.screens()]
        except ImportError: return None
        except Exception: return None

    def _toggle_projector(self):
        if getattr(self, 'projector_mode', False): self._return_from_projector()
        else: self._send_to_projector()

    def _send_to_projector(self):
        screens = self._get_screens()
        if not screens or len(screens) < 2:
            messagebox.showerror("Ошибка", "Второй экран не найден или нужен pyobjc")
            return
        main_idx = 0
        for i, s in enumerate(screens):
            if s['x'] == 0 and s['y'] == 0: main_idx = i; break
        target = next((s for i, s in enumerate(screens) if i != main_idx), None)
        if not target: return
        self.video_window.geometry(f"{target['w']}x{target['h']}+{target['x']}+{target['y']}")
        self.projector_mode = True
        self.projector_btn.config(text="📺 Вернуть", bg="#aa4444")

    def _return_from_projector(self):
        self.projector_mode = False
        self.projector_btn.config(text="📺 На проектор", bg="#555")
        self._position_video_window()

    # ==========================================
    # ЛОГИКА РАБОТЫ
    # ==========================================
    def _on_list_select(self, event=None):
        sel = self.results_list.curselection()
        if sel and sel[0] < len(self.last_results):
            self.status_lbl.config(text=self.last_results[sel[0]][0][:110], fg="#00cc66")

    def log_to_ui(self, message):
        def _update():
            self.console_text.config(state=tk.NORMAL)
            self.console_text.insert(tk.END, str(message) + "\n")
            self.console_text.see(tk.END)
            self.console_text.config(state=tk.DISABLED)
        self.root.after(0, _update)

    def _filter_local_files(self, event=None):
        self._render_local_files()

    def _render_local_files(self):
        self.results_list.delete(0, tk.END)
        self.last_results = []
        query = self.local_search_entry.get().strip().lower() if hasattr(self, 'local_search_entry') else ""
        count = 0
        for f, path in getattr(self, 'all_local_files', []):
            if query and query not in f.lower(): continue
            self.results_list.insert(tk.END, f"  {f[:36]}{'...' if len(f)>36 else ''}")
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
        mode = self.mode.get()
        if mode == "youtube":
            if hasattr(self, 'local_search_frame'): self.local_search_frame.pack_forget()
            self.search_frame.pack(fill=tk.X, pady=(0, 5))
            self.download_btn.grid()
            if hasattr(self, 'delete_btn'): self.delete_btn.grid_remove()
        else:
            self.search_frame.pack_forget()
            if hasattr(self, 'local_search_frame'): self.local_search_frame.pack(fill=tk.X, pady=(0, 5), before=self.results_list)
            self.download_btn.grid_remove()
            if hasattr(self, 'delete_btn'): self.delete_btn.grid()
            self.load_local_files()

    def delete_selected_local(self):
        if self.mode.get() != "local": return
        sel = self.results_list.curselection()
        if not sel: return
        fname, path = self.last_results[sel[0]]
        if messagebox.askyesno("Удалить", f"Удалить {fname}?"):
            try:
                os.remove(path)
                for vid, p in list(self.video_cache.items()):
                    if p == path: del self.video_cache[vid]
                self._save_cache()
                self.load_local_files()
            except Exception as e: messagebox.showerror("Ошибка", str(e))

    def _get_next_number(self):
        max_num = 0
        if os.path.exists(self.download_folder):
            for f in os.listdir(self.download_folder):
                m = re.match(r'^(\d+)\s*-\s*', f)
                if m: max_num = max(max_num, int(m.group(1)))
        return max_num + 1

    def download_selected(self):
        if self.mode.get() != "youtube": return
        sel = self.results_list.curselection()
        if not sel: return
        vid = self.last_results[sel[0]][1]
        if not re.match(r'^[A-Za-z0-9_-]{11}$', vid): return
        if self.video_cache.get(vid) and os.path.exists(self.video_cache[vid]):
            self.status_lbl.config(text="Уже в библиотеке", fg="#00cc66")
            return
        self.status_lbl.config(text="Скачивание...", fg="#00cc66")
        threading.Thread(target=self._download, args=(vid, False), daemon=True).start()

    def play_selected(self):
        sel = self.results_list.curselection()
        if not sel: return
        item = self.last_results[sel[0]][1]
        if self.mode.get() == "youtube":
            if not re.match(r'^[A-Za-z0-9_-]{11}$', item): return
            cached = self.video_cache.get(item)
            if cached and os.path.exists(cached):
                self.status_lbl.config(text="Из библиотеки", fg="#00cc66")
                self._play_local(cached)
                return
            self.status_lbl.config(text="Загрузка...", fg="#00cc66")
            threading.Thread(target=self._download, args=(item, True), daemon=True).start()
        else:
            self._play_local(item)

    def _download(self, video_id, auto_play):
        url = f"https://youtu.be/{video_id}"
        outtmpl = os.path.join(self.temp_dir, '%(title)s.%(ext)s')
        
        for f in os.listdir(self.temp_dir):
            try: os.remove(os.path.join(self.temp_dir, f))
            except Exception: pass

        fmt = self.config.get("format", "bv*+ba/b")
        use_pot = self.config.get("use_pot", False)
        
        ydl_opts = {
            'quiet': True,
            'format': fmt,
            'outtmpl': outtmpl,
            'nopart': True,
            'cookiesfrombrowser': ('chrome',),
            'progress_hooks': [self._progress_hook]
        }
        
        if use_pot:
            self.log_to_ui("🔞 [Скачивание] Режим 18+ ВКЛЮЧЕН (web_creator)")
            ydl_opts.update({
                'remote_components': ['ejs:github'],
                'extractor_args': {'youtube': {'player_client': ['web_creator']}},
                'source_address': '0.0.0.0',
                'http_chunk_size': 10485760,
                'socket_timeout': 60,
                'retries': 20,
                'fragment_retries': 20,
                'retry_sleep': 2
            })
        else:
            self.log_to_ui("🟢 [Скачивание] Обычный режим (android/web)")
            ydl_opts.update({
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
            })

        if self.proxy_url:
            ydl_opts['proxy'] = self.proxy_url.replace('socks5h://', 'socks5://')

        try:
            self.log_to_ui(f"📥 [Скачивание] Запуск: {video_id}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
                
            self.log_to_ui("📦 [Скачивание] Файл загружен, ищем на диске...")
            downloaded_files = [os.path.join(self.temp_dir, f) for f in os.listdir(self.temp_dir) if f.endswith(('.mp4', '.mkv', '.webm'))]
            if not downloaded_files: raise Exception("Скачанный файл не найден")
            
            temp_path = max(downloaded_files, key=os.path.getmtime)
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
            self.log_to_ui(f"❌ ОШИБКА СКАЧИВАНИЯ: {e}")
            self.root.after(0, lambda err=str(e): self.status_lbl.config(text=f"Ошибка: {err}", fg="red"))


    def _progress_hook(self, d):
        if d['status'] == 'downloading':
            self.log_to_ui(f"📥 [Скачивание] {d.get('_percent_str', '0%').strip()} | {d.get('_speed_str', 'N/A').strip()}")
        elif d['status'] == 'finished':
            self.log_to_ui("🔗 [Скачивание] Завершено, склеиваем...")

    def _play_local(self, path):
        if not os.path.exists(path): return
        try:
            self.current_original_path = path
            self.current_pitch = 0
            self.pitch_scale.set(0)
            self.player.stop()
            media = self.instance.media_new_path(path)
            self.player.set_media(media)
            self.player.play()
            self.play_pause_btn.config(text="⏸")
            
            if hasattr(self, 'video_window') and self.video_window.winfo_exists():
                self.video_window.deiconify()
                self.root.after(50, self._position_video_window)
            self.status_lbl.config(text="▶ Воспроизведение", fg="#00cc66")
            self.on_volume_change(self.vol_scale.get())
        except Exception: pass

    def on_pitch_release(self, event):
        semitones = int(self.pitch_scale.get())
        if not self.current_original_path or semitones == self.current_pitch: return
        self.current_pitch = semitones
        threading.Thread(target=self._apply_pitch, args=(semitones,), daemon=True).start()

    def _apply_pitch(self, semitones):
        src = self.current_original_path
        if not src or not os.path.exists(src): return
        if semitones == 0: self.root.after(0, lambda: self._play_pitched(src, 0)); return
        key = (src, semitones)
        if key in self.pitch_cache and os.path.exists(self.pitch_cache[key]):
            self.root.after(0, lambda p=self.pitch_cache[key]: self._play_pitched(p, semitones))
            return
        self.root.after(0, lambda: self.status_lbl.config(text=f"Тональность {semitones:+d}...", fg="orange"))
        ratio = 2 ** (semitones / 12.0)
        sr = 44100
        out_path = os.path.join(self.temp_dir, f"pitched_{semitones:+d}.mp4")
        cmd = [self.ffmpeg_path, "-y", "-i", src, "-filter_complex", f"[0:a]asetrate={int(sr*ratio)},aresample={sr},atempo={1.0/ratio:.6f}[a]", "-map", "0:v?", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out_path]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=True)
            self.pitch_cache[key] = out_path
            self.root.after(0, lambda p=out_path: self._play_pitched(p, semitones))
        except Exception as e: self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка pitch: {e}", fg="red"))

    def _play_pitched(self, path, semitones):
        if not os.path.exists(path): return
        pos = self.player.get_position()
        was_playing = self.player.is_playing()
        self.player.stop()
        self.player.set_media(self.instance.media_new_path(path))
        self.player.play()
        if 0 < pos < 1: self.root.after(250, lambda: self.player.set_position(pos))
        if not was_playing: self.root.after(350, self.player.pause)
        self.status_lbl.config(text=f"▶ Тональность: {semitones:+d}", fg="#00cc66")

    def do_search(self):
        q = self.search_entry.get().strip()
        if not q: return
        self.search_btn.config(state=tk.DISABLED, bg="#555")
        self.stop_btn.config(state=tk.NORMAL, bg="#aa0000")
        self.status_lbl.config(text="Поиск...", fg="#00cc66")
        self.results_list.delete(0, tk.END)
        threading.Thread(target=self._search_task, args=(q,), daemon=True).start()

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
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
            }
            if self.proxy_url: 
                base_opts['proxy'] = self.proxy_url.replace('socks5h://', 'socks5://')
            
            from concurrent.futures import ThreadPoolExecutor, as_completed
            
            seen_ids = set()
            all_results = []
            
            def do_one(q):
                try:
                    opts = dict(base_opts)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(f"ytsearch10:{q}", download=False)
                        return [(e.get('title', ''), e['id']) for e in info.get('entries', []) if e.get('id')]
                except Exception as ex:
                    self.log_to_ui(f"⚠️ Ошибка в '{q}': {ex}")
                    return []
            
            self.log_to_ui(f"📡 Отправляю {len(variants)} запросов параллельно...")
            
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(do_one, v): v for v in variants}
                for fut in as_completed(futures):
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
            self.log_to_ui(f"❌ ОШИБКА ПОИСКА: {e}")
            self.root.after(0, lambda err=str(e): self.status_lbl.config(text=f"Ошибка: {err}", fg="red"))
        finally: 
            self.root.after(0, lambda: self.search_btn.config(state=tk.NORMAL, bg="#00cc66"))
            self.root.after(0, lambda: self.stop_btn.config(state=tk.DISABLED, bg="#555"))


    def _update_results(self, results):
        self.results_list.delete(0, tk.END)
        self.last_results = results
        for title, vid in results: self.results_list.insert(tk.END, f"  {title}")
        self.status_lbl.config(text=f"Найдено {len(results)}", fg="#888")

    def stop_search_command(self):
        self.stop_search.set()
        self.search_btn.config(state=tk.NORMAL, bg="#00cc66")
        self.stop_btn.config(state=tk.DISABLED, bg="#555")

    def toggle_play_pause(self):
        if self.player.is_playing():
            self.player.pause(); self.play_pause_btn.config(text="▶")
        else:
            sel = self.results_list.curselection()
            if sel: self.play_selected()
            elif self.player.get_media(): self.player.play(); self.play_pause_btn.config(text="⏸")

    def stop(self):
        self.player.stop(); self.play_pause_btn.config(text="▶")
        self.current_original_path = None

    def update_progress(self):
        while not self.stop_progress.is_set():
            try:
                if self.player.get_length() > 0:
                    pos = self.player.get_position() * 100
                    self.root.after(0, lambda p=pos: self.progress_var.set(p))
            except Exception: pass
            time.sleep(0.5)

    def on_progress_move(self, val): pass
    def on_progress_release(self, event):
        if self.player.get_length() > 0: self.player.set_position(self.progress_var.get() / 100.0)

    def on_volume_change(self, val): self.player.audio_set_volume(int(float(val)))

    def update_zapret_status(self):
        if IS_WIN: self.zapret_status.config(text="Zapret ON" if self.zapret_process and self.zapret_process.ready else "Zapret OFF")
        else: self.zapret_status.config(text="tpws ON" if self.proxy_url else "tpws OFF")

    def restart_zapret(self):
        if IS_WIN and self.zapret_process:
            self.zapret_process.shutdown(); self.zapret_process.start()
        elif IS_MAC:
            if self.tpws_process:
                self.tpws_process.shutdown()
                if self.tpws_process.start(): self.proxy_url = TPWS_PROXY
            else: self._check_tpws()
        self.update_zapret_status()

    def _check_for_updates(self):
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            r = requests.get(url, proxies={"https": self.proxy_url} if self.proxy_url else None, timeout=10)
            if r.status_code == 200:
                latest = r.json().get("tag_name", "").lstrip("v")
                if latest and tuple(map(int, latest.split('.'))) > tuple(map(int, APP_VERSION.split('.'))):
                    self.root.after(0, lambda: self.status_lbl.config(text=f"⬆ Доступна версия {latest}", fg="#ffaa00"))
        except Exception: pass

    def on_closing(self):
        self.stop_progress.set()
        self.stop()
        if self.zapret_process: self.zapret_process.shutdown()
        if self.tpws_process: self.tpws_process.shutdown()
        if hasattr(self, 'node_process') and self.node_process:
            self.node_process.terminate()
        try: shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception: pass
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = KaraokeApp(root)
    root.mainloop()