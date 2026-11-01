import os
import sys
import platform
import socket
import ctypes
import json
import re
import atexit
import shutil
import time
import tempfile
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox
import webbrowser
import requests
import logging
import traceback

# ============================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

if IS_WIN:
    LOG_DIR = r"C:\Users\Suncho\Desktop\karaoke_player_you_tube"
else:
    LOG_DIR = os.path.expanduser("~/Desktop/karaoke_player_you_tube")

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
threading.excepthook = lambda args: logging.critical("ОШИБКА В ПОТОКЕ", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

# ============================================================
# НАСТРОЙКА VLC
# ============================================================
if IS_MAC:
    for _vlc_candidate in (
        "/Applications/VLC.app/Contents/MacOS",
        "/Applications/MacPorts/VLC.app/Contents/MacOS",
    ):
        if os.path.exists(f"{_vlc_candidate}/lib/libvlc.dylib"):
            os.environ["DYLD_LIBRARY_PATH"] = f"{_vlc_candidate}/lib"
            os.environ.setdefault("VLC_PLUGIN_PATH", f"{_vlc_candidate}/plugins")
            break
    else:
        if os.path.exists("/opt/local/libexec/vlc3/lib/libvlc.dylib"):
            os.environ["DYLD_LIBRARY_PATH"] = "/opt/local/libexec/vlc3/lib"

if IS_WIN:
    vlc_found = False
    for _vlc_win in (r"C:\Program Files\VideoLAN\VLC", r"C:\Program Files (x86)\VideoLAN\VLC"):
        dll_path = os.path.join(_vlc_win, "libvlc.dll")
        if os.path.exists(dll_path):
            try:
                os.add_dll_directory(_vlc_win)
                logging.info(f"VLC найден: {_vlc_win}")
                vlc_found = True
            except Exception as e:
                logging.error(f"Ошибка add_dll_directory: {e}")
            break
    if not vlc_found:
        logging.warning("VLC не найден в стандартных путях Windows!")

try:
    import vlc
    import yt_dlp
except Exception as e:
    logging.critical(f"Ошибка импорта библиотек: {e}")
    messagebox.showerror("Ошибка", f"Не установлены библиотеки:\n{e}\n\npip install python-vlc yt-dlp requests")
    sys.exit(1)

# ---------- Константы ----------
TPWS_PROXY = "socks5h://127.0.0.1:9876"
TPWS_PORT = 9876
APP_VERSION = "1.0.2"
GITHUB_REPO = "aokraynov-commits/karaoke.py"
ZAPRET_DIR = r"C:\Zapret\Dev"
ZAPRET_EXE_TARGET = os.path.join(ZAPRET_DIR, "Zapret.exe")

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

# ---------- Windows Admin ----------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

if IS_WIN and not is_admin():
    logging.info("Запуск с правами администратора...")
    script = os.path.abspath(sys.argv[0])
    params = ' '.join([f'"{arg}"' if ' ' in arg else arg for arg in sys.argv[1:]])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}', None, 1)
    sys.exit(0)

