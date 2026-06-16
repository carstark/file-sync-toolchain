#!/usr/bin/env python3
"""
change_execution.py  –  Script 4 des file-sync-toolchain
==========================================================
Version 1.1  –  Zwei-Lauf-fähig, Entscheidungen persistent

Ablauf:
  Lauf 1  (z.B. auf Win + USB)
    → Entscheidungen für alle Kategorien einholen
    → Ausführen was erreichbar ist (Win + USB)
    → execution_report.json auf USB speichern
    → Meldet was noch aussteht (Mac-Seite)

  Lauf 2  (Mac + USB)
    → Entscheidungen aus Lauf 1 laden (schreibgeschützt)
    → Nur noch offene Einträge ausführen
    → Complete melden

Entscheidungen sind nach Lauf 1 nicht mehr änderbar.
Fehler beim Löschen: Dateien manuell aus harvest/_GELOESCHT/ holen.
"""

import json
import os
import shutil
import hashlib
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from datetime import datetime
from collections import defaultdict


# ==============================================================================
# HILFSFUNKTIONEN
# ==============================================================================

def longpath(p: Path) -> Path:
    import platform
    if platform.system() == "Windows" and not str(p).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(p.resolve()))
    return p


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(longpath(path)), "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def copy_verified(src: Path, dst: Path) -> tuple[bool, str]:
    src_lp = longpath(src)
    dst_lp = longpath(dst)
    dst_lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(src_lp), str(dst_lp))
        if sha256(src_lp) != sha256(dst_lp):
            dst_lp.unlink(missing_ok=True)
            return False, "Hash-Mismatch"
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def delete_file(path: Path) -> tuple[bool, str]:
    lp = longpath(path)
    try:
        lp.unlink(missing_ok=True)
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def fmt_size(b: int) -> str:
    if b >= 1 << 30: return f"{b/(1<<30):.1f} GB"
    if b >= 1 << 20: return f"{b/(1<<20):.1f} MB"
    return f"{b/(1<<10):.1f} KB"


def top_folder(rel_path: str, depth: int = 2) -> str:
    parts = Path(rel_path).parts
    return str(Path(*parts[:min(depth, len(parts))]))


def find_in_roots(roots: list[str], rel_path: str) -> Path | None:
    for root in roots:
        r = Path(root)
        if not r.is_dir():
            continue
        for cand in [r / rel_path, r / rel_path.replace("/", "\\")]:
            lp = longpath(cand)
            if lp.is_file():
                return lp
    return None


def roots_reachable(roots: list[str]) -> bool:
    return any(Path(r).is_dir() for r in roots)


# ==============================================================================
# EXECUTION REPORT
# ==============================================================================

class ExecReport:
    """Zustandsspeicher zwischen Läufen."""

    def __init__(self, path: Path):
        self.path = path
        self.decisions: dict[str, str] = {}   # folder_key → "confirm"|"restore"|"delete"|"keep"|"mac"|"win"
        self.entries:   list[dict]     = []
        self.run1_done: bool           = False
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.decisions  = data.get("decisions", {})
            self.entries    = data.get("entries", [])
            self.run1_done  = data.get("meta", {}).get("run1_done", False)
        except Exception:
            pass

    def save(self, run1_done: bool = False):
        done    = sum(1 for e in self.entries if e.get("status") == "ok")
        pending = sum(1 for e in self.entries if e.get("status") == "pending")
        errors  = sum(1 for e in self.entries if e.get("status") == "error")
        complete = (pending == 0 and errors == 0 and len(self.entries) > 0)

        data = {
            "meta": {
                "created":   datetime.now().isoformat(),
                "tool":      "change_execution.py",
                "version":   "1.1",
                "run1_done": run1_done or self.run1_done,
                "complete":  complete,
            },
            "summary": {
                "done":     done,
                "pending":  pending,
                "error":    errors,
                "complete": complete,
            },
            "decisions": self.decisions,
            "entries":   self.entries,
        }
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        return complete

    def done_keys(self) -> set[str]:
        return {e["rel_path"] + "::" + e.get("sub", "")
                for e in self.entries if e.get("status") == "ok"}

    def upsert(self, rel_path: str, sub: str, status: str,
               action: str, decision: str, **kwargs):
        key = rel_path + "::" + sub
        for e in self.entries:
            if e.get("rel_path") == rel_path and e.get("sub") == sub:
                e["status"]   = status
                e["decision"] = decision
                e.update(kwargs)
                return
        self.entries.append({
            "rel_path": rel_path, "sub": sub,
            "action": action, "decision": decision,
            "status": status, **kwargs
        })


# ==============================================================================
# HAUPT-APP
# ==============================================================================

class ExecutionApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("⚡ Change Execution v1.1")
        self.root.geometry("1020x820")
        self.root.resizable(True, True)

        self._report:      dict | None      = None
        self._exec_report: ExecReport | None = None
        self._harvest_root: Path | None     = None
        self._usb_root:     Path | None     = None
        self._exec_log:     list[str]       = []

        # Entscheidungen (Lauf 1: vom User, Lauf 2: aus Report geladen)
        self._decisions:       dict[str, str] = {}  # del/restore
        self._delete_keep:     dict[str, bool] = {} # DELETE-Einträge
        self._conflict_winner: dict[str, str]  = {} # mac|win

        # Einträge aus harvest_report
        self._copy_entries:   list[dict] = []
        self._del_entries:    list[dict] = []
        self._delete_entries: list[dict] = []
        self._conflict_entries: list[dict] = []

        # Treeview-Mapping
        self._del_item_map: dict[str, str] = {}  # iid → folder_key

        # Root-Caches
        self._win_roots: list[str] = []
        self._mac_roots: list[str] = []

        self._locked = False  # True nach Lauf 1 → Entscheidungen schreibgeschützt

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        hdr = tk.Frame(self.root, bg="#922b21", height=54)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text="⚡ Change Execution – Änderungen ausführen",
                 font=("Helvetica", 14, "bold"),
                 bg="#922b21", fg="white").pack(pady=13)

        # USB
        usb_frame = tk.LabelFrame(self.root,
                                  text="Schritt 1 – USB-Laufwerk wählen",
                                  padx=10, pady=6)
        usb_frame.pack(fill=tk.X, padx=10, pady=(8, 4))
        self.usb_var = tk.StringVar(value="(noch nicht gewählt)")
        tk.Label(usb_frame, textvariable=self.usb_var,
                 font=("Courier", 9), anchor="w",
                 fg="#c0392b").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(usb_frame, text="📂  USB-Laufwerk wählen",
                  command=self._pick_usb,
                  font=("Helvetica", 10, "bold"),
                  bg="#922b21", fg="white", width=24).pack(side=tk.RIGHT)

        # Status
        info = tk.LabelFrame(self.root, text="Status", padx=10, pady=4)
        info.pack(fill=tk.X, padx=10, pady=4)
        self.status_var = tk.StringVar(value="–")
        self.lauf_var   = tk.StringVar(value="–")
        for label, var in [("Status:", self.status_var),
                            ("Lauf:",   self.lauf_var)]:
            row = tk.Frame(info)
            row.pack(fill=tk.X)
            tk.Label(row, text=label, width=10, anchor="w",
                     font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
            tk.Label(row, textvariable=var,
                     font=("Courier", 9), anchor="w").pack(side=tk.LEFT)

        # Notebook
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        self.overview_frame  = tk.Frame(self.nb)
        self.copy_frame      = tk.Frame(self.nb)
        self.del_frame       = tk.Frame(self.nb)
        self.delete_frame    = tk.Frame(self.nb)
        self.conflict_frame  = tk.Frame(self.nb)
        self.log_frame       = tk.Frame(self.nb)

        self.nb.add(self.overview_frame, text="📊 Übersicht")
        self.nb.add(self.copy_frame,     text="📋 COPY")
        self.nb.add(self.del_frame,      text="🗑 Löschungen")
        self.nb.add(self.delete_frame,   text="🗑 DELETE")
        self.nb.add(self.conflict_frame, text="⚠️  CONFLICT")
        self.nb.add(self.log_frame,      text="📋 Log")

        self._build_overview_tab()
        self._build_copy_tab()
        self._build_del_tab()
        self._build_delete_tab()
        self._build_conflict_tab()
        self._build_log_tab()

        # Buttons
        btn = tk.Frame(self.root)
        btn.pack(fill=tk.X, padx=10, pady=6)

        self.exec_btn = tk.Button(
            btn, text="▶  Ausführen (dieser Lauf)",
            command=self._execute,
            font=("Helvetica", 12, "bold"),
            bg="#922b21", fg="white",
            state=tk.DISABLED, height=2, width=28)
        self.exec_btn.pack(side=tk.LEFT, padx=5)

        self.log_btn = tk.Button(
            btn, text="📋 Log speichern",
            command=self._save_log,
            font=("Helvetica", 11),
            state=tk.DISABLED, height=2, width=16)
        self.log_btn.pack(side=tk.LEFT, padx=5)

        self.lock_label = tk.Label(
            btn, text="",
            font=("Helvetica", 10, "italic"), fg="#922b21")
        self.lock_label.pack(side=tk.LEFT, padx=10)

    # ------------------------------------------------------------------
    # Tab-Aufbau
    # ------------------------------------------------------------------

    def _build_overview_tab(self):
        self.overview_text = tk.Text(self.overview_frame,
                                     font=("Courier", 10),
                                     state=tk.DISABLED)
        sb = ttk.Scrollbar(self.overview_frame, orient="vertical",
                            command=self.overview_text.yview)
        self.overview_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.overview_text.pack(fill=tk.BOTH, expand=True)

    def _build_copy_tab(self):
        tk.Label(self.copy_frame,
                 text="Automatisch – keine Entscheidung nötig.",
                 font=("Helvetica", 10), fg="#555").pack(
                     anchor="w", padx=10, pady=6)
        cols = ("action", "count", "destination")
        self.copy_tree = ttk.Treeview(self.copy_frame, columns=cols,
                                      show="headings", height=6)
        self.copy_tree.heading("action",      text="Aktion")
        self.copy_tree.heading("count",       text="Dateien")
        self.copy_tree.heading("destination", text="Ziel (nach Ausführung)")
        self.copy_tree.column("action",      width=160, anchor="w")
        self.copy_tree.column("count",       width=80,  anchor="center")
        self.copy_tree.column("destination", width=700, anchor="w")
        csb = ttk.Scrollbar(self.copy_frame, orient="vertical",
                             command=self.copy_tree.yview)
        self.copy_tree.configure(yscrollcommand=csb.set)
        csb.pack(side=tk.RIGHT, fill=tk.Y)
        self.copy_tree.pack(fill=tk.BOTH, expand=True, padx=10)

    def _build_del_tab(self):
        top = tk.Frame(self.del_frame)
        top.pack(fill=tk.X, padx=10, pady=6)
        tk.Label(top,
                 text="Vorauswahl: ✔ Löschen  "
                      "| Sicherheitskopie bleibt in harvest/_GELOESCHT/",
                 font=("Helvetica", 10), fg="#555").pack(side=tk.LEFT)
        self.all_confirm_btn = tk.Button(
            top, text="Alle: ✔ Löschen",
            command=lambda: self._set_all("confirm"), width=16)
        self.all_confirm_btn.pack(side=tk.RIGHT, padx=4)
        self.all_restore_btn = tk.Button(
            top, text="Alle: ↩ Wiederherstellen",
            command=lambda: self._set_all("restore"), width=22)
        self.all_restore_btn.pack(side=tk.RIGHT, padx=4)

        cols = ("decision", "files", "size", "folder")
        self.del_tree = ttk.Treeview(self.del_frame, columns=cols,
                                     show="headings", selectmode="browse")
        self.del_tree.heading("decision", text="Entscheidung")
        self.del_tree.heading("files",    text="Dateien")
        self.del_tree.heading("size",     text="Größe")
        self.del_tree.heading("folder",   text="Ordner")
        self.del_tree.column("decision", width=170, anchor="center")
        self.del_tree.column("files",    width=70,  anchor="center")
        self.del_tree.column("size",     width=90,  anchor="center")
        self.del_tree.column("folder",   width=640, anchor="w")
        dsb = ttk.Scrollbar(self.del_frame, orient="vertical",
                             command=self.del_tree.yview)
        self.del_tree.configure(yscrollcommand=dsb.set)
        dsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.del_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self.del_tree.bind("<Double-1>", self._toggle_decision)
        self.del_tree.tag_configure("confirm", background="#d5f5e3")
        self.del_tree.tag_configure("restore", background="#d6eaf8")
        self.del_tree.tag_configure("header",  background="#ecf0f1",
                                    font=("Helvetica", 9, "bold"))
        tk.Label(self.del_frame,
                 text="Doppelklick: umschalten  |  "
                      "🔒 Nach Lauf 1 nicht mehr änderbar",
                 font=("Helvetica", 8), fg="#888").pack(
                     anchor="w", padx=10, pady=2)

    def _build_delete_tab(self):
        tk.Label(self.delete_frame,
                 text="Beide Rechner haben gelöscht.  "
                      "Vorauswahl: von USB entfernen.\n"
                      "Sicherheitskopie verbleibt in harvest/.",
                 font=("Helvetica", 10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)
        cols = ("decision", "rel_path")
        self.delete_tree = ttk.Treeview(self.delete_frame, columns=cols,
                                        show="headings", height=10)
        self.delete_tree.heading("decision", text="Entscheidung")
        self.delete_tree.heading("rel_path", text="Datei")
        self.delete_tree.column("decision", width=120, anchor="center")
        self.delete_tree.column("rel_path", width=840, anchor="w")
        delsb = ttk.Scrollbar(self.delete_frame, orient="vertical",
                               command=self.delete_tree.yview)
        self.delete_tree.configure(yscrollcommand=delsb.set)
        delsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.delete_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self.delete_tree.bind("<Double-1>", self._toggle_keep)
        self.delete_tree.tag_configure("delete", background="#fadbd8")
        self.delete_tree.tag_configure("keep",   background="#d6eaf8")
        tk.Label(self.delete_frame,
                 text="Doppelklick: umschalten  |  "
                      "🔒 Nach Lauf 1 nicht mehr änderbar",
                 font=("Helvetica", 8), fg="#888").pack(
                     anchor="w", padx=10, pady=2)

    def _build_conflict_tab(self):
        tk.Label(self.conflict_frame,
                 text="Mac UND Win verändert. Wähle welche Version gewinnt.\n"
                      "Verlierer-Version bleibt in harvest/conflicts/.",
                 font=("Helvetica", 10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)
        cols = ("winner", "rel_path", "mac_info", "win_info")
        self.conf_tree = ttk.Treeview(self.conflict_frame, columns=cols,
                                      show="headings", height=10)
        self.conf_tree.heading("winner",   text="Gewinner")
        self.conf_tree.heading("rel_path", text="Datei")
        self.conf_tree.heading("mac_info", text="Mac  (Größe / mtime)")
        self.conf_tree.heading("win_info", text="Win  (Größe / mtime)")
        self.conf_tree.column("winner",   width=90,  anchor="center")
        self.conf_tree.column("rel_path", width=380, anchor="w")
        self.conf_tree.column("mac_info", width=230, anchor="center")
        self.conf_tree.column("win_info", width=230, anchor="center")
        cfsb = ttk.Scrollbar(self.conflict_frame, orient="vertical",
                              command=self.conf_tree.yview)
        self.conf_tree.configure(yscrollcommand=cfsb.set)
        cfsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.conf_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self.conf_tree.bind("<Double-1>", self._toggle_winner)
        self.conf_tree.tag_configure("mac",  background="#fde8d8")
        self.conf_tree.tag_configure("win",  background="#d6eaf8")
        self.conf_tree.tag_configure("none", background="#fef9e7")
        tk.Label(self.conflict_frame,
                 text="Doppelklick: Mac ↔ Win wechseln  |  "
                      "🔒 Nach Lauf 1 nicht mehr änderbar",
                 font=("Helvetica", 8), fg="#888").pack(
                     anchor="w", padx=10, pady=2)

    def _build_log_tab(self):
        self.log_text = tk.Text(self.log_frame, font=("Courier", 8),
                                state=tk.DISABLED,
                                bg="#1e1e1e", fg="#d4d4d4")
        lsb = ttk.Scrollbar(self.log_frame, orient="vertical",
                             command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # USB wählen
    # ------------------------------------------------------------------

    def _pick_usb(self):
        path = filedialog.askdirectory(
            title="USB-Laufwerk (Wurzel) wählen...",
            initialdir=str(Path.home()))
        if not path:
            return

        self._usb_root = Path(path)
        sync = self._usb_root / "_sync"
        if not sync.is_dir():
            messagebox.showwarning("Kein _sync-Ordner",
                                   f"Kein _sync-Ordner auf:\n{self._usb_root}")
            return

        dated = sorted([d for d in sync.iterdir() if d.is_dir()],
                       reverse=True)
        if not dated:
            return

        sync_dir     = dated[0]
        harvest_root = sync_dir / "harvest"
        report_path  = harvest_root / "harvest_report.json"

        if not report_path.is_file():
            messagebox.showwarning(
                "Kein harvest_report",
                f"harvest_report.json nicht gefunden:\n{harvest_root}\n\n"
                f"Bitte zuerst Script 3 ausführen.")
            return

        try:
            self._report = json.loads(
                report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Lesefehler", str(exc))
            return

        self._harvest_root = harvest_root

        # Execution-Report laden (Lauf 2: Entscheidungen wiederherstellen)
        exec_report_path = harvest_root / "execution_report.json"
        self._exec_report = ExecReport(exec_report_path)

        # USB-Roots aus harvest_report Meta lesen (nicht USB-Unterordner scannen)
        # Format im Report: entries[*].dst enthält den USB-Pfad
        # Zuverlässigster Weg: dst-Pfade der ok-Einträge → gemeinsame Wurzel
        self._usb_roots = self._derive_usb_roots(harvest_root)

        # Lauf-Status bestimmen
        if self._exec_report.run1_done:
            self._locked = True
            lauf_str = "🔒 Lauf 2 – Entscheidungen aus Lauf 1 geladen (schreibgeschützt)"
        else:
            self._locked = False
            lauf_str = "Lauf 1 – Entscheidungen treffen + ausführen"

        self.usb_var.set(
            f"✔  {self._usb_root}  →  _sync/{sync_dir.name}/harvest/")
        s = self._report["summary"]
        self.status_var.set(
            f"harvest_report: OK={s['ok']:,}  "
            f"DELETE-ausstehend={s['pending_delete']:,}  "
            f"Complete={s.get('complete')}")
        self.lauf_var.set(lauf_str)

        if self._locked:
            self.lock_label.config(
                text="🔒 Entscheidungen aus Lauf 1 – nicht mehr änderbar")

        self._populate_all()
        self.exec_btn.config(state=tk.NORMAL)
        self._log(f"USB: {self._usb_root}")
        self._log(f"Lauf: {'2 (locked)' if self._locked else '1'}")

    # ------------------------------------------------------------------
    # Tabs befüllen
    # ------------------------------------------------------------------

    def _populate_all(self):
        entries = self._report.get("entries", [])
        self._copy_entries     = [e for e in entries
                                   if e.get("action") in
                                   ("COPY MAC","COPY WIN","COPY")
                                   and e.get("status") == "ok"]
        self._del_entries      = [e for e in entries
                                   if e.get("action") in
                                   ("CONFLICT del Win","CONFLICT del Mac")
                                   and e.get("status") == "ok"]
        self._delete_entries   = [e for e in entries
                                   if e.get("status") == "pending_delete"]
        self._conflict_entries = [e for e in entries
                                   if e.get("action") in
                                   ("CONFLICT modified","CONFLICT new")
                                   and e.get("status") == "ok"]

        self._populate_overview()
        self._populate_copy()
        self._populate_del()
        self._populate_delete()
        self._populate_conflict()

    def _populate_overview(self):
        er   = self._exec_report
        done = sum(1 for e in er.entries if e.get("status") == "ok")
        pend = sum(1 for e in er.entries if e.get("status") == "pending")
        err  = sum(1 for e in er.entries if e.get("status") == "error")

        lines = [
            "=" * 62,
            "  CHANGE EXECUTION – ÜBERSICHT",
            f"  Stand: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"  {'🔒 Lauf 2 – Entscheidungen schreibgeschützt' if self._locked else 'Lauf 1 – Entscheidungen treffen'}",
            "=" * 62, "",
            "  Automatisch:",
            f"    COPY MAC  → Win + USB:    {sum(1 for e in self._copy_entries if e['action']=='COPY MAC'):>6,} Dateien",
            f"    COPY WIN  → Mac + USB:    {sum(1 for e in self._copy_entries if e['action']=='COPY WIN'):>6,} Dateien",
            "",
            "  Entscheidung (Ordner-weise, Doppelklick):",
            f"    del Win  (Win gelöscht):  {sum(1 for e in self._del_entries if e['action']=='CONFLICT del Win'):>6,} Dateien",
            f"    del Mac  (Mac gelöscht):  {sum(1 for e in self._del_entries if e['action']=='CONFLICT del Mac'):>6,} Dateien",
            f"    DELETE   (beide gelöscht):{len(self._delete_entries):>6,} Dateien",
            f"    CONFLICT modified:        {len(self._conflict_entries):>6,} Dateien",
            "",
        ]
        if er.run1_done:
            lines += [
                "─" * 62,
                "  LAUF 1 ABGESCHLOSSEN:",
                f"    Erledigt:   {done:,}",
                f"    Ausstehend: {pend:,}",
                f"    Fehler:     {err:,}",
                "",
            ]
        lines += [
            "─" * 62,
            "  HINWEIS:",
            "  Entscheidungen sind nach Lauf 1 nicht mehr änderbar.",
            "  Fehler → Dateien manuell aus harvest/_GELOESCHT/ holen.",
            "",
        ]
        self.overview_text.config(state=tk.NORMAL)
        self.overview_text.delete("1.0", tk.END)
        self.overview_text.insert(tk.END, "\n".join(lines))
        self.overview_text.config(state=tk.DISABLED)

    def _populate_copy(self):
        self.copy_tree.delete(*self.copy_tree.get_children())
        groups = {
            "COPY MAC": ("COPY MAC → Win + USB",  "win"),
            "COPY WIN": ("COPY WIN → Mac + USB",  "mac"),
            "COPY":     ("COPY beide → USB",      "both"),
        }
        for act, (label, slot) in groups.items():
            grp = [e for e in self._copy_entries if e.get("action") == act]
            if not grp:
                continue
            dst_root = self._harvest_root / "staging" / slot
            self.copy_tree.insert("", tk.END, values=(
                label, f"{len(grp):,}", str(dst_root)))

    def _populate_del(self):
        self.del_tree.delete(*self.del_tree.get_children())
        self._del_item_map = {}

        for category, action in [
            ("── Win hat gelöscht (del Win) ──", "CONFLICT del Win"),
            ("── Mac hat gelöscht (del Mac) ──", "CONFLICT del Mac"),
        ]:
            cat = [e for e in self._del_entries if e.get("action") == action]
            if not cat:
                continue
            self.del_tree.insert("", tk.END,
                values=("", f"{len(cat):,}", "", category),
                tags=("header",))

            folder_map: dict[str, list] = defaultdict(list)
            for e in cat:
                folder_map[top_folder(e["rel_path"])].append(e)

            for folder, fentries in sorted(folder_map.items()):
                key = f"{action}::{folder}"
                # Lauf 2: Entscheidung aus Report laden
                if self._locked and key in self._exec_report.decisions:
                    dec = self._exec_report.decisions[key]
                else:
                    dec = self._decisions.get(key, "confirm")
                self._decisions[key] = dec

                total_b = 0
                for e in fentries:
                    try:
                        dst = longpath(Path(e.get("dst", "")))
                        if dst.is_file():
                            total_b += dst.stat().st_size
                    except Exception:
                        pass

                label = "✔ Löschen" if dec == "confirm" else "↩ Wiederherstellen"
                iid = self.del_tree.insert(
                    "", tk.END,
                    values=(label, f"{len(fentries):,}",
                            fmt_size(total_b), folder),
                    tags=(dec,))
                self._del_item_map[iid] = key

        # Lauf 2: Baum sperren
        if self._locked:
            self.all_confirm_btn.config(state=tk.DISABLED)
            self.all_restore_btn.config(state=tk.DISABLED)

    def _populate_delete(self):
        self.delete_tree.delete(*self.delete_tree.get_children())
        self._delete_keep = {}
        for e in self._delete_entries:
            rel = e["rel_path"]
            # Lauf 2: aus Report laden
            if self._locked:
                keep = self._exec_report.decisions.get(f"DELETE::{rel}", "delete") == "keep"
            else:
                keep = False
            self._delete_keep[rel] = keep
            label = "💾 Behalten" if keep else "🗑 Löschen"
            tag   = "keep" if keep else "delete"
            self.delete_tree.insert("", tk.END,
                values=(label, rel), tags=(tag,))

    def _populate_conflict(self):
        self.conf_tree.delete(*self.conf_tree.get_children())
        self._conflict_winner = {}
        if not self._conflict_entries:
            self.conf_tree.insert("", tk.END,
                values=("", "(keine CONFLICT modified im Report)", "", ""),
                tags=("none",))
            return
        for e in self._conflict_entries:
            rel = e["rel_path"]
            if self._locked:
                winner = self._exec_report.decisions.get(
                    f"CONFLICT::{rel}", "mac")
            else:
                winner = "mac"
            self._conflict_winner[rel] = winner
            ps  = e.get("per_source", {})
            def fmt(m):
                if not m: return "–"
                return (f"{fmt_size(m.get('size',0))}  "
                        f"{datetime.fromtimestamp(m.get('mtime',0)).strftime('%Y-%m-%d %H:%M')}")
            label = "📱 Mac" if winner == "mac" else "🖥 Win"
            tag   = "mac"   if winner == "mac" else "win"
            self.conf_tree.insert("", tk.END,
                values=(label, rel, fmt(ps.get("mac")), fmt(ps.get("win"))),
                tags=(tag,))

    # ------------------------------------------------------------------
    # Doppelklick-Interaktion (nur in Lauf 1)
    # ------------------------------------------------------------------

    def _toggle_decision(self, event):
        if self._locked:
            return
        iid = self.del_tree.identify_row(event.y)
        if not iid or iid not in self._del_item_map:
            return
        key     = self._del_item_map[iid]
        current = self._decisions.get(key, "confirm")
        new     = "restore" if current == "confirm" else "confirm"
        self._decisions[key] = new
        vals    = list(self.del_tree.item(iid, "values"))
        vals[0] = "✔ Löschen" if new == "confirm" else "↩ Wiederherstellen"
        self.del_tree.item(iid, values=vals, tags=(new,))

    def _toggle_keep(self, event):
        if self._locked:
            return
        iid  = self.delete_tree.identify_row(event.y)
        if not iid:
            return
        vals    = list(self.delete_tree.item(iid, "values"))
        rel     = vals[1]
        current = self._delete_keep.get(rel, False)
        new     = not current
        self._delete_keep[rel] = new
        vals[0] = "💾 Behalten" if new else "🗑 Löschen"
        self.delete_tree.item(iid, values=vals,
                              tags=("keep" if new else "delete",))

    def _toggle_winner(self, event):
        if self._locked:
            return
        iid  = self.conf_tree.identify_row(event.y)
        if not iid:
            return
        vals    = list(self.conf_tree.item(iid, "values"))
        rel     = vals[1]
        current = self._conflict_winner.get(rel, "mac")
        new     = "win" if current == "mac" else "mac"
        self._conflict_winner[rel] = new
        vals[0] = "🖥 Win" if new == "win" else "📱 Mac"
        self.conf_tree.item(iid, values=vals,
                            tags=("win" if new == "win" else "mac",))

    def _set_all(self, decision: str):
        if self._locked:
            return
        for iid, key in self._del_item_map.items():
            self._decisions[key] = decision
            vals    = list(self.del_tree.item(iid, "values"))
            vals[0] = "✔ Löschen" if decision == "confirm" else "↩ Wiederherstellen"
            self.del_tree.item(iid, values=vals, tags=(decision,))

    # ------------------------------------------------------------------
    # Ausführung
    # ------------------------------------------------------------------

    def _execute(self):
        if not self._harvest_root:
            return

        # Entscheidungen in Report sichern (Lauf 1)
        if not self._locked:
            er = self._exec_report
            # del-Entscheidungen
            for key, dec in self._decisions.items():
                er.decisions[key] = dec
            # DELETE-Entscheidungen
            for rel, keep in self._delete_keep.items():
                er.decisions[f"DELETE::{rel}"] = "keep" if keep else "delete"
            # CONFLICT-Entscheidungen
            for rel, winner in self._conflict_winner.items():
                er.decisions[f"CONFLICT::{rel}"] = winner

        ok = messagebox.askyesno(
            "Ausführen bestätigen",
            f"Lauf {'2' if self._locked else '1'} wird jetzt ausgeführt.\n\n"
            f"Nicht erreichbare Roots werden übersprungen\n"
            f"und beim nächsten Lauf (USB umstecken) nachgeholt.\n\n"
            f"Jetzt starten?"
        )
        if not ok:
            return

        self.exec_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._run_all, daemon=True).start()

    def _run_all(self):
        # Ziel-Roots abfragen wenn nötig
        self.root.after(0, self._ask_roots)

    def _derive_usb_roots(self, harvest_root: Path) -> list[str]:
        """
        Leitet USB-Roots aus den dst-Pfaden im harvest_report ab.
        Sucht den gemeinsamen Eltern-Ordner aller dst-Pfade auf dem USB.
        Fallback: bekannte Unterordner-Namen direkt unter USB-Root prüfen.
        """
        entries = self._report.get("entries", [])
        known_roots: set[str] = set()

        for e in entries:
            dst = e.get("dst", "")
            if not dst:
                continue
            p = Path(dst)
            # harvest_root liegt auf USB — alles was darunter liegt ignorieren.
            # Wir suchen Roots die NEBEN dem _sync-Ordner liegen.
            # Typisch: /Volumes/USB/MichasDateien - 20260125/...
            # dst sieht so aus: /Volumes/USB/_sync/.../harvest/staging/mac/rel
            # Daraus können wir die USB-Wurzel nicht ableiten.
            # Stattdessen: Unterordner von usb_root die keine _-Ordner sind.
            break

        # Zuverlässigste Methode: Unterordner von usb_root prüfen
        # aber NUR solche die Dateien enthalten (keine System-Ordner)
        if self._usb_root:
            candidates = []
            try:
                for d in self._usb_root.iterdir():
                    if (d.is_dir()
                            and not d.name.startswith(".")
                            and not d.name.startswith("_")
                            and d.name not in ("System Volume Information",
                                               "Spotlight-V100",
                                               "$RECYCLE.BIN",
                                               "RECYCLER")):
                        candidates.append(str(d))
            except Exception:
                pass
            if candidates:
                return candidates

        return [str(self._usb_root)] if self._usb_root else []

    def _ask_roots(self):
        """
        Fragt Roots ab — mit klaren Dialogen welche Root gerade gefragt wird
        und was passiert wenn man abbricht.
        Im Hauptthread (wegen filedialog).
        """
        need_win = any(e["action"] == "COPY MAC"
                       for e in self._copy_entries)
        need_mac = any(e["action"] == "COPY WIN"
                       for e in self._copy_entries)

        # Auch für Löschungen / Wiederherstellungen prüfen
        for e in self._del_entries:
            act    = e["action"]
            folder = top_folder(e["rel_path"])
            key    = f"{act}::{folder}"
            dec    = self._exec_report.decisions.get(
                        key, self._decisions.get(key, "confirm"))
            if act == "CONFLICT del Win" and dec == "confirm":
                need_mac = True   # Mac muss löschen
            if act == "CONFLICT del Mac" and dec == "confirm":
                need_win = True   # Win muss löschen
            if act == "CONFLICT del Win" and dec == "restore":
                need_win = True   # Win bekommt Datei zurück
            if act == "CONFLICT del Mac" and dec == "restore":
                need_mac = True   # Mac bekommt Datei zurück

        if self._conflict_entries:
            need_mac = need_win = True

        # Win-Root abfragen
        if need_win and not self._win_roots:
            ans = messagebox.askyesno(
                "Win-Wurzelverzeichnis",
                "Für diesen Lauf wird das Win-Wurzelverzeichnis benötigt.\n\n"
                "Ist Win gerade erreichbar?\n"
                "(Netzwerkfreigabe, lokaler Ordner, oder externer Mount)\n\n"
                "Ja  → Ordner wählen\n"
                "Nein → Win-Aktionen überspringen (werden beim nächsten Lauf nachgeholt)"
            )
            if ans:
                p = filedialog.askdirectory(
                    title="Win-Wurzelverzeichnis wählen "
                          "(oberster Sync-Ordner auf Win)")
                self._win_roots = [p] if p else []
            else:
                self._win_roots = []
                self._log("Win-Root: übersprungen (nicht erreichbar)")

        # Mac-Root abfragen
        if need_mac and not self._mac_roots:
            ans = messagebox.askyesno(
                "Mac-Wurzelverzeichnis",
                "Für diesen Lauf wird das Mac-Wurzelverzeichnis benötigt.\n\n"
                "Ist Mac gerade erreichbar?\n"
                "(Netzwerkfreigabe, lokaler Ordner, oder /Volumes/...)\n\n"
                "Ja  → Ordner wählen\n"
                "Nein → Mac-Aktionen überspringen (werden beim nächsten Lauf nachgeholt)"
            )
            if ans:
                p = filedialog.askdirectory(
                    title="Mac-Wurzelverzeichnis wählen "
                          "(oberster Sync-Ordner auf Mac)")
                self._mac_roots = [p] if p else []
            else:
                self._mac_roots = []
                self._log("Mac-Root: übersprungen (nicht erreichbar)")

        threading.Thread(target=self._run_execution, daemon=True).start()

    def _run_execution(self):
        er = self._exec_report

        # bereits erledigte überspringen
        done_keys = er.done_keys()

        total_ok = total_pend = total_err = 0

        # 1. COPY
        total_ok, total_pend, total_err = self._run_copy(
            done_keys, total_ok, total_pend, total_err)

        # 2. Löschungen
        total_ok, total_pend, total_err = self._run_del(
            done_keys, total_ok, total_pend, total_err)

        # 3. DELETE
        total_ok, total_pend, total_err = self._run_delete(
            done_keys, total_ok, total_pend, total_err)

        # 4. CONFLICT (wird nach Harvest/conflicts komplett)
        #    Hier: Gewinner-Version auf alle drei kopieren
        total_ok, total_pend, total_err = self._run_conflicts(
            done_keys, total_ok, total_pend, total_err)

        # Report speichern
        complete = er.save(run1_done=True)

        msg = (
            f"Lauf abgeschlossen.\n\n"
            f"  Erledigt:   {total_ok:,}\n"
            f"  Ausstehend: {total_pend:,}\n"
            f"  Fehler:     {total_err:,}\n"
        )
        if complete:
            msg += "\n✅ EXECUTION COMPLETE – alle Läufe abgeschlossen."
        else:
            msg += (
                "\n⏳ Bitte USB an den anderen Rechner anschließen\n"
                "und Change Execution dort erneut starten."
            )

        self.root.after(0, lambda: [
            self.exec_btn.config(state=tk.NORMAL),
            self.log_btn.config(state=tk.NORMAL),
            messagebox.showinfo("Lauf abgeschlossen", msg),
            self._populate_overview(),
        ])

    # ---- COPY ----

    def _run_copy(self, done_keys, ok, pend, err):
        slot_map = {
            "COPY MAC": ("mac",  [("Win", self._win_roots),
                                  ("USB", self._usb_roots)]),
            "COPY WIN": ("win",  [("Mac", self._mac_roots),
                                  ("USB", self._usb_roots)]),
            "COPY":     ("both", [("USB", self._usb_roots)]),
        }

        for entry in self._copy_entries:
            rel = entry["rel_path"]
            act = entry["action"]

            if act not in slot_map:
                continue

            staging_slot, targets = slot_map[act]

            # Quelle: immer relativ zu harvest_root/staging/<slot>/
            # NICHT über dst-Pfad aus harvest_report (plattformabhängig)
            src = longpath(self._harvest_root / "staging" / staging_slot / rel)

            if not src.is_file():
                self._log(f"MISS staging/{staging_slot}  {rel[:65]}")
                self._exec_report.upsert(rel, act, "error", act, "copy",
                                         error=f"staging/{staging_slot} nicht gefunden")
                err += 1
                continue

            any_pend = False
            for label, roots in targets:
                key = f"{rel}::{act}::{label}"
                if key in done_keys:
                    continue
                if not roots_reachable(roots):
                    self._log(f"PEND [{label} nicht erreichbar]  {rel[:55]}")
                    self._exec_report.upsert(rel, f"{act}::{label}",
                        "pending", act, "copy")
                    any_pend = True
                    continue
                dst    = Path(roots[0]) / rel
                c_ok, info = copy_verified(src, dst)
                if c_ok:
                    self._log(f"OK   COPY→{label}  {rel[:58]}")
                    self._exec_report.upsert(rel, f"{act}::{label}",
                        "ok", act, "copy")
                    ok += 1
                else:
                    self._log(f"ERR  COPY→{label}  {info}  {rel[:50]}")
                    self._exec_report.upsert(rel, f"{act}::{label}",
                        "error", act, "copy", error=info)
                    err  += 1

            if any_pend:
                pend += 1
        return ok, pend, err

    # ---- Löschungen ----

    def _run_del(self, done_keys, ok, pend, err):
        for entry in self._del_entries:
            rel    = entry["rel_path"]
            act    = entry["action"]
            folder = top_folder(rel)
            key    = f"{act}::{folder}"
            dec    = self._exec_report.decisions.get(
                        key, self._decisions.get(key, "confirm"))

            if dec == "confirm":
                # Löschen auf dem Rechner der NICHT gelöscht hat
                targets = []
                if act == "CONFLICT del Win":
                    targets = [("Mac", self._mac_roots)]
                elif act == "CONFLICT del Mac":
                    targets = [("Win", self._win_roots)]

                for label, roots in targets:
                    ekey = f"{rel}::del::{label}"
                    if ekey in done_keys:
                        continue
                    if not roots_reachable(roots):
                        self._log(f"PEND [{label}]  DEL  {rel[:55]}")
                        self._exec_report.upsert(rel, f"del::{label}",
                            "pending", act, dec)
                        pend += 1
                        continue
                    target = Path(roots[0]) / rel
                    d_ok, info = delete_file(target)
                    if d_ok:
                        self._log(f"OK   DEL→{label}  {rel[:58]}")
                        self._exec_report.upsert(rel, f"del::{label}",
                            "ok", act, dec)
                        ok += 1
                    else:
                        if not target.exists():
                            # schon weg → ok
                            self._exec_report.upsert(rel, f"del::{label}",
                                "ok", act, dec)
                            ok += 1
                        else:
                            self._log(f"ERR  DEL  {info}  {rel[:55]}")
                            self._exec_report.upsert(rel, f"del::{label}",
                                "error", act, dec, error=info)
                            err += 1

            elif dec == "restore":
                # Wiederherstellen von USB/_GELOESCHT → auf den Rechner der gelöscht hat
                # Pfad relativ zu harvest_root/_GELOESCHT/<datum>/<sub>/<rel>
                geloescht_root = self._harvest_root / "_GELOESCHT"
                sub_folder = "del_win" if act == "CONFLICT del Win" else "del_mac"
                # Datum-Unterordner suchen (neuester)
                src = None
                if geloescht_root.is_dir():
                    dated = sorted([d for d in geloescht_root.iterdir()
                                    if d.is_dir()], reverse=True)
                    for dated_dir in dated:
                        candidate = longpath(dated_dir / sub_folder / rel)
                        if candidate.is_file():
                            src = candidate
                            break

                targets = []
                if act == "CONFLICT del Win":
                    targets = [("Win", self._win_roots)]
                elif act == "CONFLICT del Mac":
                    targets = [("Mac", self._mac_roots)]

                for label, roots in targets:
                    ekey = f"{rel}::restore::{label}"
                    if ekey in done_keys:
                        continue
                    if src is None or not src.is_file():
                        self._log(f"MISS restore-Quelle  {rel[:60]}")
                        self._exec_report.upsert(rel, f"restore::{label}",
                            "error", act, dec,
                            error="Quelle in _GELOESCHT nicht gefunden")
                        err += 1
                        continue
                    if not roots_reachable(roots):
                        self._log(f"PEND [{label}]  RST  {rel[:55]}")
                        self._exec_report.upsert(rel, f"restore::{label}",
                            "pending", act, dec)
                        pend += 1
                        continue
                    dst    = Path(roots[0]) / rel
                    c_ok, info = copy_verified(src, dst)
                    if c_ok:
                        self._log(f"OK   RST→{label}  {rel[:58]}")
                        self._exec_report.upsert(rel, f"restore::{label}",
                            "ok", act, dec)
                        ok += 1
                    else:
                        self._log(f"ERR  RST  {info}  {rel[:55]}")
                        self._exec_report.upsert(rel, f"restore::{label}",
                            "error", act, dec, error=info)
                        err += 1

        return ok, pend, err

    # ---- DELETE ----

    def _run_delete(self, done_keys, ok, pend, err):
        for entry in self._delete_entries:
            rel  = entry["rel_path"]
            keep = self._delete_keep.get(rel, False)
            ekey = f"{rel}::DELETE"
            if ekey in done_keys:
                continue
            if keep:
                self._log(f"KEEP DELETE  {rel[:65]}")
                self._exec_report.upsert(rel, "DELETE", "ok",
                                         "DELETE", "keep")
                ok += 1
                continue
            # Von USB löschen
            src = find_in_roots(self._usb_roots, rel)
            if src is None:
                self._log(f"MISS DELETE  {rel[:65]}")
                self._exec_report.upsert(rel, "DELETE", "ok",
                                         "DELETE", "delete",
                                         note="bereits nicht vorhanden")
                ok += 1
                continue
            d_ok, info = delete_file(src)
            if d_ok:
                self._log(f"OK   DELETE  {rel[:65]}")
                self._exec_report.upsert(rel, "DELETE", "ok",
                                         "DELETE", "delete")
                ok += 1
            else:
                self._log(f"ERR  DELETE  {info}  {rel[:55]}")
                self._exec_report.upsert(rel, "DELETE", "error",
                                         "DELETE", "delete", error=info)
                err += 1
        return ok, pend, err

    # ---- CONFLICT ----

    def _run_conflicts(self, done_keys, ok, pend, err):
        for entry in self._conflict_entries:
            rel    = entry["rel_path"]
            winner = self._conflict_winner.get(rel, "mac")

            # Gewinner-Version aus harvest/conflicts/<rel>/_mac oder _win
            src_path = (self._harvest_root / "conflicts" / rel
                        / f"_{winner}")
            ekey = f"{rel}::CONFLICT"
            if ekey in done_keys:
                continue
            if not longpath(src_path).is_file():
                self._log(f"MISS conflict-Quelle [{winner}]  {rel[:55]}")
                self._exec_report.upsert(rel, "CONFLICT", "error",
                    "CONFLICT modified", winner,
                    error=f"harvest/conflicts/{rel}/_{winner} nicht gefunden")
                err += 1
                continue

            # Auf alle drei verteilen: USB + Mac + Win
            targets = [("USB",  self._usb_roots),
                       ("Mac",  self._mac_roots),
                       ("Win",  self._win_roots)]
            any_pend = False
            for label, roots in targets:
                if not roots_reachable(roots):
                    self._log(f"PEND [{label}]  CONF  {rel[:55]}")
                    any_pend = True
                    continue
                dst   = Path(roots[0]) / rel
                c_ok, info = copy_verified(longpath(src_path), dst)
                if c_ok:
                    self._log(f"OK   CONF→{label}  {rel[:55]}")
                    ok += 1
                else:
                    self._log(f"ERR  CONF→{label}  {info}  {rel[:50]}")
                    err += 1

            status = "pending" if any_pend else "ok"
            if any_pend:
                pend += 1
            self._exec_report.upsert(rel, "CONFLICT", status,
                "CONFLICT modified", winner)

        return ok, pend, err

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self._exec_log.append(line)

        def _upd():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, line)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

        self.root.after(0, _upd)

    def _save_log(self):
        if not self._harvest_root:
            return
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        lp = self._harvest_root / f"execution_log_{ts}.txt"
        lp.write_text("".join(self._exec_log), encoding="utf-8")
        import subprocess, platform
        s = platform.system()
        if s == "Darwin":
            subprocess.Popen(["open", str(lp)])
        elif s == "Windows":
            subprocess.Popen(["notepad", str(lp)])
        else:
            subprocess.Popen(["xdg-open", str(lp)])

    def run(self):
        self.root.mainloop()


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    app = ExecutionApp()
    app.run()
