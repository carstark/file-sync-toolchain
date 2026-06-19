#!/usr/bin/env python3
"""
change_execution.py  –  Script 4 des file-sync-toolchain
==========================================================
Version 3.0  –  Alle Lektionen integriert

Fixes gegenüber v2.0:
  - fexists/find_file: NFC+NFD-Normalisierung, os.path.isfile statt Path.is_file
  - copy_file/del_file: String-basiert, kein pathlib für IO-Operationen
    (Python 3.12+ entfernt \\?\\-Präfix aus Path-Objekten → longpath wirkungslos)
  - Windows-illegale Zeichen: automatisch sanitieren bei Quelle UND Ziel
  - Access-Denied-Popup: Liste nicht löschbarer Dateien mit Anleitung
  - EXECUTION COMPLETE: nur wenn wirklich alle Einträge erledigt
  - Warnung vor Execution wenn illegale Zeichen in Dateinamen gefunden
"""

import hashlib, json, os, shutil, threading, time
import tkinter as tk, unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


# ══════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN  –  longpath + NFC/NFD korrekt
# ══════════════════════════════════════════════════════════════════

_IS_WIN = (os.name == "nt")

def _lp(s: str) -> str:
    """Windows Long-Path-String als reiner String (nicht Path-Objekt)."""
    if _IS_WIN and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + os.path.abspath(s)
    return s

def fexists(path) -> bool:
    """Prüft Existenz: NFC + NFD + Long-Path, alles als String."""
    for norm in ["NFC", "NFD", None]:
        s = unicodedata.normalize(norm, str(path)) if norm else str(path)
        try:
            if os.path.isfile(s):            return True
            if _IS_WIN and os.path.isfile(_lp(s)): return True
        except Exception:
            pass
    return False

def find_file(base, rel: str) -> Path | None:
    """
    Sucht base/rel als Datei.
    Versucht NFC und NFD — nötig weil Mac HFS+ NFD liefert,
    exFAT aber NFC speichert (und umgekehrt).
    Erkennt auch Symlinks (z.B. macOS Photos/iPhoto-Mediatheken
    verlinken oft interne Datenbank-Dateien statt sie zu duplizieren) —
    os.path.isfile() folgt Symlinks normalerweise, aber bei manchen
    exFAT-Edge-Cases meldet es False obwohl die Datei via os.path.exists
    durchaus aufgelöst werden kann. Daher zusätzlicher exists()-Check
    mit anschließender realpath-Auflösung.
    """
    for norm in ["NFC", "NFD"]:
        r   = unicodedata.normalize(norm, rel)
        s   = os.path.join(str(base), *r.replace("\\", "/").split("/"))
        try:
            if os.path.isfile(s):
                return Path(s)
            if _IS_WIN and os.path.isfile(_lp(s)):
                return Path(_lp(s))
            # Symlink-Fallback: existiert der Pfad überhaupt (auch als Link)?
            if os.path.islink(s) or os.path.exists(s):
                resolved = os.path.realpath(s)
                if os.path.isfile(resolved):
                    return Path(resolved)
        except Exception:
            pass
    return None

def copy_file(src, dst) -> tuple[bool, str]:
    """Kopiert src→dst mit SHA256-Verifikation. Vollständig String-basiert."""
    # Symlink-Quellen auflösen — macOS' native copyfile()-Optimierung in
    # shutil kann Symlinks am Ziel reproduzieren statt sie aufzulösen.
    try:
        if os.path.islink(str(src)):
            resolved = os.path.realpath(str(src))
            if os.path.isfile(resolved):
                src = resolved
    except Exception:
        pass

    ss, ds = _lp(str(src)), _lp(str(dst))
    try:
        os.makedirs(_lp(str(Path(str(dst)).parent)), exist_ok=True)
        shutil.copy2(ss, ds)
        # Falls trotzdem ein Symlink am Ziel entstand: nachträglich aufloesen
        if os.path.islink(ds):
            real_target = os.path.realpath(ds)
            if os.path.isfile(real_target) and real_target != ds:
                with open(real_target, "rb") as f:
                    content = f.read()
                os.unlink(ds)
                with open(ds, "wb") as f:
                    f.write(content)
        if _sha256(ss) != _sha256(ds):
            try: os.unlink(ds)
            except: pass
            return False, "Hash-Mismatch"
        return True, "ok"
    except Exception as e:
        return False, str(e)

def del_file(path) -> tuple[bool, str]:
    s = _lp(str(path))
    try:
        if os.path.isfile(s):
            os.unlink(s)
        return True, "ok"
    except Exception as e:
        return False, str(e)

