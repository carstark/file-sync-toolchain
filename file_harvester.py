#!/usr/bin/env python3
"""
file_harvester.py - Script 3 des file-sync-toolchain
======================================================
Version 1.0

Liest change_directive.json (aus journal_checker.py) und sammelt
alle betroffenen Dateien kontrolliert ein.

Ausgabestruktur:
  harvest/
  ├── staging/
  │   ├── mac/          ← COPY MAC
  │   ├── win/          ← COPY WIN
  │   └── both/         ← COPY (neu auf beiden, identisch)
  ├── conflicts/
  │   └── <rel_path>/
  │       ├── _mac      ← CONFLICT modified, Mac-Version
  │       └── _win      ← CONFLICT modified, Win-Version
  └── _GELOESCHT/
      └── YYYY-MM-DD/
          ├── del_win/  ← CONFLICT del Win (von USB gesichert)
          └── del_mac/  ← CONFLICT del Mac (von USB gesichert)

  harvest_report.json   ← maschinenlesbar für Script 4
  harvest_log.txt       ← menschenlesbar, chronologisch

Hash-Prüfung (SHA256) nur für kopierte Dateien – kein Vollscan.
"""

import json
import sys
import hashlib
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from datetime import datetime


# ==============================================================================
# AKTIONSKLASSEN (aus journal_checker.py – müssen übereinstimmen)
# ==============================================================================

A_OK           = "OK"
A_COPY_MAC     = "COPY MAC"
A_COPY_WIN     = "COPY WIN"
A_COPY         = "COPY"
A_DELETE       = "DELETE"
A_CONF_MOD     = "CONFLICT modified"
A_CONF_NEW     = "CONFLICT new"
A_CONF_DEL_MAC = "CONFLICT del Mac"
A_CONF_DEL_WIN = "CONFLICT del Win"

# Aktionen die der Harvester anfasst (OK wird still übersprungen)
HARVEST_ACTIONS = {
    A_COPY_MAC, A_COPY_WIN, A_COPY,
    A_CONF_MOD, A_CONF_NEW,
    A_CONF_DEL_MAC, A_CONF_DEL_WIN,
    A_DELETE,
}


# ==============================================================================
# HASHING
# ==============================================================================

def sha256(path: Path, chunk: int = 1 << 20) -> str:
    """SHA256-Hash einer Datei (1 MB Chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ==============================================================================
# KOPIEREN MIT VERIFIKATION
# ==============================================================================

def copy_verified(src: Path, dst: Path) -> tuple[bool, str]:
    """
    Kopiert src → dst und verifiziert per SHA256.
    Gibt (True, hash) oder (False, Fehlermeldung) zurück.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        h_src = sha256(src)
        h_dst = sha256(dst)
        if h_src != h_dst:
            dst.unlink(missing_ok=True)
            return False, f"Hash-Mismatch: {src.name}"
        return True, h_src
    except Exception as exc:
        return False, str(exc)


# ==============================================================================
# HARVESTER-LOGIK
# ==============================================================================

