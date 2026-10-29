#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Караоке-плеер для macOS.
- Видео — в отдельном Toplevel-окне поверх левой области (обход ограничения Tk/NSView).
- Обход блокировок — внешний zapret-macos-easy (tpws на 127.0.0.1:9876).
- Pitch shift (тональность) — через ffmpeg, требует ffmpeg в PATH.
"""

import os
import sys
import platform
import socket
import ctypes
import json

IS_MAC = platform.system() == "Darwin"

if IS_MAC:
    for _vlc_candidate in (
        "/Applications/VLC.app/Contents/MacOS",
        "/Applications/MacPorts/VLC.app/Contents/MacOS",
    ):
        if os.path.exists(f"{_vlc_candidate}/lib/libvlc.dylib"):
            os.environ["DYLD_LIBRARY_PATH"] = f"{_vlc_candidate}/lib"
            os.environ.setdefault("VLC_PLUGIN_PATH", f"{_vlc_candidate}/plugins")
            break

import tkinter as tk
from tkinter import messagebox
import requests
import webbrowser
import threading
import re
import atexit
import shutil
import time
import tempfile
import subprocess

try:
    import yt_dlp
except ImportError:
    print("Нет модуля yt-dlp. Установите: /opt/local/bin/python3.12 -m pip install yt-dlp")
    sys.exit(1)

try:
    import vlc
except ImportError:
    print("Нет модуля python-vlc. Установите: /opt/local/bin/python3.12 -m pip install python-vlc")
    sys.exit(1)


TPWS_PROXY = "socks5h://127.0.0.1:9876"

APP_VERSION = "1.0.0"
GITHUB_REPO = "aokraynov-commits/karaoke.py"  # <-- замените на ваш репозиторий

TPWS_PORT = 9876


def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def find_ffmpeg():
    bundled = resource_path(os.path.join("ffmpeg", "ffmpeg"))
    if os.path.exists(bundled):
        return bundled
    for p in ("/opt/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg") or "ffmpeg"


def _load_libtk():
    """Загружает libtk по стандартным путям (fallback, если в процессе ещё нет Tk)."""
    libtk_name = f"libtk{tk.TkVersion}.dylib"
    candidates = []
    # 1) Внутри .app (PyInstaller)
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, libtk_name))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.abspath(
            os.path.join(exe_dir, "..", "Frameworks", libtk_name)))
    # 2) Python framework
    prefix = getattr(sys, "base_prefix", sys.prefix)
    candidates.append(os.path.join(prefix, "lib", libtk_name))
    # 3) MacPorts / Homebrew
    candidates += [
        f"/opt/local/lib/{libtk_name}",
        f"/opt/homebrew/lib/{libtk_name}",
        f"/usr/local/lib/{libtk_name}",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ctypes.cdll.LoadLibrary(path)
            except Exception:
                continue
    return None


_libtk_cache = None

def _get_libtk_handle():
    """Возвращает handle на libtk БЕЗ повторной загрузки, если она уже в процессе."""
    global _libtk_cache
    if _libtk_cache is not None:
        return _libtk_cache
    # 1) Пытаемся взять символ из уже загруженной libtk в текущем процессе.
    #    Tkinter._tkinter всегда линкуется с libtk, она уже в памяти.
    try:
        proc = ctypes.CDLL(None)
        _ = proc.TkMacOSXGetRootControl  # проверка: символ доступен?
        _libtk_cache = proc
        return _libtk_cache
    except AttributeError:
        pass
    # 2) Fallback: явная загрузка (dev-режим, не PyInstaller)
    _libtk_cache = _load_libtk()
    return _libtk_cache


def get_nsview(tk_widget):
    if not IS_MAC:
        return None
    handle = _get_libtk_handle()
    if handle is None:
        return None
    try:
        f = handle.TkMacOSXGetRootControl
    except AttributeError:
        return None
    f.restype = ctypes.c_void_p
    f.argtypes = (ctypes.c_void_p,)
    try:
        return f(ctypes.c_void_p(tk_widget.winfo_id()))
    except Exception as e:
        print(f"[NSView] {e}")
        return None


class TpwsProcess:
    """Запускает встроенный tpws на 127.0.0.1:9876."""
    def __init__(self, binary_path, port=9876):
        self.binary_path = binary_path
        self.port = port
        self.process = None
        self.ready = False

    def is_available(self):
        return os.path.exists(self.binary_path) and os.access(self.binary_path, os.X_OK)

    def start(self):
        if self.ready:
            return True
        if not self.is_available():
            return False
        try:
            cmd = [
                self.binary_path,
                "--socks",
                "--bind-addr=127.0.0.1",
                f"--port={self.port}",
                "--split-pos=midsld",
                "--oob",
            ]
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        self.ready = False


class KaraokeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Караоке-плеер")
        self.root.geometry("1200x750+100+100")
        self.root.configure(bg="#0a0a0a")

        if not os.path.exists("/Applications/VLC.app") and not os.path.exists("/Applications/MacPorts/VLC.app"):
            messagebox.showerror("VLC не найден", "Установите VLC:\n    sudo port install VLC")
            sys.exit(1)

        self.instance = vlc.Instance(
            "--http-user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        )
        self.player = self.instance.media_player_new()

        self.current_url = None
        self.current_original_path = None
        self.current_pitch = 0
        self.projector_mode = False
        self.current_video_id = None
        self.last_results = []
        self.mode = tk.StringVar(value="youtube")

        # Всегда одна папка — независимо от dev/.app режима
        base = os.path.join(os.path.expanduser("~"), "Music", "KaraokePlayer")
        self.download_folder = os.path.join(base, "KaraokeDownloads")
        print(f"[folder] download_folder = {self.download_folder}")
        os.makedirs(self.download_folder, exist_ok=True)

        self.ffmpeg_path = find_ffmpeg()
        self.temp_dir = tempfile.mkdtemp(prefix="karaoke_")
        self.current_temp_youtube = None

        # Кэш video_id -> локальный путь
        self.cache_file = os.path.join(self.download_folder, ".cache.json")
        self.video_cache = self._load_cache()
        self._clean_cache()  # удалить дубликаты и мёртвые записи при старте
        # Кэш pitch: (path, semitones) -> temp-путь
        self.pitch_cache = {}

        self.proxy_url = None
        self._check_tpws()
        # Фоновая проверка обновлений (не блокирует запуск)
        threading.Thread(target=self._check_for_updates, daemon=True).start()

        self.progress_var = tk.DoubleVar(value=0.0)
        self.stop_progress = threading.Event()
        self.progress_thread = threading.Thread(target=self.update_progress, daemon=True)
        self.progress_thread.start()

        self.search_thread = None
        self.stop_search = threading.Event()

        # Lock и generation для безопасных VLC-операций
        self._play_lock = threading.Lock()
        self._play_generation = 0

        self.create_widgets()
        self._create_video_window()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        try:
            self.root.createcommand("::tk::mac::Quit", self.on_closing)
        except Exception:
            pass

    # ---------- Cache ----------
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

    # ---------- tpws ----------

    def _check_for_updates(self):
        """Фоновая проверка обновлений через GitHub Releases."""
        if GITHUB_REPO == "yourname/karaoke-player":
            return  # репозиторий не настроен
        try:
            proxies = None
            if self.proxy_url:
                proxies = {"http": self.proxy_url, "https": self.proxy_url}
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                proxies=proxies, timeout=10
            )
            if r.status_code != 200:
                return
            data = r.json()
            latest = data.get("tag_name", "").lstrip("v")
            if not latest:
                return
            # Простое сравнение версий (1.0.0 < 1.0.1)
            def ver_tuple(v):
                return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
            if ver_tuple(latest) > ver_tuple(APP_VERSION):
                url = data.get("html_url", "")
                self.root.after(0, lambda u=url, lv=latest: self._show_update_notice(u, lv))
        except Exception as e:
            print(f"[update] Ошибка проверки: {e}")

    def _show_update_notice(self, url, latest_version):
        """Показывает ненавязчивое уведомление в статус-баре."""
        try:
            self.status_lbl.config(
                text=f"Доступна версия {latest_version} → нажмите для скачивания",
                fg="#ffaa00"
            )
            # При клике на статус открывается страница релиза
            self.status_lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
        except Exception:
            pass

    def _check_tpws(self):
        # 1) Уже запущенный tpws (zapret-macos-easy)
        try:
            with socket.create_connection(("127.0.0.1", TPWS_PORT), timeout=1.0):
                self.proxy_url = TPWS_PROXY
                self.tpws_process = None
                return True
        except OSError:
            pass
        # 2) Пробуем запустить встроенный tpws
        bundled = resource_path(os.path.join("zapret", "tpws", "tpws"))
        self.tpws_process = TpwsProcess(bundled, port=TPWS_PORT)
        if self.tpws_process.start():
            self.proxy_url = TPWS_PROXY
            return True
        self.proxy_url = None
        return False

    # ---------- UI ----------
    def create_widgets(self):
        bg = "#0a0a0a"
        fg = "#e0e0e0"
        accent = "#00cc66"
        entry_bg = "#1a1a1a"
        list_bg = "#111111"

        main = tk.Frame(self.root, bg=bg)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.video_placeholder = tk.Frame(main, bg="black")
        self.video_placeholder.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        tk.Label(self.video_placeholder, text="🎤", fg="#1a1a1a", bg="black",
                 font=("Segoe UI", 96)).place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.placeholder_hint = tk.Label(
            self.video_placeholder, text="Видео появится здесь",
            fg="#333", bg="black", font=("Segoe UI", 12))
        self.placeholder_hint.place(relx=0.5, rely=0.62, anchor=tk.CENTER)

        right = tk.Frame(main, bg=bg, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        mode_frame = tk.Frame(right, bg=bg)
        mode_frame.pack(fill=tk.X, pady=(0, 5))
        tk.Radiobutton(mode_frame, text="YouTube", variable=self.mode, value="youtube",
                       command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg,
                       activebackground=bg, activeforeground=fg,
                       font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Radiobutton(mode_frame, text="Локальные файлы", variable=self.mode, value="local",
                       command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg,
                       activebackground=bg, activeforeground=fg,
                       font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=10)

        self.search_frame = tk.Frame(right, bg=bg)
        tk.Label(self.search_frame, text="🔍 Поиск", fg=fg, bg=bg,
                 font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        self.search_entry = tk.Entry(self.search_frame, bg=entry_bg, fg=fg,
                                     font=("Segoe UI", 11), relief=tk.FLAT,
                                     insertbackground=fg)
        self.search_entry.pack(fill=tk.X, ipady=4, pady=5)
        self.search_entry.bind('<Return>', lambda e: self.do_search())
        btn_frame = tk.Frame(self.search_frame, bg=bg)
        btn_frame.pack(fill=tk.X)
        self.search_btn = tk.Button(btn_frame, text="Найти", bg=accent, fg="#000",
                                    font=("Segoe UI", 10, "bold"), relief=tk.FLAT,
                                    command=self.do_search)
        self.search_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.stop_btn = tk.Button(btn_frame, text="⏹ Стоп", bg="#555", fg=fg,
                                  font=("Segoe UI", 10, "bold"), relief=tk.FLAT,
                                  command=self.stop_search_command, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        status_frame = tk.Frame(right, bg=bg)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_lbl = tk.Label(status_frame, text="Готов", fg="#888", bg=bg,
                                   font=("Segoe UI", 9))
        self.status_lbl.pack(side=tk.LEFT)
        self.zapret_status = tk.Label(status_frame, text="", fg=accent, bg=bg,
                                      font=("Segoe UI", 9))
        self.zapret_status.pack(side=tk.RIGHT)
        self.update_zapret_status()

        tk.Label(right, text="🎵 Список", fg=fg, bg=bg,
                 font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(10, 0))
        self.results_list = tk.Listbox(right, bg=list_bg, fg=fg,
                                       font=("Menlo", 10), relief=tk.FLAT,
                                       selectbackground=accent, selectforeground="#000",
                                       height=10, width=30, highlightthickness=0)
        self.results_list.pack(fill=tk.BOTH, expand=True, pady=5)
        self.results_list.bind('<Double-1>', lambda e: self.play_selected())
        self.results_list.bind('<<ListboxSelect>>', self._on_list_select)

        # Кнопки управления
        ctrl_frame = tk.Frame(right, bg=bg)
        ctrl_frame.pack(fill=tk.X, pady=5)
        # Порядок: 📥 Загрузить | ▶/⏸ | ⏹ | 🗑 (только в локальном режиме)
        self.download_btn = tk.Button(ctrl_frame, text="📥", bg=accent, fg="#000",
                                      font=("Segoe UI", 12, "bold"), relief=tk.FLAT,
                                      command=self.download_selected, width=4)
        self.download_btn.grid(row=0, column=0, padx=2)
        self.play_pause_btn = tk.Button(ctrl_frame, text="▶", bg=accent, fg="#000",
                                        font=("Segoe UI", 12, "bold"), relief=tk.FLAT,
                                        command=self.toggle_play_pause, width=4)
        self.play_pause_btn.grid(row=0, column=1, padx=2)
        self.stop_play_btn = tk.Button(ctrl_frame, text="⏹", bg=accent, fg="#000",
                                       font=("Segoe UI", 12, "bold"), relief=tk.FLAT,
                                       command=self.stop, width=4)
        self.stop_play_btn.grid(row=0, column=2, padx=2)
        self.delete_btn = tk.Button(ctrl_frame, text="🗑", bg="#cc3333", fg="#fff",
                                    font=("Segoe UI", 12, "bold"), relief=tk.FLAT,
                                    command=self.delete_selected_local, width=4)
        self.delete_btn.grid(row=0, column=3, padx=2)
        self.delete_btn.grid_remove()

        # Прогресс
        progress_frame = tk.Frame(right, bg=bg)
        progress_frame.pack(fill=tk.X, pady=5)
        tk.Label(progress_frame, text="Прогресс", fg=fg, bg=bg,
                 font=("Segoe UI", 9)).pack(anchor=tk.W)
        self.progress_scale = tk.Scale(progress_frame, from_=0, to=100,
                                       orient=tk.HORIZONTAL, length=200,
                                       variable=self.progress_var,
                                       command=self.on_progress_move,
                                       bg=bg, fg=fg, troughcolor="#333",
                                       highlightthickness=0)
        self.progress_scale.pack(fill=tk.X)
        self.progress_scale.bind("<ButtonRelease-1>", self.on_progress_release)

        # Громкость
        vol_frame = tk.Frame(right, bg=bg)
        vol_frame.pack(fill=tk.X, pady=5)
        tk.Label(vol_frame, text="🔊 Громкость", fg=fg, bg=bg,
                 font=("Segoe UI", 9)).pack(anchor=tk.W)
        self.vol_scale = tk.Scale(vol_frame, from_=0, to=200, orient=tk.HORIZONTAL,
                                  length=200, command=self.on_volume_change,
                                  bg=bg, fg=fg, troughcolor="#333",
                                  highlightthickness=0)
        self.vol_scale.set(100)
        self.vol_scale.pack(fill=tk.X)
        self.vol_val = tk.Label(vol_frame, text="100%", fg=accent, bg=bg,
                                font=("Segoe UI", 9))
        self.vol_val.pack(anchor=tk.E)

        # Тональность
        pitch_frame = tk.Frame(right, bg=bg)
        pitch_frame.pack(fill=tk.X, pady=5)
        tk.Label(pitch_frame, text="🎚 Тональность (полутона)", fg=fg, bg=bg,
                 font=("Segoe UI", 9)).pack(anchor=tk.W)
        self.pitch_scale = tk.Scale(pitch_frame, from_=-6, to=6, orient=tk.HORIZONTAL,
                                    length=200, bg=bg, fg=fg, troughcolor="#333",
                                    highlightthickness=0, resolution=1)
        self.pitch_scale.set(0)
        self.pitch_scale.pack(fill=tk.X)
        self.pitch_val = tk.Label(pitch_frame, text="0", fg=accent, bg=bg,
                                  font=("Segoe UI", 9))
        self.pitch_val.pack(anchor=tk.E)
        self.pitch_scale.bind("<ButtonRelease-1>", self.on_pitch_release)

        # Нижние кнопки
        self.restart_zapret_btn = tk.Button(right, text="🔄 Проверить tpws",
                                            bg=accent, fg="#000",
                                            font=("Segoe UI", 9, "bold"),
                                            relief=tk.FLAT, command=self.restart_zapret)
        self.restart_zapret_btn.pack(fill=tk.X, pady=(5, 2))
        self.projector_btn = tk.Button(right, text="📺 На проектор",
                                       bg="#555", fg=fg,
                                       font=("Segoe UI", 9, "bold"),
                                       relief=tk.FLAT, command=self._toggle_projector)
        self.projector_btn.pack(fill=tk.X, pady=(0, 2))
        self.open_folder_btn = tk.Button(right, text="📂 Открыть папку библиотеки",
                                         bg="#333", fg=fg,
                                         font=("Segoe UI", 9, "bold"),
                                         relief=tk.FLAT, command=self._open_folder)
        self.open_folder_btn.pack(fill=tk.X, pady=(0, 5))

        self.on_mode_change()

    def _open_folder(self):
        try:
            subprocess.run(["open", self.download_folder], check=False)
        except Exception:
            pass

    # ---------- Видео-окно ----------
    def _create_video_window(self):
        self.video_window = tk.Toplevel(self.root)
        self.video_window.configure(bg="black")
        # Всегда с рамкой — иначе окно перехватывает клики и закрывает крестики
        self.video_window.title("Karaoke Video")
        self.video_window.attributes("-topmost", False)

        self.video_canvas = tk.Canvas(self.video_window, bg="black",
                                      highlightthickness=0, bd=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)

        self.root.bind("<Configure>", self._on_root_configure)
        self.root.bind("<FocusIn>", lambda e: self._raise_video())
        self.root.bind("<Button-1>", lambda e: self._raise_video(), add="+")
        self.root.bind("<Unmap>", self._on_root_unmap)
        self.root.bind("<Map>", self._on_root_map)
        self.root.after(200, self._position_video_window)
        self.root.after(500, self._attach_vlc)
        self._keep_video_on_top()

    def _on_root_unmap(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        try:
            if hasattr(self, 'video_window') and self.video_window.winfo_exists():
                self.video_window.attributes("-topmost", False)
                self.video_window.withdraw()
        except Exception:
            pass

    def _on_root_map(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        try:
            if hasattr(self, 'video_window') and self.video_window.winfo_exists():
                self.video_window.deiconify()
                self.video_window.attributes("-topmost", True)
                self.root.after(50, self._position_video_window)
                self.root.after(100, self._raise_video)
        except Exception:
            pass

    def _is_maximized(self):
        """True, если окно развёрнуто (zoomed) или в fullscreen."""
        try:
            if self.root.attributes("-fullscreen"):
                return True
            if self.root.state() == "zoomed":
                return True
        except Exception:
            pass
        return False

    def _raise_video(self):
        try:
            if not hasattr(self, 'video_window') or not self.video_window.winfo_exists():
                return
            if self._is_maximized() or getattr(self, 'projector_mode', False):
                self.video_window.attributes("-topmost", False)
                return
            self.video_window.attributes("-topmost", True)
            self.video_window.lift()
        except Exception:
            pass

    def _keep_video_on_top(self):
        self._raise_video()
        try:
            self.root.after(400, self._keep_video_on_top)
        except Exception:
            pass

    def _on_root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self.root.after(50, self._position_video_window)
        # Если сменился maximized — обновить topmost
        self.root.after(100, self._sync_video_topmost)

    def _sync_video_topmost(self):
        self._raise_video()

    def _get_screens(self):
        """Список экранов через AppKit (pyobjc). Возвращает [{x,y,w,h}, ...] или None."""
        try:
            from AppKit import NSScreen
            result = []
            for s in NSScreen.screens():
                f = s.frame()
                result.append({
                    'x': int(f.origin.x),
                    'y': int(f.origin.y),
                    'w': int(f.size.width),
                    'h': int(f.size.height),
                })
            return result
        except ImportError:
            return None
        except Exception:
            return None

    def _toggle_projector(self):
        if getattr(self, 'projector_mode', False):
            self._return_from_projector()
        else:
            self._send_to_projector()

    def _send_to_projector(self):
        screens = self._get_screens()
        if screens is None:
            messagebox.showerror(
                "Нужен pyobjc",
                "Для работы с проектором установите:\n\n"
                "sudo port install py312-pyobjc"
            )
            return
        if len(screens) < 2:
            messagebox.showinfo(
                "Второй экран не найден",
                "Подключите проектор или второй монитор и попробуйте снова.\n\n"
                f"Сейчас подключено экранов: {len(screens)}"
            )
            return
        # Определяем главный экран (обычно origin = 0,0)
        main_idx = 0
        for i, s in enumerate(screens):
            if s['x'] == 0 and s['y'] == 0:
                main_idx = i
                break
        # Второй — любой, кроме главного
        target = None
        for i, s in enumerate(screens):
            if i != main_idx:
                target = s
                break
        if target is None:
            return
        try:
            self.video_window.attributes("-topmost", False)
            self.video_window.geometry(
                f"{target['w']}x{target['h']}+{target['x']}+{target['y']}"
            )
            self.video_window.lift()
            self.projector_mode = True
            try:
                self.projector_btn.config(text="📺 Вернуть на главный", bg="#aa4444")
            except Exception:
                pass
            self.status_lbl.config(text="Видео на проекторе", fg="#00cc66")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _return_from_projector(self):
        self.projector_mode = False
        try:
            self.projector_btn.config(text="📺 На проектор", bg="#555")
        except Exception:
            pass
        try:
            self.video_window.attributes("-topmost", False)
            self._position_video_window()
            self.root.after(100, self._raise_video)
            self.status_lbl.config(text="Видео на главном экране", fg="#888")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _position_video_window(self):
        if getattr(self, 'projector_mode', False):
            return
        try:
            if not hasattr(self, 'video_window') or not self.video_window.winfo_exists():
                return
            self.root.update_idletasks()
            x = self.video_placeholder.winfo_rootx()
            y = self.video_placeholder.winfo_rooty()
            w = self.video_placeholder.winfo_width()
            h = self.video_placeholder.winfo_height()
            # При maximized гарантируем, что видео не перекрывает заголовок macOS
            if self._is_maximized():
                y = 30  # высота заголовка macOS + запас
                screen_h = self.root.winfo_screenheight()
                h = screen_h - 35  # 30 сверху + 5 снизу
            if w > 10 and h > 10:
                self.video_window.geometry(f"{w}x{h}+{x}+{y}")
                if not self._is_maximized():
                    self.video_window.lift()
        except Exception as e:
            print(f"[video] position error: {e}")

    def _attach_vlc(self):
        if not IS_MAC:
            return
        nsview = get_nsview(self.video_canvas)
        if nsview:
            try:
                self.player.set_nsobject(nsview)
                print(f"[VLC] NSView привязан: {nsview}")
            except Exception as e:
                print(f"[VLC] set_nsobject не удался: {e}")
        else:
            print("[VLC] NSView не получен")

    # ---------- Play/Pause (не перезагружает трек) ----------
    def toggle_play_pause(self):
        try:
            if self.player.is_playing():
                threading.Thread(target=self._do_pause, daemon=True).start()
            else:
                sel = self.results_list.curselection()
                if sel:
                    self.play_selected()
                elif self.player.get_media() is not None:
                    threading.Thread(target=self._do_resume, daemon=True).start()
        except Exception:
            pass

    def _do_pause(self):
        with self._play_lock:
            try:
                self.player.pause()
                self.root.after(0, lambda: self.play_pause_btn.config(text="▶"))
            except Exception:
                pass

    def _do_resume(self):
        with self._play_lock:
            try:
                self.player.play()
                self.root.after(0, lambda: self.play_pause_btn.config(text="⏸"))
            except Exception:
                pass

    def update_progress(self):
        # ВАЖНО: не вызываем progress_var.set() из потока — Tkinter не потокобезопасен!
        while not self.stop_progress.is_set():
            try:
                if self.player.get_length() > 0:
                    pos = self.player.get_position() * 100
                    # Планируем обновление в главном потоке Tk
                    self.root.after(0, lambda p=pos: self._safe_set_progress(p))
            except Exception:
                pass
            time.sleep(0.5)

    def _safe_set_progress(self, pos):
        try:
            self.progress_var.set(pos)
        except Exception:
            pass

    def on_progress_move(self, val):
        pass

    def on_progress_release(self, event):
        try:
            pos = self.progress_var.get() / 100.0
            if self.player.get_length() > 0:
                self.player.set_position(pos)
        except Exception:
            pass

    # ---------- Нумерация ----------
    def _get_next_number(self):
        max_num = 0
        if os.path.exists(self.download_folder):
            for f in os.listdir(self.download_folder):
                m = re.match(r'^(\d+)\s*-\s*', f)
                if m:
                    num = int(m.group(1))
                    if num > max_num:
                        max_num = num
        return max_num + 1

    def _numbered_name(self, original_name):
        return f"{self._get_next_number():02d} - {original_name}"

    # ---------- Режимы ----------
    def on_mode_change(self):
        # Сброс предыдущего списка, чтобы не смешивать YouTube ID и локальные пути
        try:
            self.results_list.delete(0, tk.END)
            self.last_results = []
        except Exception:
            pass
        mode = self.mode.get()
        if mode == "youtube":
            self.search_frame.pack(fill=tk.X, pady=(0, 5))
            self.download_btn.grid()
            self.delete_btn.grid_remove()
        else:
            self.search_frame.pack_forget()
            self.download_btn.grid_remove()
            self.delete_btn.grid()
            self.load_local_files()

    def _truncate(self, text, limit=30):
        if len(text) > limit:
            return text[:limit] + "..."
        return text

    def _on_list_select(self, event=None):
        try:
            sel = self.results_list.curselection()
            if not sel:
                return
            idx = sel[0]
            if idx < len(self.last_results):
                title = self.last_results[idx][0]
                self.status_lbl.config(text=title[:110], fg="#00cc66")
        except Exception:
            pass

    def load_local_files(self):
        self._clean_cache()
        self.results_list.delete(0, tk.END)
        self.last_results = []
        if not os.path.exists(self.download_folder):
            return
        exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv', '.mpg', '.mpeg', '.ts')
        files = [f for f in os.listdir(self.download_folder)
                 if f.lower().endswith(exts) and not f.startswith('.')]
        print(f"[local] found {len(files)} file(s) in {self.download_folder}")
        for _f in sorted(files):
            print(f"[local]   {_f}")
        for f in sorted(files,
                        key=lambda x: os.path.getmtime(os.path.join(self.download_folder, x)),
                        reverse=True):
            self.results_list.insert(tk.END, f"  {self._truncate(f)}")
            self.last_results.append((f, os.path.join(self.download_folder, f)))

    # ---------- Удаление локального файла ----------
    def delete_selected_local(self):
        if self.mode.get() != "local":
            messagebox.showinfo("Только локально", "Удаление доступно только в режиме «Локальные файлы»")
            return
        sel = self.results_list.curselection()
        if not sel:
            messagebox.showwarning("Не выбрано", "Выберите файл для удаления")
            return
        idx = sel[0]
        if idx >= len(self.last_results):
            return
        fname, path = self.last_results[idx]
        if not messagebox.askyesno("Удалить", f"Удалить файл?\n\n{fname}"):
            return
        try:
            if self.current_original_path == path:
                self.stop()
                self.current_original_path = None
            os.remove(path)
            for vid, p in list(self.video_cache.items()):
                if p == path:
                    del self.video_cache[vid]
            self._save_cache()
            self.load_local_files()
            self.status_lbl.config(text=f"Удалено: {fname}", fg="#888")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    # ---------- Резолвер пути ----------
    def _resolve_downloaded_path(self, ydl, info):
        if info.get('requested_downloads'):
            p = info['requested_downloads'][0].get('filepath')
            if p and os.path.exists(p):
                return p
        p = info.get('_filename')
        if p and os.path.exists(p):
            return p
        p = ydl.prepare_filename(info)
        if p and os.path.exists(p):
            return p
        candidates = []
        if os.path.isdir(self.temp_dir):
            for f in os.listdir(self.temp_dir):
                fp = os.path.join(self.temp_dir, f)
                if os.path.isfile(fp) and f.lower().endswith(('.mp4', '.mkv', '.webm', '.m4a')):
                    candidates.append(fp)
        if candidates:
            return max(candidates, key=os.path.getmtime)
        return None

    # ---------- Скачивание ----------
    def download_selected(self):
        if self.mode.get() != "youtube":
            return
        sel = self.results_list.curselection()
        if not sel:
            messagebox.showwarning("Не выбрано", "Выберите видео для сохранения")
            return
        idx = sel[0]
        if idx < len(self.last_results):
            vid = self.last_results[idx][1]
            if not self._is_youtube_id(vid):
                messagebox.showerror(
                    "Ошибка",
                    "В списке не YouTube-трек. Сделайте новый поиск.")
                return
            existing = self._check_already_downloaded(vid)
            if existing:
                self.status_lbl.config(
                    text=f"Уже в библиотеке: {os.path.basename(existing)}", fg="#00cc66")
                return
            self.status_lbl.config(text="Скачивание...", fg="#00cc66")
            self.download_btn.config(state=tk.DISABLED)
            threading.Thread(target=self._download_to_library,
                             args=(vid,), daemon=True).start()

    def _find_video_id_by_path(self, path):
        """Ищет video_id для файла, если он есть в кэше."""
        for vid, p in self.video_cache.items():
            if p == path:
                return vid
        return None

    def _clean_cache(self):
        """Удаляет мёртвые записи и дубликаты (один файл — один video_id)."""
        removed = 0
        paths_seen = {}
        for vid, p in list(self.video_cache.items()):
            if not os.path.exists(p):
                del self.video_cache[vid]
                removed += 1
                continue
            if p in paths_seen:
                del self.video_cache[vid]
                removed += 1
            else:
                paths_seen[p] = vid
        if removed:
            self._save_cache()
            print(f"[cache] очищено {removed} записей (мёртвые + дубликаты)")

    def _find_by_name(self, basename_without_number):
        """Ищет файл в папке, у которого отрезан префикс 'NN - '."""
        if not os.path.isdir(self.download_folder):
            return None
        for f in os.listdir(self.download_folder):
            if f.startswith("."):
                continue
            m = re.match(r'^\d+\s*-\s*(.+)$', f)
            name = m.group(1) if m else f
            if name == basename_without_number:
                return os.path.join(self.download_folder, f)
        return None

    def _check_already_downloaded(self, video_id, ydl_info=None):
        """
        Проверяет, есть ли уже этот трек в библиотеке.
        Только по video_id — без ложных совпадений по имени.
        """
        cached = self.video_cache.get(video_id)
        if cached and os.path.exists(cached):
            return cached
        return None

    def _download_to_library(self, video_id):
        if not self._is_youtube_id(video_id):
            self.root.after(0, lambda: self.status_lbl.config(
                text="Неверный video_id", fg="red"))
            return
        url = f"https://youtu.be/{video_id}"
        ydl_opts = {
            'quiet': True,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': os.path.join(self.temp_dir, '%(title)s.%(ext)s'),
            'merge_output_format': 'mp4',
            'ffmpeg_location': self.ffmpeg_path,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
            },
            'no_warnings': True,
            'proxy': self.proxy_url,
            'extractor_args': {'youtube': {'player_client': ['android', 'web_embedded']}},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Сначала проверяем по метаданным (без скачивания)
                probe_opts = dict(ydl_opts)
                probe_opts['skip_download'] = True
                with yt_dlp.YoutubeDL(probe_opts) as ydl_probe:
                    probe_info = ydl_probe.extract_info(url, download=False)
                existing = self._check_already_downloaded(video_id, probe_info)
                if existing:
                    self.video_cache[video_id] = existing
                    self._save_cache()
                    self.root.after(0, lambda n=os.path.basename(existing):
                                    self.status_lbl.config(
                                        text=f"Уже в библиотеке: {n}", fg="#00cc66"))
                    return

                info = ydl.extract_info(url, download=True)
                temp_path = self._resolve_downloaded_path(ydl, info)
                if not temp_path:
                    raise RuntimeError("Скачано, но файл не найден")

                # Проверка по имени (страховка на случай дубля)
                base_name = os.path.basename(temp_path)
                existing = self._check_already_downloaded(video_id, info)
                if existing:
                    # На всякий случай удаляем скачанный временный
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    self.video_cache[video_id] = existing
                    self._save_cache()
                    self.root.after(0, lambda n=os.path.basename(existing):
                                    self.status_lbl.config(
                                        text=f"Уже в библиотеке: {n}", fg="#00cc66"))
                    return

                new_name = self._numbered_name(base_name)
                dest = os.path.join(self.download_folder, new_name)
                shutil.move(temp_path, dest)
                self.video_cache[video_id] = dest
                self._save_cache()
                self.root.after(0, lambda n=new_name:
                                self.status_lbl.config(text=f"Сохранено: {n}", fg="#00cc66"))
        except Exception as e:
            self.root.after(0, lambda err=str(e):
                            self.status_lbl.config(text=f"Ошибка: {err}", fg="red"))
        finally:
            self.root.after(0, lambda: self.download_btn.config(state=tk.NORMAL))

    # ---------- Воспроизведение ----------
    def _is_youtube_id(self, vid):
        """True, если строка похожа на YouTube video_id (11 символов)."""
        if not isinstance(vid, str):
            return False
        import re as _re
        return bool(_re.match(r'^[A-Za-z0-9_-]{11}$', vid))

    def play_selected(self):
        mode = self.mode.get()
        sel = self.results_list.curselection()
        if not sel:
            messagebox.showwarning("Не выбрано", "Выберите трек")
            return
        idx = sel[0]
        if idx >= len(self.last_results):
            return
        item = self.last_results[idx][1]
        if mode == "youtube":
            # Защита: если это не YouTube ID, а путь — играем как локальный
            if not self._is_youtube_id(item):
                if os.path.exists(item):
                    self._play_local(item)
                else:
                    messagebox.showerror(
                        "Ошибка",
                        "Некорректный трек. Сделайте новый поиск в YouTube.")
                return
            cached = self.video_cache.get(item)
            if cached and os.path.exists(cached):
                self.status_lbl.config(text="Из библиотеки", fg="#00cc66")
                self._play_local(cached)
                return
            self.status_lbl.config(text="Загрузка...", fg="#00cc66")
            threading.Thread(target=self._download_and_play,
                             args=(item,), daemon=True).start()
        else:
            self._play_local(item)

    def _download_and_play(self, video_id):
        if not self._is_youtube_id(video_id):
            self.root.after(0, lambda: self.status_lbl.config(
                text="Неверный video_id", fg="red"))
            return
        url = f"https://youtu.be/{video_id}"
        ydl_opts = {
            'quiet': True,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': os.path.join(self.temp_dir, '%(title)s.%(ext)s'),
            'merge_output_format': 'mp4',
            'ffmpeg_location': self.ffmpeg_path,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
            },
            'no_warnings': True,
            'proxy': self.proxy_url,
            'extractor_args': {'youtube': {'player_client': ['android', 'web_embedded']}},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_path = self._resolve_downloaded_path(ydl, info)
                if not temp_path:
                    raise RuntimeError("Скачано, но файл не найден")
                # Проверка на дубль по имени (страховка)
                existing = self._check_already_downloaded(video_id, info)
                if existing:
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    dest = existing
                else:
                    new_name = self._numbered_name(os.path.basename(temp_path))
                    dest = os.path.join(self.download_folder, new_name)
                    shutil.move(temp_path, dest)
                self.video_cache[video_id] = dest
                self._save_cache()
                duration = info.get('duration', 0)
                self.root.after(0, lambda p=dest: self._play_local(p))
                # Обновляем список локальных
                if self.mode.get() == "local":
                    self.root.after(0, self.load_local_files)
        except Exception as e:
            self.root.after(0, lambda err=str(e):
                            self.status_lbl.config(text=f"Ошибка: {err}", fg="red"))

    def _play_local(self, path):
        if not os.path.exists(path):
            self.status_lbl.config(
                text=f"Файл не найден: {os.path.basename(path)}", fg="red")
            return
        print(f"[PLAY] {os.path.basename(path)}")
        try:
            self.placeholder_hint.place_forget()
        except Exception:
            pass
        self.current_original_path = path
        self.current_pitch = 0
        try:
            self.pitch_scale.set(0)
            self.pitch_val.config(text="0")
        except Exception:
            pass
        # VLC-операции — в фоновом потоке, чтобы не блокировать UI
        self._play_generation += 1
        gen = self._play_generation
        self.play_pause_btn.config(text="⏸")
        self.status_lbl.config(text="▶ Воспроизведение", fg="#00cc66")
        threading.Thread(target=self._do_play, args=(path, gen), daemon=True).start()

    def _do_play(self, path, gen):
        with self._play_lock:
            if gen != self._play_generation:
                return  # отменено — запрошено новое воспроизведение
            try:
                self.player.stop()
                media = self.instance.media_new_path(path)
                self.player.set_media(media)
                self.player.play()
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: self.status_lbl.config(
                    text=f"Ошибка VLC: {err}", fg="red"))
                return
        self.root.after(0, lambda: self.on_volume_change(self.vol_scale.get()))

    # ---------- Pitch shift ----------
    def on_pitch_release(self, event):
        semitones = int(self.pitch_scale.get())
        self.pitch_val.config(text=f"{semitones:+d}" if semitones else "0")
        if not self.current_original_path:
            return
        if semitones == self.current_pitch:
            return
        self.current_pitch = semitones
        threading.Thread(target=self._apply_pitch, args=(semitones,), daemon=True).start()

    def _apply_pitch(self, semitones):
        src = self.current_original_path
        if not src or not os.path.exists(src):
            self.root.after(0, lambda: self.status_lbl.config(
                text="Нет загруженного трека", fg="orange"))
            return

        if semitones == 0:
            self.root.after(0, lambda: self._play_pitched(src, 0))
            return

        key = (src, semitones)
        if key in self.pitch_cache and os.path.exists(self.pitch_cache[key]):
            self.root.after(0, lambda p=self.pitch_cache[key]: self._play_pitched(p, semitones))
            return

        self.root.after(0, lambda: self.status_lbl.config(
            text=f"Обработка тональности {semitones:+d}...", fg="orange"))

        ratio = 2 ** (semitones / 12.0)
        sr = 44100
        out_path = os.path.join(self.temp_dir, f"pitched_{semitones:+d}.mp4")
        asetrate = int(sr * ratio)
        atempo = 1.0 / ratio
        cmd = [
            self.ffmpeg_path, "-y", "-i", src,
            "-filter_complex",
            f"[0:a]asetrate={asetrate},aresample={sr},atempo={atempo:.6f}[a]",
            "-map", "0:v?", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            out_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=180)
            if result.returncode != 0:
                err = result.stderr.decode(errors="ignore")[:120]
                self.root.after(0, lambda: self.status_lbl.config(
                    text=f"Ошибка pitch: {err}", fg="red"))
                return
            self.pitch_cache[key] = out_path
            self.root.after(0, lambda p=out_path: self._play_pitched(p, semitones))
        except Exception as e:
            self.root.after(0, lambda: self.status_lbl.config(
                text=f"Ошибка pitch: {e}", fg="red"))

    def _play_pitched(self, path, semitones):
        if not os.path.exists(path):
            return
        try:
            pos = self.player.get_position()
            was_playing = self.player.is_playing()
            self.player.stop()
            media = self.instance.media_new_path(path)
            self.player.set_media(media)
            self.player.play()
            if 0 < pos < 1:
                self.root.after(250, lambda: self.player.set_position(pos))
            if not was_playing:
                self.root.after(350, self.player.pause)
            self.status_lbl.config(
                text=f"▶ Тональность: {semitones:+d}", fg="#00cc66")
        except Exception as e:
            self.status_lbl.config(text=f"Ошибка: {e}", fg="red")

    # ---------- Поиск ----------
    def do_search(self):
        if self.mode.get() != "youtube":
            return
        q = self.search_entry.get().strip()
        if not q:
            messagebox.showwarning("Пусто", "Введите запрос")
            return
        if self.search_thread and self.search_thread.is_alive():
            self.stop_search.set()
            self.search_thread.join(timeout=0.5)
        self.stop_search.clear()
        self.search_btn.config(state=tk.DISABLED, bg="#555")
        self.stop_btn.config(state=tk.NORMAL, bg="#aa0000")
        self.status_lbl.config(text="Поиск...", fg="#00cc66")
        self.results_list.delete(0, tk.END)
        self.search_thread = threading.Thread(target=self._search_task,
                                              args=(q,), daemon=True)
        self.search_thread.start()

    def _search_task(self, query):
        try:
            opts = {
                'quiet': True,
                'extract_flat': True,
                'no_warnings': True,
                'proxy': self.proxy_url,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch15:{query} караоке", download=False)
                if self.stop_search.is_set():
                    return
                results = []
                if info and 'entries' in info:
                    for e in info['entries']:
                        if self.stop_search.is_set():
                            return
                        if e.get('id'):
                            title = e.get('title', '')
                            results.append((title, e['id']))
                self.root.after(0, lambda res=results: self._update_results(res))
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda msg=err:
                            self.status_lbl.config(text=f"Ошибка поиска: {msg}", fg="red"))
        finally:
            self.root.after(0, self._search_finished)

    def _update_results(self, results):
        self.results_list.delete(0, tk.END)
        self.last_results = results
        for title, vid in results:
            self.results_list.insert(tk.END, f"  {self._truncate(title)}")
        self.status_lbl.config(text=f"Найдено {len(results)}", fg="#888")

    def _search_finished(self):
        self.search_btn.config(state=tk.NORMAL, bg="#00cc66")
        self.stop_btn.config(state=tk.DISABLED, bg="#555")

    def stop_search_command(self):
        if self.search_thread and self.search_thread.is_alive():
            self.stop_search.set()
            self.search_btn.config(state=tk.NORMAL, bg="#00cc66")
            self.stop_btn.config(state=tk.DISABLED, bg="#555")
            self.status_lbl.config(text="Поиск остановлен", fg="orange")

    # ---------- Управление ----------
    def stop(self):
        threading.Thread(target=self._do_stop, daemon=True).start()
        self.current_url = None
        self.current_original_path = None
        self.current_pitch = 0
        try:
            self.pitch_scale.set(0)
            self.pitch_val.config(text="0")
        except Exception:
            pass
        try:
            self.placeholder_hint.place(relx=0.5, rely=0.62, anchor=tk.CENTER)
        except Exception:
            pass
        try:
            self.play_pause_btn.config(text="▶")
        except Exception:
            pass
        try:
            self.status_lbl.config(text="⏹ Остановлено", fg="#888")
        except Exception:
            pass

    def _do_stop(self):
        with self._play_lock:
            try:
                self.player.stop()
            except Exception:
                pass

    def on_volume_change(self, val):
        try:
            vol = int(float(val))
            self.player.audio_set_volume(vol)
            self.vol_val.config(text=f"{vol}%")
        except Exception:
            pass

    def update_zapret_status(self):
        if self.proxy_url:
            self.zapret_status.config(text="tpws ON :9876", fg="#00cc66")
        else:
            self.zapret_status.config(text="tpws OFF", fg="orange")

    def restart_zapret(self):
        if self._check_tpws():
            self.status_lbl.config(text="tpws подключён на :9876", fg="#00cc66")
        else:
            self.status_lbl.config(
                text="tpws не запущен — откройте zapret-macos-easy", fg="orange")
        self.update_zapret_status()

    def cleanup(self):
        self.stop_progress.set()
        self.stop()
        try:
            if hasattr(self, 'tpws_process') and self.tpws_process:
                self.tpws_process.shutdown()
        except Exception:
            pass

        try:
            if hasattr(self, 'video_window') and self.video_window:
                self.video_window.destroy()
        except Exception:
            pass
        if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def on_closing(self):
        try:
            self.stop_progress.set()
        except Exception:
            pass
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.player.set_media(None)
        except Exception:
            pass
        try:
            if hasattr(self, 'tpws_process') and self.tpws_process:
                self.tpws_process.shutdown()
        except Exception:
            pass
        try:
            if hasattr(self, 'video_window') and self.video_window.winfo_exists():
                self.video_window.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        try:
            if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    root = tk.Tk()
    app = KaraokeApp(root)
    root.mainloop()
