#!/usr/bin/env python3
"""
file_harvester.py  –  Script 3 des file-sync-toolchain
========================================================
Version 2.0  –  Zwei-Lauf-fähig, Roots aus Directive

Ablauf:
  Lauf 1  (z.B. Mac + USB angeschlossen)
    → liest change_directive.json vom USB (_sync/DATUM/)
    → erkennt welche Roots gerade erreichbar sind
    → erntet alle Einträge deren Quelle verfügbar ist
    → speichert harvest_report.json auf USB
    → meldet was noch fehlt und bittet USB umzustecken

  Lauf 2  (z.B. Win + USB angeschlossen)
    → findet harvest_report.json von Lauf 1
    → verarbeitet nur noch offene (pending) Einträge
    → meldet Harvest complete wenn alles erledigt

Ausgabe auf USB (_sync/DATUM/harvest/):
  staging/mac/        ← COPY MAC
  staging/win/        ← COPY WIN
  staging/both/       ← COPY identisch auf beiden
  conflicts/<path>/
      _mac / _win     ← CONFLICT modified, beide Versionen
  _GELOESCHT/DATUM/
      del_win/        ← von USB gesichert (Win hatte gelöscht)
      del_mac/        ← von USB gesichert (Mac hatte gelöscht)

  harvest_report.json   ← Zustandsspeicher zwischen Läufen
  harvest_log_<ts>.txt  ← menschenlesbares Log
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
# AKTIONSKLASSEN
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

HARVEST_ACTIONS = {
    A_COPY_MAC, A_COPY_WIN, A_COPY,
    A_CONF_MOD, A_CONF_NEW,
    A_CONF_DEL_MAC, A_CONF_DEL_WIN,
    A_DELETE,
}

# Welcher Slot muss als Quelle erreichbar sein
ACTION_SOURCES = {
    A_COPY_MAC:     ["mac"],
    A_COPY_WIN:     ["win"],
    A_COPY:         ["mac", "win"],   # einer reicht
    A_CONF_MOD:     ["mac", "win"],   # beide gebraucht
    A_CONF_NEW:     ["mac", "win"],
    A_CONF_DEL_MAC: ["usb"],
    A_CONF_DEL_WIN: ["usb"],
    A_DELETE:       [],               # nichts kopieren, Script 4 löscht
}


# ==============================================================================
# HASHING & KOPIEREN
# ==============================================================================

def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def copy_verified(src: Path, dst: Path) -> tuple[bool, str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        h_src = sha256(src)
        h_dst = sha256(dst)
        if h_src != h_dst:
            dst.unlink(missing_ok=True)
            return False, f"Hash-Mismatch nach Kopieren"
        return True, h_src
    except Exception as exc:
        return False, str(exc)


# ==============================================================================
# ROOT-AUFLÖSUNG
# ==============================================================================

def find_file_in_roots(roots: list[str], rel_path: str) -> Path | None:
    """
    Sucht rel_path in allen bekannten Roots eines Slots.
    Gibt den ersten Treffer zurück, None wenn nicht gefunden.
    """
    for root in roots:
        r = Path(root)
        if not r.is_dir():
            continue
        candidate = r / rel_path
        if candidate.is_file():
            return candidate
        # Windows-Backslash-Fallback
        candidate2 = r / rel_path.replace("/", "\\")
        if candidate2.is_file():
            return candidate2
    return None


def roots_reachable(roots: list[str]) -> bool:
    """True wenn mindestens eine Root gerade gemountet/erreichbar ist."""
    return any(Path(r).is_dir() for r in roots)


# ==============================================================================
# HARVESTER-KERN
# ==============================================================================

class Harvester:

    def __init__(
        self,
        directive:      dict,
        directive_path: Path,
        harvest_root:   Path,
        on_progress:    callable,
        on_counter:     callable,
        on_error:       callable,
        on_log:         callable,
        on_done:        callable,
        cancel_event:   threading.Event,
    ):
        self.directive      = directive
        self.directive_path = directive_path
        self.harvest_root   = harvest_root
        self.on_progress    = on_progress
        self.on_counter     = on_counter
        self.on_error       = on_error
        self.on_log         = on_log
        self.on_done        = on_done
        self.cancel_event   = cancel_event

        self.today = datetime.now().strftime("%Y-%m-%d")
        self.report_entries: list[dict] = []

    # Pfad-Helfer
    def _staging(self, slot: str, rel: str) -> Path:
        return self.harvest_root / "staging" / slot / rel

    def _conflict(self, rel: str, slot: str) -> Path:
        return self.harvest_root / "conflicts" / rel / f"_{slot}"

    def _deleted(self, sub: str, rel: str) -> Path:
        return self.harvest_root / "_GELOESCHT" / self.today / sub / rel

    def _do_copy(self, src: Path, dst: Path, rel: str, action: str) -> bool:
        ok, info = copy_verified(src, dst)
        status = "ok" if ok else "error"
        self.on_log(f"{'OK  ' if ok else 'ERR '} {action:<24s} {rel}")
        self.report_entries.append({
            "rel_path": rel, "action": action, "status": status,
            "src": str(src), "dst": str(dst),
            "sha256" if ok else "error": info,
        })
        if not ok:
            self.on_error(rel, info)
        return ok

    def run(self):
        sources = self.directive.get("sources", {})
        entries = [e for e in self.directive.get("entries", [])
                   if e["action"] in HARVEST_ACTIONS]

        # Vorhandenen Report laden (Lauf 2: bereits erledigte überspringen)
        done_keys = self._load_done_keys()
        pending = [e for e in entries if e["rel_path"] not in done_keys]

        total = len(pending)
        self.on_log(f"Harvest gestartet – {total} ausstehende Einträge")
        self.on_log(f"  (von {len(entries)} gesamt, {len(done_keys)} bereits erledigt)")
        self.on_log(f"Harvest-Verzeichnis: {self.harvest_root}")

        for i, entry in enumerate(pending):
            if self.cancel_event.is_set():
                self.on_log("⏹ Abgebrochen.")
                break

            rel    = entry["rel_path"]
            act    = entry["action"]
            pres   = entry.get("presence", {})

            self.on_progress(i + 1, total, rel, act)

            needed_slots = ACTION_SOURCES.get(act, [])

            # Prüfen ob benötigte Quellen erreichbar sind
            if act in (A_CONF_MOD, A_CONF_NEW):
                # Beide Versionen sammeln – was erreichbar ist, wird kopiert
                copied_any = False
                for slot in ("mac", "win"):
                    if not pres.get(slot):
                        continue
                    roots = sources.get(slot, {}).get("roots", [])
                    if not roots_reachable(roots):
                        self.on_log(f"SKIP [{slot} nicht erreichbar]  {rel}")
                        continue
                    src = find_file_in_roots(roots, rel)
                    if src is None:
                        self.on_error(rel, f"{act}: {slot}-Version nicht gefunden")
                        self.on_counter("not_found", 1)
                        continue
                    dst = self._conflict(rel, slot)
                    if self._do_copy(src, dst, rel, f"{act} [{slot}]"):
                        copied_any = True
                if copied_any:
                    self.on_counter("conflict_mod", 1)
                continue

            if act == A_DELETE:
                self.on_log(f"SKIP DELETE  {rel}  → Script 4 entscheidet")
                self.report_entries.append({
                    "rel_path": rel, "action": act,
                    "status": "pending_delete",
                    "note": "Wird von change_execution.py nach Bestätigung gelöscht",
                })
                self.on_counter("delete_pending", 1)
                continue

            # Quelle bestimmen
            src = None

            if act == A_COPY_MAC:
                roots = sources.get("mac", {}).get("roots", [])
                if not roots_reachable(roots):
                    self._skip_not_reachable(rel, act, "mac")
                    continue
                src = find_file_in_roots(roots, rel)
                dst = self._staging("mac", rel)

            elif act == A_COPY_WIN:
                roots = sources.get("win", {}).get("roots", [])
                if not roots_reachable(roots):
                    self._skip_not_reachable(rel, act, "win")
                    continue
                src = find_file_in_roots(roots, rel)
                dst = self._staging("win", rel)

            elif act == A_COPY:
                # Einer reicht – Mac bevorzugt
                for slot in ("mac", "win"):
                    roots = sources.get(slot, {}).get("roots", [])
                    if roots_reachable(roots):
                        src = find_file_in_roots(roots, rel)
                        if src:
                            break
                dst = self._staging("both", rel)
                if src is None:
                    self._skip_not_reachable(rel, act, "mac+win")
                    continue

            elif act in (A_CONF_DEL_WIN, A_CONF_DEL_MAC):
                roots = sources.get("usb", {}).get("roots", [])
                if not roots_reachable(roots):
                    self._skip_not_reachable(rel, act, "usb")
                    continue
                src = find_file_in_roots(roots, rel)
                sub = "del_win" if act == A_CONF_DEL_WIN else "del_mac"
                dst = self._deleted(sub, rel)

            if src is None:
                self.on_error(rel, f"{act}: Quelldatei nicht gefunden")
                self.on_counter("not_found", 1)
                continue

            if self._do_copy(src, dst, rel, act):
                counter = {
                    A_COPY_MAC:     "copy_mac",
                    A_COPY_WIN:     "copy_win",
                    A_COPY:         "copy_both",
                    A_CONF_DEL_WIN: "conflict_del",
                    A_CONF_DEL_MAC: "conflict_del",
                }.get(act, "ok")
                self.on_counter(counter, 1)

        self._save_report()

    def _skip_not_reachable(self, rel: str, act: str, slot: str):
        self.on_log(f"PEND [{slot} nicht angeschlossen]  {rel}")
        self.report_entries.append({
            "rel_path": rel, "action": act,
            "status": "pending",
            "reason": f"{slot} nicht erreichbar",
        })
        self.on_counter("pending", 1)

    def _load_done_keys(self) -> set[str]:
        """Liest vorhandenen harvest_report und gibt bereits erledigte rel_paths zurück."""
        report_path = self.harvest_root / "harvest_report.json"
        if not report_path.is_file():
            return set()
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            return {
                e["rel_path"] for e in data.get("entries", [])
                if e.get("status") == "ok"
            }
        except Exception:
            return set()

    def _save_report(self):
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        # Bestehenden Report laden und zusammenführen
        report_path = self.harvest_root / "harvest_report.json"
        existing = []
        if report_path.is_file():
            try:
                existing = json.loads(
                    report_path.read_text(encoding="utf-8")
                ).get("entries", [])
            except Exception:
                pass

        # Neue Einträge mergen (bestehende ok-Einträge bleiben)
        existing_keys = {e["rel_path"] for e in existing if e.get("status") == "ok"}
        merged = existing + [
            e for e in self.report_entries
            if e["rel_path"] not in existing_keys
        ]

        ok_count      = sum(1 for e in merged if e.get("status") == "ok")
        pending_count = sum(1 for e in merged if e.get("status") == "pending")
        error_count   = sum(1 for e in merged if e.get("status") == "error")
        del_count     = sum(1 for e in merged if e.get("status") == "pending_delete")

        complete = (pending_count == 0 and error_count == 0)

        report = {
            "meta": {
                "created":       datetime.now().isoformat(),
                "tool":          "file_harvester.py",
                "version":       "2.0",
                "directive":     str(self.directive_path),
                "harvest_root":  str(self.harvest_root),
                "complete":      complete,
                "last_run":      ts,
            },
            "summary": {
                "ok":             ok_count,
                "pending":        pending_count,
                "error":          error_count,
                "pending_delete": del_count,
                "complete":       complete,
            },
            "entries": merged,
        }

        self.harvest_root.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        # Log
        log_path = self.harvest_root / f"harvest_log_{ts}.txt"
        # Log wird von GUI gesammelt – hier nur Report-Pfad zurück
        self.on_done(report, report_path, log_path)


# ==============================================================================
# GUI
# ==============================================================================

class HarvesterApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🌾 File Harvester v2")
        self.root.geometry("860x760")
        self.root.resizable(True, True)

        self._directive:      dict | None = None
        self._directive_path: Path | None = None
        self._harvest_root:   Path | None = None
        self._usb_root:       Path | None = None
        self.cancel_event = threading.Event()
        self._log_lines:  list[str] = []
        self._errors:     list[str] = []

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

        # Schritt 1 – USB wählen
        usb_frame = tk.LabelFrame(
            self.root,
            text="Schritt 1 – USB-Laufwerk wählen",
            padx=10, pady=8)
        usb_frame.pack(fill=tk.X, padx=10, pady=(8, 4))

        self.usb_display_var = tk.StringVar(value="(noch nicht gewählt)")
        tk.Label(usb_frame, textvariable=self.usb_display_var,
                 font=("Courier", 9), anchor="w",
                 fg="#c0392b").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(usb_frame, text="📂  USB-Laufwerk wählen",
                  command=self._pick_usb,
                  font=("Helvetica", 10, "bold"),
                  bg="#1a5276", fg="white",
                  width=24).pack(side=tk.RIGHT)

        # Schritt 2 – erkannte Directive + Harvest-Ordner
        info_frame = tk.LabelFrame(
            self.root,
            text="Schritt 2 – Erkannte Konfiguration",
            padx=10, pady=6)
        info_frame.pack(fill=tk.X, padx=10, pady=4)

        self.directive_display = tk.StringVar(value="–")
        self.harvest_display   = tk.StringVar(value="–")
        self.run_label_var     = tk.StringVar(value="–")

        for label, var in [
            ("Directive:",     self.directive_display),
            ("Harvest-Ordner:", self.harvest_display),
            ("Lauf-Status:",   self.run_label_var),
        ]:
            row = tk.Frame(info_frame)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, width=16, anchor="w",
                     font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
            tk.Label(row, textvariable=var,
                     font=("Courier", 8), anchor="w",
                     fg="#555").pack(side=tk.LEFT, fill=tk.X)

        # Zähler-Panel
        cnt = tk.LabelFrame(self.root, text="Fortschritt", padx=10, pady=8)
        cnt.pack(fill=tk.X, padx=10, pady=4)

        self.progress_label = tk.Label(cnt, text="Bereit.",
                                       anchor="w", font=("Helvetica", 10))
        self.progress_label.pack(fill=tk.X)
        self.file_label = tk.Label(cnt, text="", anchor="w",
                                   font=("Courier", 8), fg="#555")
        self.file_label.pack(fill=tk.X)
        self.progress_bar = ttk.Progressbar(cnt, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(4, 6))

        grid = tk.Frame(cnt)
        grid.pack(fill=tk.X)
        self._counter_vars = {}
        counter_defs = [
            ("copy_mac",      "✅ COPY MAC",       "#2980b9"),
            ("copy_win",      "✅ COPY WIN",       "#2980b9"),
            ("copy_both",     "✅ COPY (beide)",   "#2980b9"),
            ("conflict_mod",  "📋 CONFLICT mod",  "#e67e22"),
            ("conflict_del",  "⚠️  CONFLICT del",  "#e67e22"),
            ("delete_pending","🗑  DELETE pend",   "#c0392b"),
            ("pending",       "⏳ Ausstehend",     "#7f8c8d"),
            ("not_found",     "❌ Nicht gefunden", "#c0392b"),
        ]
        for col, (key, label, color) in enumerate(counter_defs):
            var = tk.StringVar(value="0")
            self._counter_vars[key] = var
            cell = tk.Frame(grid, relief=tk.GROOVE, bd=1, padx=4, pady=3)
            cell.grid(row=0, column=col, padx=2, sticky="ew")
            grid.columnconfigure(col, weight=1)
            tk.Label(cell, text=label, font=("Helvetica", 7),
                     fg=color, wraplength=80,
                     justify="center").pack()
            tk.Label(cell, textvariable=var,
                     font=("Helvetica", 12, "bold")).pack()
        self._counters = {k: 0 for k in self._counter_vars}

        # Fehler-Panel (versteckt bis Fehler)
        self.error_frame = tk.LabelFrame(
            self.root, text="❌ Fehler / Nicht gefunden",
            padx=8, pady=4)
        self.error_listbox = tk.Listbox(
            self.error_frame, font=("Courier", 8),
            height=4, bg="#fff0f0")
        esb = ttk.Scrollbar(self.error_frame, orient="vertical",
                             command=self.error_listbox.yview)
        self.error_listbox.configure(yscrollcommand=esb.set)
        esb.pack(side=tk.RIGHT, fill=tk.Y)
        self.error_listbox.pack(fill=tk.BOTH, expand=True)

        # Log
        log_frame = tk.LabelFrame(self.root, text="Log", padx=8, pady=4)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self.log_text = tk.Text(
            log_frame, font=("Courier", 8),
            state=tk.DISABLED, height=8,
            bg="#1e1e1e", fg="#d4d4d4")
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
            bg="#1a5276", fg="white",
            state=tk.DISABLED, height=2, width=22)
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
    # USB wählen → alles ableiten
    # ------------------------------------------------------------------

    def _pick_usb(self):
        path = filedialog.askdirectory(
            title="USB-Laufwerk (Wurzel) wählen...",
            initialdir=str(Path.home())
        )
        if not path:
            return

        self._usb_root = Path(path)
        sync = self._usb_root / "_sync"

        if not sync.is_dir():
            messagebox.showwarning(
                "Kein _sync-Ordner",
                f"Kein _sync-Ordner gefunden auf:\n{self._usb_root}\n\n"
                f"Bitte zuerst Script 1 + 2 ausführen."
            )
            return

        # Neuestes Datum-Unterverzeichnis
        dated = sorted([d for d in sync.iterdir() if d.is_dir()], reverse=True)
        if not dated:
            messagebox.showwarning("Kein Datum-Ordner",
                                   f"_sync enthält keine Unterordner:\n{sync}")
            return

        sync_dir = dated[0]

        # Neueste Directive suchen
        directives = sorted(
            sync_dir.glob("change_directive_*.json"), reverse=True
        )
        # Bereits geharveste überspringen
        directives = [d for d in directives if "_harvested" not in d.name]

        if not directives:
            messagebox.showwarning(
                "Keine Directive",
                f"Keine change_directive_*.json in:\n{sync_dir}\n\n"
                f"Bitte zuerst Script 2 ausführen."
            )
            return

        directive_path = directives[0]

        try:
            directive = json.loads(
                directive_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            messagebox.showerror("Lesefehler", str(exc))
            return

        self._directive      = directive
        self._directive_path = directive_path
        self._harvest_root   = sync_dir / "harvest"

        # Lauf-Status ermitteln
        report_path = self._harvest_root / "harvest_report.json"
        if report_path.is_file():
            try:
                rep = json.loads(report_path.read_text(encoding="utf-8"))
                ok      = rep["summary"].get("ok", 0)
                pending = rep["summary"].get("pending", 0)
                complete = rep["summary"].get("complete", False)
                if complete:
                    run_status = f"✅ Lauf 1 vollständig – {ok} OK, 0 ausstehend"
                else:
                    run_status = (f"⏳ Lauf 2 – {ok} bereits erledigt, "
                                  f"{pending} noch ausstehend")
            except Exception:
                run_status = "Lauf 1 (Report nicht lesbar)"
        else:
            # Einträge zählen die bearbeitet werden müssen
            n = sum(1 for e in directive.get("entries", [])
                    if e["action"] in HARVEST_ACTIONS and e["action"] != A_OK)
            run_status = f"Lauf 1 – {n} Einträge zu verarbeiten"

        # Anzeige aktualisieren
        self.usb_display_var.set(
            f"✔  {self._usb_root}  →  _sync/{sync_dir.name}/"
        )
        self.directive_display.set(directive_path.name)
        self.harvest_display.set(str(self._harvest_root))
        self.run_label_var.set(run_status)
        self.start_btn.config(state=tk.NORMAL)
        self._append_log(f"USB erkannt: {self._usb_root}")
        self._append_log(f"Directive:   {directive_path.name}")
        self._append_log(f"Status:      {run_status}")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _cb_progress(self, current, total, filename, action):
        pct = int(current / total * 100) if total else 0
        self.root.after(0, lambda: [
            self.progress_label.config(
                text=f"Verarbeite {current:,} / {total:,}  [{pct}%]"),
            self.file_label.config(
                text=f"  {action:<24s}  {filename[-68:]}"),
            self.progress_bar.config(value=pct),
        ])

    def _cb_counter(self, name, delta):
        self._counters[name] = self._counters.get(name, 0) + delta
        val = str(self._counters[name])
        self.root.after(0, lambda: self._counter_vars[name].set(val))

    def _cb_error(self, rel, reason):
        msg = f"{rel}  →  {reason}"
        self._errors.append(msg)

        def _upd():
            if not self.error_frame.winfo_ismapped():
                self.error_frame.pack(fill=tk.X, padx=10, pady=2,
                                      before=self.log_text.master)
            self.error_listbox.insert(tk.END, msg)
            self.error_listbox.see(tk.END)

        self.root.after(0, _upd)
        self._append_log(f"ERR  {msg}")

    def _cb_log(self, msg: str):
        self._log_lines.append(msg)
        self._append_log(msg)

    def _cb_done(self, report, report_path, log_path):
        s = report["summary"]
        complete = s.get("complete", False)

        # Log speichern
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        log_path = self._harvest_root / f"harvest_log_{ts}.txt"
        try:
            log_path.write_text("\n".join(self._log_lines), encoding="utf-8")
        except Exception:
            pass
        self._log_path = log_path

        def _upd():
            self.start_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            self.log_btn.config(state=tk.NORMAL)
            self.progress_bar.config(value=100)

            if complete:
                msg = (f"✅ HARVEST COMPLETE – "
                       f"{s['ok']} OK  |  "
                       f"{s['pending_delete']} DELETE ausstehend")
                self.progress_label.config(text=msg, fg="#27ae60")
                self.status_var.set(msg)
                messagebox.showinfo(
                    "Harvest abgeschlossen",
                    f"Alle Einträge erledigt!\n\n"
                    f"  OK:              {s['ok']:,}\n"
                    f"  DELETE ausstehend: {s['pending_delete']:,}\n\n"
                    f"Bereit für Script 4 (change_execution.py)."
                )
                # Directive umbenennen → _harvested
                try:
                    new_name = self._directive_path.stem + "_harvested.json"
                    self._directive_path.rename(
                        self._directive_path.parent / new_name
                    )
                    self._append_log(f"Directive umbenannt → {new_name}")
                except Exception as exc:
                    self._append_log(f"Umbenennen fehlgeschlagen: {exc}")

            else:
                msg = (f"⏳ Lauf abgeschlossen – "
                       f"{s['ok']} OK  |  "
                       f"{s['pending']} noch ausstehend  |  "
                       f"{s['error']} Fehler")
                self.progress_label.config(text=msg, fg="#e67e22")
                self.status_var.set(msg)
                messagebox.showinfo(
                    "Lauf abgeschlossen",
                    f"Erster Lauf abgeschlossen.\n\n"
                    f"  Erledigt:     {s['ok']:,}\n"
                    f"  Ausstehend:   {s['pending']:,}\n"
                    f"  Fehler:       {s['error']:,}\n\n"
                    f"Bitte USB nun an den anderen Rechner anschließen\n"
                    f"und Harvester dort erneut starten."
                )

        self.root.after(0, _upd)

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"

        def _upd():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, line)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

        self.root.after(0, _upd)

    # ------------------------------------------------------------------
    # Start / Cancel
    # ------------------------------------------------------------------

    def _start(self):
        if not self._directive or not self._harvest_root:
            messagebox.showerror("Fehler", "Bitte zuerst USB-Laufwerk wählen.")
            return

        # Reset Zähler + Log
        self.cancel_event.clear()
        self._errors.clear()
        self._log_lines.clear()
        self._counters = {k: 0 for k in self._counter_vars}
        for var in self._counter_vars.values():
            var.set("0")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.error_listbox.delete(0, tk.END)
        self.progress_bar.config(value=0)

        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.log_btn.config(state=tk.DISABLED)
        self.status_var.set("⏳ Harvest läuft …")

        harvester = Harvester(
            directive      = self._directive,
            directive_path = self._directive_path,
            harvest_root   = self._harvest_root,
            on_progress    = self._cb_progress,
            on_counter     = self._cb_counter,
            on_error       = self._cb_error,
            on_log         = self._cb_log,
            on_done        = self._cb_done,
            cancel_event   = self.cancel_event,
        )

        threading.Thread(target=harvester.run, daemon=True).start()

    def _cancel(self):
        self.cancel_event.set()
        self.status_var.set("⏹ Abbrechen …")
        self.cancel_btn.config(state=tk.DISABLED)

    def _open_log(self):
        if self._log_path and self._log_path.is_file():
            import subprocess, platform
            sys_name = platform.system()
            if sys_name == "Darwin":
                subprocess.Popen(["open", str(self._log_path)])
            elif sys_name == "Windows":
                subprocess.Popen(["notepad", str(self._log_path)])
            else:
                subprocess.Popen(["xdg-open", str(self._log_path)])

    def run(self):
        self.root.mainloop()


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    app = HarvesterApp()
    app.run()