class Harvester:
    """
    Führt den Harvest durch. Läuft in einem eigenen Thread.
    Kommuniziert mit der GUI über Callbacks.
    """

    def __init__(
        self,
        directive_path: Path,
        mac_root:       Path | None,
        win_root:       Path | None,
        usb_root:       Path | None,
        harvest_root:   Path,
        on_progress:    callable,   # (current, total, filename, action)
        on_counter:     callable,   # (counter_name, delta)
        on_error:       callable,   # (rel_path, reason)
        on_done:        callable,   # (report)
        cancel_event:   threading.Event,
    ):
        self.directive_path = directive_path
        self.roots = {"mac": mac_root, "win": win_root, "usb": usb_root}
        self.harvest_root   = harvest_root
        self.on_progress    = on_progress
        self.on_counter     = on_counter
        self.on_error       = on_error
        self.on_done        = on_done
        self.cancel_event   = cancel_event

        self.today = datetime.now().strftime("%Y-%m-%d")
        self.log_lines: list[str] = []
        self.report_entries: list[dict] = []

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"[{ts}] {msg}")

    def _dst_staging(self, slot: str, rel_path: str) -> Path:
        return self.harvest_root / "staging" / slot / rel_path

    def _dst_conflict(self, rel_path: str, slot: str) -> Path:
        return self.harvest_root / "conflicts" / rel_path / f"_{slot}"

    def _dst_deleted(self, sub: str, rel_path: str) -> Path:
        return self.harvest_root / "_GELOESCHT" / self.today / sub / rel_path

    def _find_source(self, slot: str, rel_path: str) -> Path | None:
        root = self.roots.get(slot)
        if root is None:
            return None
        candidate = root / rel_path
        if candidate.is_file():
            return candidate
        # Windows-Backslash-Fallback
        candidate2 = root / rel_path.replace("/", "\\")
        if candidate2.is_file():
            return candidate2
        return None

    def _do_copy(self, src: Path, dst: Path,
                 rel_path: str, action: str) -> bool:
        ok, info = copy_verified(src, dst)
        if ok:
            self._log(f"OK   {action:<22s} {rel_path}")
            self.report_entries.append({
                "rel_path": rel_path,
                "action":   action,
                "status":   "ok",
                "src":      str(src),
                "dst":      str(dst),
                "sha256":   info,
            })
        else:
            self._log(f"ERR  {action:<22s} {rel_path}  → {info}")
            self.report_entries.append({
                "rel_path": rel_path,
                "action":   action,
                "status":   "error",
                "src":      str(src),
                "dst":      str(dst),
                "error":    info,
            })
            self.on_error(rel_path, info)
        return ok

    def run(self):
        # Directive laden
        try:
            raw = self.directive_path.read_text(encoding="utf-8")
            directive = json.loads(raw)
        except Exception as exc:
            self.on_error("(directive)", str(exc))
            return

        entries = [e for e in directive.get("entries", [])
                   if e["action"] in HARVEST_ACTIONS]
        total = len(entries)

        self._log(f"Harvest gestartet – {total} Einträge zu verarbeiten")
        self._log(f"Harvest-Verzeichnis: {self.harvest_root}")

        for i, entry in enumerate(entries):
            if self.cancel_event.is_set():
                self._log("⏹ Abgebrochen durch Nutzer.")
                break

            rel   = entry["rel_path"]
            act   = entry["action"]
            pres  = entry.get("presence", {})

            self.on_progress(i + 1, total, rel, act)

            # ----------------------------------------------------------
            # COPY MAC – neu auf Mac, auf USB+Win kopieren
            # ----------------------------------------------------------
            if act == A_COPY_MAC:
                src = self._find_source("mac", rel)
                if src is None:
                    self.on_error(rel, "COPY MAC: Quelldatei auf Mac nicht gefunden")
                    self._log(f"MISS COPY MAC            {rel}")
                    self.on_counter("not_found", 1)
                    continue
                dst = self._dst_staging("mac", rel)
                if self._do_copy(src, dst, rel, act):
                    self.on_counter("copy_mac", 1)

            # ----------------------------------------------------------
            # COPY WIN – neu auf Win, auf USB+Mac kopieren
            # ----------------------------------------------------------
            elif act == A_COPY_WIN:
                src = self._find_source("win", rel)
                if src is None:
                    self.on_error(rel, "COPY WIN: Quelldatei auf Win nicht gefunden")
                    self._log(f"MISS COPY WIN            {rel}")
                    self.on_counter("not_found", 1)
                    continue
                dst = self._dst_staging("win", rel)
                if self._do_copy(src, dst, rel, act):
                    self.on_counter("copy_win", 1)

            # ----------------------------------------------------------
            # COPY – neu auf beiden, identisch → von Mac nehmen
            # ----------------------------------------------------------
            elif act == A_COPY:
                src = self._find_source("mac", rel)
                if src is None:
                    src = self._find_source("win", rel)
                if src is None:
                    self.on_error(rel, "COPY: Quelldatei weder auf Mac noch Win gefunden")
                    self.on_counter("not_found", 1)
                    continue
                dst = self._dst_staging("both", rel)
                if self._do_copy(src, dst, rel, act):
                    self.on_counter("copy_both", 1)

            # ----------------------------------------------------------
            # CONFLICT modified – beide Versionen sichern
            # ----------------------------------------------------------
            elif act in (A_CONF_MOD, A_CONF_NEW):
                missed = 0
                for slot in ("mac", "win"):
                    if not pres.get(slot):
                        continue
                    src = self._find_source(slot, rel)
                    if src is None:
                        self.on_error(rel, f"{act}: {slot}-Version nicht gefunden")
                        missed += 1
                        continue
                    dst = self._dst_conflict(rel, slot)
                    self._do_copy(src, dst, rel, f"{act} [{slot}]")
                if missed == 0:
                    self.on_counter("conflict_mod", 1)
                else:
                    self.on_counter("not_found", missed)

            # ----------------------------------------------------------
            # CONFLICT del Win – von USB sichern (Win hat gelöscht)
            # ----------------------------------------------------------
            elif act == A_CONF_DEL_WIN:
                src = self._find_source("usb", rel)
                if src is None:
                    self.on_error(rel, "CONFLICT del Win: USB-Quelle nicht gefunden")
                    self.on_counter("not_found", 1)
                    continue
                dst = self._dst_deleted("del_win", rel)
                if self._do_copy(src, dst, rel, act):
                    self.on_counter("conflict_del", 1)

            # ----------------------------------------------------------
            # CONFLICT del Mac – von USB sichern (Mac hat gelöscht)
            # ----------------------------------------------------------
            elif act == A_CONF_DEL_MAC:
                src = self._find_source("usb", rel)
                if src is None:
                    self.on_error(rel, "CONFLICT del Mac: USB-Quelle nicht gefunden")
                    self.on_counter("not_found", 1)
                    continue
                dst = self._dst_deleted("del_mac", rel)
                if self._do_copy(src, dst, rel, act):
                    self.on_counter("conflict_del", 1)

            # ----------------------------------------------------------
            # DELETE – auf beiden Rechnern gelöscht
            # Harvester tut nichts (Script 4 löscht von USB nach Bestätigung)
            # ----------------------------------------------------------
            elif act == A_DELETE:
                self._log(f"SKIP DELETE              {rel}  → Script 4 entscheidet")
                self.report_entries.append({
                    "rel_path": rel,
                    "action":   act,
                    "status":   "pending_delete",
                    "note":     "Wird von change_execution.py nach Bestätigung gelöscht",
                })
                self.on_counter("delete_pending", 1)

        # Report speichern
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        report = {
            "meta": {
                "created":        datetime.now().isoformat(),
                "tool":           "file_harvester.py",
                "version":        "1.0",
                "directive":      str(self.directive_path),
                "harvest_root":   str(self.harvest_root),
                "cancelled":      self.cancel_event.is_set(),
            },
            "entries": self.report_entries,
            "summary": {
                "total_processed": len(self.report_entries),
                "ok":    sum(1 for e in self.report_entries if e["status"] == "ok"),
                "error": sum(1 for e in self.report_entries if e["status"] == "error"),
                "pending_delete": sum(
                    1 for e in self.report_entries if e["status"] == "pending_delete"
                ),
            },
        }

        report_path = self.harvest_root / f"harvest_report_{ts}.json"
        log_path    = self.harvest_root / f"harvest_log_{ts}.txt"

        try:
            self.harvest_root.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            log_path.write_text(
                "\n".join(self.log_lines), encoding="utf-8"
            )
        except Exception as exc:
            self.on_error("(report)", str(exc))

        self._log(f"Harvest abgeschlossen. Report: {report_path.name}")
        self.on_done(report, report_path, log_path)


