#!/usr/bin/env python3
"""
file_harvester.py  –  Script 3 des file-sync-toolchain
========================================================
Version 2.1  –  Gesamtübersicht, CONFLICT-Fix, Zähler-Gesamt

Änderungen v2.1:
  - Gesamtübersicht-Tab nach Harvest (Dateien + MB/GB pro Kategorie)
  - CONFLICT modified: beide Versionen getrennt als pending_mac / pending_win
    verfolgt → Lauf auf Mac holt Mac-Version, Lauf auf Win Win-Version
  - Zähler-Kacheln zeigen Gesamt-Stand aus Report (nicht nur aktuellen Lauf)
  - DELETE-Meldung klarer formuliert
  - Merge-Bug behoben: neueste Einträge überschreiben immer alte
"""

import json
import os
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

# Status-Werte im Report
# CONFLICT modified wird pro Slot getrennt verfolgt:
#   "ok_mac" / "ok_win"     → diese Seite erfolgreich eingesammelt
#   "pending_mac"           → Mac-Version fehlt noch
#   "pending_win"           → Win-Version fehlt noch
#   "ok"                    → beide Seiten vorhanden (vollständig)


# ==============================================================================
# HASHING & KOPIEREN
# ==============================================================================

def longpath(p: Path) -> Path:
    """Windows: Langpfad-Praefix fuer Pfade > 260 Zeichen."""
    import platform
    if platform.system() == "Windows" and not str(p).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(p.resolve()))
    return p


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    lp = longpath(path)
    with open(str(lp), "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def copy_verified(src: Path, dst: Path) -> tuple[bool, str]:
    # Symlink-Quellen explizit auflösen, bevor kopiert wird.
    # Grund: shutil.copy2() nutzt auf macOS intern die native copyfile()-
    # Syscall-Optimierung, die Symlinks am Ziel reproduzieren kann statt
    # sie aufzulösen — selbst mit follow_symlinks=True (Python-Default).
    # Das tritt z.B. bei Apple Photos/iPhoto-Mediatheken auf, die interne
    # Datenbank-Dateien zwischen migrierten Bibliotheksversionen verlinken.
    try:
        if src.is_symlink() or os.path.islink(str(src)):
            resolved = os.path.realpath(str(src))
            if os.path.isfile(resolved):
                src = Path(resolved)
    except Exception:
        pass

    src_lp = longpath(src)
    dst_lp = longpath(dst)
    dst_lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(src_lp), str(dst_lp))
        # Falls trotzdem ein Symlink am Ziel entstand: erneut auflösen
        # und tatsächlichen Inhalt nachträglich an dieselbe Stelle schreiben.
        if dst_lp.is_symlink() or os.path.islink(str(dst_lp)):
            real_target = os.path.realpath(str(dst_lp))
            if os.path.isfile(real_target) and real_target != str(dst_lp):
                content = Path(real_target).read_bytes()
                dst_lp.unlink()
                dst_lp.write_bytes(content)
        h_src = sha256(src_lp)
        h_dst = sha256(dst_lp)
        if h_src != h_dst:
            dst_lp.unlink(missing_ok=True)
            return False, "Hash-Mismatch nach Kopieren"
        return True, h_src
    except Exception as exc:
        return False, str(exc)


def dir_stats(path: Path) -> tuple[int, int]:
    """Gibt (Anzahl Dateien, Bytes) eines Verzeichnisses zurück."""
    count = 0
    total = 0
    if not path.is_dir():
        return 0, 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                pass
    return count, total


def fmt_size(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.1f} GB"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.1f} MB"
    return f"{b / (1 << 10):.1f} KB"


# ==============================================================================
# ROOT-AUFLÖSUNG
# ==============================================================================

def find_file_in_roots(roots: list[str], rel_path: str) -> Path | None:
    for root in roots:
        r = Path(root)
        if not r.is_dir():
            continue
        for candidate in [r / rel_path, r / rel_path.replace("/", "\\")]:
            lp = longpath(candidate)
            if lp.is_file():
                return lp
    return None


def roots_reachable(roots: list[str]) -> bool:
    return any(Path(r).is_dir() for r in roots)


# ==============================================================================
# HARVESTER-KERN
# ==============================================================================

