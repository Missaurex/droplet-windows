"""Droplet Звонки для Windows: звонок в WhatsApp или Sipuni → запись → текст → итог Claude → примечание amoCRM.
Живёт в трее (у часов). Ключей от amoCRM нет: пишет расширение Chrome во вкладке amoCRM, входом самого МОПа."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import wave
import webbrowser
import winreg
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

import core

APP = "Droplet Звонки"
PORT = 47811
DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DropletCalls"
CALLS = Path.home() / "Documents" / "Droplet Звонки"
OWN = Path(sys.executable).name.lower()  # себя в списке «кто держит микрофон» пропускаем
NO_WINDOW = 0x08000000
DATA.mkdir(parents=True, exist_ok=True)
CALLS.mkdir(parents=True, exist_ok=True)


def log(*a):
    with open(DATA / "log.txt", "a", encoding="utf-8") as f:
        f.write(datetime.now().strftime("%m-%d %H:%M:%S ") + " ".join(map(str, a)) + "\n")


# ---------- настройки ----------
CFG_FILE = DATA / "config.json"
cfg = {"auto": True, "first": True}
try:
    cfg.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
except (OSError, ValueError):
    pass


def save_cfg():
    CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


# ---------- мост с расширением Chrome ----------
class Bridge:
    """Вкладка amoCRM раз в 2 секунды спрашивает «есть дела?» и выполняет их своими запросами amoCRM."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs, self.results, self.n = [], {}, 0
        self.card, self.seen = None, 0.0

    def ready(self):
        return time.time() - self.seen < 6

    def call(self, job, timeout=40):
        ev = threading.Event()
        with self.lock:
            self.n += 1
            job["id"] = self.n
            self.results[self.n] = [ev, (0, "")]
            self.jobs.append(job)
        ev.wait(timeout)
        with self.lock:
            if job in self.jobs:
                self.jobs.remove(job)
            return self.results.pop(job["id"])[1]

    def get(self, path):
        return self.call({"type": "get", "path": path})

    def post(self, path, body):
        return self.call({"type": "post", "path": path, "body": body})

    def open(self, c):
        self.call({"type": "open", "path": f"/{c[0]}/detail/{c[1]}"}, timeout=10)

    def poll(self, body):
        self.seen = time.time()
        if body.get("visible"):
            self.card = core.card(body.get("url", ""))
        with self.lock:
            jobs, self.jobs = self.jobs, []
        return jobs

    def done(self, body):
        with self.lock:
            r = self.results.get(body.get("id"))
            if r:
                r[1] = (int(body.get("code") or 0), body.get("text") or "")
                r[0].set()
        return {}


bridge = Bridge()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Только наше расширение: обычный сайт не может прислать свой заголовок без разрешения (CORS), а мы его не даём.
        if self.headers.get("X-Droplet") != "1" or not (self.headers.get("Origin") or "").startswith("chrome-extension://"):
            self.send_response(403)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        out = bridge.poll(body) if self.path == "/poll" else bridge.done(body) if self.path == "/done" else {}
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


# ---------- кто сейчас звонит ----------
MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def mic_users():
    """Приложения, которые прямо сейчас держат микрофон (Windows отмечает это в реестре: LastUsedTimeStop = 0)."""
    out = []

    def scan(path):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            if name == "NonPackaged":
                scan(path + "\\NonPackaged")
                continue
            try:
                if winreg.QueryValueEx(winreg.OpenKey(k, name), "LastUsedTimeStop")[0] == 0:
                    out.append(name)
            except OSError:
                pass

    scan(MIC_KEY)
    return out


def messenger_text(app):
    """Тексты отдельного окна звонка WhatsApp/Telegram (главное окно со списком чатов пропускаем — там чужие номера)."""
    import psutil
    import uiautomation as auto
    key = "telegram" if "telegram" in app else "whatsapp"
    pids = {p.pid for p in psutil.process_iter(["name"]) if key in (p.info["name"] or "").lower()}
    out = []
    with auto.UIAutomationInitializerInThread():
        for w in auto.GetRootControl().GetChildren():
            title = (w.Name or "").strip().lower()
            if w.ProcessId not in pids or title == key or title.startswith(key + " ("):
                continue
            for c, _ in auto.WalkControl(w, includeTop=True, maxDepth=12):
                n = (c.Name or "").strip()
                if n and len(n) < 80 and n not in out:
                    out.append(n)
                if len(out) > 300:
                    break
    return " | ".join(out)[:1500]