# ==============================================================================
# GUI
# ==============================================================================

class HarvesterApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🌾 File Harvester")
        self.root.geometry("860x720")
        self.root.resizable(True, True)

        self.directive_var  = tk.StringVar()
        self.mac_root_var   = tk.StringVar()
        self.win_root_var   = tk.StringVar()
        self.usb_root_var   = tk.StringVar()
        self.harvest_dir_var = tk.StringVar()

        self.cancel_event = threading.Event()
        self._errors: list[tuple[str, str]] = []

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg="#1a5276", height=58)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="🌾 File Harvester – Dateien einsammeln",
                 font=("Helvetica", 15, "bold"),
                 bg="#1a5276", fg="white").pack(pady=14)

        # Eingaben
        inp = tk.LabelFrame(self.root, text="Konfiguration", padx=10, pady=8)
        inp.pack(fill=tk.X, padx=10, pady=(8, 4))

        def file_row(parent, label, var, pick_fn):
            row = tk.Frame(parent)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label, width=18, anchor="w",
                     font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
            tk.Entry(row, textvariable=var,
                     font=("Courier", 9)).pack(side=tk.LEFT,
                                               fill=tk.X, expand=True)
            tk.Button(row, text="📂", command=pick_fn,
                      width=3).pack(side=tk.LEFT, padx=(4, 0))

        file_row(inp, "Directive (JSON):",  self.directive_var,
                 self._pick_directive)
        file_row(inp, "Mac-Wurzel:",        self.mac_root_var,
                 lambda: self._pick_dir(self.mac_root_var))
        file_row(inp, "Win-Wurzel:",        self.win_root_var,
                 lambda: self._pick_dir(self.win_root_var))
        file_row(inp, "USB-Wurzel:",        self.usb_root_var,
                 lambda: self._pick_dir(self.usb_root_var))
        file_row(inp, "Harvest-Verzeichnis:", self.harvest_dir_var,
                 lambda: self._pick_dir(self.harvest_dir_var))

        # Zähler-Panel
        cnt = tk.LabelFrame(self.root, text="Fortschritt", padx=10, pady=8)
        cnt.pack(fill=tk.X, padx=10, pady=4)

        self.progress_label = tk.Label(
            cnt, text="Bereit.", anchor="w", font=("Helvetica", 10))
        self.progress_label.pack(fill=tk.X)

        self.file_label = tk.Label(
            cnt, text="", anchor="w", font=("Courier", 9), fg="#555")
        self.file_label.pack(fill=tk.X)

        self.progress_bar = ttk.Progressbar(cnt, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(4, 6))

        # Zähler-Grid
        grid = tk.Frame(cnt)
        grid.pack(fill=tk.X)

        self._counter_vars = {}
        counter_defs = [
            ("copy_mac",      "✅ COPY MAC",        "#2980b9"),
            ("copy_win",      "✅ COPY WIN",        "#2980b9"),
            ("copy_both",     "✅ COPY (beide)",    "#2980b9"),
            ("conflict_mod",  "📋 CONFLICT mod",   "#e67e22"),
            ("conflict_del",  "⚠️  CONFLICT del",   "#e67e22"),
            ("delete_pending","🗑  DELETE pending", "#c0392b"),
            ("not_found",     "❌ Nicht gefunden",  "#c0392b"),
        ]

        for col, (key, label, color) in enumerate(counter_defs):
            var = tk.StringVar(value="0")
            self._counter_vars[key] = var
            cell = tk.Frame(grid, relief=tk.GROOVE, bd=1, padx=6, pady=4)
            cell.grid(row=0, column=col, padx=3, sticky="ew")
            grid.columnconfigure(col, weight=1)
            tk.Label(cell, text=label, font=("Helvetica", 8),
                     fg=color, wraplength=90,
                     justify="center").pack()
            tk.Label(cell, textvariable=var,
                     font=("Helvetica", 13, "bold")).pack()

        self._counters = {k: 0 for k in self._counter_vars}

        # Fehler-Panel (versteckt bis Fehler auftreten)
        self.error_frame = tk.LabelFrame(
            self.root, text="❌ Nicht gefunden / Fehler",
            padx=8, pady=6)
        # wird erst eingeblendet wenn nötig

        self.error_listbox = tk.Listbox(
            self.error_frame, font=("Courier", 8),
            height=5, bg="#fff0f0")
        esb = ttk.Scrollbar(self.error_frame, orient="vertical",
                             command=self.error_listbox.yview)
        self.error_listbox.configure(yscrollcommand=esb.set)
        esb.pack(side=tk.RIGHT, fill=tk.Y)
        self.error_listbox.pack(fill=tk.BOTH, expand=True)

        # Log-Bereich
        log_frame = tk.LabelFrame(self.root, text="Log", padx=8, pady=6)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        self.log_text = tk.Text(log_frame, font=("Courier", 8),
                                state=tk.DISABLED, height=10, bg="#1e1e1e",
                                fg="#d4d4d4", insertbackground="white")
        lsb = ttk.Scrollbar(log_frame, orient="vertical",
                             command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Statusleiste
        self.status_var = tk.StringVar(value="Bereit.")
        tk.Label(self.root, textvariable=self.status_var,
                 anchor="w", font=("Helvetica", 9),
                 relief=tk.SUNKEN).pack(fill=tk.X, padx=10, pady=(0, 2))

        # Buttons
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=10, pady=6)

        self.start_btn = tk.Button(
            btn_frame, text="▶️  Harvest starten",
            command=self._start,
            font=("Helvetica", 12, "bold"),
            bg="#1a5276", fg="white", height=2, width=22)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.cancel_btn = tk.Button(
            btn_frame, text="⏹ Abbrechen",
            command=self._cancel,
            font=("Helvetica", 12),
            state=tk.DISABLED, height=2, width=15)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)

        self.log_btn = tk.Button(
            btn_frame, text="📋 Log öffnen",
            command=self._open_log,
            font=("Helvetica", 12),
            state=tk.DISABLED, height=2, width=15)
        self.log_btn.pack(side=tk.LEFT, padx=5)

        self._log_path = None

    # ------------------------------------------------------------------
    # Picker
    # ------------------------------------------------------------------

    def _pick_directive(self):
        p = filedialog.askopenfilename(
            title="change_directive.json wählen...",
            filetypes=[("JSON", "*.json"), ("Alle", "*.*")]
        )
        if p:
            self.directive_var.set(p)
            # Harvest-Verzeichnis vorschlagen (neben der Directive)
            if not self.harvest_dir_var.get():
                self.harvest_dir_var.set(
                    str(Path(p).parent / "harvest")
                )

    def _pick_dir(self, var: tk.StringVar):
        d = filedialog.askdirectory(
            initialdir=var.get() or str(Path.home())
        )
        if d:
            var.set(d)

    # ------------------------------------------------------------------
    # Callbacks aus Harvester-Thread
    # ------------------------------------------------------------------

    def _cb_progress(self, current, total, filename, action):
        pct = int(current / total * 100) if total else 0
        self.root.after(0, lambda: [
            self.progress_label.config(
                text=f"Verarbeite {current:,} / {total:,}  [{pct}%]"
            ),
            self.file_label.config(
                text=f"  {action:<22s}  {filename[-72:]}"
            ),
            self.progress_bar.config(value=pct),
        ])

    def _cb_counter(self, name, delta):
        self._counters[name] = self._counters.get(name, 0) + delta
        val = str(self._counters[name])
        self.root.after(0, lambda: self._counter_vars[name].set(val))

    def _cb_error(self, rel_path, reason):
        msg = f"{rel_path}  →  {reason}"
        self._errors.append((rel_path, reason))

        def _update():
            # Fehler-Panel einblenden beim ersten Fehler
            if not self.error_frame.winfo_ismapped():
                self.error_frame.pack(
                    fill=tk.X, padx=10, pady=2,
                    before=self.log_text.master
                )
            self.error_listbox.insert(tk.END, msg)
            self.error_listbox.see(tk.END)

        self.root.after(0, _update)
        self._append_log(f"ERR  {msg}")

    def _cb_done(self, report, report_path, log_path):
        self._log_path = log_path
        s = report["summary"]

        def _update():
            self.start_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            self.log_btn.config(state=tk.NORMAL)
            self.progress_bar.config(value=100)
            self.progress_label.config(
                text=f"✅ Fertig!  {s['ok']:,} OK  |  "
                     f"{s['error']:,} Fehler  |  "
                     f"{s['pending_delete']:,} DELETE ausstehend"
            )
            self.status_var.set(
                f"✅ Harvest abgeschlossen – Report: {report_path.name}"
            )
            self._append_log(
                f"=== FERTIG === OK:{s['ok']}  "
                f"Fehler:{s['error']}  "
                f"Delete-ausstehend:{s['pending_delete']}"
            )

        self.root.after(0, _update)

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"

        def _update():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, line)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

        self.root.after(0, _update)

    # ------------------------------------------------------------------
    # Start / Cancel
    # ------------------------------------------------------------------

    def _start(self):
        directive_p = self.directive_var.get().strip()
        harvest_p   = self.harvest_dir_var.get().strip()

        if not directive_p or not Path(directive_p).is_file():
            messagebox.showerror("Fehler", "Bitte eine gültige Directive-Datei wählen.")
            return
        if not harvest_p:
            messagebox.showerror("Fehler", "Bitte ein Harvest-Verzeichnis angeben.")
            return

        def opt_path(s):
            s = s.strip()
            return Path(s) if s else None

        mac_root = opt_path(self.mac_root_var.get())
        win_root = opt_path(self.win_root_var.get())
        usb_root = opt_path(self.usb_root_var.get())

        # Warnung wenn Wurzeln fehlen
        missing = []
        if mac_root is None: missing.append("Mac-Wurzel")
        if win_root is None: missing.append("Win-Wurzel")
        if usb_root is None: missing.append("USB-Wurzel")
        if missing:
            ok = messagebox.askyesno(
                "Fehlende Quellen",
                f"Folgende Quellen sind nicht gesetzt:\n"
                f"  {', '.join(missing)}\n\n"
                f"Betroffene Einträge werden als 'Nicht gefunden' geloggt.\n"
                f"Trotzdem starten?"
            )
            if not ok:
                return

        # Reset
        self.cancel_event.clear()
        self._errors.clear()
        self._counters = {k: 0 for k in self._counter_vars}
        for var in self._counter_vars.values():
            var.set("0")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.error_listbox.delete(0, tk.END)
        self.progress_bar.config(value=0)
        self._log_path = None

        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.log_btn.config(state=tk.DISABLED)
        self.status_var.set("⏳ Harvest läuft …")

        harvester = Harvester(
            directive_path = Path(directive_p),
            mac_root       = mac_root,
            win_root       = win_root,
            usb_root       = usb_root,
            harvest_root   = Path(harvest_p),
            on_progress    = self._cb_progress,
            on_counter     = self._cb_counter,
            on_error       = self._cb_error,
            on_done        = self._cb_done,
            cancel_event   = self.cancel_event,
        )

        self._append_log(f"Starte Harvest …")
        self._append_log(f"Directive: {directive_p}")
        self._append_log(f"Harvest-Dir: {harvest_p}")

        threading.Thread(target=harvester.run, daemon=True).start()

    def _cancel(self):
        self.cancel_event.set()
        self.status_var.set("⏹ Abbrechen …")
        self.cancel_btn.config(state=tk.DISABLED)

    def _open_log(self):
        if self._log_path and self._log_path.is_file():
            import subprocess, platform
            if platform.system() == "Darwin":
                subprocess.Popen(["open", str(self._log_path)])
            elif platform.system() == "Windows":
                subprocess.Popen(["notepad", str(self._log_path)])
            else:
                subprocess.Popen(["xdg-open", str(self._log_path)])

    def run(self):
        self.root.mainloop()


