"""The "Bluetooth devices" window: see what's paired and nearby, pair, connect, disconnect, forget.

Tk runs on its own thread (the tray owns the main thread). Other threads never touch Tk: they put
requests on a queue that the window's thread polls. The window reads the engine's status a couple of
times a second and calls the engine's thread-safe methods for actions.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

logger = logging.getLogger(__name__)

TITLE = "Bluetooth devices - Windows Bluetooth Multicodec"
REFRESH_MS = 500
SEARCH_SECONDS = 12
KIND_LABELS = {"audio": "Speaker / headphones", "hid": "Mouse / keyboard", "hid-le": "Mouse / keyboard"}


def _signal(rssi: int | None) -> str:
    if rssi is None:
        return ""
    bars = 4 if rssi > -60 else 3 if rssi > -70 else 2 if rssi > -80 else 1
    return "▮" * bars + "▯" * (4 - bars)


class DevicesWindow:
    def __init__(self, engine, icon_image=None):
        self.engine = engine
        self.icon_image = icon_image
        self.requests: queue.Queue[str] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.root: tk.Tk | None = None

    # ------------------------------------------------------------------ any thread
    def show(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="devices-window", daemon=True)
            self.thread.start()
        else:
            self.requests.put("show")

    def close(self) -> None:
        """Close the window and wait for its thread, so Tk is torn down on its own thread before exit."""
        if self.thread is not None and self.thread.is_alive():
            self.requests.put("quit")
            self.thread.join(timeout=3)

    # ------------------------------------------------------------------ window thread
    def _run(self) -> None:
        self.root = root = tk.Tk()
        root.title(TITLE)
        root.geometry("640x560")
        root.minsize(520, 440)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        if self.icon_image is not None:
            try:
                from PIL import ImageTk
                self._icon = ImageTk.PhotoImage(self.icon_image)
                root.iconphoto(True, self._icon)
            except Exception:
                pass
        root.protocol("WM_DELETE_WINDOW", root.withdraw)  # hide; the tray can bring it back
        self._build(root)
        self._bring_to_front()
        self._search()
        root.after(REFRESH_MS, self._tick)
        root.after(SEARCH_SECONDS * 1000 + 1500, self._keep_searching)
        root.mainloop()
        # Tk objects must die on this thread: Tcl aborts the process ("Tcl_AsyncDelete: async handler
        # deleted by the wrong thread") if Python's GC frees them from another one later
        for name in [n for n, v in vars(self).items() if isinstance(v, (tk.Misc, tk.Variable))] + ["_icon"]:
            setattr(self, name, None)
        del root
        self.root = None
        import gc
        gc.collect()

    def _build(self, root: tk.Tk) -> None:
        pad = {"padx": 12, "pady": (10, 4)}
        self.status_var = tk.StringVar()
        self.message_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        ttk.Label(root, textvariable=self.message_var, foreground="#0a5", wraplength=600,
                  font=("Segoe UI", 9)).pack(anchor="w", padx=12)

        # My devices
        ttk.Label(root, text="My devices", font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)
        mine = ttk.Frame(root)
        mine.pack(fill="both", expand=True, padx=12)
        self.mine = ttk.Treeview(mine, columns=("type", "status"), show="tree headings", height=6,
                                 selectmode="browse")
        self.mine.heading("#0", text="Name")
        self.mine.heading("type", text="Type")
        self.mine.heading("status", text="Status")
        self.mine.column("#0", width=210)
        self.mine.column("type", width=140)
        self.mine.column("status", width=240)
        self.mine.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(mine, orient="vertical", command=self.mine.yview)
        bar.pack(side="right", fill="y")
        self.mine.configure(yscrollcommand=bar.set)
        self.mine.bind("<Double-1>", lambda e: self._connect())
        self.mine.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", padx=12, pady=6)
        self.connect_button = ttk.Button(buttons, text="Connect", command=self._connect)
        self.disconnect_button = ttk.Button(buttons, text="Disconnect", command=self._disconnect)
        self.forget_button = ttk.Button(buttons, text="Forget", command=self._forget)
        for b in (self.connect_button, self.disconnect_button, self.forget_button):
            b.pack(side="left", padx=(0, 6))

        # Nearby devices
        header = ttk.Frame(root)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="Nearby devices", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Label(header, textvariable=self.search_var, foreground="#666").pack(side="left", padx=10)
        nearby = ttk.Frame(root)
        nearby.pack(fill="both", expand=True, padx=12)
        self.nearby = ttk.Treeview(nearby, columns=("type", "signal"), show="tree headings", height=7,
                                   selectmode="browse")
        self.nearby.heading("#0", text="Name")
        self.nearby.heading("type", text="Type")
        self.nearby.heading("signal", text="Signal")
        self.nearby.column("#0", width=280)
        self.nearby.column("type", width=180)
        self.nearby.column("signal", width=80, anchor="center")
        self.nearby.pack(side="left", fill="both", expand=True)
        bar2 = ttk.Scrollbar(nearby, orient="vertical", command=self.nearby.yview)
        bar2.pack(side="right", fill="y")
        self.nearby.configure(yscrollcommand=bar2.set)
        self.nearby.bind("<Double-1>", lambda e: self._pair())
        self.nearby.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        buttons2 = ttk.Frame(root)
        buttons2.pack(fill="x", padx=12, pady=(6, 12))
        self.pair_button = ttk.Button(buttons2, text="Pair & connect", command=self._pair)
        self.search_button = ttk.Button(buttons2, text="Search again", command=self._search)
        self.pair_button.pack(side="left", padx=(0, 6))
        self.search_button.pack(side="left")
        ttk.Label(buttons2, text="Put the device in pairing mode before pairing.", foreground="#666").pack(
            side="right")

    def _bring_to_front(self) -> None:
        root = self.root
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.focus_force()

    # ------------------------------------------------------------------ data
    def _my_devices(self) -> list[tuple[str, str, str, str]]:
        """(address, name, type, status) for every paired device."""
        s = self.engine.status
        rows = []
        for address, name in sorted(self.engine.settings.devices.items(), key=lambda kv: kv[1].lower()):
            if s.device == address:
                status = (f"Streaming {s.streaming} · {s.kbps:.0f} kbps" if s.streaming and s.kbps
                          else f"Connected · {s.streaming}" if s.streaming else "Connecting...")
            else:
                status = "Paired"
            rows.append((address, name, "Speaker / headphones", status))
        connected_hid = self.engine.hid.connected()
        for address, info in sorted(self.engine.settings.hid_devices.items(), key=lambda kv: kv[1]["name"].lower()):
            rows.append((address, info["name"], "Mouse / keyboard",
                         "Connected" if address in connected_hid else "Asleep or off"))
        return rows

    def _nearby_devices(self) -> list[tuple[str, str, str, str]]:
        s = self.engine.status
        mine = set(self.engine.settings.devices) | set(self.engine.settings.hid_devices)
        found = [(address, name) for address, name in s.found.items() if address not in mine]
        # Strongest signal first: the device you're holding in pairing mode is usually the closest
        found.sort(key=lambda an: (-(s.found_rssi.get(an[0]) if s.found_rssi.get(an[0]) is not None else -999),
                                   an[1].lower()))
        return [(address, name, KIND_LABELS.get(s.found_kind.get(address, "audio"), ""),
                 _signal(s.found_rssi.get(address))) for address, name in found]

    @staticmethod
    def _sync(tree: ttk.Treeview, rows) -> None:
        """Update a tree in place, keeping the selection (rows keyed by address)."""
        wanted = {r[0] for r in rows}
        for item in tree.get_children():
            if item not in wanted:
                tree.delete(item)
        for index, (address, name, *values) in enumerate(rows):
            if tree.exists(address):
                tree.item(address, text=name, values=values)
                tree.move(address, "", index)
            else:
                tree.insert("", index, iid=address, text=name, values=values)

    def _tick(self) -> None:
        try:
            while True:
                request = self.requests.get_nowait()
                if request == "quit":
                    self.root.destroy()
                    return
                if request == "show":
                    self._bring_to_front()
                    if not self.engine.status.scanning and not self.engine.status.found:
                        self._search()
        except queue.Empty:
            pass
        s = self.engine.status
        if s.adapter_problem:
            self.status_var.set(s.adapter_problem)
        elif self.engine.device is None:
            self.status_var.set("Starting Bluetooth...")
        else:
            self.status_var.set(f"Bluetooth ready · {s.adapter or 'adapter'}")
        self.message_var.set(s.message or "")
        self._sync(self.mine, self._my_devices())
        self._sync(self.nearby, self._nearby_devices())
        if not self.nearby.get_children():
            self.search_var.set("Searching... put the device in pairing mode" if s.scanning
                                else "Nothing found yet. Is the device in pairing mode?")
        else:
            self.search_var.set("Searching..." if s.scanning else "")
        self._update_buttons()
        self.root.after(REFRESH_MS, self._tick)

    def _update_buttons(self) -> None:
        ready = self.engine.device is not None
        mine = self.mine.selection()
        address = mine[0] if mine else None
        audio = address in self.engine.settings.devices
        connected = address is not None and address == self.engine.status.device
        self.connect_button.state(["!disabled"] if ready and audio and not connected else ["disabled"])
        self.disconnect_button.state(["!disabled"] if connected else ["disabled"])
        self.forget_button.state(["!disabled"] if ready and address else ["disabled"])
        self.pair_button.state(["!disabled"] if ready and self.nearby.selection() else ["disabled"])
        self.search_button.state(["!disabled"] if ready and not self.engine.status.scanning else ["disabled"])

    # ------------------------------------------------------------------ actions
    def _selected(self, tree: ttk.Treeview) -> str | None:
        selection = tree.selection()
        return selection[0] if selection else None

    def _connect(self) -> None:
        address = self._selected(self.mine)
        if address in self.engine.settings.devices and address != self.engine.status.device:
            self.engine.connect(address)

    def _disconnect(self) -> None:
        if self._selected(self.mine) == self.engine.status.device:
            self.engine.disconnect()

    def _forget(self) -> None:
        address = self._selected(self.mine)
        if not address:
            return
        name = self.mine.item(address, "text")
        if messagebox.askyesno("Forget device", f"Forget {name}? You'll need to pair it again to use it.",
                               parent=self.root):
            self.engine.forget(address)

    def _pair(self) -> None:
        address = self._selected(self.nearby)
        if address:
            self.engine.pair(address, self.nearby.item(address, "text"))

    def _search(self, fresh: bool = True) -> None:
        if self.engine.device is not None and not self.engine.status.scanning:
            self.engine.scan(SEARCH_SECONDS, fresh=fresh)
            self.searching = True
        elif self.engine.device is None and self.root is not None:
            # Bluetooth still starting: try again shortly
            self.root.after(2000, lambda: self._search(fresh))

    def _keep_searching(self) -> None:
        """While the window is open, search continuously (keeping results) so a device can be put in
        pairing mode at any time. Pauses while the window is hidden."""
        if self.root is None:
            return
        if self.root.state() != "withdrawn" and not self.engine.status.scanning:
            self._search(fresh=False)
        self.root.after(1500, self._keep_searching)
