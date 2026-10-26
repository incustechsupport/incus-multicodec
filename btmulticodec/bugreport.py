"""The "Report a bug" window: sends a description, the user's email (optional), a system summary and
(if allowed) the end of the app's log to software.incustech.app, where it's saved and emailed to us.

Like the devices window, Tk runs on this window's own thread and nothing else touches it.
"""

from __future__ import annotations

import json
import logging
import platform
import queue
import threading
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk

from . import __version__, adapters
from .settings import LOG_FILE

logger = logging.getLogger(__name__)

APP_NAME = "Windows Bluetooth Multicodec"
TITLE = f"Report a bug - {APP_NAME}"
REPORT_URL = "https://software.incustech.app/api/bug-report"
LOG_TAIL_BYTES = 400_000  # the server accepts up to 1.2 MB; the end of the log is the part that matters


def system_summary(engine) -> str:
    lines = [f"App: {APP_NAME} {__version__}",
             f"Windows: {platform.platform()} ({platform.machine()})"]
    s = engine.status
    lines.append(f"Adapter: {s.adapter or 'none'}" + (f" (problem: {s.adapter_problem})" if s.adapter_problem else ""))
    if s.streaming:
        lines.append(f"Streaming: {s.streaming}, {s.kbps:.0f} kbps to {s.device_name}")
    elif s.device_name:
        lines.append(f"Device: {s.device_name} (not streaming)")
    if s.codecs:
        lines.append(f"Device codecs: {', '.join(s.codecs)}")
    st = engine.settings
    lines.append(f"Settings: codec={st.codec} ldac_quality={st.ldac_quality} source={st.source} "
                 f"mute_speakers={st.mute_speakers}")
    try:
        for a in adapters.list_adapters():
            verdict, chip, _ = adapters.chip_support(a)
            owner = "Multicodec" if a.ours else ("Windows" if a.windows_owned else a.service or "no driver")
            lines.append(f"USB adapter: {a.name} {a.usb_id} ({chip}, {verdict}), used by {owner}"
                         + (", restart pending" if a.restart_pending else ""))
    except Exception as error:
        lines.append(f"USB adapters: couldn't list ({error})")
    return "\n".join(lines)


def log_tail() -> str:
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read()
    except OSError:
        return ""
    if size > LOG_TAIL_BYTES:
        data = data[data.find(b"\n") + 1:]  # start on a whole line
    return data.decode("utf-8", errors="replace")


def send(description: str, email: str, system: str, log: str) -> str:
    """POST the report; returns the server's reference for it. Raises RuntimeError with a readable message."""
    body = json.dumps({"app": APP_NAME, "version": __version__, "source": "app", "description": description,
                       "email": email, "system": system, "log": log}).encode("utf-8")
    request = urllib.request.Request(REPORT_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": f"windows-bluetooth-multicodec/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"{}").get("id", "")
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get("error")
        except Exception:
            message = None
        if error.code == 429:
            message = "Too many reports in a short time. Please wait a minute and try again."
        raise RuntimeError(message or f"The server said {error.code} {error.reason}.") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise RuntimeError(f"Couldn't reach software.incustech.app ({reason}). Check your internet connection.") from None