# ==============================================================================
# CLI
# ==============================================================================

def cli_mode(args: list[str]):
    import argparse

    parser = argparse.ArgumentParser(
        description="file_harvester.py – Dateien aus change_directive einsammeln"
    )
    parser.add_argument("--directive", required=True,
                        help="Pfad zur change_directive.json")
    parser.add_argument("--mac",    help="Mac-Wurzelverzeichnis")
    parser.add_argument("--win",    help="Win-Wurzelverzeichnis")
    parser.add_argument("--usb",    help="USB-Wurzelverzeichnis")
    parser.add_argument("--output", required=True,
                        help="Harvest-Zielverzeichnis")
    parsed = parser.parse_args(args)

    def opt(s):
        return Path(s) if s else None

    cancel = threading.Event()
    done_data = {}

    def on_progress(cur, total, fname, act):
        print(f"\r  {cur:>6,}/{total:,}  {act:<22s}  {fname[-55:]}",
              end="", flush=True)

    def on_counter(name, delta):
        pass  # CLI zeigt Endsumme

    def on_error(rel, reason):
        print(f"\n❌  {rel}  →  {reason}")

    def on_done(report, report_path, log_path):
        done_data["report"] = report
        done_data["report_path"] = report_path

    h = Harvester(
        directive_path = Path(parsed.directive),
        mac_root       = opt(parsed.mac),
        win_root       = opt(parsed.win),
        usb_root       = opt(parsed.usb),
        harvest_root   = Path(parsed.output),
        on_progress    = on_progress,
        on_counter     = on_counter,
        on_error       = on_error,
        on_done        = on_done,
        cancel_event   = cancel,
    )

    print(f"🌾 File Harvester")
    print(f"   Directive: {parsed.directive}")
    print(f"   Output:    {parsed.output}\n")
    h.run()

    if "report" in done_data:
        s = done_data["report"]["summary"]
        print(f"\n\n{'='*50}")
        print(f"  OK:               {s['ok']:>7,}")
        print(f"  Fehler:           {s['error']:>7,}")
        print(f"  DELETE ausstehend:{s['pending_delete']:>7,}")
        print(f"{'='*50}")
        print(f"  Report: {done_data['report_path']}")


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    if "--cli" in sys.argv:
        args = sys.argv[1:]
        args.remove("--cli")
        cli_mode(args)
    else:
        app = HarvesterApp()
        app.run()