def _sha256(path_str: str) -> str:
    h = hashlib.sha256()
    with open(path_str, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b: break
            h.update(b)
    return h.hexdigest()

def fmt_eta(s: float) -> str:
    if s < 60:   return f"{int(s)}s"
    if s < 3600: return f"{int(s/60)}min"
    return f"{s/3600:.1f}h"

def top_folder(rel: str, depth: int = 2) -> str:
    parts = Path(rel).parts
    return str(Path(*parts[:min(depth, len(parts))]))


# ══════════════════════════════════════════════════════════════════
# WINDOWS-ZEICHENBEREINIGUNG
# ══════════════════════════════════════════════════════════════════

_REPL = {'<':'(', '>':')', ':':'-', '#':'_', '|':'-', '?':'_', '*':'_', '"':"'"}

def sanitize_rel(rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")
    return "/".join("".join(_REPL.get(c, c) for c in p) for p in parts)

def has_illegal(s: str) -> bool:
    return any(c in s for c in _REPL)


# ══════════════════════════════════════════════════════════════════
# ROOT-ZUORDNUNG
# ══════════════════════════════════════════════════════════════════

def extract_root_name(src_path: str, known: set) -> str | None:
    for part in src_path.replace("\\", "/").split("/"):
        if part in known:
            return part
    return None

def build_roots_by_name(paths: list, check_exists=True) -> dict:
    result = {}
    for p in paths:
        name = Path(p).name
        if name and (not check_exists or Path(p).is_dir()):
            result[name] = p
    return result

def find_target(entry: dict, roots_by_name: dict, rel: str) -> Path | None:
    """Findet korrekten Ziel-Pfad über src-Pfad aus harvest_entry."""
    src  = entry.get("src", "")
    known = set(roots_by_name.keys())
    name  = extract_root_name(src, known) if src else None
    if name and name in roots_by_name:
        return Path(roots_by_name[name]) / rel
    # Fallback: erste Root
    if roots_by_name:
        return Path(next(iter(roots_by_name.values()))) / rel
    return None

def detect_machine(directive: dict) -> str | None:
    sources = directive.get("sources", {})
    for slot in ("mac", "win"):
        roots = sources.get(slot, {}).get("roots", [])
        if any(Path(r).is_dir() for r in roots):
            return slot
    return None

def build_usb_roots(usb_root: Path, directive: dict) -> dict:
    dir_roots = directive.get("sources", {}).get("usb", {}).get("roots", [])
    names     = {Path(r).name for r in dir_roots}
    result    = {}
    for name in names:
        cand = usb_root / name
        if cand.is_dir():
            result[name] = str(cand)
    if not result:
        exclude = {"System Volume Information","RECYCLER","$RECYCLE.BIN","_sync"}
        for d in usb_root.iterdir():
            if d.is_dir() and not d.name.startswith((".", "_", "$")) \
               and d.name not in exclude:
                result[d.name] = str(d)
    return result


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
        if not self.path.is_file(): return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.decisions    = d.get("decisions", {})
            self.done         = set(d.get("done", []))
            self.run1_machine = d.get("run1_machine", "")
            self.run1_done    = d.get("run1_done", False)
        except Exception: pass

    def save(self):
        self.path.write_text(json.dumps({
            "run1_machine": self.run1_machine,
            "run1_done":    self.run1_done,
            "decisions":    self.decisions,
            "done":         sorted(self.done),
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    def is_done(self, k: str) -> bool: return k in self.done
    def mark_done(self, k: str): self.done.add(k)


# ══════════════════════════════════════════════════════════════════
# EXECUTOR
# ══════════════════════════════════════════════════════════════════

class Executor:
    def __init__(self, entries, er, machine, usb_roots, local_roots,
                 harvest_root, on_progress, on_log, on_usb_done,
                 on_local_done, cancel):
        self.entries      = entries
        self.er           = er
        self.machine      = machine
        self.usb_roots    = usb_roots
        self.local_roots  = local_roots
        self.hr           = harvest_root
        self.on_progress  = on_progress
        self.on_log       = on_log
        self.on_usb_done  = on_usb_done
        self.on_local_done = on_local_done
        self.cancel       = cancel
        self.access_denied: list[str] = []   # für Popup

    # ── USB-Phase ────────────────────────────────────────────────

    def run_usb(self):
        SLOT = {"COPY MAC":"mac", "COPY WIN":"win", "COPY":"both"}
        todo = [(e,) for e in self.entries
                if e.get("action") in SLOT and e.get("status") == "ok"
                and not self.er.is_done(f"{e['rel_path']}::usb")]
        todo += [(e,) for e in self.entries
                 if e.get("status") == "pending_delete"
                 and not self.er.is_done(f"{e['rel_path']}::usb")]
        ok = err = 0
        for i, (entry,) in enumerate(todo):
            if self.cancel.is_set(): break
            rel = entry["rel_path"]
            act = entry.get("action","")
            self.on_progress(i+1, len(todo), rel, f"USB ◄ {act}")

            if entry.get("status") == "pending_delete":
                tgt = find_target(entry, self.usb_roots, rel)
                if tgt:
                    d_ok, info = del_file(tgt)
                    if d_ok:
                        self.er.mark_done(f"{rel}::usb"); ok += 1
                        self.on_log(f"OK   DEL-USB  {rel[:65]}")
                    else:
                        self.on_log(f"ERR  DEL-USB  {info[:30]}  {rel[:50]}")
                        if "WinError 5" in info: self.access_denied.append(str(tgt))
                        err += 1
                else:
                    self.er.mark_done(f"{rel}::usb"); ok += 1
                continue

            slot = SLOT[act]
            src  = find_file(self.hr / "staging" / slot, rel)
            if src is None and has_illegal(rel):
                rel_s = sanitize_rel(rel)
                src   = find_file(self.hr / "staging" / slot, rel_s)
            if src is None:
                self.on_log(f"MISS staging/{slot}  {rel[:60]}"); err += 1; continue

            tgt = find_target(entry, self.usb_roots, rel)
            if tgt is None:
                self.on_log(f"MISS usb-root  {rel[:65]}"); err += 1; continue

            if fexists(tgt):
                self.er.mark_done(f"{rel}::usb"); ok += 1; continue

            c_ok, info = copy_file(src, tgt)
            if c_ok:
                self.er.mark_done(f"{rel}::usb")
                self.on_log(f"OK   USB◄{slot}  {rel[:62]}"); ok += 1
            else:
                self.on_log(f"ERR  USB◄{slot}  {info[:28]}  {rel[:48]}"); err += 1

        self.er.save()
        self.on_usb_done(ok, err)

    # ── Lokale Phase ──────────────────────────────────────────────

    def run_local(self):
        m = self.machine
        GELOESCHT = self.hr / "_GELOESCHT"
        todo = []
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
        pending = [(k,e,x) for k,e,x in todo if not self.er.is_done(key_fn(e))]
        ok = err = 0
        start = time.time()

        for i, (kind, entry, extra) in enumerate(pending):
            if self.cancel.is_set(): break
            rel = entry["rel_path"]
            dk  = key_fn(entry)
            act = entry["action"]

            elapsed = time.time() - start
            eta = f"  ETA {fmt_eta(elapsed/i*(len(pending)-i))}" if i>0 else ""
            self.on_progress(i+1, len(pending), rel, f"{m.upper()} ◄ {act}{eta}")

            # ── COPY ──────────────────────────────────────────────
            if kind == "copy":
                # Quelle: staging auf USB
                src = find_file(self.hr / "staging" / extra, rel)

                # Sanitize-Fallback: Datei wurde umbenannt
                use_rel = rel
                if src is None and has_illegal(rel):
                    rel_s = sanitize_rel(rel)
                    # 1. Staging mit sanitiertem Namen
                    src = find_file(self.hr / "staging" / extra, rel_s)
                    # 2. USB-Root-Ordner (nach rename_for_windows.py)
                    if src is None:
                        root_name = extract_root_name(
                            entry.get("src",""), set(self.usb_roots.keys()))
                        if root_name and root_name in self.usb_roots:
                            src = find_file(self.usb_roots[root_name], rel_s)
                    if src is None:
                        for usb_path in self.usb_roots.values():
                            f = find_file(usb_path, rel_s)
                            if f: src = f; break
                    if src is not None:
                        use_rel = rel_s  # Ziel ebenfalls sanitiert

                if src is None:
                    self.on_log(f"MISS staging/{extra}  {rel[:60]}"); err += 1; continue

                tgt = find_target(entry, self.local_roots, use_rel)
                if tgt is None:
                    self.on_log(f"MISS local-root  {rel[:60]}"); err += 1; continue

                if fexists(tgt):
                    self.er.mark_done(dk); ok += 1; continue

                c_ok, info = copy_file(src, tgt)
                if c_ok:
                    self.er.mark_done(dk)
                    self.on_log(f"OK   {m.upper()}◄staging  {rel[:58]}"); ok += 1
                else:
                    self.on_log(f"ERR  COPY  {info[:28]}  {rel[:50]}"); err += 1
                    if "WinError 5" in info: self.access_denied.append(str(tgt))

            # ── LÖSCHUNGEN ────────────────────────────────────────
            elif kind == "del":
                folder = top_folder(rel)
                dec    = self.er.decisions.get(f"{act}::{folder}", "confirm")
                done_trivially = False

                if act == "CONFLICT del Win":
                    if m == "mac" and dec == "confirm":
                        tgt = find_target(entry, self.local_roots, rel)
                        if tgt and fexists(tgt):
                            d_ok, info = del_file(tgt)
                            if d_ok:
                                self.er.mark_done(dk); ok += 1
                                self.on_log(f"OK   DEL-MAC  {rel[:62]}")
                            else:
                                self.on_log(f"ERR  DEL  {info[:28]}  {rel[:50]}"); err += 1
                                if "WinError 5" in info:
                                    self.access_denied.append(str(tgt))
                        else:
                            self.er.mark_done(dk); ok += 1
                    elif m == "win" and dec == "restore":
                        src = self._geloescht(GELOESCHT, "del_win", rel)
                        if src:
                            tgt = find_target(entry, self.local_roots, rel)
                            if tgt:
                                c_ok, info = copy_file(src, tgt)
                                if c_ok:
                                    self.er.mark_done(dk); ok += 1
                                    self.on_log(f"OK   RST►WIN  {rel[:60]}")
                                else:
                                    self.on_log(f"ERR  RST  {info[:28]}  {rel[:50]}"); err += 1
                        else:
                            self.on_log(f"MISS _GELOESCHT/del_win  {rel[:55]}"); err += 1
                    else:
                        self.er.mark_done(dk); ok += 1  # nicht zuständig

                elif act == "CONFLICT del Mac":
                    if m == "win" and dec == "confirm":
                        tgt = find_target(entry, self.local_roots, rel)
                        if tgt and fexists(tgt):
                            d_ok, info = del_file(tgt)
                            if d_ok:
                                self.er.mark_done(dk); ok += 1
                                self.on_log(f"OK   DEL-WIN  {rel[:62]}")
                            else:
                                self.on_log(f"ERR  DEL  {info[:28]}  {rel[:50]}"); err += 1
                                if "WinError 5" in info:
                                    self.access_denied.append(str(tgt))
                        else:
                            self.er.mark_done(dk); ok += 1
                    elif m == "mac" and dec == "restore":
                        src = self._geloescht(GELOESCHT, "del_mac", rel)
                        if src:
                            tgt = find_target(entry, self.local_roots, rel)
                            if tgt:
                                c_ok, info = copy_file(src, tgt)
                                if c_ok:
                                    self.er.mark_done(dk); ok += 1
                                    self.on_log(f"OK   RST►MAC  {rel[:60]}")
                                else:
                                    self.on_log(f"ERR  RST  {info[:28]}  {rel[:50]}"); err += 1
                        else:
                            self.on_log(f"MISS _GELOESCHT/del_mac  {rel[:55]}"); err += 1
                    else:
                        self.er.mark_done(dk); ok += 1

            # ── CONFLICT modified ──────────────────────────────────
            elif kind == "conflict":
                winner = self.er.decisions.get(f"CONFLICT::{rel}", "mac")
                src    = find_file(self.hr / "conflicts" / rel, f"_{winner}")
                if src is None:
                    self.on_log(f"MISS conflict/{winner}  {rel[:58]}"); err += 1; continue
                tgt = find_target(entry, self.local_roots, rel)
                if tgt is None:
                    self.on_log(f"MISS local-root CONF  {rel[:58]}"); err += 1; continue
                c_ok, info = copy_file(src, tgt)
                if c_ok:
                    self.er.mark_done(dk)
                    self.on_log(f"OK   CONF►{m.upper()}  {rel[:58]}"); ok += 1
                else:
                    self.on_log(f"ERR  CONF  {info[:28]}  {rel[:50]}"); err += 1

        self.er.save()
        complete = (err == 0 and not self.cancel.is_set()
                    and self._all_copy_done())
        self.on_local_done(ok, err, complete)

    def _geloescht(self, root: Path, sub: str, rel: str) -> Path | None:
        if not root.is_dir(): return None
        for d in sorted(root.iterdir(), reverse=True):
            if not d.is_dir(): continue
            f = find_file(d / sub, rel)
            if f: return f
        return None

    def _all_copy_done(self) -> bool:
        """
        True wenn alle COPY MAC→Win, COPY WIN→Mac UND alle CONFLICT
        modified/new-Einträge auf BEIDEN Plattformen (mac+win) lokal
        bereitgestellt sind. CONFLICT-Einträge brauchen IMMER beide
        Seiten — unabhängig davon welche Maschine gerade den Check macht —
        sonst meldet 'complete' fälschlich True, wenn z.B. diese Runde
        gar keine COPY-Einträge offen hatte (copy_mac/copy_win == 0),
        aber CONFLICTs auf der jeweils ANDEREN Plattform noch ausstehen.
        """
        for e in self.entries:
            act, st = e.get("action",""), e.get("status","")
            if st != "ok":
                continue
            rel = e["rel_path"]
            if act == "COPY MAC":
                if not self.er.is_done(f"{rel}::local::win"):
                    return False
            elif act == "COPY WIN":
                if not self.er.is_done(f"{rel}::local::mac"):
                    return False
            elif act in ("CONFLICT modified", "CONFLICT new"):
                # Braucht IMMER beide Seiten, unabhängig von self.machine
                if not self.er.is_done(f"{rel}::local::mac"):
                    return False
                if not self.er.is_done(f"{rel}::local::win"):
                    return False
        return True


# ══════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════

class ExecutionApp:
    N_TABS = 5

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("⚡ Change Execution v3.0")
        self.root.geometry("1020x840")
        self.root.resizable(True, True)

        self._directive:      dict | None     = None
        self._harvest_report: dict | None     = None
        self._er:             ExecReport|None = None
        self._harvest_root:   Path | None     = None
        self._usb_root:       Path | None     = None
        self._machine:        str             = ""
        self._usb_roots:      dict            = {}
        self._local_roots:    dict            = {}
        self._entries:        list            = []
        self._locked:         bool            = False
        self._cancel:         threading.Event = threading.Event()
        self._log_lines:      list            = []
        self._decisions:      dict            = {}
        self._delete_keep:    dict            = {}
        self._conflict_win:   dict            = {}
        self._del_item_map:   dict            = {}
        self._tabs_seen:      set             = set()
        self._current_ex:     Executor | None = None

        self._build_ui()

    # ──────────────────────────────────────────────────────────────
    # UI
    # ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self.root, bg="#922b21", height=52)
        hdr.pack(fill=tk.X); hdr.pack_propagate(False)
        tk.Label(hdr, text="⚡ Change Execution v3.0 – Änderungen ausführen",
                 font=("Helvetica", 13, "bold"),
                 bg="#922b21", fg="white").pack(pady=13)

        usb_f = tk.LabelFrame(self.root,
                              text="Schritt 1 – USB-Laufwerk wählen",
                              padx=10, pady=6)
        usb_f.pack(fill=tk.X, padx=10, pady=(8,4))
        self._usb_var = tk.StringVar(value="(noch nicht gewählt)")
        tk.Label(usb_f, textvariable=self._usb_var,
                 font=("Courier",9), anchor="w", fg="#c0392b"
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(usb_f, text="📂  USB wählen", command=self._pick_usb,
                  font=("Helvetica",10,"bold"), bg="#922b21", fg="white",
                  width=18).pack(side=tk.RIGHT)

        mac_f = tk.LabelFrame(self.root,
                              text="Schritt 2 – Erkannter Rechner & Root",
                              padx=10, pady=6)
        mac_f.pack(fill=tk.X, padx=10, pady=4)
        self._machine_var = tk.StringVar(value="–")
        self._root_var    = tk.StringVar(value="–")
        self._lauf_var    = tk.StringVar(value="–")
        self._warn_var    = tk.StringVar(value="")
        for label, var in [("Rechner:", self._machine_var),
                            ("Root:",    self._root_var),
                            ("Lauf:",    self._lauf_var)]:
            row = tk.Frame(mac_f); row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, width=10, anchor="w",
                     font=("Helvetica",9,"bold")).pack(side=tk.LEFT)
            tk.Label(row, textvariable=var, font=("Courier",8),
                     anchor="w").pack(side=tk.LEFT, fill=tk.X)
        self._warn_label = tk.Label(mac_f, textvariable=self._warn_var,
                                    font=("Helvetica",9,"bold"),
                                    fg="#e67e22", anchor="w", justify="left")
        self._warn_label.pack(fill=tk.X)

        prog_f = tk.LabelFrame(self.root, text="Fortschritt", padx=10, pady=6)
        prog_f.pack(fill=tk.X, padx=10, pady=4)
        self._prog_label = tk.Label(prog_f, text="Bereit.",
                                    anchor="w", font=("Helvetica",10))
        self._prog_label.pack(fill=tk.X)
        self._file_label = tk.Label(prog_f, text="", anchor="w",
                                    font=("Courier",8), fg="#555")
        self._file_label.pack(fill=tk.X)
        self._prog_bar = ttk.Progressbar(prog_f, mode="determinate")
        self._prog_bar.pack(fill=tk.X, pady=(4,2))

        cnt = tk.Frame(prog_f); cnt.pack(fill=tk.X, pady=4)
        self._cnt = {}
        for col, (key, label, color) in enumerate([
            ("ok",  "✅ Erledigt", "#27ae60"),
            ("err", "❌ Fehler",   "#c0392b"),
            ("skip","⏭ Skip",     "#2980b9"),
        ]):
            var = tk.StringVar(value="0"); self._cnt[key] = var
            cell = tk.Frame(cnt, relief=tk.GROOVE, bd=1, padx=8, pady=4)
            cell.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)
            tk.Label(cell, text=label, font=("Helvetica",9), fg=color).pack()
            tk.Label(cell, textvariable=var,
                     font=("Helvetica",14,"bold")).pack()

        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab)

        frames = [(n, t) for n, t in [
            ("_f_overview",  "📊 Übersicht"),
            ("_f_copy",      "📋 COPY"),
            ("_f_del",       "🗑 Löschungen"),
            ("_f_delete",    "🗑 DELETE"),
            ("_f_conflict",  "⚠️  CONFLICT"),
            ("_f_log",       "📋 Log"),
        ]]
        for attr, title in frames:
            f = tk.Frame(self._nb)
            setattr(self, attr, f)
            self._nb.add(f, text=title)

        self._build_overview_tab()
        self._build_copy_tab()
        self._build_del_tab()
        self._build_delete_tab()
        self._build_conflict_tab()
        self._build_log_tab()

        self._status_var = tk.StringVar(value="Bereit.")
        tk.Label(self.root, textvariable=self._status_var,
                 anchor="w", font=("Helvetica",9),
                 relief=tk.SUNKEN).pack(fill=tk.X, padx=10, pady=(2,0))

        btn = tk.Frame(self.root); btn.pack(fill=tk.X, padx=10, pady=6)
        self._exec_btn = tk.Button(btn, text="▶  Ausführen (dieser Lauf)",
                                   command=self._execute,
                                   font=("Helvetica",12,"bold"),
                                   bg="#922b21", fg="white",
                                   state=tk.DISABLED, height=2, width=26)
        self._exec_btn.pack(side=tk.LEFT, padx=5)
        self._cancel_btn = tk.Button(btn, text="⏹ Abbrechen",
                                     command=lambda: self._cancel.set(),
                                     font=("Helvetica",11), state=tk.DISABLED,
                                     height=2, width=14)
        self._cancel_btn.pack(side=tk.LEFT, padx=5)
        tk.Button(btn, text="📋 Log speichern", command=self._save_log,
                  font=("Helvetica",11), height=2, width=16
                  ).pack(side=tk.LEFT, padx=5)
        self._gate_lbl = tk.Label(btn, text="",
                                  font=("Helvetica",9,"italic"), fg="#922b21")
        self._gate_lbl.pack(side=tk.LEFT, padx=10)

    # ── Tab-Inhalte ───────────────────────────────────────────────

    def _build_overview_tab(self):
        self._ov_text = tk.Text(self._f_overview, font=("Courier",10),
                                state=tk.DISABLED)
        sb = ttk.Scrollbar(self._f_overview, orient="vertical",
                            command=self._ov_text.yview)
        self._ov_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._ov_text.pack(fill=tk.BOTH, expand=True)

    def _build_copy_tab(self):
        tk.Label(self._f_copy, text="Automatisch – keine Entscheidung nötig.",
                 font=("Helvetica",10), fg="#555").pack(anchor="w", padx=10, pady=6)
        cols = ("action","count","staging","destination")
        self._copy_tree = ttk.Treeview(self._f_copy, columns=cols,
                                       show="headings", height=6)
        for col, w, lbl in [("action",140,"Aktion"),("count",80,"Dateien"),
                             ("staging",300,"Quelle (staging)"),
                             ("destination",450,"Ziel")]:
            self._copy_tree.heading(col, text=lbl)
            self._copy_tree.column(col, width=w)
        sb = ttk.Scrollbar(self._f_copy, orient="vertical",
                            command=self._copy_tree.yview)
        self._copy_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._copy_tree.pack(fill=tk.BOTH, expand=True, padx=10)

    def _build_del_tab(self):
        top = tk.Frame(self._f_del); top.pack(fill=tk.X, padx=10, pady=6)
        tk.Label(top, text="Vorauswahl: ✔ Löschen  |  "
                 "Sicherheitskopie in harvest/_GELOESCHT/",
                 font=("Helvetica",10), fg="#555").pack(side=tk.LEFT)
        self._all_conf = tk.Button(top, text="Alle: ✔ Löschen",
                                   command=lambda: self._set_all("confirm"),
                                   width=16)
        self._all_conf.pack(side=tk.RIGHT, padx=4)
        self._all_rest = tk.Button(top, text="Alle: ↩ Wiederherstellen",
                                   command=lambda: self._set_all("restore"),
                                   width=22)
        self._all_rest.pack(side=tk.RIGHT, padx=4)

        cols = ("decision","count","size","folder")
        self._del_tree = ttk.Treeview(self._f_del, columns=cols,
                                      show="headings", selectmode="browse")
        for col, w, lbl, anch in [
            ("decision",170,"Entscheidung","center"),
            ("count",70,"Dateien","center"),
            ("size",90,"Größe","center"),
            ("folder",640,"Ordner","w"),
        ]:
            self._del_tree.heading(col, text=lbl)
            self._del_tree.column(col, width=w, anchor=anch)
        sb = ttk.Scrollbar(self._f_del, orient="vertical",
                            command=self._del_tree.yview)
        self._del_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._del_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self._del_tree.bind("<Double-1>", self._toggle_del)
        for tag, bg in [("confirm","#d5f5e3"),("restore","#d6eaf8"),
                         ("header","#ecf0f1"),("done","#f0f0f0")]:
            kw = {"background": bg}
            if tag == "header": kw["font"] = ("Helvetica",9,"bold")
            if tag == "done":   kw["foreground"] = "#aaa"
            self._del_tree.tag_configure(tag, **kw)
        tk.Label(self._f_del,
                 text="Doppelklick: umschalten  |  🔒 Nach Lauf 1 gesperrt",
                 font=("Helvetica",8), fg="#888").pack(anchor="w", padx=10, pady=2)

    def _build_delete_tab(self):
        tk.Label(self._f_delete,
                 text="Beide gelöscht.  Vorauswahl: von USB entfernen.\n"
                      "Sicherheitskopie verbleibt in harvest/.",
                 font=("Helvetica",10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)
        cols = ("decision","rel_path")
        self._delete_tree = ttk.Treeview(self._f_delete, columns=cols,
                                         show="headings", height=10)
        self._delete_tree.heading("decision", text="Entscheidung")
        self._delete_tree.heading("rel_path", text="Datei")
        self._delete_tree.column("decision", width=120, anchor="center")
        self._delete_tree.column("rel_path", width=840)
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
                 font=("Helvetica",8), fg="#888").pack(anchor="w", padx=10, pady=2)

    def _build_conflict_tab(self):
        tk.Label(self._f_conflict,
                 text="Mac UND Win verändert.  Wähle welche Version gewinnt.\n"
                      "Verlierer-Version bleibt in harvest/conflicts/.",
                 font=("Helvetica",10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)
        cols = ("winner","rel_path","mac_info","win_info")
        self._conf_tree = ttk.Treeview(self._f_conflict, columns=cols,
                                       show="headings", height=10)
        for col, w, lbl in [("winner",90,"Gewinner"),("rel_path",380,"Datei"),
                             ("mac_info",230,"Mac (Größe/mtime)"),
                             ("win_info",230,"Win (Größe/mtime)")]:
            self._conf_tree.heading(col, text=lbl)
            self._conf_tree.column(col, width=w,
                                   anchor="center" if col=="winner" else "w")
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
                 text="Doppelklick: Mac ↔ Win  |  🔒 Nach Lauf 1 gesperrt",
                 font=("Helvetica",8), fg="#888").pack(anchor="w", padx=10, pady=2)

    def _build_log_tab(self):
        self._log_text = tk.Text(self._f_log, font=("Courier",8),
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
        path = filedialog.askdirectory(title="USB-Laufwerk (Wurzel) wählen")
        if not path: return

        usb  = Path(path)
        sync = usb / "_sync"
        if not sync.is_dir():
            messagebox.showwarning("Kein _sync", f"Kein _sync auf:\n{usb}")
            return

        dated = sorted([d for d in sync.iterdir() if d.is_dir()], reverse=True)
        if not dated: return
        sync_dir     = dated[0]
        harvest_root = sync_dir / "harvest"

        rep_path = harvest_root / "harvest_report.json"
        if not rep_path.is_file():
            messagebox.showwarning("Kein harvest_report",
                                   f"harvest_report.json nicht gefunden:\n{harvest_root}")
            return
        try:
            self._harvest_report = json.loads(
                rep_path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Lesefehler", str(exc)); return

        dir_cands = sorted(sync_dir.glob("change_directive_*.json"), reverse=True)
        if not dir_cands:
            messagebox.showwarning("Keine Directive",
                                   f"change_directive_*.json fehlt:\n{sync_dir}")
            return
        try:
            self._directive = json.loads(
                dir_cands[0].read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Lesefehler Directive", str(exc)); return

        self._er           = ExecReport(harvest_root / "execution_report.json")
        self._harvest_root = harvest_root
        self._usb_root     = usb
        self._entries      = self._harvest_report.get("entries", [])
        self._usb_roots    = build_usb_roots(usb, self._directive)
        self._locked       = self._er.run1_done

        lauf_str = ("🔒 Lauf 2 – Entscheidungen aus Lauf 1 gesperrt"
                    if self._locked else "Lauf 1 – Entscheidungen treffen")
        self._usb_var.set(f"✔  {usb}  →  _sync/{sync_dir.name}/harvest/")
        self._lauf_var.set(lauf_str)
        self._gate_lbl.config(
            text="🔒 Lauf 2 – direkt ausführbar" if self._locked else
                 "Alle Tabs ansehen bevor Ausführen aktiv wird"
        )

        # Windows-illegale Zeichen prüfen
        ill = sum(1 for e in self._entries
                  if e.get("action") in ("COPY MAC","COPY WIN","COPY")
                  and e.get("status") == "ok"
                  and has_illegal(e.get("rel_path","")))
        if ill > 0:
            self._warn_var.set(
                f"⚠ {ill} Dateien mit Windows-illegalen Zeichen (<>:#|?*\") "
                f"gefunden.\n"
                f"  → Empfehlung: rename_for_windows.py auf Mac ausführen, dann hier fortfahren.\n"
                f"  → Fortfahren möglich: illegale Zeichen werden beim Kopieren automatisch ersetzt."
            )
        else:
            self._warn_var.set("")

        self._detect_and_setup()

    def _detect_and_setup(self):
        machine = detect_machine(self._directive)
        if machine:
            local_roots = build_roots_by_name(
                self._directive["sources"][machine]["roots"],
                check_exists=True)
            self._machine     = machine
            self._local_roots = local_roots
            roots_str = "  ".join(list(local_roots.values())[:3])
            self._machine_var.set(
                f"{'🍎 Mac' if machine=='mac' else '🖥 Win'}  (auto-erkannt)")
            self._root_var.set(roots_str[:110])
        else:
            ok = messagebox.askyesno("Rechner nicht erkannt",
                                     "Ist dies ein Mac?\n(Nein = Windows)")
            machine = "mac" if ok else "win"
            path = filedialog.askdirectory(
                title=f"{'Mac' if ok else 'Win'}-Root wählen")
            if not path: return
            self._machine     = machine
            self._local_roots = {Path(path).name: path}
            self._machine_var.set(f"{'🍎 Mac' if ok else '🖥 Win'}  (manuell)")
            self._root_var.set(path)

        if self._locked:
            self._decisions    = {k: v for k,v in self._er.decisions.items()
                                  if "::" in k and not k.startswith(("DELETE","CONFLICT"))}
            self._delete_keep  = {k.replace("DELETE::",""):(v=="keep")
                                  for k,v in self._er.decisions.items()
                                  if k.startswith("DELETE::")}
            self._conflict_win = {k.replace("CONFLICT::",""):v
                                  for k,v in self._er.decisions.items()
                                  if k.startswith("CONFLICT::")}

        self._populate_all()
        if self._locked:
            self._exec_btn.config(state=tk.NORMAL)

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
        c_mac   = sum(1 for e in entries if e.get("action")=="COPY MAC" and e.get("status")=="ok")
        c_win   = sum(1 for e in entries if e.get("action")=="COPY WIN" and e.get("status")=="ok")
        d_win   = sum(1 for e in entries if e.get("action")=="CONFLICT del Win" and e.get("status")=="ok")
        d_mac   = sum(1 for e in entries if e.get("action")=="CONFLICT del Mac" and e.get("status")=="ok")
        deletes = sum(1 for e in entries if e.get("status")=="pending_delete")
        conf    = sum(1 for e in entries if e.get("action") in
                      ("CONFLICT modified","CONFLICT new") and e.get("status")=="ok")
        done_usb = sum(1 for k in self._er.done if "::usb" in k)
        done_loc = sum(1 for k in self._er.done if f"::local::{m}" in k)
        lines = [
            "="*62,
            f"  CHANGE EXECUTION v3.0  |  {'🔒 Lauf 2' if self._locked else 'Lauf 1'}  |  Rechner: {m.upper()}",
            "="*62,"",
            "  USB-Phase:",
            f"    COPY MAC → USB:  {c_mac:>6,}    COPY WIN → USB: {c_win:>6,}",
            f"    DELETE von USB:  {deletes:>6,}    Bereits erledigt: {done_usb:>6,}",
            "",
            f"  Lokale Phase ({m.upper()}):",
            f"    COPY MAC → Win:  {c_mac:>6,}  (nur Win)  |  COPY WIN → Mac: {c_win:>6,}  (nur Mac)",
            f"    del Win:         {d_win:>6,}    del Mac:  {d_mac:>6,}",
            f"    CONFLICT:        {conf:>6,}    Bereits erledigt: {done_loc:>6,}",
            "","─"*62,
            "  USB-Roots:",
        ]
        for name, path in self._usb_roots.items():
            lines.append(f"    {name}: {path}")
        lines += ["","  Lokale Roots:"]
        for name, path in self._local_roots.items():
            lines.append(f"    {name}: {path}")
        lines.append("")
        self._ov_text.config(state=tk.NORMAL)
        self._ov_text.delete("1.0", tk.END)
        self._ov_text.insert(tk.END, "\n".join(lines))
        self._ov_text.config(state=tk.DISABLED)

    def _populate_copy(self):
        self._copy_tree.delete(*self._copy_tree.get_children())
        m = self._machine
        for act, slot, dest in [
            ("COPY MAC","mac",f"→ USB + Win  (Win nur wenn m==win)"),
            ("COPY WIN","win",f"→ USB + Mac  (Mac nur wenn m==mac)"),
            ("COPY",    "both","→ USB"),
        ]:
            grp = [e for e in self._entries if e.get("action")==act and e.get("status")=="ok"]
            if not grp: continue
            done = sum(1 for e in grp if self._er.is_done(f"{e['rel_path']}::usb")
                       or self._er.is_done(f"{e['rel_path']}::local::{m}"))
            staging = str(self._harvest_root/"staging"/slot) if self._harvest_root else "?"
            self._copy_tree.insert("", tk.END, values=(
                act, f"{len(grp):,}  ({done:,} done)", staging, dest))

    def _populate_del(self):
        self._del_tree.delete(*self._del_tree.get_children())
        self._del_item_map = {}
        m = self._machine
        for category, action in [
            ("── Win hat gelöscht (del Win) ──","CONFLICT del Win"),
            ("── Mac hat gelöscht (del Mac) ──","CONFLICT del Mac"),
        ]:
            cat = [e for e in self._entries if e.get("action")==action and e.get("status")=="ok"]
            if not cat: continue
            self._del_tree.insert("","tk.END" if False else tk.END,
                values=("",f"{len(cat):,}","",category), tags=("header",))
            folder_map = defaultdict(list)
            for e in cat: folder_map[top_folder(e["rel_path"])].append(e)
            for folder, fentries in sorted(folder_map.items()):
                key = f"{action}::{folder}"
                dec = (self._er.decisions.get(key,"confirm") if self._locked
                       else self._decisions.get(key,"confirm"))
                self._decisions[key] = dec
                done_count = sum(1 for e in fentries
                                 if self._er.is_done(f"{e['rel_path']}::local::{m}"))
                is_done = (done_count == len(fentries))
                label = ("✅ Erledigt" if is_done else
                         "✔ Löschen" if dec=="confirm" else "↩ Wiederherstellen")
                tag = "done" if is_done else dec
                iid = self._del_tree.insert("", tk.END,
                    values=(label, f"{len(fentries):,}", "", folder), tags=(tag,))
                if not is_done: self._del_item_map[iid] = key
        if self._locked:
            self._all_conf.config(state=tk.DISABLED)
            self._all_rest.config(state=tk.DISABLED)

    def _populate_delete(self):
        self._delete_tree.delete(*self._delete_tree.get_children())
        self._delete_keep = {}
        m = self._machine
        for e in self._entries:
            if e.get("status") != "pending_delete": continue
            rel  = e["rel_path"]
            keep = (self._er.decisions.get(f"DELETE::{rel}","delete")=="keep"
                    if self._locked else False)
            self._delete_keep[rel] = keep
            done = self._er.is_done(f"{rel}::usb")
            label = "✅ Erledigt" if done else ("💾 Behalten" if keep else "🗑 Löschen")
            tag   = "done" if done else ("keep" if keep else "delete")
            self._delete_tree.insert("", tk.END, values=(label, rel), tags=(tag,))

    def _populate_conflict(self):
        self._conf_tree.delete(*self._conf_tree.get_children())
        self._conflict_win = {}
        m = self._machine
        conf = [e for e in self._entries
                if e.get("action") in ("CONFLICT modified","CONFLICT new")
                and e.get("status")=="ok"]
        if not conf:
            self._conf_tree.insert("","",values=("","(keine CONFLICT modified)","",""),
                                   tags=("done",)); return
        for e in conf:
            rel    = e["rel_path"]
            winner = (self._er.decisions.get(f"CONFLICT::{rel}","mac")
                      if self._locked else "mac")
            self._conflict_win[rel] = winner
            done   = self._er.is_done(f"{rel}::local::{m}")
            ps = e.get("per_source",{})
            def fmt(d):
                if not d: return "–"
                return (f"{d.get('size',0)/1e6:.1f}MB  "
                        f"{datetime.fromtimestamp(d.get('mtime',0)).strftime('%Y-%m-%d %H:%M')}")
            label = "✅ Erledigt" if done else ("📱 Mac" if winner=="mac" else "🖥 Win")
            tag   = "done" if done else ("mac" if winner=="mac" else "win")
            self._conf_tree.insert("", tk.END,
                values=(label, rel, fmt(ps.get("mac")), fmt(ps.get("win"))), tags=(tag,))

    # ──────────────────────────────────────────────────────────────
    # TAB-GATE
    # ──────────────────────────────────────────────────────────────

    def _on_tab(self, event):
        idx = self._nb.index(self._nb.select())
        self._tabs_seen.add(idx)
        if not self._locked and len(self._tabs_seen) >= self.N_TABS:
            self._exec_btn.config(state=tk.NORMAL)
            self._gate_lbl.config(text="")
        elif not self._locked:
            self._gate_lbl.config(
                text=f"Noch {self.N_TABS - len(self._tabs_seen)} Tab(s) ansehen")

    # ──────────────────────────────────────────────────────────────
    # DOPPELKLICK
    # ──────────────────────────────────────────────────────────────

    def _toggle_del(self, event):
        if self._locked: return
        iid = self._del_tree.identify_row(event.y)
        if not iid or iid not in self._del_item_map: return
        key = self._del_item_map[iid]
        new = "restore" if self._decisions.get(key,"confirm")=="confirm" else "confirm"
        self._decisions[key] = new
        vals = list(self._del_tree.item(iid,"values"))
        vals[0] = "✔ Löschen" if new=="confirm" else "↩ Wiederherstellen"
        self._del_tree.item(iid, values=vals, tags=(new,))

    def _toggle_delete(self, event):
        if self._locked: return
        iid = self._delete_tree.identify_row(event.y)
        if not iid: return
        vals = list(self._delete_tree.item(iid,"values"))
        if vals[0] == "✅ Erledigt": return
        rel  = vals[1]
        new  = not self._delete_keep.get(rel, False)
        self._delete_keep[rel] = new
        vals[0] = "💾 Behalten" if new else "🗑 Löschen"
        self._delete_tree.item(iid, values=vals, tags=("keep" if new else "delete",))

    def _toggle_conflict(self, event):
        if self._locked: return
        iid = self._conf_tree.identify_row(event.y)
        if not iid: return
        vals = list(self._conf_tree.item(iid,"values"))
        if vals[0] == "✅ Erledigt": return
        rel    = vals[1]
        new    = "win" if self._conflict_win.get(rel,"mac")=="mac" else "mac"
        self._conflict_win[rel] = new
        vals[0] = "🖥 Win" if new=="win" else "📱 Mac"
        self._conf_tree.item(iid, values=vals, tags=("win" if new=="win" else "mac",))

    def _set_all(self, decision: str):
        if self._locked: return
        for iid, key in self._del_item_map.items():
            self._decisions[key] = decision
            vals = list(self._del_tree.item(iid,"values"))
            vals[0] = "✔ Löschen" if decision=="confirm" else "↩ Wiederherstellen"
            self._del_tree.item(iid, values=vals, tags=(decision,))

    # ──────────────────────────────────────────────────────────────
    # AUSFÜHREN
    # ──────────────────────────────────────────────────────────────

    def _execute(self):
        if not self._er or not self._harvest_root: return

        if not self._locked:
            for key, dec in self._decisions.items():
                self._er.decisions[key] = dec
            for rel, keep in self._delete_keep.items():
                self._er.decisions[f"DELETE::{rel}"] = "keep" if keep else "delete"
            for rel, winner in self._conflict_win.items():
                self._er.decisions[f"CONFLICT::{rel}"] = winner
            self._er.run1_machine = self._machine

            ok = messagebox.askyesno("Ausführen bestätigen",
                f"Lauf 1 – {self._machine.upper()} + USB\n\n"
                "Phase 1: USB-Operationen\n"
                f"Phase 2: {self._machine.upper()}-Operationen\n\n"
                "Nicht erreichbare Roots werden übersprungen.\nJetzt starten?")
            if not ok: return

        self._exec_btn.config(state=tk.DISABLED)
        self._cancel_btn.config(state=tk.NORMAL)
        self._cancel.clear()
        for var in self._cnt.values(): var.set("0")
        self._log_lines.clear()

        ex = Executor(
            entries       = self._entries,
            er            = self._er,
            machine       = self._machine,
            usb_roots     = self._usb_roots,
            local_roots   = self._local_roots,
            harvest_root  = self._harvest_root,
            on_progress   = self._cb_progress,
            on_log        = self._cb_log,
            on_usb_done   = self._cb_usb_done,
            on_local_done = self._cb_local_done,
            cancel        = self._cancel,
        )
        self._current_ex = ex
        threading.Thread(target=ex.run_usb, daemon=True).start()

    def _cb_usb_done(self, ok: int, err: int):
        self._cnt["ok"].set(str(ok))
        self._cnt["err"].set(str(err))
        def _show():
            self._prog_label.config(
                text=f"✅ USB-Phase: {ok:,} ok  |  {err:,} Fehler",
                fg="#27ae60" if err==0 else "#c0392b")
            if err > 0:
                messagebox.showwarning("USB-Phase",
                    f"USB-Phase mit {err:,} Fehlern abgeschlossen.\nSiehe Log.")
            else:
                messagebox.showinfo("USB-Phase",
                    f"USB vollständig.\n{ok:,} Operationen ok.\n\n"
                    f"Jetzt: {self._machine.upper()}-Operationen …")
            ex = self._current_ex
            if ex:
                threading.Thread(target=ex.run_local, daemon=True).start()
        self.root.after(0, _show)

    def _cb_local_done(self, ok: int, err: int, complete: bool):
        prev_ok  = int(self._cnt["ok"].get() or 0)
        prev_err = int(self._cnt["err"].get() or 0)
        self._cnt["ok"].set(str(prev_ok + ok))
        self._cnt["err"].set(str(prev_err + err))

        def _show():
            self._exec_btn.config(state=tk.NORMAL)
            self._cancel_btn.config(state=tk.DISABLED)
            self._prog_bar.config(value=100)

            if not self._locked:
                self._er.run1_done = True
            self._er.save()

            # Access-Denied-Popup
            ex = self._current_ex
            if ex and ex.access_denied:
                self._show_access_denied(ex.access_denied)

            if complete and self._locked:
                self._prog_label.config(
                    text="✅ EXECUTION COMPLETE", fg="#27ae60")
                self._status_var.set("✅ COMPLETE")
                messagebox.showinfo("COMPLETE",
                    "Alle Operationen abgeschlossen.\n"
                    "Mac, Win und USB sind synchronisiert.")
            elif not complete and not self._locked:
                self._prog_label.config(
                    text="⏳ Lauf 1 fertig – USB umstöpseln", fg="#e67e22")
                self._status_var.set("⏳ USB umstöpseln")
                messagebox.showinfo("Lauf 1 abgeschlossen",
                    f"Erledigt: {prev_ok+ok:,}  |  Fehler: {err:,}\n\n"
                    "USB an den anderen Rechner anschließen\n"
                    "und Change Execution dort starten.")
            else:
                self._status_var.set(f"⚠ {err:,} Fehler")

            self._populate_all()
        self.root.after(0, _show)

    def _show_access_denied(self, files: list[str]):
        """Popup für Dateien die nicht gelöscht werden konnten (WinError 5)."""
        win = tk.Toplevel(self.root)
        win.title("⚠ Manuell löschen erforderlich")
        win.geometry("720x420")
        win.grab_set()

        tk.Label(win,
                 text=f"⚠ {len(files)} Datei(en) konnten nicht gelöscht werden\n"
                      "(Zugriff verweigert – WinError 5)",
                 font=("Helvetica",11,"bold"), fg="#922b21",
                 justify="left").pack(anchor="w", padx=12, pady=(12,4))

        tk.Label(win,
                 text="Bitte manuell löschen:\n"
                      "  Rechtsklick → Eigenschaften → Schreibschutz entfernen\n"
                      "  Oder: Als Administrator ausführen",
                 font=("Helvetica",9), fg="#555",
                 justify="left").pack(anchor="w", padx=12, pady=(0,6))

        lb_f = tk.Frame(win); lb_f.pack(fill=tk.BOTH, expand=True, padx=12)
        lb = tk.Listbox(lb_f, font=("Courier",8), height=10)
        sb = ttk.Scrollbar(lb_f, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y); lb.pack(fill=tk.BOTH, expand=True)
        for f in files:
            lb.insert(tk.END, f)

        def _open_folder():
            if lb.curselection():
                f = files[lb.curselection()[0]]
                import subprocess, platform
                folder = str(Path(f).parent)
                if platform.system() == "Windows":
                    subprocess.Popen(["explorer", folder])
                else:
                    subprocess.Popen(["open", folder])

        btn_f = tk.Frame(win); btn_f.pack(fill=tk.X, padx=12, pady=8)
        tk.Button(btn_f, text="📂 Ordner öffnen",
                  command=_open_folder, width=18).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_f, text="✔ Verstanden",
                  command=win.destroy,
                  font=("Helvetica",11,"bold"),
                  bg="#2c3e50", fg="white",
                  width=14).pack(side=tk.RIGHT, padx=4)

    def _cb_progress(self, i, total, rel, label):
        pct = int(i/total*100) if total else 0
        self.root.after(0, lambda: [
            self._prog_bar.config(value=pct),
            self._prog_label.config(text=f"{label}  –  {i:,}/{total:,}  [{pct}%]"),
            self._file_label.config(text=f"  {rel[-80:]}"),
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
        if not self._harvest_root: return
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        lp = self._harvest_root / f"execution_log_{ts}.txt"
        lp.write_text("".join(self._log_lines), encoding="utf-8")
        import subprocess, platform
        s = platform.system()
        if s == "Darwin":  subprocess.Popen(["open", str(lp)])
        elif s == "Windows": subprocess.Popen(["notepad", str(lp)])

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    ExecutionApp().run()