class Harvester:

    def __init__(
        self,
        directive:         dict,
        directive_path:    Path,
        harvest_root:      Path,
        on_progress:       callable,
        on_counter:        callable,
        on_error:          callable,
        on_log:            callable,
        on_done:           callable,
        cancel_event:      threading.Event,
        usb_root_override: Path | None = None,
    ):
        self.directive         = directive
        self.directive_path    = directive_path
        self.harvest_root      = harvest_root
        self.on_progress       = on_progress
        self.on_counter        = on_counter
        self.on_error          = on_error
        self.on_log            = on_log
        self.on_done           = on_done
        self.cancel_event      = cancel_event
        self.usb_root_override = usb_root_override

        self.today = datetime.now().strftime("%Y-%m-%d")
        self.report_entries: list[dict] = []

    def _staging(self, slot: str, rel: str) -> Path:
        return self.harvest_root / "staging" / slot / rel

    def _conflict(self, rel: str, slot: str) -> Path:
        return self.harvest_root / "conflicts" / rel / f"_{slot}"

    def _deleted(self, sub: str, rel: str) -> Path:
        return self.harvest_root / "_GELOESCHT" / self.today / sub / rel

    def _do_copy(self, src: Path, dst: Path, rel: str, action: str) -> bool:
        ok, info = copy_verified(src, dst)
        status = "ok" if ok else "error"
        self.on_log(f"{'OK  ' if ok else 'ERR '} {action:<26s} {rel}")
        self.report_entries.append({
            "rel_path": rel, "action": action, "status": status,
            "src": str(src), "dst": str(dst),
            **({"sha256": info} if ok else {"error": info}),
        })
        if not ok:
            self.on_error(rel, info)
        return ok

    def run(self):
        sources = self.directive.get("sources", {})

        # USB-Root-Override: lokalen Pfad als Ersatz für Directive-Roots
        if self.usb_root_override:
            usb_src = sources.get("usb", {})
            old_roots = usb_src.get("roots", [])
            new_roots = []
            for old in old_roots:
                candidate = self.usb_root_override / Path(old).name
                new_roots.append(str(candidate if candidate.is_dir()
                                     else self.usb_root_override))
            usb_src["roots"] = new_roots or [str(self.usb_root_override)]
            sources["usb"] = usb_src
            self.on_log("USB-Root-Override:")
            for r in usb_src["roots"]:
                self.on_log(f"  {r}")

        entries = [e for e in self.directive.get("entries", [])
                   if e["action"] in HARVEST_ACTIONS]

        # Bestehenden Report laden – nötig um pro-Slot-Status (mac/win)
        # für CONFLICT-Einträge nachzuschlagen. Die Directive selbst
        # trägt nie einen "status" — der lebt ausschließlich im Report.
        prior_status = self._load_prior_status()

        done_keys = self._load_done_keys()
        pending   = [e for e in entries if e["rel_path"] not in done_keys]

        total = len(pending)
        self.on_log(f"Harvest gestartet – {total} ausstehende Einträge")
        self.on_log(f"  (von {len(entries)} gesamt, "
                    f"{len(done_keys)} bereits erledigt)")
        self.on_log(f"Harvest-Verzeichnis: {self.harvest_root}")

        for i, entry in enumerate(pending):
            if self.cancel_event.is_set():
                self.on_log("⏹ Abgebrochen.")
                break

            rel  = entry["rel_path"]
            act  = entry["action"]
            pres = entry.get("presence", {})

            self.on_progress(i + 1, total, rel, act)

            # ----------------------------------------------------------
            # CONFLICT modified / new – je Slot einzeln verfolgen
            # ----------------------------------------------------------
            if act in (A_CONF_MOD, A_CONF_NEW):
                prior = prior_status.get(rel, {})
                mac_done = prior.get("status") in ("ok_mac", "ok")
                win_done = prior.get("status") in ("ok_win", "ok")

                result = {"rel_path": rel, "action": act,
                          "presence": pres, "slots": {}}

                for slot, already_done in (("mac", mac_done),
                                           ("win", win_done)):
                    if not pres.get(slot):
                        continue
                    if already_done:
                        continue
                    roots = sources.get(slot, {}).get("roots", [])
                    if not roots_reachable(roots):
                        result["slots"][slot] = "pending"
                        continue
                    src = find_file_in_roots(roots, rel)
                    if src is None:
                        self.on_error(rel, f"{act}: {slot}-Version nicht gefunden")
                        result["slots"][slot] = "error"
                        continue
                    dst = self._conflict(rel, slot)
                    ok, info = copy_verified(src, dst)
                    if ok:
                        result["slots"][slot] = "ok"
                        self.on_log(f"OK   {act} [{slot}]  {rel}")
                    else:
                        self.on_error(rel, info)
                        result["slots"][slot] = "error"

                # Gesamt-Status bestimmen
                mac_st = result["slots"].get("mac",
                         "ok" if not pres.get("mac") else "n/a")
                win_st = result["slots"].get("win",
                         "ok" if not pres.get("win") else "n/a")

                if "pending" in (mac_st, win_st):
                    result["status"] = ("ok_mac" if mac_st == "ok"
                                        else "ok_win" if win_st == "ok"
                                        else "pending")
                elif "error" in (mac_st, win_st):
                    result["status"] = "error"
                else:
                    result["status"] = "ok"

                self.report_entries.append(result)
                if result["status"] == "ok":
                    self.on_counter("conflict_mod", 1)
                elif result["status"] in ("ok_mac", "ok_win"):
                    self.on_counter("conflict_partial", 1)
                continue

            # ----------------------------------------------------------
            # DELETE – Script 4 entscheidet
            # ----------------------------------------------------------
            if act == A_DELETE:
                self.on_log(f"SKIP DELETE  {rel}  → Script 4 entscheidet")
                self.report_entries.append({
                    "rel_path": rel, "action": act,
                    "status": "pending_delete",
                    "usb_roots": sources.get("usb", {}).get("roots", []),
                })
                self.on_counter("delete_pending", 1)
                continue

            # ----------------------------------------------------------
            # Alle anderen Aktionen
            # ----------------------------------------------------------
            src = None

            if act == A_COPY_MAC:
                roots = sources.get("mac", {}).get("roots", [])
                if not roots_reachable(roots):
                    self._skip(rel, act, "mac"); continue
                src = find_file_in_roots(roots, rel)
                dst = self._staging("mac", rel)

            elif act == A_COPY_WIN:
                roots = sources.get("win", {}).get("roots", [])
                if not roots_reachable(roots):
                    self._skip(rel, act, "win"); continue
                src = find_file_in_roots(roots, rel)
                dst = self._staging("win", rel)

            elif act == A_COPY:
                for slot in ("mac", "win"):
                    roots = sources.get(slot, {}).get("roots", [])
                    if roots_reachable(roots):
                        src = find_file_in_roots(roots, rel)
                        if src:
                            break
                dst = self._staging("both", rel)
                if src is None:
                    self._skip(rel, act, "mac+win"); continue

            elif act in (A_CONF_DEL_WIN, A_CONF_DEL_MAC):
                roots = sources.get("usb", {}).get("roots", [])
                if not roots_reachable(roots):
                    self._skip(rel, act, "usb"); continue
                src = find_file_in_roots(roots, rel)
                sub = "del_win" if act == A_CONF_DEL_WIN else "del_mac"
                dst = self._deleted(sub, rel)

            if src is None:
                self.on_error(rel, f"{act}: Quelldatei nicht gefunden")
                self.on_counter("not_found", 1)
                continue

            if self._do_copy(src, dst, rel, act):
                key = {A_COPY_MAC: "copy_mac", A_COPY_WIN: "copy_win",
                       A_COPY: "copy_both", A_CONF_DEL_WIN: "conflict_del",
                       A_CONF_DEL_MAC: "conflict_del"}.get(act, "ok")
                self.on_counter(key, 1)

        self._save_report()

    def _skip(self, rel: str, act: str, slot: str):
        self.on_log(f"PEND [{slot} nicht erreichbar]  {rel}")
        self.report_entries.append({
            "rel_path": rel, "action": act,
            "status": "pending", "reason": f"{slot} nicht erreichbar",
        })
        self.on_counter("pending", 1)

    def _load_prior_status(self) -> dict[str, dict]:
        """
        Liest den bestehenden harvest_report und gibt eine Map
        rel_path → Report-Eintrag zurück. Wird gebraucht weil die
        Directive selbst NIE ein 'status'-Feld trägt (das lebt nur
        im Report) — frühere Versionen lasen fälschlich vom
        Directive-Eintrag, was 'status' immer None ergab.
        """
        report_path = self.harvest_root / "harvest_report.json"
        if not report_path.is_file():
            return {}
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            return {e["rel_path"]: e for e in data.get("entries", [])}
        except Exception:
            return {}

    def _load_done_keys(self) -> set[str]:
        """
        Überspringt: ok, ok_mac, ok_win, pending_delete.
        Wiederholt:  pending, error (nochmal versuchen).
        CONFLICT partial (ok_mac/ok_win) bekommt Slot-Status mit.
        """
        report_path = self.harvest_root / "harvest_report.json"
        if not report_path.is_file():
            return set()
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            skip = set()
            for e in data.get("entries", []):
                st = e.get("status", "")
                if st in ("ok", "pending_delete"):
                    skip.add(e["rel_path"])
                # ok_mac / ok_win: noch pending für andere Seite
                # → NICHT überspringen, Harvester versucht fehlende Seite
            return skip
        except Exception:
            return set()

    def _save_report(self):
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        report_path = self.harvest_root / "harvest_report.json"

        existing = []
        if report_path.is_file():
            try:
                existing = json.loads(
                    report_path.read_text(encoding="utf-8")
                ).get("entries", [])
            except Exception:
                pass

        # Merge: neue Einträge überschreiben alte per rel_path
        merged_map = {e["rel_path"]: e for e in existing}
        for e in self.report_entries:
            merged_map[e["rel_path"]] = e
        merged = list(merged_map.values())

        from collections import Counter
        st = Counter(e.get("status", "?") for e in merged)

        ok_total      = st["ok"]
        pending_total = st["pending"] + st["ok_mac"] + st["ok_win"]
        error_total   = st["error"]
        del_total     = st["pending_delete"]
        complete      = (pending_total == 0 and error_total == 0)

        report = {
            "meta": {
                "created":      datetime.now().isoformat(),
                "tool":         "file_harvester.py",
                "version":      "2.1",
                "directive":    str(self.directive_path),
                "harvest_root": str(self.harvest_root),
                "complete":     complete,
                "last_run":     ts,
            },
            "summary": {
                "ok":             ok_total,
                "pending":        pending_total,
                "error":          error_total,
                "pending_delete": del_total,
                "complete":       complete,
            },
            "entries": merged,
        }

        self.harvest_root.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        self.on_done(report, report_path,
                     self.harvest_root / f"harvest_log_{ts}.txt")