class BugReportWindow:
    def __init__(self, engine, icon_image=None):
        self.engine = engine
        self.icon_image = icon_image
        self.requests: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------ any thread
    def show(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="bug-report-window", daemon=True)
            self.thread.start()
        else:
            self.requests.put("show")

    def close(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.requests.put("quit")
            self.thread.join(timeout=3)

    # ------------------------------------------------------------------ window thread
    def _run(self) -> None:
        root = tk.Tk()
        root.title(TITLE)
        root.geometry("560x520")
        root.minsize(460, 440)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        icon = None
        if self.icon_image is not None:
            try:
                from PIL import ImageTk
                icon = ImageTk.PhotoImage(self.icon_image)
                root.iconphoto(True, icon)
            except Exception:
                pass
        # The system summary is gathered once, up front, so "what's sent" matches what was shown
        system = system_summary(self.engine)
        widgets = self._build(root, system)
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.focus_force()
        widgets["text"].focus_set()

        def poll():
            try:
                while True:
                    request = self.requests.get_nowait()
                    if request == "quit":
                        root.destroy()
                        return
                    if request == "show":
                        root.deiconify()
                        root.lift()
                        root.focus_force()
                    elif isinstance(request, tuple):  # ("sent", id) / ("failed", message) from the sender
                        self._finished(root, widgets, *request)
            except queue.Empty:
                pass
            root.after(150, poll)

        root.after(150, poll)
        root.mainloop()
        # Tk objects must die on this thread (see DevicesWindow._run)
        widgets.clear()
        del root, icon
        import gc
        gc.collect()

    def _build(self, root: tk.Tk, system: str) -> dict:
        pad = {"padx": 12}
        ttk.Label(root, text="What went wrong?", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
        ttk.Label(root, text="What you were doing, what you expected, and what happened instead. "
                             "The make and model of your headphones or speaker helps too.",
                  wraplength=520, foreground="#555").pack(anchor="w", **pad)
        box = ttk.Frame(root)
        box.pack(fill="both", expand=True, padx=12, pady=(6, 0))
        text = tk.Text(box, height=10, wrap="word", font=("Segoe UI", 10), undo=True, relief="solid", borderwidth=1)
        text.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        bar.pack(side="right", fill="y")
        text.configure(yscrollcommand=bar.set)

        ttk.Label(root, text="Your email (optional, only if you'd like a reply)").pack(anchor="w", padx=12, pady=(10, 2))
        email = tk.StringVar()
        ttk.Entry(root, textvariable=email).pack(fill="x", **pad)

        include_log = tk.BooleanVar(value=True)
        ttk.Checkbutton(root, variable=include_log,
                        text="Include the app's log (it lists your Bluetooth devices' names and addresses)"
                        ).pack(anchor="w", padx=12, pady=(10, 0))
        ttk.Label(root, text="Always included: app and Windows version, your Bluetooth adapters, the codec "
                             "in use and the app's settings.", wraplength=520, foreground="#555"
                  ).pack(anchor="w", padx=12, pady=(2, 0))

        status = tk.StringVar()
        ttk.Label(root, textvariable=status, wraplength=520).pack(anchor="w", padx=12, pady=(8, 0))
        buttons = ttk.Frame(root)
        buttons.pack(fill="x", padx=12, pady=12)
        close = ttk.Button(buttons, text="Close", command=root.destroy)
        close.pack(side="right")
        widgets = {"text": text, "email": email, "include_log": include_log, "status": status, "close": close}
        sendb = ttk.Button(buttons, text="Send report", command=lambda: self._send(widgets, system))
        sendb.pack(side="right", padx=(0, 6))
        widgets["send"] = sendb
        ttk.Button(buttons, text="See what's sent", command=lambda: self._preview(root, widgets, system)).pack(side="left")
        return widgets

    def _fields(self, widgets: dict, system: str) -> tuple[str, str, str, str]:
        description = widgets["text"].get("1.0", "end").strip()
        log = log_tail() if widgets["include_log"].get() else ""
        return description, widgets["email"].get().strip(), system, log

    def _preview(self, root: tk.Tk, widgets: dict, system: str) -> None:
        description, email, system, log = self._fields(widgets, system)
        win = tk.Toplevel(root)
        win.title("What's sent")
        win.geometry("700x520")
        view = tk.Text(win, wrap="none", font=("Consolas", 9))
        view.pack(fill="both", expand=True)
        view.insert("1.0", f"Email: {email or '(none)'}\n\nWhat went wrong:\n{description or '(not written yet)'}\n\n"
                           f"System:\n{system}\n\nLog ({len(log):,} characters):\n{log or '(not included)'}")
        view.configure(state="disabled")

    def _send(self, widgets: dict, system: str) -> None:
        description, email, system, log = self._fields(widgets, system)
        if len(description) < 10:
            widgets["status"].set("Please describe what went wrong (at least a sentence).")
            return
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            widgets["status"].set("That email address doesn't look right.")
            return
        widgets["send"].state(["disabled"])
        widgets["status"].set("Sending...")

        def worker():
            try:
                reference = send(description, email, system, log)
                logger.info("bug report sent (%s)", reference)
                self.requests.put(("sent", reference))
            except Exception as error:
                logger.warning("bug report failed: %s", error)
                self.requests.put(("failed", str(error)))
        threading.Thread(target=worker, name="bug-report-send", daemon=True).start()

    def _finished(self, root: tk.Tk, widgets: dict, outcome: str, detail: str) -> None:
        if outcome == "sent":
            widgets["text"].delete("1.0", "end")
            widgets["status"].set(f"Thanks, your report has been sent (reference {detail})."
                                  + (" We'll reply by email if we need more details." if widgets["email"].get().strip() else ""))
            widgets["close"].focus_set()
        else:
            widgets["status"].set(f"The report wasn't sent: {detail}")
        widgets["send"].state(["!disabled"])
