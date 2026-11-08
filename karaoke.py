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

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"

if IS_MAC:
    for _vlc_candidate in ("/Applications/VLC.app/Contents/MacOS", "/Applications/MacPorts/VLC.app/Contents/MacOS"):
        if os.path.exists(f"{_vlc_candidate}/lib/libvlc.dylib"):
            os.environ["DYLD_LIBRARY_PATH"] = f"{_vlc_candidate}/lib"
            os.environ.setdefault("VLC_PLUGIN_PATH", f"{_vlc_candidate}/plugins")
            break

try:
    import vlc
    import yt_dlp
except Exception as e:
    print(f"Ошибка импорта: {e}")
    sys.exit(1)

TPWS_PROXY = "socks5h://127.0.0.1:9876"
TPWS_PORT = 9876
APP_VERSION = "1.0.29"
GITHUB_REPO = "SteepCanyon/karaoke.py"

def resource_path(relative_path):
    if getattr(sys, 'frozen', False): return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

def find_ffmpeg():
    bundled = resource_path(os.path.join("ffmpeg", "ffmpeg"))
    if os.path.exists(bundled): return bundled
    for p in ("/opt/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p): return p
    return shutil.which("ffmpeg") or "ffmpeg"

class TpwsProcess:
    def __init__(self, binary_path, port=TPWS_PORT, strategy=None):
        self.binary_path, self.port, self.process, self.ready = binary_path, port, None, False
        self.strategy = strategy or ["--split-pos=midsld", "--oob"]
    def start(self):
        if self.ready: return True
        if not os.path.exists(self.binary_path): return False
        try:
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

class KaraokeApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Караоке-плеер v{APP_VERSION}")
        self.root.geometry("1200x750")
        self.root.configure(bg="#0a0a0a")

        self.config_file = os.path.expanduser("~/.karaoke_config.json")
        self.config = self._load_config()

        os.environ["PATH"] = "/opt/local/bin:/opt/homebrew/bin:" + os.environ.get("PATH", "")

        # POT сервер (запускается лениво, только для 18+)
        self.node_process = None
        self.node_server_dir = None
        possible_dirs = [
            resource_path("node_server"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundle_env", "node_server"),
            os.path.expanduser("~/Desktop/bgutil-ytdlp-pot-provider/server")
        ]
        for d in possible_dirs:
            if os.path.exists(os.path.join(d, "build", "main.js")):
                self.node_server_dir = d
                break

        self.tpws_process = TpwsProcess(resource_path(os.path.join("zapret", "tpws", "tpws")), port=TPWS_PORT, strategy=self.config.get("strategy"))
        if self.tpws_process.start():
            self.proxy_url = TPWS_PROXY
        else:
            self.proxy_url = None

        try:
            self.instance = vlc.Instance("--http-user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "--avcodec-hw=none")
            self.player = self.instance.media_player_new()
        except Exception as e:
            print(f"Ошибка VLC: {e}")
            sys.exit(1)

        self.current_url = None
        self.projector_mode = False
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

        self.create_widgets()
        self._create_video_window()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _load_config(self):
        defaults = {
            "use_cookies": False,
            "use_pot": False,
            "strategy": ["--split-pos=midsld", "--oob"],
            "format": "bv*+ba/b",
            "extended_search": True,
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

    def log_to_ui(self, message):
        if hasattr(self, 'console_text'):
            def _update():
                self.console_text.config(state=tk.NORMAL)
                self.console_text.insert(tk.END, str(message) + "\n")
                self.console_text.see(tk.END)
                self.console_text.config(state=tk.DISABLED)
            self.root.after(0, _update)
        else:
            print(message)

    def _ensure_pot_server(self):
        if self.node_process is not None:
            return True
        if not self.node_server_dir:
            return False
        try:
            node_bin = "/usr/local/bin/node"
            bundled_node = os.path.join(self.node_server_dir, "node")
            if os.path.exists(bundled_node):
                node_bin = bundled_node
                try: os.chmod(node_bin, 0o755)
                except Exception: pass
            elif not os.path.exists(node_bin):
                node_bin = "node"

            self.node_process = subprocess.Popen(
                [node_bin, "build/main.js"], cwd=self.node_server_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.log_to_ui("🚀 POT сервер запущен (18+)")
            time.sleep(1.5)
            return True
        except Exception as e:
            self.log_to_ui(f"❌ Ошибка POT: {e}")
            return False

    def _get_cookie_opt(self):
        if not self.config.get("use_cookies", False):
            return {}
        source = self.config.get("cookie_source", "chrome")
        if source == "file":
            cookie_file = resource_path("cookies.txt")
            if os.path.exists(cookie_file):
                return {'cookiefile': cookie_file}
            return {}
        return {'cookiesfrombrowser': (source,)}

    def create_widgets(self):
        bg, fg, accent, entry_bg, list_bg = "#0a0a0a", "#e0e0e0", "#00cc66", "#1a1a1a", "#111111"

        main = tk.Frame(self.root, bg=bg)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        self.video_placeholder = tk.Frame(self.notebook, bg="black")
        self.notebook.add(self.video_placeholder, text="💻 Консоль (Видео)")

        self.settings_frame = tk.Frame(self.notebook, bg=bg)
        self.notebook.add(self.settings_frame, text="⚙️ Настройки")

        # Консоль внутри вкладки
        console_frame = tk.Frame(self.video_placeholder, bg="#111")
        console_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.console_text = tk.Text(console_frame, bg="#0a0a0a", fg="#00cc66", font=("Menlo", 11), state=tk.DISABLED, wrap=tk.WORD, highlightthickness=0)
        self.console_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = tk.Scrollbar(console_frame, command=self.console_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_text.config(yscrollcommand=scrollbar.set)
        tk.Label(self.video_placeholder, text="Видео откроется в отдельном окне VLC", fg="#555", bg="black", font=("Segoe UI", 10)).pack(side=tk.BOTTOM, pady=5)

        self._build_settings_ui()

        right = tk.Frame(main, bg=bg, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        mode_frame = tk.Frame(right, bg=bg)
        mode_frame.pack(fill=tk.X, pady=(0, 5))
        tk.Radiobutton(mode_frame, text="YouTube", variable=self.mode, value="youtube", command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg).pack(side=tk.LEFT)
        tk.Radiobutton(mode_frame, text="Локальные", variable=self.mode, value="local", command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg).pack(side=tk.LEFT, padx=10)

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

        self.local_search_frame = tk.Frame(right, bg=bg)
        tk.Label(self.local_search_frame, text="🔍 Поиск в локальных", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        self.local_search_entry = tk.Entry(self.local_search_frame, bg=entry_bg, fg=fg, insertbackground=fg, relief=tk.FLAT)
        self.local_search_entry.pack(fill=tk.X, ipady=4, pady=5)
        self.local_search_entry.bind("<KeyRelease>", self._filter_local_files)

        status_frame = tk.Frame(right, bg=bg)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_lbl = tk.Label(status_frame, text="Готов", fg="#888", bg=bg)
        self.status_lbl.pack(side=tk.LEFT)
        self.zapret_status = tk.Label(status_frame, text="", fg=accent, bg=bg)
        self.zapret_status.pack(side=tk.RIGHT)
        self.update_zapret_status()

        tk.Label(right, text="🎵 Список", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(10, 0))
        self.results_list = tk.Listbox(right, bg=list_bg, fg=fg, relief=tk.FLAT, selectbackground=accent, selectforeground="#000", height=8, width=30)
        self.results_list.pack(fill=tk.BOTH, expand=True, pady=5)
        self.results_list.bind('<Double-1>', lambda e: self.play_selected())
        self.results_list.bind('<<ListboxSelect>>', self._on_list_select)

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

        progress_frame = tk.Frame(right, bg=bg)
        progress_frame.pack(fill=tk.X, pady=5)
        self.progress_scale = tk.Scale(progress_frame, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.progress_var, command=self.on_progress_move, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0)
        self.progress_scale.pack(fill=tk.X)
        self.progress_scale.bind("<ButtonRelease-1>", self.on_progress_release)

        vol_frame = tk.Frame(right, bg=bg)
        vol_frame.pack(fill=tk.X, pady=5)
        tk.Label(vol_frame, text="🔊 Громкость", fg=fg, bg=bg).pack(anchor=tk.W)
        self.vol_scale = tk.Scale(vol_frame, from_=0, to=200, orient=tk.HORIZONTAL, command=self.on_volume_change, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0)
        self.vol_scale.set(100)
        self.vol_scale.pack(fill=tk.X)

        pitch_frame = tk.Frame(right, bg=bg)
        pitch_frame.pack(fill=tk.X, pady=5)
        tk.Label(pitch_frame, text="🎚 Тональность", fg=fg, bg=bg).pack(anchor=tk.W)
        self.pitch_scale = tk.Scale(pitch_frame, from_=-6, to=6, orient=tk.HORIZONTAL, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0, resolution=1)
        self.pitch_scale.pack(fill=tk.X)
        self.pitch_scale.bind("<ButtonRelease-1>", self.on_pitch_release)

        self.on_mode_change()

    def _build_settings_ui(self):
        bg, fg, accent = "#0a0a0a", "#e0e0e0", "#00cc66"
        entry_bg = "#1a1a1a"
        frame = tk.Frame(self.settings_frame, bg=bg, padx=20, pady=20)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="Стратегия обхода (tpws):", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
        self.strategy_var = tk.StringVar()
        strategies = {
            "midsld + oob": ["--split-pos=midsld", "--oob"],
            "disorder + split 1": ["--disorder", "--split-pos=1"],
            "disorder + host + split 1": ["--disorder", "--split-pos=host", "--split-pos=1"],
            "split 1 + oob": ["--split-pos=1", "--oob"]
        }
        self.strategy_combo = ttk.Combobox(frame, textvariable=self.strategy_var, values=list(strategies.keys()), state="readonly")
        self.strategy_combo.pack(fill=tk.X, pady=(0, 15))
        
        curr_strategy = self.config.get("strategy", ["--split-pos=midsld", "--oob"])
        for k, v in strategies.items():
            if v == curr_strategy:
                self.strategy_var.set(k)
                break
        self.strategies_map = strategies

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

        self.extended_search_var = tk.BooleanVar(value=self.config.get("extended_search", True))
        tk.Checkbutton(frame, text="🔎 Расширенный поиск", 
                       variable=self.extended_search_var, 
                       bg=bg, fg=fg, selectcolor=entry_bg, activebackground=bg, activeforeground=fg).pack(anchor=tk.W, pady=(0, 15))

        self.use_cookies_var = tk.BooleanVar(value=self.config.get("use_cookies", False))
        self.cookies_check = tk.Checkbutton(
            frame, text="🔓 Обход ограничений YouTube (включить куки)", 
            variable=self.use_cookies_var, command=self._toggle_cookie_options,
            bg=bg, fg=fg, selectcolor=entry_bg, activebackground=bg, activeforeground=fg,
            font=("Segoe UI", 10, "bold"))
        self.cookies_check.pack(anchor=tk.W, pady=(0, 10))

        self.cookie_options_frame = tk.Frame(frame, bg=bg)
        
        tk.Label(self.cookie_options_frame, text="Источник куки:", fg=fg, bg=bg).pack(anchor=tk.W, pady=(0, 5))
        self.cookie_var = tk.StringVar()
        cookie_options = {
            "Chrome (авто)": "chrome",
            "Safari (авто)": "safari",
            "Firefox (авто)": "firefox",
            "Файл cookies.txt": "file"
        }
        self.cookie_combo = ttk.Combobox(self.cookie_options_frame, textvariable=self.cookie_var, values=list(cookie_options.keys()), state="readonly")
        self.cookie_combo.pack(fill=tk.X, pady=(0, 5))
        
        curr_cookie = self.config.get("cookie_source", "chrome")
        for k, v in cookie_options.items():
            if v == curr_cookie:
                self.cookie_var.set(k)
                break
        self.cookie_options_map = cookie_options

        self.use_pot_var = tk.BooleanVar(value=self.config.get("use_pot", False))
        self.pot_check = tk.Checkbutton(
            self.cookie_options_frame, 
            text="🔞 Режим 18+ (web_creator + POT сервер)", 
            variable=self.use_pot_var,
            bg=bg, fg=fg, selectcolor=entry_bg, activebackground=bg, activeforeground=fg)
        self.pot_check.pack(anchor=tk.W, pady=(0, 15))

        if self.use_cookies_var.get():
            self.cookie_options_frame.pack(fill=tk.X, pady=(0, 15))

        tk.Label(frame, text="Папка для скачивания:", fg=fg, bg=bg, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
        folder_frame = tk.Frame(frame, bg=bg)
        folder_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.folder_var = tk.StringVar(value=self.download_folder)
        self.folder_entry = tk.Entry(folder_frame, textvariable=self.folder_var, bg=entry_bg, fg=fg, insertbackground=fg, relief=tk.FLAT)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        tk.Button(folder_frame, text="...", bg="#555", fg=fg, command=self._browse_folder, width=3).pack(side=tk.RIGHT, padx=(5, 0))

        # Кнопки управления
        action_frame = tk.Frame(frame, bg=bg)
        action_frame.pack(fill=tk.X, pady=(10, 5))

        self.restart_zapret_btn = tk.Button(action_frame, text="🔄 Проверить tpws", bg=accent, fg="#000", font=("Segoe UI", 9, "bold"), relief=tk.FLAT, command=self.restart_zapret)
        self.restart_zapret_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))

        self.projector_btn = tk.Button(action_frame, text="📺 На проектор", bg="#555", fg=fg, font=("Segoe UI", 9, "bold"), relief=tk.FLAT, command=self._toggle_projector)
        self.projector_btn.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(3, 0))

        btn_frame = tk.Frame(frame, bg=bg)
        btn_frame.pack(fill=tk.X, pady=(10, 20))
        
        tk.Button(btn_frame, text="💾 Сохранить", bg="#00cc66", fg="#000", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, command=self._save_settings).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        tk.Button(btn_frame, text="🗑 Сбросить настройки", bg="#cc3333", fg="#fff", font=("Segoe UI", 10, "bold"), relief=tk.FLAT, command=self._reset_settings).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(5, 0))

        tk.Label(frame, text="Автор: SteepCanyon", fg="#888", bg=bg).pack(anchor=tk.W)
        tk.Label(frame, text=f"Версия: {APP_VERSION}", fg="#888", bg=bg).pack(anchor=tk.W)
        tk.Button(frame, text="🌐 GitHub", bg="#333", fg=fg, relief=tk.FLAT, command=lambda: webbrowser.open(f"https://github.com/{GITHUB_REPO}")).pack(anchor=tk.W, pady=(5, 0))

    def _toggle_cookie_options(self):
        if self.use_cookies_var.get():
            self.cookie_options_frame.pack(fill=tk.X, pady=(0, 15), after=self.cookies_check)
        else:
            self.cookie_options_frame.pack_forget()

    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get())
        if folder:
            self.folder_var.set(folder)

    def _reset_settings(self):
        if not messagebox.askyesno("Сброс", "Сбросить все настройки до дефолтных?\n(Куки и режим 18+ будут выключены)"):
            return
        try:
            if os.path.exists(self.config_file):
                os.remove(self.config_file)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось удалить конфиг: {e}")
            return
            
        self.config = self._load_config()
        
        # Обновляем UI
        self.use_cookies_var.set(self.config.get("use_cookies", False))
        self.use_pot_var.set(self.config.get("use_pot", False))
        self.extended_search_var.set(self.config.get("extended_search", True))
        
        curr_strategy = self.config.get("strategy")
        for k, v in self.strategies_map.items():
            if v == curr_strategy:
                self.strategy_var.set(k)
                break
                
        if IS_MAC:
            bundled_tpws = resource_path(os.path.join("zapret", "tpws", "tpws"))
            self.tpws_process.shutdown()
            self.tpws_process = TpwsProcess(bundled_tpws, port=TPWS_PORT, strategy=self.config["strategy"])
            if self.tpws_process.start(): self.proxy_url = TPWS_PROXY
            else: self.proxy_url = None
            self.update_zapret_status()

        self._toggle_cookie_options()
        messagebox.showinfo("Готово", "Настройки сброшены! Попробуй поиск и скачивание снова.")

    def _save_settings(self):
        old_strategy = self.config.get("strategy")
        old_folder = self.download_folder

        self.config["use_cookies"] = self.use_cookies_var.get()
        self.config["use_pot"] = self.use_pot_var.get()
        self.config["strategy"] = self.strategies_map.get(self.strategy_var.get(), ["--split-pos=midsld", "--oob"])
        self.config["format"] = self.formats_map.get(self.format_var.get(), "bv*+ba/b")
        self.config["extended_search"] = self.extended_search_var.get()
        self.config["cookie_source"] = self.cookie_options_map.get(self.cookie_var.get(), "chrome")
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
        except Exception: pass

    def _on_root_unmap(self, event=None):
        if event and event.widget is not self.root: return
        if hasattr(self, 'video_window') and self.video_window.winfo_exists(): self.video_window.withdraw()

    def _on_root_map(self, event=None):
        if event and event.widget is not self.root: return
        if hasattr(self, 'video_window') and self.video_window.winfo_exists():
            self.video_window.deiconify()
            self.root.after(50, self._position_video_window)

    def _on_list_select(self, event=None):
        sel = self.results_list.curselection()
        if sel and sel[0] < len(self.last_results):
            self.status_lbl.config(text=self.last_results[sel[0]][0][:110], fg="#00cc66")

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

    def _progress_hook(self, d):
        if d['status'] == 'downloading':
            self.log_to_ui(f"📥 {d.get('_percent_str', '0%').strip()} | {d.get('_speed_str', 'N/A').strip()}")
        elif d['status'] == 'finished':
            self.log_to_ui("🔗 Завершено, склеиваем...")

    def _download(self, video_id, auto_play):
        url = f"https://youtu.be/{video_id}"
        outtmpl = os.path.join(self.temp_dir, '%(title)s.%(ext)s')
        
        for f in os.listdir(self.temp_dir):
            try: os.remove(os.path.join(self.temp_dir, f))
            except Exception: pass

        fmt = self.config.get("format", "bv*+ba/b")
        use_pot = self.config.get("use_pot", False)
        use_cookies = self.config.get("use_cookies", False)
        
        ydl_opts = {
            'quiet': True,
            'format': fmt,
            'outtmpl': outtmpl,
            'nopart': True,
            'progress_hooks': [self._progress_hook],
        }
        
        if use_cookies:
            cookie_opt = self._get_cookie_opt()
            if cookie_opt:
                ydl_opts.update(cookie_opt)
                self.log_to_ui(f"🍪 Куки: {list(cookie_opt.keys())[0]}")
        
        if use_pot:
            self.log_to_ui("🔞 Режим 18+ (POT)")
            self._ensure_pot_server()
            ydl_opts.update({
                'remote_components': ['ejs:github'],
                'extractor_args': {'youtube': {'player_client': ['web_creator']}},
                'source_address': '0.0.0.0',
            })
        else:
            self.log_to_ui("🟢 Обычный режим")
            ydl_opts.update({
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
            })

        if self.proxy_url:
            ydl_opts['proxy'] = self.proxy_url.replace('socks5h://', 'socks5://')

        try:
            self.log_to_ui(f"📥 Запуск скачивания: {video_id}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_path = None
                if info.get('requested_downloads'):
                    temp_path = info['requested_downloads'][0].get('filepath')
                if not temp_path or not os.path.exists(temp_path):
                    candidates = [os.path.join(self.temp_dir, f) for f in os.listdir(self.temp_dir) if f.endswith(('.mp4', '.mkv', '.webm'))]
                    if candidates: temp_path = max(candidates, key=os.path.getsize)
                if not temp_path or not os.path.exists(temp_path):
                    raise Exception("Файл не найден")
                
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
            self.log_to_ui(f"❌ ОШИБКА: {e}")
            self.root.after(0, lambda err=str(e): self.status_lbl.config(text=f"Ошибка: {err}", fg="red"))

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
            self.log_to_ui(f"🔍 Поиск: {query}")
            
            if self.config.get("extended_search", True):
                search_query = f"{query} караоке karaoke минус instrumental"
            else:
                search_query = f"{query} караоке"
            
            opts = {
                'quiet': True, 
                'extract_flat': True, 
                'no_warnings': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
            }
            if self.proxy_url: 
                opts['proxy'] = self.proxy_url.replace('socks5h://', 'socks5://')
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch15:{search_query}", download=False)
                results = []
                if info and 'entries' in info:
                    for e in info['entries']:
                        if e.get('id') and e.get('title'):
                            results.append((e['title'][:45], e['id']))
                            
            self.log_to_ui(f"✅ Найдено: {len(results)}")
            self.root.after(0, lambda: self._update_results(results))
                
        except Exception as e: 
            self.log_to_ui(f"❌ Ошибка поиска: {e}")
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
        self.zapret_status.config(text="tpws ON" if self.proxy_url else "tpws OFF")

    def restart_zapret(self):
        if self.tpws_process:
            self.tpws_process.shutdown()
            if self.tpws_process.start(): self.proxy_url = TPWS_PROXY
            else: self.proxy_url = None
        self.update_zapret_status()

    def on_closing(self):
        self.stop_progress.set()
        self.stop()
        
        if self.tpws_process: 
            self.tpws_process.shutdown()
        
        try:
            subprocess.run(["killall", "tpws"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["killall", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["killall", "node"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        try: 
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception: 
            pass
            
        time.sleep(0.5)
        
        try:
            self.root.destroy()
        except Exception:
            pass
            
        os._exit(0)

if __name__ == "__main__":
    root = tk.Tk()
    app = KaraokeApp(root)
    root.mainloop()