# ==============================================================================
# GUI
# ==============================================================================

class HarvesterApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🌾 File Harvester v2.1")
        self.root.geometry("900x800")
        self.root.resizable(True, True)

        self._directive:      dict | None = None
        self._directive_path: Path | None = None
        self._harvest_root:   Path | None = None
        self._usb_root:       Path | None = None
        self.cancel_event = threading.Event()
        self._log_lines:  list[str] = []
        self._errors:     list[str] = []
        self._last_report: dict | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg="#1a5276", height=56)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="🌾 File Harvester – Dateien einsammeln",
                 font=("Helvetica", 14, "bold"),
                 bg="#1a5276", fg="white").pack(pady=14)

        # Schritt 1 – USB
        usb_frame = tk.LabelFrame(self.root,
                                  text="Schritt 1 – USB-Laufwerk wählen",
                                  padx=10, pady=6)
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

        # Schritt 2 – Konfiguration
        info_frame = tk.LabelFrame(self.root,
                                   text="Schritt 2 – Erkannte Konfiguration",
                                   padx=10, pady=4)
        info_frame.pack(fill=tk.X, padx=10, pady=4)

        self.directive_display = tk.StringVar(value="–")
        self.harvest_display   = tk.StringVar(value="–")
        self.run_label_var     = tk.StringVar(value="–")

        for label, var in [("Directive:",      self.directive_display),
                            ("Harvest-Ordner:", self.harvest_display),
                            ("Lauf-Status:",    self.run_label_var)]:
            row = tk.Frame(info_frame)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, width=16, anchor="w",
                     font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
            tk.Label(row, textvariable=var,
                     font=("Courier", 8), anchor="w",
                     fg="#333").pack(side=tk.LEFT, fill=tk.X)

        # Fortschritt
        cnt = tk.LabelFrame(self.root, text="Fortschritt", padx=10, pady=6)
        cnt.pack(fill=tk.X, padx=10, pady=4)

        self.progress_label = tk.Label(cnt, text="Bereit.",
                                       anchor="w", font=("Helvetica", 10))
        self.progress_label.pack(fill=tk.X)
        self.file_label = tk.Label(cnt, text="", anchor="w",
                                   font=("Courier", 8), fg="#555")
        self.file_label.pack(fill=tk.X)
        self.progress_bar = ttk.Progressbar(cnt, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(4, 6))

        # Zähler-Kacheln (zeigen Gesamt aus Report)
        grid = tk.Frame(cnt)
        grid.pack(fill=tk.X)
        self._counter_vars = {}
        counter_defs = [
            ("copy_mac",         "✅ COPY MAC",        "#2980b9"),
            ("copy_win",         "✅ COPY WIN",        "#2980b9"),
            ("copy_both",        "✅ COPY (beide)",    "#2980b9"),
            ("conflict_mod",     "📋 CONFLICT mod",   "#e67e22"),
            ("conflict_partial", "📋 CONFLICT part",  "#e67e22"),
            ("conflict_del",     "⚠️  CONFLICT del",   "#e67e22"),
            ("delete_pending",   "🗑  DELETE (USB)",   "#7f8c8d"),
            ("pending",          "⏳ Ausstehend",      "#7f8c8d"),
            ("not_found",        "❌ Nicht gefunden",  "#c0392b"),
        ]
        for col, (key, label, color) in enumerate(counter_defs):
            var = tk.StringVar(value="0")
            self._counter_vars[key] = var
            cell = tk.Frame(grid, relief=tk.GROOVE, bd=1, padx=3, pady=3)
            cell.grid(row=0, column=col, padx=2, sticky="ew")
            grid.columnconfigure(col, weight=1)
            tk.Label(cell, text=label, font=("Helvetica", 7),
                     fg=color, wraplength=78, justify="center").pack()
            tk.Label(cell, textvariable=var,
                     font=("Helvetica", 11, "bold")).pack()
        self._counters = {k: 0 for k in self._counter_vars}

        # Notebook: Log + Gesamtübersicht
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # Tab 1: Log
        log_frame = tk.Frame(self.nb)
        self.nb.add(log_frame, text="Log")
        self.log_text = tk.Text(log_frame, font=("Courier", 8),
                                state=tk.DISABLED,
                                bg="#1e1e1e", fg="#d4d4d4")
        lsb = ttk.Scrollbar(log_frame, orient="vertical",
                             command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Fehler-Listbox im Log-Tab (versteckt bis Fehler)
        self.error_frame = tk.LabelFrame(log_frame,
                                         text="❌ Fehler / Nicht gefunden",
                                         padx=6, pady=4)
        self.error_listbox = tk.Listbox(self.error_frame,
                                        font=("Courier", 7),
                                        height=4, bg="#fff0f0")
        esb = ttk.Scrollbar(self.error_frame, orient="vertical",
                             command=self.error_listbox.yview)
        self.error_listbox.configure(yscrollcommand=esb.set)
        esb.pack(side=tk.RIGHT, fill=tk.Y)
        self.error_listbox.pack(fill=tk.BOTH, expand=True)

        # Tab 2: Gesamtübersicht (wird nach Harvest befüllt)
        self.overview_frame = tk.Frame(self.nb)
        self.nb.add(self.overview_frame, text="Gesamtübersicht")
        self.overview_text = tk.Text(self.overview_frame,
                                     font=("Courier", 9),
                                     state=tk.DISABLED)
        osb = ttk.Scrollbar(self.overview_frame, orient="vertical",
                             command=self.overview_text.yview)
        self.overview_text.configure(yscrollcommand=osb.set)
        osb.pack(side=tk.RIGHT, fill=tk.Y)
        self.overview_text.pack(fill=tk.BOTH, expand=True)

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
            state=tk.DISABLED, height=2, width=14)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)

        self.log_btn = tk.Button(
            btn_frame, text="📋 Log öffnen",
            command=self._open_log,
            font=("Helvetica", 12),
            state=tk.DISABLED, height=2, width=14)
        self.log_btn.pack(side=tk.LEFT, padx=5)

        self.overview_btn = tk.Button(
            btn_frame, text="📊 Übersicht",
            command=lambda: self.nb.select(self.overview_frame),
            font=("Helvetica", 12),
            state=tk.DISABLED, height=2, width=14)
        self.overview_btn.pack(side=tk.LEFT, padx=5)

        self._log_path = None

    # ------------------------------------------------------------------
    # USB wählen
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
                f"Kein _sync-Ordner gefunden:\n{self._usb_root}\n\n"
                f"Bitte zuerst Script 1 + 2 ausführen.")
            return

        dated = sorted([d for d in sync.iterdir() if d.is_dir()],
                       reverse=True)
        if not dated:
            messagebox.showwarning("Kein Datum-Ordner",
                                   f"_sync enthält keine Unterordner:\n{sync}")
            return

        sync_dir = dated[0]
        directives = sorted(sync_dir.glob("change_directive_*.json"),
                            reverse=True)
        directives = [d for d in directives if "_harvested" not in d.name]

        if not directives:
            messagebox.showwarning(
                "Keine Directive",
                f"Keine change_directive_*.json in:\n{sync_dir}\n\n"
                f"Bitte Script 2 ausführen.")
            return

        directive_path = directives[0]
        try:
            directive = json.loads(
                directive_path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Lesefehler", str(exc))
            return

        self._directive      = directive
        self._directive_path = directive_path
        self._harvest_root   = sync_dir / "harvest"

        # Lauf-Status
        report_path = self._harvest_root / "harvest_report.json"
        if report_path.is_file():
            try:
                rep      = json.loads(report_path.read_text(encoding="utf-8"))
                ok       = rep["summary"].get("ok", 0)
                pending  = rep["summary"].get("pending", 0)
                complete = rep["summary"].get("complete", False)
                if complete:
                    run_str = f"✅ Harvest vollständig – {ok:,} OK"
                else:
                    run_str = (f"⏳ Lauf wird fortgesetzt – "
                               f"{ok:,} erledigt, {pending:,} ausstehend")
                self._last_report = rep
            except Exception:
                run_str = "Report nicht lesbar – Lauf 1"
        else:
            n = sum(1 for e in directive.get("entries", [])
                    if e["action"] in HARVEST_ACTIONS)
            run_str = f"Lauf 1 – {n:,} Einträge zu verarbeiten"

        self.usb_display_var.set(
            f"✔  {self._usb_root}  →  _sync/{sync_dir.name}/")
        self.directive_display.set(directive_path.name)
        self.harvest_display.set(str(self._harvest_root))
        self.run_label_var.set(run_str)
        self.start_btn.config(state=tk.NORMAL)

        self._append_log(f"USB: {self._usb_root}")
        self._append_log(f"Directive: {directive_path.name}")
        self._append_log(f"Status: {run_str}")

        # Wenn bereits complete: Übersicht sofort anzeigen
        if self._last_report and self._last_report["summary"].get("complete"):
            self._build_overview(self._last_report)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _cb_progress(self, current, total, filename, action):
        pct = int(current / total * 100) if total else 0
        self.root.after(0, lambda: [
            self.progress_label.config(
                text=f"Verarbeite {current:,} / {total:,}  [{pct}%]"),
            self.file_label.config(
                text=f"  {action:<26s}  {filename[-65:]}"),
            self.progress_bar.config(value=pct),
        ])

    def _cb_counter(self, name, delta):
        self._counters[name] = self._counters.get(name, 0) + delta
        val = str(self._counters[name])
        self.root.after(0, lambda v=val, n=name:
                        self._counter_vars[n].set(v))

    def _cb_error(self, rel, reason):
        msg = f"{rel}  →  {reason}"
        self._errors.append(msg)

        def _upd():
            if not self.error_frame.winfo_ismapped():
                self.error_frame.pack(fill=tk.X, padx=4, pady=2,
                                      before=self.log_text)
            self.error_listbox.insert(tk.END, msg)
            self.error_listbox.see(tk.END)

        self.root.after(0, _upd)
        self._append_log(f"ERR  {msg}")

    def _cb_log(self, msg: str):
        self._log_lines.append(msg)
        self._append_log(msg)

    def _cb_done(self, report, report_path, log_path):
        s        = report["summary"]
        complete = s.get("complete", False)
        self._last_report = report

        # Log speichern
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        lp = self._harvest_root / f"harvest_log_{ts}.txt"
        try:
            lp.write_text("\n".join(self._log_lines), encoding="utf-8")
        except Exception:
            pass
        self._log_path = lp

        def _upd():
            self.start_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            self.log_btn.config(state=tk.NORMAL)
            self.overview_btn.config(state=tk.NORMAL)
            self.progress_bar.config(value=100)

            # Zähler aus Report aktualisieren (Gesamt-Stand)
            self._update_counters_from_report(report)

            # Übersicht aufbauen
            self._build_overview(report)

            if complete:
                msg = (f"✅ HARVEST COMPLETE – {s['ok']:,} OK  |  "
                       f"{s['pending_delete']:,} Dateien auf USB "
                       f"warten auf Lösch-Bestätigung (Script 4)")
                self.progress_label.config(text=msg, fg="#27ae60")
                self.status_var.set("✅ Harvest abgeschlossen")
                messagebox.showinfo(
                    "Harvest abgeschlossen",
                    f"Alle Einträge eingesammelt!\n\n"
                    f"  OK (kopiert):          {s['ok']:,}\n"
                    f"  Auf USB-Löschung wart.: {s['pending_delete']:,}\n\n"
                    f"Die {s['pending_delete']:,} Dateien liegen noch auf USB\n"
                    f"und werden erst in Script 4 nach Bestätigung entfernt.\n\n"
                    f"→ Gesamtübersicht-Tab für Details.\n"
                    f"→ Bereit für Script 4 (change_execution.py)."
                )
                try:
                    new_name = self._directive_path.stem + "_harvested.json"
                    self._directive_path.rename(
                        self._directive_path.parent / new_name)
                    self._append_log(f"Directive → {new_name}")
                except Exception as exc:
                    self._append_log(f"Umbenennen: {exc}")
            else:
                pending  = s.get("pending", 0)
                errors   = s.get("error", 0)
                msg = (f"⏳ Lauf abgeschlossen – {s['ok']:,} OK  |  "
                       f"{pending:,} ausstehend  |  {errors:,} Fehler")
                self.progress_label.config(text=msg, fg="#e67e22")
                self.status_var.set(msg)
                hint = ("Bitte USB an den anderen Rechner anschließen\n"
                        "und Harvester dort erneut starten.")
                messagebox.showinfo("Lauf abgeschlossen",
                    f"Lauf abgeschlossen.\n\n"
                    f"  Erledigt:    {s['ok']:,}\n"
                    f"  Ausstehend:  {pending:,}\n"
                    f"  Fehler:      {errors:,}\n\n"
                    f"{hint}")

        self.root.after(0, _upd)

    def _update_counters_from_report(self, report: dict):
        """Setzt Zähler-Kacheln auf Gesamt-Stand aus Report."""
        from collections import Counter
        entries = report.get("entries", [])
        actions = Counter(e.get("action") for e in entries
                          if e.get("status") == "ok")
        mapping = {
            A_COPY_MAC:     "copy_mac",
            A_COPY_WIN:     "copy_win",
            A_COPY:         "copy_both",
            A_CONF_MOD:     "conflict_mod",
            A_CONF_DEL_WIN: "conflict_del",
            A_CONF_DEL_MAC: "conflict_del",
        }
        for action, key in mapping.items():
            self._counters[key] = (self._counters.get(key, 0)
                                   + actions.get(action, 0))
        self._counters["delete_pending"] = sum(
            1 for e in entries if e.get("status") == "pending_delete")
        self._counters["pending"] = report["summary"].get("pending", 0)
        for key, var in self._counter_vars.items():
            var.set(str(self._counters.get(key, 0)))

    def _build_overview(self, report: dict):
        """Baut den Gesamtübersicht-Tab mit Dateianzahl und Größen."""
        if not self._harvest_root:
            return

        lines = []
        lines.append("=" * 62)
        lines.append("  HARVEST GESAMTÜBERSICHT")
        lines.append(f"  Stand: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("=" * 62)
        lines.append("")

        # Report-Zusammenfassung
        s = report["summary"]
        lines.append(f"  Eingesammelt (OK):          {s['ok']:>7,}")
        lines.append(f"  Ausstehend:                 {s.get('pending',0):>7,}")
        lines.append(f"  Fehler:                     {s.get('error',0):>7,}")
        lines.append(f"  Auf USB-Löschbestätigung:   {s['pending_delete']:>7,}")
        lines.append(f"  Harvest vollständig:        "
                     f"{'Ja ✅' if s.get('complete') else 'Nein ⏳'}")
        lines.append("")
        lines.append("-" * 62)
        lines.append("  ORDNER AUF USB (tatsächliche Dateien + Größen)")
        lines.append("-" * 62)

        # Ordner-Statistiken berechnen (in Thread würde besser sein,
        # aber Übersicht wird nur einmal aufgebaut)
        categories = [
            ("staging/mac",             "COPY MAC    → auf Win + USB verteilen"),
            ("staging/win",             "COPY WIN    → auf Mac + USB verteilen"),
            ("staging/both",            "COPY beide  → auf USB verteilen"),
            ("conflicts",               "CONFLICT modified  → Script 4 entscheidet"),
            ("_GELOESCHT",              "Gesicherte Löschungen (Sicherheitskopie)"),
            ("_GELOESCHT/2026-06-16/del_win",
             "  del_win → Win hat gelöscht (Mac unverändert)"),
            ("_GELOESCHT/2026-06-16/del_mac",
             "  del_mac → Mac hat gelöscht (Win unverändert)"),
        ]

        for rel, desc in categories:
            folder = self._harvest_root / rel
            if folder.is_dir():
                # Schnelle Schätzung über os.scandir statt rglob
                count, total_b = 0, 0
                try:
                    for dirpath, _, filenames in os.walk(str(folder)):
                        for fn in filenames:
                            try:
                                fp = os.path.join(dirpath, fn)
                                total_b += os.path.getsize(fp)
                                count += 1
                            except OSError:
                                pass
                except Exception:
                    pass
                lines.append(
                    f"  {desc}")
                lines.append(
                    f"  {'':>4}{count:>7,} Dateien   {fmt_size(total_b):>10}")
            else:
                lines.append(f"  {desc}")
                lines.append(f"  {'':>4}(leer / nicht vorhanden)")
            lines.append("")

        lines.append("-" * 62)
        lines.append("  NÄCHSTE SCHRITTE")
        lines.append("-" * 62)
        lines.append("  1. Script 4 (change_execution.py) starten")
        lines.append("  2. COPY-Dateien werden automatisch verteilt")
        lines.append("  3. Löschungen bestätigen oder rückgängig machen")
        lines.append("  4. CONFLICT modified: Versionen vergleichen + entscheiden")
        lines.append("")

        self.overview_text.config(state=tk.NORMAL)
        self.overview_text.delete("1.0", tk.END)
        self.overview_text.insert(tk.END, "\n".join(lines))
        self.overview_text.config(state=tk.DISABLED)
        self.nb.select(self.overview_frame)

    def _append_log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
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
            messagebox.showerror("Fehler",
                                 "Bitte zuerst USB-Laufwerk wählen.")
            return

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
        self.progress_label.config(fg="black")

        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.log_btn.config(state=tk.DISABLED)
        self.overview_btn.config(state=tk.DISABLED)
        self.status_var.set("⏳ Harvest läuft …")

        harvester = Harvester(
            directive         = self._directive,
            directive_path    = self._directive_path,
            harvest_root      = self._harvest_root,
            on_progress       = self._cb_progress,
            on_counter        = self._cb_counter,
            on_error          = self._cb_error,
            on_log            = self._cb_log,
            on_done           = self._cb_done,
            cancel_event      = self.cancel_event,
            usb_root_override = self._usb_root,
        )
        threading.Thread(target=harvester.run, daemon=True).start()

    def _cancel(self):
        self.cancel_event.set()
        self.status_var.set("⏹ Abbrechen …")
        self.cancel_btn.config(state=tk.DISABLED)

    def _open_log(self):
        if self._log_path and self._log_path.is_file():
            import subprocess, platform
            s = platform.system()
            if s == "Darwin":
                subprocess.Popen(["open", str(self._log_path)])
            elif s == "Windows":
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