# ---------- Windows Zapret ----------
def install_zapret():
    if not IS_WIN: return False
    src = resource_path("zapret")
    if not os.path.exists(src): return False
    if os.path.exists(ZAPRET_EXE_TARGET): return True
    try:
        os.makedirs(ZAPRET_DIR, exist_ok=True)
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(ZAPRET_DIR, item)
            if os.path.isdir(s): shutil.copytree(s, d, dirs_exist_ok=True)
            else: shutil.copy2(s, d)
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
        if not os.path.exists(self.exe_path): return False
        try:
            self.process = subprocess.Popen(
                [self.exe_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0
            )
            time.sleep(1)
            self.ready = True
            logging.info("Zapret успешно запущен.")
            return True
        except Exception as e:
            logging.error(f"Ошибка запуска Zapret: {e}")
            return False

    def shutdown(self):
        if self.process:
            self.process.terminate()
            self.process = None
            self.ready = False

# ---------- Mac tpws ----------
class TpwsProcess:
    def __init__(self, binary_path, port=TPWS_PORT):
        self.binary_path = binary_path
        self.port = port
        self.process = None
        self.ready = False

    def start(self):
        if self.ready: return True
        if not os.path.exists(self.binary_path): return False
        try:
            cmd = [self.binary_path, "--socks", "--bind-addr=127.0.0.1", f"--port={self.port}", "--split-pos=midsld", "--oob"]
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
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception: pass
            self.process = None
        self.ready = False

# ---------- NSView (только Mac) ----------
_libtk_cache = None
def get_nsview(tk_widget):
    global _libtk_cache
    if not IS_MAC: return None
    if _libtk_cache is None:
        try:
            import tkinter as tk
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
        self.projector_mode = False

        if IS_WIN:
            if not os.path.exists(ZAPRET_EXE_TARGET):
                if not install_zapret():
                    messagebox.showerror("Ошибка", "Не удалось установить Zapret.")
                    sys.exit(1)
            self.zapret_process = ZapretProcess(ZAPRET_EXE_TARGET)
            self.zapret_process.start()
        else:
            bundled_tpws = resource_path(os.path.join("zapret", "tpws", "tpws"))
            self.tpws_process = TpwsProcess(bundled_tpws, port=TPWS_PORT)
            if self.tpws_process.start():
                self.proxy_url = TPWS_PROXY

        try:
            self.instance = vlc.Instance("--http-user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15")
            self.player = self.instance.media_player_new()
        except Exception as e:
            logging.critical(f"Ошибка инициализации VLC: {e}")
            sys.exit(1)

        self.current_url = None
        self.current_original_path = None
        self.current_pitch = 0
        self.last_results = []
        self.all_local_files = []
        self.mode = tk.StringVar(value="youtube")

        base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        self.download_folder = os.path.join(base, "KaraokeDownloads")
        os.makedirs(self.download_folder, exist_ok=True)

        self.ffmpeg_path = find_ffmpeg()
        self.temp_dir = tempfile.mkdtemp(prefix="karaoke_")
        self.video_cache = self._load_cache()
        self.pitch_cache = {}

        self.progress_var = tk.DoubleVar(value=0.0)
        self.stop_progress = threading.Event()
        threading.Thread(target=self.update_progress, daemon=True).start()

        self.search_thread = None
        self.stop_search = threading.Event()

        try:
            self.create_widgets()
            self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
            threading.Thread(target=self._check_for_updates, daemon=True).start()
            logging.info("Интерфейс успешно создан.")
        except Exception as e:
            logging.critical(f"Ошибка создания интерфейса: {e}")
            messagebox.showerror("Критическая ошибка", f"Не удалось создать интерфейс:\n{e}")

    def _load_cache(self):
        cache_file = os.path.join(self.download_folder, ".cache.json")
        try:
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
        return {}

    def _save_cache(self):
        try:
            with open(os.path.join(self.download_folder, ".cache.json"), "w", encoding="utf-8") as f:
                json.dump(self.video_cache, f, ensure_ascii=False, indent=2)
        except Exception: pass

    def create_widgets(self):
        bg, fg, accent, entry_bg, list_bg = "#0a0a0a", "#e0e0e0", "#00cc66", "#1a1a1a", "#111111"

        main = tk.Frame(self.root, bg=bg)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.video_placeholder = tk.Frame(main, bg="black")
        self.video_placeholder.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        
        right = tk.Frame(main, bg=bg, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        mode_frame = tk.Frame(right, bg=bg)
        mode_frame.pack(fill=tk.X, pady=(0, 5))
        tk.Radiobutton(mode_frame, text="YouTube", variable=self.mode, value="youtube", command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg, activebackground=bg, activeforeground=fg).pack(side=tk.LEFT)
        tk.Radiobutton(mode_frame, text="Локальные", variable=self.mode, value="local", command=self.on_mode_change, fg=fg, bg=bg, selectcolor=bg, activebackground=bg, activeforeground=fg).pack(side=tk.LEFT, padx=10)

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
        self.play_pause_btn = tk.Button(ctrl_frame, text="▶", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.toggle_play_pause, width=4)
        self.play_pause_btn.grid(row=0, column=0, padx=2)
        self.stop_play_btn = tk.Button(ctrl_frame, text="⏹", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.stop, width=4)
        self.stop_play_btn.grid(row=0, column=1, padx=2)
        self.download_btn = tk.Button(ctrl_frame, text="📥", bg=accent, fg="#000", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.download_selected, width=4)
        self.download_btn.grid(row=0, column=2, padx=2)
        self.delete_btn = tk.Button(ctrl_frame, text="🗑", bg="#cc3333", fg="#fff", font=("Segoe UI", 12, "bold"), relief=tk.FLAT, command=self.delete_selected_local, width=4)
        self.delete_btn.grid(row=0, column=3, padx=2)
        self.delete_btn.grid_remove()

        progress_frame = tk.Frame(right, bg=bg)
        progress_frame.pack(fill=tk.X, pady=5)
        tk.Label(progress_frame, text="Прогресс", fg=fg, bg=bg).pack(anchor=tk.W)
        self.progress_scale = tk.Scale(progress_frame, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.progress_var, command=self.on_progress_move, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0)
        self.progress_scale.pack(fill=tk.X)
        self.progress_scale.bind("<ButtonRelease-1>", self.on_progress_release)

        vol_frame = tk.Frame(right, bg=bg)
        vol_frame.pack(fill=tk.X, pady=5)
        tk.Label(vol_frame, text="🔊 Громкость", fg=fg, bg=bg).pack(anchor=tk.W)
        self.vol_scale = tk.Scale(vol_frame, from_=0, to=200, orient=tk.HORIZONTAL, command=self.on_volume_change, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0)
        self.vol_scale.set(100)
        self.vol_scale.pack(fill=tk.X)

        # --- Тональность ---
        pitch_frame = tk.Frame(right, bg=bg)
        pitch_frame.pack(fill=tk.X, pady=5)
        tk.Label(pitch_frame, text="🎚 Тональность (полутона)", fg=fg, bg=bg).pack(anchor=tk.W)
        self.pitch_scale = tk.Scale(pitch_frame, from_=-6, to=6, orient=tk.HORIZONTAL, bg=bg, fg=fg, troughcolor="#333", highlightthickness=0, resolution=1)
        self.pitch_scale.set(0)
        self.pitch_scale.pack(fill=tk.X)
        self.pitch_val = tk.Label(pitch_frame, text="0", fg=accent, bg=bg)
        self.pitch_val.pack(anchor=tk.E)
        self.pitch_scale.bind("<ButtonRelease-1>", self.on_pitch_release)

        self.restart_zapret_btn = tk.Button(right, text="🔄 Проверить обход", bg=accent, fg="#000", relief=tk.FLAT, command=self.restart_zapret)
        self.restart_zapret_btn.pack(fill=tk.X, pady=(5, 2))
        
        # --- ПРОЕКТОР ---
        self.projector_btn = tk.Button(right, text="📺 На проектор", bg="#555", fg=fg, font=("Segoe UI", 9, "bold"), relief=tk.FLAT, command=self._toggle_projector)
        self.projector_btn.pack(fill=tk.X, pady=(0, 5))

        self.on_mode_change()
        self._create_video_window()

    # --- Управление проектором ---
    def _get_screens(self):
        if not IS_MAC:
            return None
        try:
            from AppKit import NSScreen
            result = []
            for s in NSScreen.screens():
                f = s.frame()
                result.append({'x': int(f.origin.x), 'y': int(f.origin.y), 'w': int(f.size.width), 'h': int(f.size.height)})
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
            messagebox.showerror("Нужен pyobjc", "Для работы с проектором установите:\n\nsudo port install py312-pyobjc")
            return
        if len(screens) < 2:
            messagebox.showinfo("Второй экран не найден", f"Подключите проектор или второй монитор.\nСейчас подключено: {len(screens)}")
            return
        main_idx = 0
        for i, s in enumerate(screens):
            if s['x'] == 0 and s['y'] == 0:
                main_idx = i
                break
        target = None
        for i, s in enumerate(screens):
            if i != main_idx:
                target = s
                break
        if target is None: return
        try:
            self.video_window.attributes("-topmost", False)
            self.video_window.geometry(f"{target['w']}x{target['h']}+{target['x']}+{target['y']}")
            self.projector_mode = True
            try:
                self.projector_btn.config(text="📺 Вернуть на главный", bg="#aa4444")
            except Exception: pass
            self.status_lbl.config(text="Видео на проекторе", fg="#00cc66")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _return_from_projector(self):
        self.projector_mode = False
        try:
            self.projector_btn.config(text="📺 На проектор", bg="#555")
        except Exception: pass
        try:
            self.video_window.attributes("-topmost", False)
            self._position_video_window()
            self.status_lbl.config(text="Видео на главном экране", fg="#888")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _create_video_window(self):
        self.video_window = tk.Toplevel(self.root)
        self.video_window.title("Karaoke Video")
        self.video_window.configure(bg="black")
        self.video_window.transient(self.root)
        if IS_MAC:
            self.video_window.overrideredirect(True)

        self.video_canvas = tk.Canvas(self.video_window, bg="black", highlightthickness=0, bd=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)

        self.root.bind("<Configure>", lambda e: self.root.after(50, self._position_video_window) if e.widget is self.root else None)
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
        except Exception as e:
            logging.error(f"Ошибка привязки VLC: {e}")

    def _on_root_unmap(self, event=None):
        if event and event.widget is not self.root: return
        if hasattr(self, 'video_window') and self.video_window.winfo_exists():
            self.video_window.withdraw()

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
        if not os.path.exists(self.download_folder): return self._render_local_files()
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
            self.local_search_frame.pack(fill=tk.X, pady=(0, 5))
            self.download_btn.grid_remove()
            self.delete_btn.grid()
            self.load_local_files()

    def delete_selected_local(self):
        if self.mode.get() != "local": return
        sel = self.results_list.curselection()
        if not sel: return
        fname, path = self.last_results[sel[0]]
        if messagebox.askyesno("Удалить", f"Удалить файл?\n\n{fname}"):
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

    def _resolve_downloaded_path(self, ydl, info):
        if info.get('requested_downloads'):
            p = info['requested_downloads'][0].get('filepath')
            if p and os.path.exists(p): return p
        candidates = []
        if os.path.isdir(self.temp_dir):
            for f in os.listdir(self.temp_dir):
                fp = os.path.join(self.temp_dir, f)
                if os.path.isfile(fp) and f.lower().endswith(('.mp4', '.mkv', '.webm', '.m4a')): candidates.append(fp)
        return max(candidates, key=os.path.getmtime) if candidates else None

    def _check_already_downloaded(self, video_id):
        cached = self.video_cache.get(video_id)
        if cached and os.path.exists(cached): return cached
        return None

    def download_selected(self):
        if self.mode.get() != "youtube": return
        sel = self.results_list.curselection()
        if not sel: return
        vid = self.last_results[sel[0]][1]
        if not re.match(r'^[A-Za-z0-9_-]{11}$', vid):
            messagebox.showerror("Ошибка", "Некорректный трек. Сделайте новый поиск.")
            return
        if self._check_already_downloaded(vid):
            self.status_lbl.config(text="Уже в библиотеке", fg="#00cc66")
            return
        self.status_lbl.config(text="Скачивание...", fg="#00cc66")
        self.download_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._download, args=(vid, False), daemon=True).start()

    def play_selected(self):
        sel = self.results_list.curselection()
        if not sel: return
        item = self.last_results[sel[0]][1]
        if self.mode.get() == "youtube":
            if not re.match(r'^[A-Za-z0-9_-]{11}$', item):
                messagebox.showerror("Ошибка", "Некорректный трек. Сделайте новый поиск.")
                return
            cached = self._check_already_downloaded(item)
            if cached:
                self.status_lbl.config(text="Из библиотеки", fg="#00cc66")
                self._play_local(cached)
                return
            self.status_lbl.config(text="Загрузка...", fg="#00cc66")
            threading.Thread(target=self._download, args=(item, True), daemon=True).start()
        else:
            self._play_local(item)

    def _download(self, video_id, auto_play):
        url = f"https://youtu.be/{video_id}"
        ydl_opts = {
            'quiet': True,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': os.path.join(self.temp_dir, '%(title)s.%(ext)s'),
            'merge_output_format': 'mp4',
            'ffmpeg_location': self.ffmpeg_path,
            'no_warnings': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'web_embedded']}},
        }
        if self.proxy_url:
            ydl_opts['proxy'] = self.proxy_url

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_path = self._resolve_downloaded_path(ydl, info)
                if not temp_path: raise Exception("Файл не найден")
                
                new_name = f"{self._get_next_number():02d} - {os.path.basename(temp_path)}"
                dest = os.path.join(self.download_folder, new_name)
                shutil.move(temp_path, dest)
                self.video_cache[video_id] = dest
                self._save_cache()
                
                if auto_play:
                    self.root.after(0, lambda: self._play_local(dest))
                else:
                    self.root.after(0, lambda: self.status_lbl.config(text=f"Сохранено: {new_name}", fg="#00cc66"))
        except Exception as e:
            logging.error(f"Ошибка загрузки: {e}")
            self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка: {e}", fg="red"))
        finally:
            self.root.after(0, lambda: self.download_btn.config(state=tk.NORMAL))

    def _play_local(self, path):
        if not os.path.exists(path): return
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
            self.status_lbl.config(text="▶ Воспроизведение", fg="#00cc66")
            self.on_volume_change(self.vol_scale.get())
        except Exception as e:
            logging.error(f"Ошибка VLC Play: {e}")

    # ---------- Тональность ----------
    def on_pitch_release(self, event):
        semitones = int(self.pitch_scale.get())
        self.pitch_val.config(text=f"{semitones:+d}" if semitones else "0")
        if not self.current_original_path: return
        if semitones == self.current_pitch: return
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
                self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка pitch: {err}", fg="red"))
                return
            self.pitch_cache[key] = out_path
            self.root.after(0, lambda p=out_path: self._play_pitched(p, semitones))
        except Exception as e:
            self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка pitch: {e}", fg="red"))

    def _play_pitched(self, path, semitones):
        if not os.path.exists(path): return
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
            self.status_lbl.config(text=f"▶ Тональность: {semitones:+d}", fg="#00cc66")
        except Exception as e:
            self.status_lbl.config(text=f"Ошибка: {e}", fg="red")

    def do_search(self):
        q = self.search_entry.get().strip()
        if not q: return
        if self.search_thread and self.search_thread.is_alive():
            self.stop_search.set()
            self.search_thread.join(timeout=0.5)
        self.stop_search.clear()
        self.search_btn.config(state=tk.DISABLED, bg="#555")
        self.stop_btn.config(state=tk.NORMAL, bg="#aa0000")
        self.status_lbl.config(text="Поиск...", fg="#00cc66")
        self.results_list.delete(0, tk.END)
        self.search_thread = threading.Thread(target=self._search_task, args=(q,), daemon=True)
        self.search_thread.start()

    def _search_task(self, query):
        try:
            opts = {'quiet': True, 'extract_flat': True, 'no_warnings': True}
            if self.proxy_url: opts['proxy'] = self.proxy_url
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch15:{query} караоке", download=False)
                if self.stop_search.is_set(): return
                results = [(e.get('title', '')[:45], e['id']) for e in info.get('entries', []) if e.get('id')]
                self.root.after(0, lambda: self._update_results(results))
        except Exception as e:
            logging.error(f"Ошибка поиска: {e}")
            self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка поиска: {e}", fg="red"))
        finally:
            self.root.after(0, lambda: self.search_btn.config(state=tk.NORMAL, bg="#00cc66"))

    def _update_results(self, results):
        self.results_list.delete(0, tk.END)
        self.last_results = results
        for title, vid in results: self.results_list.insert(tk.END, f"  {title}")
        self.status_lbl.config(text=f"Найдено {len(results)}", fg="#888")

    def stop_search_command(self):
        if self.search_thread and self.search_thread.is_alive():
            self.stop_search.set()
            self.search_btn.config(state=tk.NORMAL, bg="#00cc66")
            self.stop_btn.config(state=tk.DISABLED, bg="#555")
            self.status_lbl.config(text="Поиск остановлен", fg="orange")

    def toggle_play_pause(self):
        if self.player.is_playing():
            self.player.pause()
            self.play_pause_btn.config(text="▶")
        else:
            sel = self.results_list.curselection()
            if sel: self.play_selected()
            elif self.player.get_media():
                self.player.play()
                self.play_pause_btn.config(text="⏸")

    def stop(self):
        self.player.stop()
        self.play_pause_btn.config(text="▶")

    def update_progress(self):
        while not self.stop_progress.is_set():
            try:
                if self.player.get_length() > 0:
                    self.progress_var.set(self.player.get_position() * 100)
            except Exception: pass
            time.sleep(0.5)

    def on_progress_move(self, val): pass
    def on_progress_release(self, event):
        if self.player.get_length() > 0:
            self.player.set_position(self.progress_var.get() / 100.0)

    def on_volume_change(self, val):
        self.player.audio_set_volume(int(float(val)))

    def update_zapret_status(self):
        if IS_WIN:
            self.zapret_status.config(text="Zapret ON" if self.zapret_process and self.zapret_process.ready else "Zapret OFF")
        else:
            self.zapret_status.config(text="tpws ON" if self.proxy_url else "tpws OFF")

    def restart_zapret(self):
        if IS_WIN and self.zapret_process:
            self.zapret_process.shutdown()
            self.zapret_process.start()
        elif IS_MAC and self.tpws_process:
            self.tpws_process.shutdown()
            if self.tpws_process.start(): self.proxy_url = TPWS_PROXY
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
        logging.info("Закрытие приложения...")
        self.stop_progress.set()
        self.stop()
        if self.zapret_process: self.zapret_process.shutdown()
        if self.tpws_process: self.tpws_process.shutdown()
        try: shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception: pass
        self.root.destroy()

if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = KaraokeApp(root)
        root.mainloop()
    except Exception as e:
        logging.critical(f"Критический сбой в mainloop: {e}", exc_info=True)
        messagebox.showerror("Критическая ошибка", f"Приложение вылетело:\n{e}\n\nЛог сохранен в:\n{LOG_FILE}")