# ---------- запись ----------
class Recorder:
    """Свой голос — микрофон, клиент — звук компьютера (то, что слышно в наушниках). Храним сразу в 16 кГц."""
    RATE = 48000

    def __init__(self):
        self.on = False

    def start(self):
        import soundcard as sc
        self.on, self.me, self.them, self.started = True, [], [], datetime.now()
        threading.Thread(target=self._rec, args=(sc.default_microphone, self.me), daemon=True).start()
        threading.Thread(target=self._rec, args=(lambda: sc.get_microphone(sc.default_speaker().name, include_loopback=True), self.them),
                         daemon=True).start()

    def _rec(self, device, buf):
        try:
            with device().recorder(samplerate=self.RATE, channels=1) as r:
                while self.on:
                    x = r.record(numframes=self.RATE // 2).reshape(-1)
                    buf.append(x[: len(x) // 3 * 3].reshape(-1, 3).mean(axis=1).astype(np.float32))
        except Exception:
            log("запись:", traceback.format_exc())

    def stop(self):
        self.on = False
        time.sleep(0.8)
        me = np.concatenate(self.me) if self.me else np.zeros(0, np.float32)
        them = np.concatenate(self.them) if self.them else np.zeros(0, np.float32)
        return me, them, self.started


rec = Recorder()


def save_wav(path, a):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())


# ---------- речь в текст (Whisper на этом компьютере) ----------
_model = None
_model_lock = threading.Lock()


def whisper():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            # shortcut: модель «small» — быстрая на обычном ноутбуке; если путает слова — поменять на «medium»
            _model = WhisperModel("small", device="cpu", compute_type="int8", download_root=str(DATA / "models"))
        return _model


def model_ready():
    return _model is not None or any((DATA / "models").glob("*small*"))


def transcribe(a):
    if len(a) < 16000:
        return []
    segs, _ = whisper().transcribe(a, language="ru", vad_filter=True, beam_size=1)
    return [(s.start, s.text.strip()) for s in segs if s.text.strip()]


# ---------- Claude Code (подписка Pro) ----------
def claude_path():
    p = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    return p if Path(p).exists() else None


def run(args, inp=None, timeout=60):
    try:
        r = subprocess.run(args, input=inp, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout + r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        log("run:", args[0], e)
        return ""


def claude_logged_in():
    c = claude_path()
    return bool(c) and '"loggedIn": true' in run([c, "auth", "status"], timeout=15).replace('"loggedIn":true', '"loggedIn": true')


def ask_claude(text):
    c = claude_path()
    return run([c, "-p", "--output-format", "text"], inp=text, timeout=300) if c else ""


def install_claude():
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://claude.ai/install.ps1 | iex"], timeout=600)


def login_claude():
    c = claude_path()
    if c:
        subprocess.Popen([c, "auth", "login", "--claudeai"], creationflags=NO_WINDOW)


# ---------- после звонка ----------
finish_lock = threading.Lock()


def clip(text):
    subprocess.run(["clip"], input=text.encode("utf-16"), creationflags=NO_WINDOW)


def finish(audio, st):
    with finish_lock:  # два звонка подряд — по очереди
        me, them, started = audio
        minutes = max(1, round(max(len(me), len(them)) / 16000 / 60))
        folder = CALLS / (started.strftime("%Y-%m-%d %H-%M") + (" " + st["phone"] if st.get("phone") else ""))
        folder.mkdir(parents=True, exist_ok=True)
        save_wav(folder / "МОП.wav", me)
        save_wav(folder / "Клиент.wav", them)
        notify("Расшифровываю звонок…")
        dialog = core.merge(transcribe(me), transcribe(them))
        if not dialog:
            notify("Не разобрал речь в звонке. Запись в папке «Droplet Звонки»")
            return
        r = ask_claude(core.prompt(dialog, st.get("ctx", "")))
        note = core.note_text(r, minutes, dialog)
        (folder / "Звонок.txt").write_text(note, encoding="utf-8")
        # Только точная карточка: Sipuni — сделка, открытая весь звонок; WhatsApp — один контакт с номером из окна звонка.
        c = None if st.get("changed") else st.get("card")
        if not c and st.get("phone") and bridge.ready():
            c = core.find(bridge.get, st["phone"])
        if c and bridge.ready():
            code, text = bridge.post(f"/api/v4/{c[0]}/{c[1]}/notes",
                                     json.dumps([{"note_type": "common", "params": {"text": note}}], ensure_ascii=False))
            log("примечание", c, code, text[:200])
            if code == 200:
                notify("Вписал итог в примечание сделки ✓")
                return
        clip(note)
        notify("Chrome с amoCRM закрыт — в amoCRM не писал. Итог в буфере: открой сделку и вставь Ctrl+V" if not bridge.ready()
               else "Не уверен, чья это карточка, — в amoCRM не писал. Итог в буфере: открой сделку клиента и вставь Ctrl+V")


def watch():
    st = None
    while True:
        time.sleep(2)
        try:
            app = core.call_app(mic_users(), OWN)
            if app and not rec.on and cfg["auto"]:
                st = {"app": app[0], "kind": app[1], "card": None, "changed": False, "phone": None, "looked": False, "ctx": ""}
                rec.start()
                log("звонок:", app)
                notify("Записываю звонок. Предупреди клиента, что идёт запись")
            if rec.on and st:
                if st["kind"] == "browser":
                    core.track_card(st, bridge.card if bridge.ready() else None)
                elif not st["looked"]:
                    t = messenger_text(st["app"])
                    st["ctx"] = t or st["ctx"]
                    ph = core.call_phone(t)
                    if ph:
                        st["looked"], st["phone"] = True, ph
                        c = core.find(bridge.get, ph) if bridge.ready() else None
                        if c:
                            st["card"] = c
                            bridge.open(c)
                            notify("Открыл карточку клиента в amoCRM")
            if not app and rec.on:
                done, st = st, None
                threading.Thread(target=finish, args=(rec.stop(), done or {}), daemon=True).start()
        except Exception:
            log("watch:", traceback.format_exc())


# ---------- трей, автозапуск, окно подключения ----------
icon = None


def notify(text):
    log(text)
    if icon:
        try:
            icon.notify(text, APP)
        except Exception:
            pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart(on=None):
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
    if on is None:
        try:
            winreg.QueryValueEx(k, "DropletCalls")
            return True
        except OSError:
            return False
    if on:
        winreg.SetValueEx(k, "DropletCalls", 0, winreg.REG_SZ, f'"{sys.executable}"')
    else:
        try:
            winreg.DeleteValue(k, "DropletCalls")
        except OSError:
            pass


def extension_dir():
    """Папка расширения в постоянном месте: Chrome запоминает путь при «Загрузить распакованное»."""
    src = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "extension"
    dst = DATA / "extension"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def setup_window(root):
    import tkinter as tk
    w = tk.Toplevel(root)
    w.title("Droplet Звонки — подключение")
    w.geometry("560x470")
    w.attributes("-topmost", True)
    tk.Label(w, text="Подключение — нажимай кнопки сверху вниз", font=("Segoe UI", 13, "bold")).pack(pady=(14, 8))
    state = {"amo": None}

    def amo_check():
        state["amo"] = bridge.get("/api/v4/account")[0] == 200

    def chrome_ext():
        d = extension_dir()
        subprocess.Popen(["explorer", str(d)])
        for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe")):
            if Path(c).exists():
                subprocess.Popen([c, "chrome://extensions"])
                break

    def bg(f):
        return lambda: threading.Thread(target=f, daemon=True).start()

    steps = [
        ("Claude Code", "Поставит сам, около минуты", lambda: claude_path() is not None, "Установить", bg(install_claude)),
        ("Вход в Claude", "Твоя подписка Pro — в браузере нажми «Разрешить»", claude_logged_in, "Войти", login_claude),
        ("Расширение Chrome", "В Chrome включи «Режим разработчика» (справа сверху) → «Загрузить распакованное» → выбери открывшуюся папку",
         bridge.ready, "Установить", chrome_ext),
        ("amoCRM", "Открой amoCRM в Chrome и войди", lambda: bool(state["amo"]), "Открыть", lambda: webbrowser.open("https://www.amocrm.ru/")),
        ("Распознавание речи", "Скачает один раз (~500 МБ)", model_ready, "Скачать", bg(whisper)),
    ]
    rows = []
    for title, hint, _, btn, act in steps:
        f = tk.Frame(w)
        f.pack(fill="x", padx=16, pady=5)
        mark = tk.Label(f, text="○", width=2, font=("Segoe UI", 14))
        mark.pack(side="left")
        box = tk.Frame(f)
        box.pack(side="left", fill="x", expand=True)
        tk.Label(box, text=title, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
        tk.Label(box, text=hint, font=("Segoe UI", 9), fg="#666", anchor="w", wraplength=330, justify="left").pack(fill="x")
        b = tk.Button(f, text=btn, command=act, width=11)
        b.pack(side="right")
        rows.append((mark, b))
    tk.Label(w, text="Лучше звонить в наушниках — так голоса МОПа и клиента не смешаются.", fg="#666").pack(pady=10)

    def refresh():
        if not w.winfo_exists():
            return

        def check():
            if bridge.ready() and not state["amo"]:
                amo_check()
            res = [s[2]() for s in steps]
            w.after(0, lambda: paint(res))
        threading.Thread(target=check, daemon=True).start()
        w.after(3000, refresh)

    def paint(res):
        if not w.winfo_exists():
            return
        for (mark, b), ok in zip(rows, res):
            mark.config(text="✓" if ok else "○", fg="#1a7f37" if ok else "#999")
            b.config(state="disabled" if ok else "normal")

    refresh()


def main():
    import tkinter as tk
    from PIL import Image, ImageDraw
    import pystray

    global icon
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        return  # уже запущен
    threading.Thread(target=server.serve_forever, daemon=True).start()
    extension_dir()
    if getattr(sys, "frozen", False) and cfg["first"]:
        autostart(True)

    root = tk.Tk()
    root.withdraw()

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((12, 18, 52, 58), fill=(217, 119, 87))
    ImageDraw.Draw(img).polygon([(32, 2), (14, 32), (50, 32)], fill=(217, 119, 87))

    def status(_):
        if rec.on:
            return "● Идёт запись звонка"
        return "amoCRM подключён" if bridge.ready() else "Открой amoCRM в Chrome"

    def toggle_auto(_i, _it):
        cfg["auto"] = not cfg["auto"]
        save_cfg()

    def toggle_start(_i, _it):
        autostart(not autostart())

    def quit_app(i, _it):
        i.stop()
        root.after(0, root.destroy)

    icon = pystray.Icon("DropletCalls", img, APP, pystray.Menu(
        pystray.MenuItem(status, None, enabled=False),
        pystray.MenuItem("Записывать звонки сам", toggle_auto, checked=lambda _: cfg["auto"]),
        pystray.MenuItem("Подключение…", lambda: root.after(0, lambda: setup_window(root)), default=True),
        pystray.MenuItem("Папка звонков", lambda: os.startfile(CALLS)),
        pystray.MenuItem("Запускать с Windows", toggle_start, checked=lambda _: autostart()),
        pystray.MenuItem("Выход", quit_app),
    ))
    icon.run_detached()
    threading.Thread(target=watch, daemon=True).start()
    if model_ready():
        threading.Thread(target=whisper, daemon=True).start()  # прогреть заранее — итог будет быстрее
    if cfg["first"]:
        cfg["first"] = False
        save_cfg()
        root.after(500, lambda: setup_window(root))
    root.mainloop()


if __name__ == "__main__":
    main()
