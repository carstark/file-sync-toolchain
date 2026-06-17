#!/usr/bin/env python3
"""
change_execution.py  –  Script 4 des file-sync-toolchain
==========================================================
Version 2.0  –  Komplette Neuentwicklung

Korrekturen gegenüber v1.x:
  - Korrekte Root-Zuordnung: src-Pfad aus harvest_report → Root-Name → Ziel-Pfad
  - Auto-Erkennung welcher Rechner (Mac/Win) angeschlossen ist
  - USB-Phase zuerst: USB complete → kurze Meldung → lokaler Rechner
  - Tab-Viewing-Gate: alle Tabs ansehen bevor Ausführen aktiv (Lauf 1)
  - Lauf 2: Ausführen direkt nach Root-Bestätigung, ohne Rückfrage
  - Fortschrittsanzeige mit ETA
"""

import hashlib, json, os, shutil, threading, time, tkinter as tk
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


# ══════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════════════

def longpath(p: Path) -> Path:
    import platform
    if platform.system() == "Windows" and not str(p).startswith("\\\\?\\"):
        try:
            return Path("\\\\?\\" + str(p.resolve()))
        except Exception:
            return p
    return p

def fexists(p: Path) -> bool:
    try:
        if p.is_file(): return True
        return longpath(p).is_file()
    except Exception:
        return False

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    lp = longpath(p)
    with open(str(lp), "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block: break
            h.update(block)
    return h.hexdigest()

def copy_file(src: Path, dst: Path) -> tuple[bool, str]:
    s, d = longpath(src), longpath(dst)
    try:
        os.makedirs(str(d.parent), exist_ok=True)
        shutil.copy2(str(s), str(d))
        if sha256(s) != sha256(d):
            try: d.unlink()
            except: pass
            return False, "Hash-Mismatch"
        return True, "ok"
    except Exception as e:
        return False, str(e)

def del_file(p: Path) -> tuple[bool, str]:
    try:
        lp = longpath(p)
        if lp.is_file():
            lp.unlink()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def fmt_size(b: int) -> str:
    if b >= 1<<30: return f"{b/(1<<30):.1f} GB"
    if b >= 1<<20: return f"{b/(1<<20):.1f} MB"
    return f"{b/(1<<10):.0f} KB"

def fmt_eta(seconds: float) -> str:
    if seconds < 60:   return f"{int(seconds)}s"
    if seconds < 3600: return f"{int(seconds/60)}min"
    return f"{seconds/3600:.1f}h"

def top_folder(rel: str, depth: int = 2) -> str:
    parts = Path(rel).parts
    return str(Path(*parts[:min(depth, len(parts))]))


# ══════════════════════════════════════════════════════════════════
# ROOT-ZUORDNUNG
# ══════════════════════════════════════════════════════════════════

def extract_root_name(src_path: str, known_names: set) -> str | None:
    """
    Extrahiert den Root-Ordner-Namen aus einem absoluten Pfad.
    Funktioniert plattformübergreifend (Mac-Pfad auf Win und umgekehrt).
    """
    for part in src_path.replace("\\", "/").split("/"):
        if part in known_names:
            return part
    return None

def build_roots_by_name(paths: list, check_exists: bool = True) -> dict:
    """Baut {ordnername: absoluter_pfad} aus einer Liste von Root-Pfaden."""
    result = {}
    for p in paths:
        name = Path(p).name
        if name and (not check_exists or Path(p).is_dir()):
            result[name] = p
    return result

def find_target(harvest_entry: dict, roots_by_name: dict,
                rel_path: str) -> Path | None:
    """
    Findet den korrekten Ziel-Pfad für eine Datei.
    Nutzt src-Pfad des harvest_entry um Root-Namen zu extrahieren,
    dann matched mit lokalem Root.
    """
    src = harvest_entry.get("src", "")
    known = set(roots_by_name.keys())

    if src:
        name = extract_root_name(src, known)
        if name and name in roots_by_name:
            return Path(roots_by_name[name]) / rel_path

    # Fallback: alle Roots durchsuchen ob Datei schon existiert
    for root in roots_by_name.values():
        cand = Path(root) / rel_path
        if fexists(cand):
            return cand

    # Letzter Fallback: ersten Root nehmen
    if roots_by_name:
        return Path(next(iter(roots_by_name.values()))) / rel_path

    return None

def detect_machine(directive: dict) -> str | None:
    """Erkennt ob Mac oder Win lokal erreichbar ist."""
    sources = directive.get("sources", {})
    for slot in ("mac", "win"):
        roots = sources.get(slot, {}).get("roots", [])
        if any(Path(r).is_dir() for r in roots):
            return slot
    return None

def build_usb_roots(usb_root: Path, directive: dict) -> dict:
    """USB-Root-Ordner: Directive-Namen → tatsächliche Pfade auf aktuellem USB."""
    dir_roots = directive.get("sources", {}).get("usb", {}).get("roots", [])
    names = {Path(r).name for r in dir_roots}
    result = {}
    for name in names:
        cand = usb_root / name
        if cand.is_dir():
            result[name] = str(cand)
    # Fallback: sichtbare Nicht-System-Unterordner
    if not result:
        exclude = {"System Volume Information", "RECYCLER", "$RECYCLE.BIN"}
        for d in usb_root.iterdir():
            if d.is_dir() and not d.name.startswith((".", "_", "$")) \
               and d.name not in exclude:
                result[d.name] = str(d)
    return result

def spot_check(roots_by_name: dict, harvest_entries: list,
               slot: str, n: int = 5) -> tuple[int, int]:
    """Stichpunktartige Prüfung: wie viele bekannte Dateien werden gefunden?"""
    found = checked = 0
    for entry in harvest_entries:
        if checked >= n:
            break
        if entry.get("action", "").find(slot.upper()) < 0:
            continue
        rel = entry.get("rel_path", "")
        target = find_target(entry, roots_by_name, rel)
        if target:
            # für Quelle: staging existiert, für Ziel: echte Datei
            if fexists(target):
                found += 1
            checked += 1
    return found, checked


# ══════════════════════════════════════════════════════════════════
# EXECUTION REPORT
# ══════════════════════════════════════════════════════════════════

class ExecReport:
    def __init__(self, path: Path):
        self.path          = path
        self.decisions:    dict = {}
        self.done:         set  = set()
        self.run1_machine: str  = ""
        self.run1_done:    bool = False
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.decisions    = d.get("decisions", {})
            self.done         = set(d.get("done", []))
            self.run1_machine = d.get("run1_machine", "")
            self.run1_done    = d.get("run1_done", False)
        except Exception:
            pass

    def save(self):
        self.path.write_text(
            json.dumps({
                "run1_machine": self.run1_machine,
                "run1_done":    self.run1_done,
                "decisions":    self.decisions,
                "done":         sorted(self.done),
            }, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark_done(self, key: str):
        self.done.add(key)


# ══════════════════════════════════════════════════════════════════
# EXECUTOR
# ══════════════════════════════════════════════════════════════════

class Executor:
    def __init__(self, entries, er, machine,
                 usb_roots, local_roots, harvest_root,
                 on_progress, on_log, on_usb_done, on_local_done,
                 cancel):
        self.entries      = entries
        self.er           = er
        self.machine      = machine       # "mac" | "win"
        self.usb_roots    = usb_roots     # name → path
        self.local_roots  = local_roots   # name → path
        self.hr           = harvest_root
        self.on_progress  = on_progress   # (i, total, rel, label)
        self.on_log       = on_log        # (msg)
        self.on_usb_done  = on_usb_done   # (ok, err)
        self.on_local_done = on_local_done # (ok, err, complete)
        self.cancel       = cancel

    # ─── USB-Phase ────────────────────────────────────────────────

    def run_usb(self):
        SLOT = {"COPY MAC": "mac", "COPY WIN": "win", "COPY": "both"}
        todo = []
        for e in self.entries:
            act, st = e.get("action",""), e.get("status","")
            if act in SLOT and st == "ok":
                todo.append(("copy_usb", e))
            elif st == "pending_delete":
                todo.append(("delete_usb", e))

        pending = [t for t in todo
                   if not self.er.is_done(f"{t[1]['rel_path']}::usb")]
        ok = err = 0
        total = len(pending)

        for i, (kind, entry) in enumerate(pending):
            if self.cancel.is_set(): break
            rel = entry["rel_path"]
            self.on_progress(i+1, total, rel, f"USB ◄ {entry.get('action','')}")

            if kind == "delete_usb":
                tgt = find_target(entry, self.usb_roots, rel)
                if tgt and fexists(tgt):
                    d_ok, info = del_file(tgt)
                    if d_ok:
                        self.er.mark_done(f"{rel}::usb")
                        self.on_log(f"OK   DEL-USB  {rel[:65]}")
                        ok += 1
                    else:
                        self.on_log(f"ERR  DEL-USB  {info[:30]}  {rel[:50]}")
                        err += 1
                else:
                    self.er.mark_done(f"{rel}::usb")  # bereits nicht vorhanden
                    ok += 1

            else:  # copy_usb
                slot = SLOT[entry["action"]]
                src = longpath(self.hr / "staging" / slot / rel)
                if not fexists(src):
                    self.on_log(f"MISS staging/{slot}  {rel[:60]}")
                    err += 1
                    continue
                tgt = find_target(entry, self.usb_roots, rel)
                if not tgt:
                    self.on_log(f"MISS usb-root  {rel[:65]}")
                    err += 1
                    continue
                if fexists(tgt):
                    self.er.mark_done(f"{rel}::usb")
                    ok += 1
                    continue
                c_ok, info = copy_file(src, tgt)
                if c_ok:
                    self.er.mark_done(f"{rel}::usb")
                    self.on_log(f"OK   USB◄{slot}  {rel[:62]}")
                    ok += 1
                else:
                    self.on_log(f"ERR  USB◄{slot}  {info[:28]}  {rel[:48]}")
                    err += 1

        self.er.save()
        self.on_usb_done(ok, err)

    # ─── Lokale Phase ──────────────────────────────────────────────

    def run_local(self):
        m = self.machine
        todo = []
        GELOESCHT = self.hr / "_GELOESCHT"

        for e in self.entries:
            act, st = e.get("action",""), e.get("status","")
            if act == "COPY MAC" and m == "win" and st == "ok":
                todo.append(("copy", e, "mac"))
            elif act == "COPY WIN" and m == "mac" and st == "ok":
                todo.append(("copy", e, "win"))
            elif act in ("CONFLICT del Win","CONFLICT del Mac") and st == "ok":
                todo.append(("del", e, None))
            elif act in ("CONFLICT modified","CONFLICT new") and st == "ok":
                todo.append(("conflict", e, None))

        key_fn = lambda e: f"{e['rel_path']}::local::{m}"
        pending = [(k,e,x) for k,e,x in todo
                   if not self.er.is_done(key_fn(e))]

        ok = err = 0
        total = len(pending)
        start = time.time()

        for i, (kind, entry, extra) in enumerate(pending):
            if self.cancel.is_set(): break
            rel = entry["rel_path"]
            dk  = key_fn(entry)
            act = entry["action"]

            # ETA
            elapsed = time.time() - start
            if i > 0:
                eta = elapsed / i * (total - i)
                eta_str = f"  ETA {fmt_eta(eta)}"
            else:
                eta_str = ""

            self.on_progress(i+1, total, rel,
                             f"{m.upper()} ◄ {act}{eta_str}")

            # ── COPY ──────────────────────────────────────────────
            if kind == "copy":
                src = longpath(self.hr / "staging" / extra / rel)
                if not fexists(src):
                    self.on_log(f"MISS staging/{extra}  {rel[:60]}")
                    err += 1
                    continue
                tgt = find_target(entry, self.local_roots, rel)
                if not tgt:
                    self.on_log(f"MISS local-root  {rel[:60]}")
                    err += 1
                    continue
                if fexists(tgt):
                    self.er.mark_done(dk)
                    ok += 1
                    continue
                c_ok, info = copy_file(src, tgt)
                if c_ok:
                    self.er.mark_done(dk)
                    self.on_log(f"OK   {m.upper()}◄staging  {rel[:58]}")
                    ok += 1
                else:
                    self.on_log(f"ERR  COPY  {info[:28]}  {rel[:50]}")
                    err += 1

            # ── LÖSCHUNGEN ────────────────────────────────────────
            elif kind == "del":
                folder = top_folder(rel)
                dec = self.er.decisions.get(f"{act}::{folder}", "confirm")

                if act == "CONFLICT del Win":
                    if m == "mac" and dec == "confirm":
                        tgt = find_target(entry, self.local_roots, rel)
                        if tgt and fexists(tgt):
                            d_ok, info = del_file(tgt)
                            if d_ok:
                                self.er.mark_done(dk)
                                ok += 1
                                self.on_log(f"OK   DEL-MAC  {rel[:62]}")
                            else:
                                self.on_log(f"ERR  DEL  {info[:28]}  {rel[:50]}")
                                err += 1
                        else:
                            self.er.mark_done(dk)
                            ok += 1

                    elif m == "win" and dec == "restore":
                        src = self._find_in_geloescht(GELOESCHT, "del_win", rel)
                        if src:
                            tgt = find_target(entry, self.local_roots, rel)
                            if tgt:
                                c_ok, info = copy_file(src, tgt)
                                if c_ok:
                                    self.er.mark_done(dk)
                                    ok += 1
                                    self.on_log(f"OK   RST►WIN  {rel[:60]}")
                                else:
                                    self.on_log(f"ERR  RST  {info[:28]}  {rel[:50]}")
                                    err += 1
                        else:
                            self.on_log(f"MISS _GELOESCHT/del_win  {rel[:55]}")
                            err += 1
                    else:
                        self.er.mark_done(dk)
                        ok += 1

                elif act == "CONFLICT del Mac":
                    if m == "win" and dec == "confirm":
                        tgt = find_target(entry, self.local_roots, rel)
                        if tgt and fexists(tgt):
                            d_ok, info = del_file(tgt)
                            if d_ok:
                                self.er.mark_done(dk)
                                ok += 1
                                self.on_log(f"OK   DEL-WIN  {rel[:62]}")
                            else:
                                self.on_log(f"ERR  DEL  {info[:28]}  {rel[:50]}")
                                err += 1
                        else:
                            self.er.mark_done(dk)
                            ok += 1

                    elif m == "mac" and dec == "restore":
                        src = self._find_in_geloescht(GELOESCHT, "del_mac", rel)
                        if src:
                            tgt = find_target(entry, self.local_roots, rel)
                            if tgt:
                                c_ok, info = copy_file(src, tgt)
                                if c_ok:
                                    self.er.mark_done(dk)
                                    ok += 1
                                    self.on_log(f"OK   RST►MAC  {rel[:60]}")
                                else:
                                    self.on_log(f"ERR  RST  {info[:28]}  {rel[:50]}")
                                    err += 1
                        else:
                            self.on_log(f"MISS _GELOESCHT/del_mac  {rel[:55]}")
                            err += 1
                    else:
                        self.er.mark_done(dk)
                        ok += 1

            # ── CONFLICT ──────────────────────────────────────────
            elif kind == "conflict":
                winner = self.er.decisions.get(f"CONFLICT::{rel}", "mac")
                src = longpath(self.hr / "conflicts" / rel / f"_{winner}")
                if not fexists(src):
                    self.on_log(f"MISS conflict/{winner}  {rel[:58]}")
                    err += 1
                    continue
                tgt = find_target(entry, self.local_roots, rel)
                if not tgt:
                    self.on_log(f"MISS local-root CONF  {rel[:58]}")
                    err += 1
                    continue
                c_ok, info = copy_file(src, tgt)
                if c_ok:
                    self.er.mark_done(dk)
                    ok += 1
                    self.on_log(f"OK   CONF►{m.upper()}  {rel[:58]}")
                else:
                    self.on_log(f"ERR  CONF  {info[:28]}  {rel[:50]}")
                    err += 1

        self.er.save()
        complete = (err == 0 and not self.cancel.is_set())
        self.on_local_done(ok, err, complete)

    def _find_in_geloescht(self, geloescht: Path, sub: str,
                           rel: str) -> Path | None:
        if not geloescht.is_dir():
            return None
        dated = sorted([d for d in geloescht.iterdir() if d.is_dir()],
                       reverse=True)
        for d in dated:
            cand = longpath(d / sub / rel)
            if cand.is_file():
                return cand
        return None


# ══════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════

class ExecutionApp:

    N_TABS = 5  # Übersicht, COPY, Löschungen, DELETE, CONFLICT

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("⚡ Change Execution v2.0")
        self.root.geometry("1020x840")
        self.root.resizable(True, True)

        # State
        self._directive:     dict | None    = None
        self._harvest_report:dict | None    = None
        self._er:            ExecReport | None = None
        self._harvest_root:  Path | None    = None
        self._usb_root:      Path | None    = None
        self._machine:       str            = ""   # "mac" | "win"
        self._usb_roots:     dict           = {}
        self._local_roots:   dict           = {}
        self._entries:       list           = []
        self._locked:        bool           = False  # Lauf 2: schreibgeschützt
        self._cancel:        threading.Event = threading.Event()
        self._log_lines:     list           = []

        # Decisions (Lauf 1 interaktiv)
        self._decisions:     dict           = {}
        self._delete_keep:   dict           = {}
        self._conflict_win:  dict           = {}
        self._del_item_map:  dict           = {}  # iid → key

        # Tab-Gate
        self._tabs_seen:     set            = set()

        self._build_ui()

    # ──────────────────────────────────────────────────────────────
    # UI AUFBAU
    # ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self.root, bg="#922b21", height=52)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text="⚡ Change Execution v2.0 – Änderungen ausführen",
                 font=("Helvetica", 13, "bold"),
                 bg="#922b21", fg="white").pack(pady=13)

        # Schritt 1: USB
        usb_f = tk.LabelFrame(self.root,
                              text="Schritt 1 – USB-Laufwerk wählen",
                              padx=10, pady=6)
        usb_f.pack(fill=tk.X, padx=10, pady=(8, 4))
        self._usb_var = tk.StringVar(value="(noch nicht gewählt)")
        tk.Label(usb_f, textvariable=self._usb_var,
                 font=("Courier", 9), anchor="w",
                 fg="#c0392b").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(usb_f, text="📂  USB wählen",
                  command=self._pick_usb,
                  font=("Helvetica", 10, "bold"),
                  bg="#922b21", fg="white", width=18).pack(side=tk.RIGHT)

        # Schritt 2: Rechner-Info
        mac_f = tk.LabelFrame(self.root,
                              text="Schritt 2 – Erkannter Rechner & Root-Verzeichnis",
                              padx=10, pady=6)
        mac_f.pack(fill=tk.X, padx=10, pady=4)
        self._machine_var = tk.StringVar(value="–")
        self._root_var    = tk.StringVar(value="–")
        self._lauf_var    = tk.StringVar(value="–")
        for label, var in [("Rechner:", self._machine_var),
                            ("Root:",    self._root_var),
                            ("Lauf:",    self._lauf_var)]:
            row = tk.Frame(mac_f)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, width=10, anchor="w",
                     font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
            tk.Label(row, textvariable=var,
                     font=("Courier", 8), anchor="w").pack(side=tk.LEFT, fill=tk.X)

        # Fortschritt
        prog_f = tk.LabelFrame(self.root, text="Fortschritt", padx=10, pady=6)
        prog_f.pack(fill=tk.X, padx=10, pady=4)
        self._prog_label = tk.Label(prog_f, text="Bereit.",
                                    anchor="w", font=("Helvetica", 10))
        self._prog_label.pack(fill=tk.X)
        self._file_label = tk.Label(prog_f, text="",
                                    anchor="w", font=("Courier", 8), fg="#555")
        self._file_label.pack(fill=tk.X)
        self._prog_bar = ttk.Progressbar(prog_f, mode="determinate")
        self._prog_bar.pack(fill=tk.X, pady=(4, 2))

        # Zähler-Kacheln
        cnt = tk.Frame(prog_f)
        cnt.pack(fill=tk.X, pady=4)
        self._cnt_vars = {}
        for col, (key, label, color) in enumerate([
            ("ok",   "✅ Erledigt", "#27ae60"),
            ("err",  "❌ Fehler",   "#c0392b"),
            ("skip", "⏭ Skip",     "#2980b9"),
        ]):
            var = tk.StringVar(value="0")
            self._cnt_vars[key] = var
            cell = tk.Frame(cnt, relief=tk.GROOVE, bd=1, padx=8, pady=4)
            cell.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)
            tk.Label(cell, text=label, font=("Helvetica", 9),
                     fg=color).pack()
            tk.Label(cell, textvariable=var,
                     font=("Helvetica", 14, "bold")).pack()

        # Notebook
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab)

        self._f_overview  = tk.Frame(self._nb)
        self._f_copy      = tk.Frame(self._nb)
        self._f_del       = tk.Frame(self._nb)
        self._f_delete    = tk.Frame(self._nb)
        self._f_conflict  = tk.Frame(self._nb)
        self._f_log       = tk.Frame(self._nb)

        for frame, title in [
            (self._f_overview, "📊 Übersicht"),
            (self._f_copy,     "📋 COPY"),
            (self._f_del,      "🗑 Löschungen"),
            (self._f_delete,   "🗑 DELETE"),
            (self._f_conflict, "⚠️  CONFLICT"),
            (self._f_log,      "📋 Log"),
        ]:
            self._nb.add(frame, text=title)

        self._build_overview_tab()
        self._build_copy_tab()
        self._build_del_tab()
        self._build_delete_tab()
        self._build_conflict_tab()
        self._build_log_tab()

        # Statuszeile
        self._status_var = tk.StringVar(value="Bereit.")
        tk.Label(self.root, textvariable=self._status_var,
                 anchor="w", font=("Helvetica", 9),
                 relief=tk.SUNKEN).pack(fill=tk.X, padx=10, pady=(0, 2))

        # Buttons
        btn = tk.Frame(self.root)
        btn.pack(fill=tk.X, padx=10, pady=6)

        self._exec_btn = tk.Button(
            btn, text="▶  Ausführen (dieser Lauf)",
            command=self._execute,
            font=("Helvetica", 12, "bold"),
            bg="#922b21", fg="white",
            state=tk.DISABLED, height=2, width=26)
        self._exec_btn.pack(side=tk.LEFT, padx=5)

        self._cancel_btn = tk.Button(
            btn, text="⏹ Abbrechen",
            command=lambda: self._cancel.set(),
            font=("Helvetica", 11),
            state=tk.DISABLED, height=2, width=14)
        self._cancel_btn.pack(side=tk.LEFT, padx=5)

        self._log_save_btn = tk.Button(
            btn, text="📋 Log speichern",
            command=self._save_log,
            font=("Helvetica", 11),
            state=tk.DISABLED, height=2, width=16)
        self._log_save_btn.pack(side=tk.LEFT, padx=5)

        self._gate_label = tk.Label(
            btn, text="",
            font=("Helvetica", 9, "italic"), fg="#922b21")
        self._gate_label.pack(side=tk.LEFT, padx=10)

    def _build_overview_tab(self):
        self._overview_text = tk.Text(self._f_overview,
                                      font=("Courier", 10),
                                      state=tk.DISABLED)
        sb = ttk.Scrollbar(self._f_overview, orient="vertical",
                            command=self._overview_text.yview)
        self._overview_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._overview_text.pack(fill=tk.BOTH, expand=True)

    def _build_copy_tab(self):
        tk.Label(self._f_copy,
                 text="Automatisch – keine Entscheidung nötig.",
                 font=("Helvetica", 10), fg="#555").pack(anchor="w",
                                                          padx=10, pady=6)
        cols = ("action", "count", "staging", "destination")
        self._copy_tree = ttk.Treeview(self._f_copy, columns=cols,
                                       show="headings", height=6)
        for col, w, label in [
            ("action",      140, "Aktion"),
            ("count",        80, "Dateien"),
            ("staging",     300, "Quelle (staging)"),
            ("destination", 450, "Ziel"),
        ]:
            self._copy_tree.heading(col, text=label)
            self._copy_tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(self._f_copy, orient="vertical",
                            command=self._copy_tree.yview)
        self._copy_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._copy_tree.pack(fill=tk.BOTH, expand=True, padx=10)

    def _build_del_tab(self):
        top = tk.Frame(self._f_del)
        top.pack(fill=tk.X, padx=10, pady=6)
        tk.Label(top,
                 text="Vorauswahl: ✔ Löschen  |  "
                      "Sicherheitskopie bleibt in harvest/_GELOESCHT/",
                 font=("Helvetica", 10), fg="#555").pack(side=tk.LEFT)
        self._all_conf_btn = tk.Button(
            top, text="Alle: ✔ Löschen",
            command=lambda: self._set_all_del("confirm"), width=16)
        self._all_conf_btn.pack(side=tk.RIGHT, padx=4)
        self._all_rest_btn = tk.Button(
            top, text="Alle: ↩ Wiederherstellen",
            command=lambda: self._set_all_del("restore"), width=22)
        self._all_rest_btn.pack(side=tk.RIGHT, padx=4)

        cols = ("decision", "count", "size", "folder")
        self._del_tree = ttk.Treeview(self._f_del, columns=cols,
                                      show="headings", selectmode="browse")
        for col, w, label in [
            ("decision", 170, "Entscheidung"),
            ("count",     70, "Dateien"),
            ("size",       90, "Größe"),
            ("folder",    640, "Ordner"),
        ]:
            self._del_tree.heading(col, text=label)
            self._del_tree.column(col, width=w, anchor="center" if col != "folder" else "w")
        sb = ttk.Scrollbar(self._f_del, orient="vertical",
                            command=self._del_tree.yview)
        self._del_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._del_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self._del_tree.bind("<Double-1>", self._toggle_del)
        self._del_tree.tag_configure("confirm", background="#d5f5e3")
        self._del_tree.tag_configure("restore", background="#d6eaf8")
        self._del_tree.tag_configure("header",  background="#ecf0f1",
                                     font=("Helvetica", 9, "bold"))
        self._del_tree.tag_configure("done",    background="#f0f0f0",
                                     foreground="#aaa")
        tk.Label(self._f_del,
                 text="Doppelklick: umschalten  |  🔒 Nach Lauf 1 gesperrt",
                 font=("Helvetica", 8), fg="#888").pack(anchor="w",
                                                         padx=10, pady=2)

    def _build_delete_tab(self):
        tk.Label(self._f_delete,
                 text="Beide Rechner haben gelöscht.  "
                      "Vorauswahl: von USB entfernen.\n"
                      "Sicherheitskopie verbleibt in harvest/.",
                 font=("Helvetica", 10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)
        cols = ("decision", "rel_path")
        self._delete_tree = ttk.Treeview(self._f_delete, columns=cols,
                                         show="headings", height=10)
        self._delete_tree.heading("decision", text="Entscheidung")
        self._delete_tree.heading("rel_path", text="Datei")
        self._delete_tree.column("decision", width=120, anchor="center")
        self._delete_tree.column("rel_path", width=840, anchor="w")
        sb = ttk.Scrollbar(self._f_delete, orient="vertical",
                            command=self._delete_tree.yview)
        self._delete_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._delete_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self._delete_tree.bind("<Double-1>", self._toggle_delete)
        self._delete_tree.tag_configure("delete", background="#fadbd8")
        self._delete_tree.tag_configure("keep",   background="#d6eaf8")
        self._delete_tree.tag_configure("done",   background="#f0f0f0",
                                        foreground="#aaa")
        tk.Label(self._f_delete,
                 text="Doppelklick: umschalten  |  🔒 Nach Lauf 1 gesperrt",
                 font=("Helvetica", 8), fg="#888").pack(anchor="w",
                                                         padx=10, pady=2)

    def _build_conflict_tab(self):
        tk.Label(self._f_conflict,
                 text="Mac UND Win verändert.  Wähle welche Version gewinnt.\n"
                      "Verlierer-Version bleibt in harvest/conflicts/ als Sicherheitskopie.",
                 font=("Helvetica", 10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)
        cols = ("winner", "rel_path", "mac_info", "win_info")
        self._conf_tree = ttk.Treeview(self._f_conflict, columns=cols,
                                       show="headings", height=10)
        for col, w, label in [
            ("winner",   90,  "Gewinner"),
            ("rel_path", 380, "Datei"),
            ("mac_info", 230, "Mac (Größe / mtime)"),
            ("win_info", 230, "Win (Größe / mtime)"),
        ]:
            self._conf_tree.heading(col, text=label)
            self._conf_tree.column(col, width=w,
                                   anchor="center" if col == "winner" else "w")
        sb = ttk.Scrollbar(self._f_conflict, orient="vertical",
                            command=self._conf_tree.yview)
        self._conf_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._conf_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self._conf_tree.bind("<Double-1>", self._toggle_conflict)
        self._conf_tree.tag_configure("mac",  background="#fde8d8")
        self._conf_tree.tag_configure("win",  background="#d6eaf8")
        self._conf_tree.tag_configure("done", background="#f0f0f0",
                                      foreground="#aaa")
        tk.Label(self._f_conflict,
                 text="Doppelklick: Mac ↔ Win wechseln  |  🔒 Nach Lauf 1 gesperrt",
                 font=("Helvetica", 8), fg="#888").pack(anchor="w",
                                                         padx=10, pady=2)

    def _build_log_tab(self):
        self._log_text = tk.Text(self._f_log, font=("Courier", 8),
                                 state=tk.DISABLED,
                                 bg="#1e1e1e", fg="#d4d4d4")
        sb = ttk.Scrollbar(self._f_log, orient="vertical",
                            command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(fill=tk.BOTH, expand=True)

    # ──────────────────────────────────────────────────────────────
    # USB WÄHLEN
    # ──────────────────────────────────────────────────────────────

    def _pick_usb(self):
        path = filedialog.askdirectory(
            title="USB-Laufwerk (Wurzel) wählen")
        if not path:
            return

        usb = Path(path)
        sync = usb / "_sync"
        if not sync.is_dir():
            messagebox.showwarning("Kein _sync", f"Kein _sync auf:\n{usb}")
            return

        dated = sorted([d for d in sync.iterdir() if d.is_dir()], reverse=True)
        if not dated:
            return
        sync_dir     = dated[0]
        harvest_root = sync_dir / "harvest"

        # harvest_report laden
        rep_path = harvest_root / "harvest_report.json"
        if not rep_path.is_file():
            messagebox.showwarning("Kein harvest_report",
                                   f"harvest_report.json nicht gefunden:\n{harvest_root}")
            return
        try:
            self._harvest_report = json.loads(
                rep_path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Lesefehler", str(exc))
            return

        # Directive laden
        dir_candidates = sorted(
            list(sync_dir.glob("change_directive_*.json")), reverse=True)
        if not dir_candidates:
            messagebox.showwarning(
                "Keine Directive",
                f"change_directive_*.json nicht gefunden in:\n{sync_dir}")
            return
        try:
            self._directive = json.loads(
                dir_candidates[0].read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Lesefehler Directive", str(exc))
            return

        # ExecReport
        er_path = harvest_root / "execution_report.json"
        self._er           = ExecReport(er_path)
        self._harvest_root = harvest_root
        self._usb_root     = usb
        self._entries      = self._harvest_report.get("entries", [])

        # USB-Roots aufbauen
        self._usb_roots = build_usb_roots(usb, self._directive)

        # Lauf-Status
        self._locked = self._er.run1_done
        lauf_str = ("🔒 Lauf 2 – Entscheidungen aus Lauf 1 gesperrt"
                    if self._locked else "Lauf 1 – Entscheidungen treffen")

        self._usb_var.set(f"✔  {usb}  →  _sync/{sync_dir.name}/harvest/")
        self._lauf_var.set(lauf_str)
        self._gate_label.config(
            text="🔒 Lauf 2: Entscheidungen nicht mehr änderbar"
            if self._locked else
            "Alle Tabs ansehen bevor Ausführen aktiv wird"
        )

        # Rechner auto-erkennen
        self._detect_and_setup()

    def _detect_and_setup(self):
        machine = detect_machine(self._directive)

        if machine:
            local_roots = build_roots_by_name(
                self._directive["sources"][machine]["roots"],
                check_exists=True)
            self._machine     = machine
            self._local_roots = local_roots

            roots_str = "\n     ".join(local_roots.values()) or "–"
            self._machine_var.set(
                f"{'🍎 Mac' if machine == 'mac' else '🖥 Win'}  "
                f"(auto-erkannt)")
            self._root_var.set(roots_str[:120])

            # Stichprobe
            ok_n, chk = spot_check(
                local_roots, self._entries, machine, n=5)
            if chk > 0 and ok_n == 0:
                messagebox.showwarning(
                    "Root-Validierung",
                    f"Stichprobe: 0/{chk} bekannte Dateien gefunden.\n"
                    f"Root-Verzeichnis evtl. falsch:\n"
                    f"{list(local_roots.values())[:2]}\n\n"
                    f"Bitte manuell prüfen."
                )
        else:
            # Manuell fragen
            ok = messagebox.askyesno(
                "Rechner nicht erkannt",
                "Mac oder Win-Root nicht automatisch gefunden.\n\n"
                "Ist dies ein Mac?\n"
                "(Nein = Windows)"
            )
            machine = "mac" if ok else "win"
            path = filedialog.askdirectory(
                title=f"{'Mac'if ok else 'Win'}-Root-Verzeichnis wählen "
                      f"(oberster Sync-Ordner)")
            if not path:
                return
            self._machine     = machine
            self._local_roots = {Path(path).name: path}
            self._machine_var.set(f"{'🍎 Mac' if ok else '🖥 Win'}  (manuell)")
            self._root_var.set(path)

        # Wenn Lauf 2: Entscheidungen aus Report laden
        if self._locked:
            self._decisions    = {k: v for k, v in self._er.decisions.items()
                                  if "::" in k and not k.startswith(("DELETE", "CONFLICT"))}
            self._delete_keep  = {k.replace("DELETE::", ""): (v == "keep")
                                  for k, v in self._er.decisions.items()
                                  if k.startswith("DELETE::")}
            self._conflict_win = {k.replace("CONFLICT::", ""): v
                                  for k, v in self._er.decisions.items()
                                  if k.startswith("CONFLICT::")}

        self._populate_all()

        # Lauf 2: Execute sofort verfügbar
        if self._locked:
            self._exec_btn.config(state=tk.NORMAL)
            self._gate_label.config(
                text="🔒 Lauf 2 – direkt ausführbar (keine Rückfrage)")

    # ──────────────────────────────────────────────────────────────
    # TABS BEFÜLLEN
    # ──────────────────────────────────────────────────────────────

    def _populate_all(self):
        self._populate_overview()
        self._populate_copy()
        self._populate_del()
        self._populate_delete()
        self._populate_conflict()

    def _populate_overview(self):
        entries = self._entries
        m = self._machine or "?"

        c_mac  = sum(1 for e in entries if e.get("action") == "COPY MAC"
                     and e.get("status") == "ok")
        c_win  = sum(1 for e in entries if e.get("action") == "COPY WIN"
                     and e.get("status") == "ok")
        d_win  = sum(1 for e in entries if e.get("action") == "CONFLICT del Win"
                     and e.get("status") == "ok")
        d_mac  = sum(1 for e in entries if e.get("action") == "CONFLICT del Mac"
                     and e.get("status") == "ok")
        deletes = sum(1 for e in entries if e.get("status") == "pending_delete")
        conf   = sum(1 for e in entries
                     if e.get("action") in ("CONFLICT modified","CONFLICT new")
                     and e.get("status") == "ok")

        done_usb   = sum(1 for k in self._er.done if "::usb" in k)
        done_local = sum(1 for k in self._er.done if f"::local::{m}" in k)

        lines = [
            "=" * 62,
            "  CHANGE EXECUTION v2.0 – ÜBERSICHT",
            f"  Rechner: {'Mac' if m=='mac' else 'Win'}  |  "
            f"{'Lauf 2 (gesperrt)' if self._locked else 'Lauf 1'}",
            "=" * 62, "",
            "  USB-Phase (automatisch):",
            f"    COPY MAC → USB:   {c_mac:>6,}",
            f"    COPY WIN → USB:   {c_win:>6,}",
            f"    DELETE von USB:   {deletes:>6,}",
            f"    Bereits erledigt: {done_usb:>6,}",
            "",
            f"  Lokale Phase ({m.upper()}):",
            f"    COPY MAC → Win:   {c_mac:>6,}  (nur auf Win)",
            f"    COPY WIN → Mac:   {c_win:>6,}  (nur auf Mac)",
            f"    del Win:          {d_win:>6,}",
            f"    del Mac:          {d_mac:>6,}",
            f"    CONFLICT:         {conf:>6,}",
            f"    Bereits erledigt: {done_local:>6,}",
            "",
            "─" * 62,
            "  USB-Roots:",
        ]
        for name, path in self._usb_roots.items():
            lines.append(f"    {name}: {path}")
        lines += [
            "",
            "  Lokale Roots:",
        ]
        for name, path in self._local_roots.items():
            lines.append(f"    {name}: {path}")
        lines += [""]

        self._overview_text.config(state=tk.NORMAL)
        self._overview_text.delete("1.0", tk.END)
        self._overview_text.insert(tk.END, "\n".join(lines))
        self._overview_text.config(state=tk.DISABLED)

    def _populate_copy(self):
        self._copy_tree.delete(*self._copy_tree.get_children())
        entries = self._entries
        m = self._machine

        groups = [
            ("COPY MAC", "mac",  f"→ USB + Win  (auf {'Win' if m=='win' else 'Mac (anderer Lauf)'})"),
            ("COPY WIN", "win",  f"→ USB + Mac  (auf {'Mac' if m=='mac' else 'Win (anderer Lauf)'})"),
            ("COPY",     "both", "→ USB"),
        ]
        for act, slot, dest in groups:
            grp = [e for e in entries if e.get("action") == act
                   and e.get("status") == "ok"]
            if not grp:
                continue
            staging = str(self._harvest_root / "staging" / slot) \
                if self._harvest_root else "?"
            done = sum(1 for e in grp
                       if self._er.is_done(f"{e['rel_path']}::usb") or
                       self._er.is_done(f"{e['rel_path']}::local::{m}"))
            self._copy_tree.insert("", tk.END, values=(
                act,
                f"{len(grp):,}  ({done:,} done)",
                staging,
                dest,
            ))

    def _populate_del(self):
        self._del_tree.delete(*self._del_tree.get_children())
        self._del_item_map = {}
        entries = self._entries
        m = self._machine

        for category, action in [
            ("── Win hat gelöscht (del Win) ──", "CONFLICT del Win"),
            ("── Mac hat gelöscht (del Mac) ──", "CONFLICT del Mac"),
        ]:
            cat = [e for e in entries if e.get("action") == action
                   and e.get("status") == "ok"]
            if not cat:
                continue
            self._del_tree.insert("", tk.END,
                values=("", f"{len(cat):,}", "", category),
                tags=("header",))

            folder_map = defaultdict(list)
            for e in cat:
                folder_map[top_folder(e["rel_path"])].append(e)

            for folder, fentries in sorted(folder_map.items()):
                key = f"{action}::{folder}"
                # Entscheidung: aus Report (Lauf 2) oder Default (confirm)
                dec = (self._er.decisions.get(key, "confirm")
                       if self._locked else
                       self._decisions.get(key, "confirm"))
                self._decisions[key] = dec

                # Erledigt?
                done_count = sum(
                    1 for e in fentries
                    if self._er.is_done(f"{e['rel_path']}::local::{m}")
                )
                is_done = (done_count == len(fentries))

                label = "✔ Löschen" if dec == "confirm" else "↩ Wiederherstellen"
                if is_done:
                    label = f"✅ Erledigt ({done_count})"

                tag = "done" if is_done else dec
                iid = self._del_tree.insert(
                    "", tk.END,
                    values=(label, f"{len(fentries):,}", "", folder),
                    tags=(tag,))
                if not is_done:
                    self._del_item_map[iid] = key

        if self._locked:
            self._all_conf_btn.config(state=tk.DISABLED)
            self._all_rest_btn.config(state=tk.DISABLED)

    def _populate_delete(self):
        self._delete_tree.delete(*self._delete_tree.get_children())
        self._delete_keep = {}
        entries = self._entries
        m = self._machine

        for e in entries:
            if e.get("status") != "pending_delete":
                continue
            rel  = e["rel_path"]
            keep = (self._er.decisions.get(f"DELETE::{rel}", "delete") == "keep"
                    if self._locked else False)
            self._delete_keep[rel] = keep
            done = self._er.is_done(f"{rel}::usb")

            if done:
                label = "✅ Erledigt"
                tag   = "done"
            else:
                label = "💾 Behalten" if keep else "🗑 Löschen"
                tag   = "keep" if keep else "delete"

            self._delete_tree.insert("", tk.END,
                values=(label, rel), tags=(tag,))

    def _populate_conflict(self):
        self._conf_tree.delete(*self._conf_tree.get_children())
        self._conflict_win = {}
        entries = self._entries
        m = self._machine

        conf = [e for e in entries
                if e.get("action") in ("CONFLICT modified","CONFLICT new")
                and e.get("status") == "ok"]

        if not conf:
            self._conf_tree.insert("", tk.END,
                values=("", "(keine CONFLICT modified Einträge)", "", ""),
                tags=("done",))
            return

        for e in conf:
            rel    = e["rel_path"]
            winner = (self._er.decisions.get(f"CONFLICT::{rel}", "mac")
                      if self._locked else "mac")
            self._conflict_win[rel] = winner
            done   = self._er.is_done(f"{rel}::local::{m}")

            ps = e.get("per_source", {})
            def fmt(d):
                if not d: return "–"
                return (f"{fmt_size(d.get('size',0))}  "
                        f"{datetime.fromtimestamp(d.get('mtime',0)).strftime('%Y-%m-%d %H:%M')}")

            if done:
                label, tag = "✅ Erledigt", "done"
            else:
                label = "📱 Mac" if winner == "mac" else "🖥 Win"
                tag   = "mac" if winner == "mac" else "win"

            self._conf_tree.insert("", tk.END,
                values=(label, rel, fmt(ps.get("mac")), fmt(ps.get("win"))),
                tags=(tag,))

    # ──────────────────────────────────────────────────────────────
    # TAB-GATE
    # ──────────────────────────────────────────────────────────────

    def _on_tab(self, event):
        idx = self._nb.index(self._nb.select())
        self._tabs_seen.add(idx)
        self._check_gate()

    def _check_gate(self):
        if self._locked:
            return  # Lauf 2: immer verfügbar
        if not self._er:
            return
        # Alle 5 Content-Tabs gesehen? (Index 0–4, Log=5 nicht nötig)
        if len(self._tabs_seen) >= self.N_TABS:
            self._exec_btn.config(state=tk.NORMAL)
            self._gate_label.config(text="")
        else:
            remaining = self.N_TABS - len(self._tabs_seen)
            self._gate_label.config(
                text=f"Noch {remaining} Tab(s) ansehen")

    # ──────────────────────────────────────────────────────────────
    # DOPPELKLICK-INTERAKTION (nur Lauf 1)
    # ──────────────────────────────────────────────────────────────

    def _toggle_del(self, event):
        if self._locked: return
        iid = self._del_tree.identify_row(event.y)
        if not iid or iid not in self._del_item_map: return
        key     = self._del_item_map[iid]
        current = self._decisions.get(key, "confirm")
        new     = "restore" if current == "confirm" else "confirm"
        self._decisions[key] = new
        vals    = list(self._del_tree.item(iid, "values"))
        vals[0] = "✔ Löschen" if new == "confirm" else "↩ Wiederherstellen"
        self._del_tree.item(iid, values=vals, tags=(new,))

    def _toggle_delete(self, event):
        if self._locked: return
        iid = self._delete_tree.identify_row(event.y)
        if not iid: return
        vals    = list(self._delete_tree.item(iid, "values"))
        if vals[0] == "✅ Erledigt": return
        rel     = vals[1]
        current = self._delete_keep.get(rel, False)
        new     = not current
        self._delete_keep[rel] = new
        vals[0] = "💾 Behalten" if new else "🗑 Löschen"
        self._delete_tree.item(iid, values=vals,
                               tags=("keep" if new else "delete",))

    def _toggle_conflict(self, event):
        if self._locked: return
        iid = self._conf_tree.identify_row(event.y)
        if not iid: return
        vals    = list(self._conf_tree.item(iid, "values"))
        if vals[0] == "✅ Erledigt": return
        rel     = vals[1]
        current = self._conflict_win.get(rel, "mac")
        new     = "win" if current == "mac" else "mac"
        self._conflict_win[rel] = new
        vals[0] = "🖥 Win" if new == "win" else "📱 Mac"
        self._conf_tree.item(iid, values=vals,
                             tags=("win" if new == "win" else "mac",))

    def _set_all_del(self, decision: str):
        if self._locked: return
        for iid, key in self._del_item_map.items():
            self._decisions[key] = decision
            vals    = list(self._del_tree.item(iid, "values"))
            vals[0] = "✔ Löschen" if decision == "confirm" else "↩ Wiederherstellen"
            self._del_tree.item(iid, values=vals, tags=(decision,))

    # ──────────────────────────────────────────────────────────────
    # AUSFÜHREN
    # ──────────────────────────────────────────────────────────────

    def _execute(self):
        if not self._er or not self._harvest_root:
            return

        # Entscheidungen in Report schreiben (Lauf 1)
        if not self._locked:
            for key, dec in self._decisions.items():
                self._er.decisions[key] = dec
            for rel, keep in self._delete_keep.items():
                self._er.decisions[f"DELETE::{rel}"] = "keep" if keep else "delete"
            for rel, winner in self._conflict_win.items():
                self._er.decisions[f"CONFLICT::{rel}"] = winner
            self._er.run1_machine = self._machine

        # Lauf 1: Bestätigung; Lauf 2: direkt starten
        if not self._locked:
            ok = messagebox.askyesno(
                "Ausführen bestätigen",
                f"Lauf 1 – {self._machine.upper()} + USB\n\n"
                f"Phase 1: USB-Operationen\n"
                f"Phase 2: {self._machine.upper()}-Operationen\n\n"
                f"Nicht erreichbare Roots werden übersprungen.\n"
                f"Jetzt starten?"
            )
            if not ok: return

        self._exec_btn.config(state=tk.DISABLED)
        self._cancel_btn.config(state=tk.NORMAL)
        self._cancel.clear()

        for var in self._cnt_vars.values():
            var.set("0")
        self._log_lines.clear()

        ex = Executor(
            entries      = self._entries,
            er           = self._er,
            machine      = self._machine,
            usb_roots    = self._usb_roots,
            local_roots  = self._local_roots,
            harvest_root = self._harvest_root,
            on_progress  = self._cb_progress,
            on_log       = self._cb_log,
            on_usb_done  = self._cb_usb_done,
            on_local_done = self._cb_local_done,
            cancel       = self._cancel,
        )
        self._current_executor = ex   # Referenz für _cb_usb_done

        def run():
            ex.run_usb()

        threading.Thread(target=run, daemon=True).start()

    def _cb_usb_done(self, ok: int, err: int):
        self._cnt_vars["ok"].set(str(ok))
        self._cnt_vars["err"].set(str(err))

        def _show():
            self._prog_label.config(
                text=f"✅ USB-Phase abgeschlossen – {ok:,} ok  |  {err:,} Fehler",
                fg="#27ae60" if err == 0 else "#c0392b")
            if err > 0:
                messagebox.showwarning(
                    "USB-Phase abgeschlossen",
                    f"USB-Phase fertig mit {err:,} Fehlern.\n"
                    f"Details im Log-Tab.\n\n"
                    f"Lokale Phase wird jetzt gestartet."
                )
            else:
                messagebox.showinfo(
                    "USB-Phase abgeschlossen",
                    f"USB vollständig synchronisiert.\n"
                    f"{ok:,} Operationen erfolgreich.\n\n"
                    f"Jetzt: lokale {self._machine.upper()}-Operationen."
                )
            # Lokale Phase starten
            ex_ref = getattr(self, "_current_executor", None)
            if ex_ref:
                threading.Thread(target=ex_ref.run_local, daemon=True).start()

        self.root.after(0, _show)

    def _cb_local_done(self, ok: int, err: int, complete: bool):
        prev_ok = int(self._cnt_vars["ok"].get() or "0")
        self._cnt_vars["ok"].set(str(prev_ok + ok))
        self._cnt_vars["err"].set(str(int(self._cnt_vars["err"].get() or "0") + err))

        def _show():
            self._exec_btn.config(state=tk.NORMAL)
            self._cancel_btn.config(state=tk.DISABLED)
            self._log_save_btn.config(state=tk.NORMAL)
            self._prog_bar.config(value=100)

            # Report finalisieren
            if not self._locked:
                self._er.run1_done = True
            self._er.save()

            if complete and self._locked:
                msg = (f"✅ EXECUTION COMPLETE\n\n"
                       f"Alle Operationen abgeschlossen.\n"
                       f"Mac, Win und USB sind synchronisiert.")
                self._prog_label.config(text="✅ EXECUTION COMPLETE", fg="#27ae60")
                self._status_var.set("✅ COMPLETE")
            elif not complete and not self._locked:
                msg = (f"Lauf 1 abgeschlossen.\n\n"
                       f"  Erledigt: {prev_ok + ok:,}\n"
                       f"  Fehler:   {err:,}\n\n"
                       f"USB jetzt an den anderen Rechner anschließen\n"
                       f"und Change Execution dort starten.")
                self._prog_label.config(
                    text=f"⏳ Lauf 1 fertig – USB umstöpseln",
                    fg="#e67e22")
                self._status_var.set("⏳ USB umstöpseln")
            else:
                msg = f"Lauf abgeschlossen.\n{err:,} Fehler – siehe Log."
                self._status_var.set(f"⚠ {err:,} Fehler")

            messagebox.showinfo("Lauf abgeschlossen", msg)
            self._populate_all()  # Tabs aktualisieren (done-Status)

        self.root.after(0, _show)

    def _cb_progress(self, i: int, total: int, rel: str, label: str):
        pct = int(i / total * 100) if total else 0
        self.root.after(0, lambda: [
            self._prog_bar.config(value=pct),
            self._prog_label.config(
                text=f"{label}  –  {i:,} / {total:,}  [{pct}%]"),
            self._file_label.config(text=f"  {rel[-78:]}"),
        ])

    def _cb_log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self._log_lines.append(line)

        def _upd():
            self._log_text.config(state=tk.NORMAL)
            self._log_text.insert(tk.END, line)
            self._log_text.see(tk.END)
            self._log_text.config(state=tk.DISABLED)

        self.root.after(0, _upd)

    def _save_log(self):
        if not self._harvest_root:
            return
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        lp = self._harvest_root / f"execution_log_{ts}.txt"
        lp.write_text("".join(self._log_lines), encoding="utf-8")
        import subprocess, platform
        s = platform.system()
        if s == "Darwin":
            subprocess.Popen(["open", str(lp)])
        elif s == "Windows":
            subprocess.Popen(["notepad", str(lp)])

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = ExecutionApp()
    app.run()
