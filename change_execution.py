#!/usr/bin/env python3
"""
change_execution.py  –  Script 4 des file-sync-toolchain
==========================================================
Version 1.0

Liest harvest_report.json und führt Änderungen kontrolliert aus.

Ablauf:
  1. USB wählen → Report + Harvest-Ordner automatisch gefunden
  2. Übersicht aller ausstehenden Aktionen
  3. Abschnitt für Abschnitt entscheiden + ausführen:

     COPY MAC/WIN/BOTH  → kein Dialog, Bestätigung als Zusammenfassung
     del Win            → Vorauswahl: Löschen bestätigen
                          Rückgängig: Datei von USB auf Win zurückspielen
     del Mac            → Vorauswahl: Löschen bestätigen
                          Rückgängig: Datei von USB auf Mac zurückspielen
     DELETE             → beide haben gelöscht, Vorauswahl: von USB entfernen
                          Sicherheitskopie bleibt in harvest/
     CONFLICT modified  → Metadaten nebeneinander, User wählt Version

Entscheidungen werden pro Ordner getroffen (nicht pro Datei).
Ein Ordner-Urteil gilt für alle Unterordner und Dateien darin,
außer der User klappt auf und entscheidet feiner.
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


# ==============================================================================
# REPORT LADEN & ROOTS AUFLÖSEN
# ==============================================================================

def resolve_roots(report: dict, usb_root: Path) -> dict[str, list[str]]:
    """
    Gibt Roots je Slot zurück, USB-Roots mit lokalem Override.
    Format: {"mac": [...], "win": [...], "usb": [...]}
    """
    # Roots stehen in der Directive, nicht im Report.
    # Wir nehmen sie aus dem Report-Meta falls vorhanden,
    # sonst aus den dst-Pfaden der Einträge.
    roots: dict[str, list[str]] = {"mac": [], "win": [], "usb": []}

    # USB-Roots aus Unterordnern des gewählten USB-Laufwerks ableiten
    if usb_root.is_dir():
        usb_subs = [str(d) for d in usb_root.iterdir()
                    if d.is_dir() and not d.name.startswith("_")]
        roots["usb"] = usb_subs if usb_subs else [str(usb_root)]

    return roots


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


# ==============================================================================
# HAUPT-APP
# ==============================================================================

class ExecutionApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("⚡ Change Execution")
        self.root.geometry("1020x780")
        self.root.resizable(True, True)

        self._report:       dict | None = None
        self._harvest_root: Path | None = None
        self._usb_root:     Path | None = None
        self._exec_log:     list[str]   = []
        self._log_path:     Path | None = None

        # Entscheidungen je Ordner: {rel_folder: "confirm"|"restore"|"skip"}
        self._decisions: dict[str, str] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self.root, bg="#922b21", height=54)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text="⚡ Change Execution – Änderungen ausführen",
                 font=("Helvetica", 14, "bold"),
                 bg="#922b21", fg="white").pack(pady=13)

        # USB-Picker
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

        # Statuszeile
        info_frame = tk.LabelFrame(self.root, text="Status",
                                   padx=10, pady=4)
        info_frame.pack(fill=tk.X, padx=10, pady=4)
        self.status_var = tk.StringVar(value="–")
        tk.Label(info_frame, textvariable=self.status_var,
                 font=("Courier", 9), anchor="w").pack(fill=tk.X)

        # Notebook
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # Tab 1: Übersicht
        self.overview_frame = tk.Frame(self.nb)
        self.nb.add(self.overview_frame, text="📊 Übersicht")
        self._build_overview_tab()

        # Tab 2: COPY (automatisch)
        self.copy_frame = tk.Frame(self.nb)
        self.nb.add(self.copy_frame, text="📋 COPY")
        self._build_copy_tab()

        # Tab 3: Löschungen
        self.del_frame = tk.Frame(self.nb)
        self.nb.add(self.del_frame, text="🗑 Löschungen")
        self._build_del_tab()

        # Tab 4: DELETE (beide gelöscht)
        self.delete_frame = tk.Frame(self.nb)
        self.nb.add(self.delete_frame, text="🗑 DELETE")
        self._build_delete_tab()

        # Tab 5: CONFLICT
        self.conflict_frame = tk.Frame(self.nb)
        self.nb.add(self.conflict_frame, text="⚠️ CONFLICT")
        self._build_conflict_tab()

        # Tab 6: Log
        self.log_frame = tk.Frame(self.nb)
        self.nb.add(self.log_frame, text="📋 Log")
        self.log_text = tk.Text(self.log_frame, font=("Courier", 8),
                                state=tk.DISABLED,
                                bg="#1e1e1e", fg="#d4d4d4")
        lsb = ttk.Scrollbar(self.log_frame, orient="vertical",
                             command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Buttons
        btn = tk.Frame(self.root)
        btn.pack(fill=tk.X, padx=10, pady=6)

        self.exec_copy_btn = tk.Button(
            btn, text="▶ COPY ausführen",
            command=self._exec_copy,
            font=("Helvetica", 11, "bold"),
            bg="#1a5276", fg="white",
            state=tk.DISABLED, height=2, width=20)
        self.exec_copy_btn.pack(side=tk.LEFT, padx=4)

        self.exec_del_btn = tk.Button(
            btn, text="▶ Löschungen ausführen",
            command=self._exec_deletions,
            font=("Helvetica", 11, "bold"),
            bg="#7d6608", fg="white",
            state=tk.DISABLED, height=2, width=22)
        self.exec_del_btn.pack(side=tk.LEFT, padx=4)

        self.exec_delete_btn = tk.Button(
            btn, text="▶ DELETE ausführen",
            command=self._exec_delete,
            font=("Helvetica", 11, "bold"),
            bg="#922b21", fg="white",
            state=tk.DISABLED, height=2, width=18)
        self.exec_delete_btn.pack(side=tk.LEFT, padx=4)

        self.log_btn = tk.Button(
            btn, text="📋 Log öffnen",
            command=self._open_log,
            font=("Helvetica", 11),
            state=tk.DISABLED, height=2, width=14)
        self.log_btn.pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------
    # Tab: Übersicht
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

    # ------------------------------------------------------------------
    # Tab: COPY
    # ------------------------------------------------------------------

    def _build_copy_tab(self):
        tk.Label(self.copy_frame,
                 text="Diese Dateien werden automatisch verteilt.\n"
                      "Keine Entscheidung nötig — Bestätigung genügt.",
                 font=("Helvetica", 10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)

        cols = ("action", "count", "size", "destination")
        self.copy_tree = ttk.Treeview(self.copy_frame, columns=cols,
                                      show="headings", height=8)
        self.copy_tree.heading("action",      text="Aktion")
        self.copy_tree.heading("count",       text="Dateien")
        self.copy_tree.heading("size",        text="Größe")
        self.copy_tree.heading("destination", text="Ziel")
        self.copy_tree.column("action",      width=130, anchor="w")
        self.copy_tree.column("count",       width=80,  anchor="center")
        self.copy_tree.column("size",        width=90,  anchor="center")
        self.copy_tree.column("destination", width=650, anchor="w")
        csb = ttk.Scrollbar(self.copy_frame, orient="vertical",
                             command=self.copy_tree.yview)
        self.copy_tree.configure(yscrollcommand=csb.set)
        csb.pack(side=tk.RIGHT, fill=tk.Y)
        self.copy_tree.pack(fill=tk.BOTH, expand=True, padx=10)

    # ------------------------------------------------------------------
    # Tab: Löschungen (del Win + del Mac)
    # ------------------------------------------------------------------

    def _build_del_tab(self):
        top = tk.Frame(self.del_frame)
        top.pack(fill=tk.X, padx=10, pady=6)

        tk.Label(top,
                 text="Vorauswahl: Löschen bestätigen  "
                      "(Sicherheitskopie bleibt in harvest/_GELOESCHT/)",
                 font=("Helvetica", 10), fg="#555").pack(side=tk.LEFT)

        # Gesamt-Buttons
        tk.Button(top, text="Alle: ✔ Löschen",
                  command=lambda: self._set_all_decisions("confirm"),
                  width=16).pack(side=tk.RIGHT, padx=4)
        tk.Button(top, text="Alle: ↩ Wiederherstellen",
                  command=lambda: self._set_all_decisions("restore"),
                  width=20).pack(side=tk.RIGHT, padx=4)

        # Treeview: Ordnerbaum
        cols = ("decision", "files", "size", "folder")
        self.del_tree = ttk.Treeview(self.del_frame, columns=cols,
                                     show="headings",
                                     selectmode="browse")
        self.del_tree.heading("decision", text="Entscheidung")
        self.del_tree.heading("files",    text="Dateien")
        self.del_tree.heading("size",     text="Größe")
        self.del_tree.heading("folder",   text="Ordner")
        self.del_tree.column("decision", width=160, anchor="center")
        self.del_tree.column("files",    width=70,  anchor="center")
        self.del_tree.column("size",     width=90,  anchor="center")
        self.del_tree.column("folder",   width=650, anchor="w")

        dsb = ttk.Scrollbar(self.del_frame, orient="vertical",
                             command=self.del_tree.yview)
        self.del_tree.configure(yscrollcommand=dsb.set)
        dsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.del_tree.pack(fill=tk.BOTH, expand=True, padx=10)

        # Doppelklick zum Umschalten
        self.del_tree.bind("<Double-1>", self._toggle_decision)

        tk.Label(self.del_frame,
                 text="Doppelklick auf Zeile zum Umschalten  |  "
                      "Grün = Löschen bestätigt  |  Blau = Wiederherstellen",
                 font=("Helvetica", 8), fg="#888"
                 ).pack(anchor="w", padx=10, pady=2)

        self.del_tree.tag_configure("confirm", background="#d5f5e3")
        self.del_tree.tag_configure("restore", background="#d6eaf8")
        self.del_tree.tag_configure("header",  background="#f0f0f0",
                                    font=("Helvetica", 9, "bold"))

    # ------------------------------------------------------------------
    # Tab: DELETE (beide gelöscht)
    # ------------------------------------------------------------------

    def _build_delete_tab(self):
        tk.Label(self.delete_frame,
                 text="Diese Dateien wurden auf BEIDEN Rechnern gelöscht.\n"
                      "Vorauswahl: von USB entfernen  "
                      "(Sicherheitskopie verbleibt in harvest/)",
                 font=("Helvetica", 10), fg="#555",
                 justify="left").pack(anchor="w", padx=10, pady=6)

        cols = ("keep", "rel_path")
        self.delete_tree = ttk.Treeview(self.delete_frame, columns=cols,
                                        show="headings", height=10)
        self.delete_tree.heading("keep",     text="Behalten?")
        self.delete_tree.heading("rel_path", text="Datei")
        self.delete_tree.column("keep",     width=100, anchor="center")
        self.delete_tree.column("rel_path", width=860, anchor="w")
        delsb = ttk.Scrollbar(self.delete_frame, orient="vertical",
                               command=self.delete_tree.yview)
        self.delete_tree.configure(yscrollcommand=delsb.set)
        delsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.delete_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self.delete_tree.bind("<Double-1>", self._toggle_keep)
        self.delete_tree.tag_configure("delete",  background="#fadbd8")
        self.delete_tree.tag_configure("keep",    background="#d6eaf8")

        tk.Label(self.delete_frame,
                 text="Doppelklick zum Umschalten  |  "
                      "Rot = von USB löschen  |  Blau = auf USB behalten",
                 font=("Helvetica", 8), fg="#888"
                 ).pack(anchor="w", padx=10, pady=2)

    # ------------------------------------------------------------------
    # Tab: CONFLICT
    # ------------------------------------------------------------------

    def _build_conflict_tab(self):
        tk.Label(self.conflict_frame,
                 text="Dateien die auf Mac UND Win verändert wurden.\n"
                      "Wähle welche Version künftig für alle drei gilt.\n"
                      "Nicht gewählte Versionen bleiben in harvest/conflicts/ als Sicherheitskopie.",
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
        self.conf_tree.column("rel_path", width=420, anchor="w")
        self.conf_tree.column("mac_info", width=220, anchor="center")
        self.conf_tree.column("win_info", width=220, anchor="center")
        cfsb = ttk.Scrollbar(self.conflict_frame, orient="vertical",
                              command=self.conf_tree.yview)
        self.conf_tree.configure(yscrollcommand=cfsb.set)
        cfsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.conf_tree.pack(fill=tk.BOTH, expand=True, padx=10)
        self.conf_tree.bind("<Double-1>", self._toggle_conflict_winner)
        self.conf_tree.tag_configure("mac", background="#fde8d8")
        self.conf_tree.tag_configure("win", background="#d6eaf8")
        self.conf_tree.tag_configure("open", background="#fef9e7")

        tk.Label(self.conflict_frame,
                 text="Doppelklick: Mac → Win → Mac wechseln",
                 font=("Helvetica", 8), fg="#888"
                 ).pack(anchor="w", padx=10, pady=2)

    # ------------------------------------------------------------------
    # USB wählen → Report laden
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

        dated = sorted([d for d in sync.iterdir() if d.is_dir()], reverse=True)
        if not dated:
            messagebox.showwarning("Kein Datum-Ordner",
                                   f"_sync enthält keine Unterordner")
            return

        sync_dir      = dated[0]
        harvest_root  = sync_dir / "harvest"
        report_path   = harvest_root / "harvest_report.json"

        if not report_path.is_file():
            messagebox.showwarning(
                "Kein harvest_report",
                f"Kein harvest_report.json gefunden:\n{harvest_root}\n\n"
                f"Bitte zuerst Script 3 ausführen.")
            return

        try:
            self._report = json.loads(
                report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Lesefehler", str(exc))
            return

        self._harvest_root = harvest_root

        self.usb_var.set(
            f"✔  {self._usb_root}  →  _sync/{sync_dir.name}/harvest/")

        s = self._report["summary"]
        self.status_var.set(
            f"Report geladen  |  OK: {s['ok']:,}  |  "
            f"DELETE ausstehend: {s['pending_delete']:,}  |  "
            f"Complete: {s.get('complete', False)}")

        self._populate_all()
        self._log(f"USB: {self._usb_root}")
        self._log(f"Report: {report_path.name}")

    # ------------------------------------------------------------------
    # Tabs befüllen
    # ------------------------------------------------------------------

    def _populate_all(self):
        entries = self._report.get("entries", [])
        self._populate_overview(entries)
        self._populate_copy(entries)
        self._populate_del(entries)
        self._populate_delete(entries)
        self._populate_conflict(entries)

        self.exec_copy_btn.config(state=tk.NORMAL)
        self.exec_del_btn.config(state=tk.NORMAL)
        self.exec_delete_btn.config(state=tk.NORMAL)

    def _populate_overview(self, entries):
        copy_mac = sum(1 for e in entries
                       if e.get("action") == "COPY MAC" and e.get("status") == "ok")
        copy_win = sum(1 for e in entries
                       if e.get("action") == "COPY WIN" and e.get("status") == "ok")
        del_win  = sum(1 for e in entries
                       if e.get("action") == "CONFLICT del Win"
                       and e.get("status") == "ok")
        del_mac  = sum(1 for e in entries
                       if e.get("action") == "CONFLICT del Mac"
                       and e.get("status") == "ok")
        deletes  = sum(1 for e in entries
                       if e.get("status") == "pending_delete")
        conflicts = sum(1 for e in entries
                        if e.get("action") in ("CONFLICT modified", "CONFLICT new")
                        and e.get("status") == "ok")

        lines = [
            "=" * 60,
            "  CHANGE EXECUTION – ÜBERSICHT",
            f"  Stand: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "=" * 60, "",
            "  Automatisch (keine Entscheidung nötig):",
            f"    COPY MAC  →  auf Win + USB:  {copy_mac:>6,} Dateien",
            f"    COPY WIN  →  auf Mac + USB:  {copy_win:>6,} Dateien",
            "",
            "  Entscheidung erforderlich:",
            f"    del Win  (Win hat gelöscht):  {del_win:>6,} Dateien",
            f"    del Mac  (Mac hat gelöscht):  {del_mac:>6,} Dateien",
            f"    DELETE   (beide gelöscht):    {deletes:>6,} Dateien",
            f"    CONFLICT modified:            {conflicts:>6,} Dateien",
            "",
            "  Vorauswahl Löschungen: Löschen bestätigen",
            "  (Sicherheitskopien bleiben in harvest/_GELOESCHT/)",
            "",
            "─" * 60,
            "  SCHRITTE:",
            "  1. Tab 'COPY'        → Dateien verteilen (kein Dialog)",
            "  2. Tab 'Löschungen'  → Ordner-weise bestätigen oder",
            "                         wiederherstellen",
            "  3. Tab 'DELETE'      → von USB entfernen bestätigen",
            "  4. Tab 'CONFLICT'    → Version wählen",
            "",
        ]
        self.overview_text.config(state=tk.NORMAL)
        self.overview_text.delete("1.0", tk.END)
        self.overview_text.insert(tk.END, "\n".join(lines))
        self.overview_text.config(state=tk.DISABLED)

    def _populate_copy(self, entries):
        self.copy_tree.delete(*self.copy_tree.get_children())
        self._copy_entries = []

        groups = {
            "COPY MAC": {"label": "COPY MAC → Win + USB",  "entries": []},
            "COPY WIN": {"label": "COPY WIN → Mac + USB",  "entries": []},
            "COPY":     {"label": "COPY beide → USB",      "entries": []},
        }
        for e in entries:
            act = e.get("action")
            if act in groups and e.get("status") == "ok":
                groups[act]["entries"].append(e)
                self._copy_entries.append(e)

        for act, g in groups.items():
            if not g["entries"]:
                continue
            # Größe aus dst-Pfaden schätzen
            total_b = 0
            for e in g["entries"]:
                dst = Path(e.get("dst", ""))
                try:
                    if longpath(dst).is_file():
                        total_b += longpath(dst).stat().st_size
                except Exception:
                    pass

            slot = act.split()[-1].lower() if act != "COPY" else "both"
            dst_root = self._harvest_root / "staging" / slot if self._harvest_root else Path("?")

            self.copy_tree.insert("", tk.END, values=(
                g["label"],
                f"{len(g['entries']):,}",
                fmt_size(total_b) if total_b else "–",
                str(dst_root),
            ))

    def _populate_del(self, entries):
        self.del_tree.delete(*self.del_tree.get_children())
        self._del_entries = []
        self._del_item_map = {}   # iid → folder_key

        # del Win + del Mac getrennt
        for category, action, label_color in [
            ("Win hat gelöscht (del Win)",
             "CONFLICT del Win", "header"),
            ("Mac hat gelöscht (del Mac)",
             "CONFLICT del Mac", "header"),
        ]:
            cat_entries = [e for e in entries
                           if e.get("action") == action
                           and e.get("status") == "ok"]
            if not cat_entries:
                continue

            # Kategorie-Header
            self.del_tree.insert("", tk.END,
                                  values=("", f"{len(cat_entries):,}",
                                          "", f"── {category} ──"),
                                  tags=("header",))

            # Ordner gruppieren
            folder_map: dict[str, list] = defaultdict(list)
            for e in cat_entries:
                folder_map[top_folder(e["rel_path"])].append(e)
                self._del_entries.append(e)

            for folder, fentries in sorted(folder_map.items()):
                # Vorauswahl: confirm (Löschen)
                key = f"{action}::{folder}"
                self._decisions[key] = "confirm"
                total_b = 0
                for e in fentries:
                    dst = Path(e.get("dst", ""))
                    try:
                        total_b += longpath(dst).stat().st_size
                    except Exception:
                        pass
                iid = self.del_tree.insert(
                    "", tk.END,
                    values=("✔ Löschen",
                            f"{len(fentries):,}",
                            fmt_size(total_b),
                            folder),
                    tags=("confirm",)
                )
                self._del_item_map[iid] = key

    def _populate_delete(self, entries):
        self.delete_tree.delete(*self.delete_tree.get_children())
        self._delete_entries = []
        self._delete_keep: dict[str, bool] = {}   # rel_path → keep?

        for e in entries:
            if e.get("status") == "pending_delete":
                self._delete_entries.append(e)
                rel = e["rel_path"]
                self._delete_keep[rel] = False   # Vorauswahl: löschen
                iid = self.delete_tree.insert(
                    "", tk.END,
                    values=("🗑 Löschen", rel),
                    tags=("delete",)
                )

    def _populate_conflict(self, entries):
        self.conf_tree.delete(*self.conf_tree.get_children())
        self._conflict_entries = []
        self._conflict_winner: dict[str, str] = {}   # rel_path → "mac"|"win"

        for e in entries:
            if (e.get("action") in ("CONFLICT modified", "CONFLICT new")
                    and e.get("status") == "ok"):
                rel = e["rel_path"]
                self._conflict_entries.append(e)
                self._conflict_winner[rel] = "mac"   # Vorauswahl Mac

                # Metadaten aus per_source lesen
                ps = e.get("per_source", {})
                mac_m = ps.get("mac", {})
                win_m = ps.get("win", {})

                def fmt_meta(m):
                    if not m:
                        return "–"
                    size = fmt_size(m.get("size", 0))
                    mt = datetime.fromtimestamp(
                        m.get("mtime", 0)).strftime("%Y-%m-%d %H:%M")
                    return f"{size}  {mt}"

                self.conf_tree.insert("", tk.END,
                    values=("📱 Mac", rel,
                            fmt_meta(mac_m), fmt_meta(win_m)),
                    tags=("mac",)
                )

        if not self._conflict_entries:
            self.conf_tree.insert("", tk.END,
                values=("", "(keine CONFLICT modified Einträge im Report)", "", ""),
                tags=("open",))

    # ------------------------------------------------------------------
    # Interaktion: Doppelklick
    # ------------------------------------------------------------------

    def _toggle_decision(self, event):
        iid = self.del_tree.identify_row(event.y)
        if not iid or iid not in self._del_item_map:
            return
        key = self._del_item_map[iid]
        current = self._decisions.get(key, "confirm")
        new = "restore" if current == "confirm" else "confirm"
        self._decisions[key] = new
        vals = list(self.del_tree.item(iid, "values"))
        vals[0] = "✔ Löschen" if new == "confirm" else "↩ Wiederherstellen"
        self.del_tree.item(iid, values=vals, tags=(new,))

    def _toggle_keep(self, event):
        iid = self.delete_tree.identify_row(event.y)
        if not iid:
            return
        vals = list(self.delete_tree.item(iid, "values"))
        rel  = vals[1]
        current = self._delete_keep.get(rel, False)
        self._delete_keep[rel] = not current
        vals[0] = "💾 Behalten" if not current else "🗑 Löschen"
        tag = "keep" if not current else "delete"
        self.delete_tree.item(iid, values=vals, tags=(tag,))

    def _toggle_conflict_winner(self, event):
        iid = self.conf_tree.identify_row(event.y)
        if not iid:
            return
        vals = list(self.conf_tree.item(iid, "values"))
        rel  = vals[1]
        current = self._conflict_winner.get(rel, "mac")
        new = "win" if current == "mac" else "mac"
        self._conflict_winner[rel] = new
        vals[0] = "🖥 Win" if new == "win" else "📱 Mac"
        self.conf_tree.item(iid, values=vals, tags=(new,))

    def _set_all_decisions(self, decision: str):
        for iid, key in self._del_item_map.items():
            self._decisions[key] = decision
            vals = list(self.del_tree.item(iid, "values"))
            vals[0] = "✔ Löschen" if decision == "confirm" else "↩ Wiederherstellen"
            self.del_tree.item(iid, values=vals, tags=(decision,))

    # ------------------------------------------------------------------
    # Ausführung: COPY
    # ------------------------------------------------------------------

    def _exec_copy(self):
        if not self._harvest_root:
            return

        # Ziel-Roots aus USB ableiten
        roots = resolve_roots(self._report, self._usb_root)

        # Ziele bestimmen: welche Rechner brauchen welche Dateien?
        targets = {
            "COPY MAC": self._get_win_roots() + roots["usb"],
            "COPY WIN": self._get_mac_roots() + roots["usb"],
            "COPY":     roots["usb"],
        }

        to_copy = [(e, targets.get(e["action"], []))
                   for e in self._copy_entries
                   if targets.get(e["action"])]

        if not to_copy:
            messagebox.showinfo("COPY", "Keine COPY-Einträge zu verteilen.")
            return

        count = sum(len(t) for _, t in to_copy)
        ok = messagebox.askyesno(
            "COPY bestätigen",
            f"{len(to_copy):,} Dateien werden auf Ziel-Rechner/USB verteilt.\n"
            f"  Gesamt-Kopiervorgänge: {count:,}\n\n"
            f"⚠ Ziel-Roots müssen erreichbar sein.\n\n"
            f"Jetzt ausführen?"
        )
        if not ok:
            return

        threading.Thread(
            target=self._run_copy, args=(to_copy,), daemon=True
        ).start()

    def _run_copy(self, to_copy):
        done, errors = 0, 0
        for entry, dst_roots in to_copy:
            src = Path(entry.get("dst", ""))   # staged Datei auf USB
            rel = entry["rel_path"]
            if not longpath(src).is_file():
                self._log(f"MISS {rel}")
                errors += 1
                continue
            for dst_root in dst_roots:
                dst = Path(dst_root) / rel
                ok, info = copy_verified(src, dst)
                if ok:
                    done += 1
                    self._log(f"OK   COPY → {dst_root}  {rel[:60]}")
                else:
                    errors += 1
                    self._log(f"ERR  {info}  {rel[:60]}")

        self.root.after(0, lambda: messagebox.showinfo(
            "COPY abgeschlossen",
            f"Fertig.\n  Kopiert: {done:,}\n  Fehler:  {errors:,}"
        ))

    def _get_win_roots(self) -> list[str]:
        """Fragt Win-Root ab falls nicht bekannt."""
        if hasattr(self, "_win_roots") and self._win_roots:
            return self._win_roots
        path = filedialog.askdirectory(
            title="Win-Wurzelverzeichnis wählen (z.B. Netzwerkpfad oder lokaler Ordner)"
        )
        self._win_roots = [path] if path else []
        return self._win_roots

    def _get_mac_roots(self) -> list[str]:
        if hasattr(self, "_mac_roots") and self._mac_roots:
            return self._mac_roots
        path = filedialog.askdirectory(
            title="Mac-Wurzelverzeichnis wählen (z.B. Netzwerkpfad oder Mountpunkt)"
        )
        self._mac_roots = [path] if path else []
        return self._mac_roots

    # ------------------------------------------------------------------
    # Ausführung: Löschungen (del Win / del Mac)
    # ------------------------------------------------------------------

    def _exec_deletions(self):
        confirm_count = sum(
            1 for k, v in self._decisions.items() if v == "confirm")
        restore_count = sum(
            1 for k, v in self._decisions.items() if v == "restore")

        ok = messagebox.askyesno(
            "Löschungen ausführen",
            f"Zusammenfassung:\n"
            f"  ✔ Löschen bestätigen:    {confirm_count:,} Ordner\n"
            f"  ↩ Wiederherstellen:      {restore_count:,} Ordner\n\n"
            f"Bestätigte Löschungen werden auf Mac + Win ausgeführt.\n"
            f"Wiederherstellungen werden von USB zurückgespielt.\n\n"
            f"⚠ Mac/Win müssen erreichbar sein.\n\n"
            f"Jetzt ausführen?"
        )
        if not ok:
            return

        threading.Thread(
            target=self._run_deletions, daemon=True
        ).start()

    def _run_deletions(self):
        done_del = done_restore = errors = 0

        # Ordner-Entscheidungen auf Einzel-Einträge anwenden
        for entry in self._del_entries:
            rel    = entry["rel_path"]
            act    = entry["action"]
            folder = top_folder(rel)
            key    = f"{act}::{folder}"
            dec    = self._decisions.get(key, "confirm")

            if dec == "confirm":
                # Löschen: auf Mac UND Win (je nach wer die Datei noch hat)
                # Der gesicherte USB-Pfad bleibt unangetastet
                targets = []
                if act == "CONFLICT del Win":
                    # Win hat gelöscht → Mac muss auch löschen
                    targets = self._get_mac_roots()
                elif act == "CONFLICT del Mac":
                    # Mac hat gelöscht → Win muss auch löschen
                    targets = self._get_win_roots()

                for t_root in targets:
                    target_file = Path(t_root) / rel
                    ok, info = delete_file(target_file)
                    if ok:
                        done_del += 1
                        self._log(f"DEL  {t_root}/{rel[:55]}")
                    else:
                        if "nicht gefunden" in info.lower() or "no such" in info.lower():
                            done_del += 1   # schon weg → ok
                        else:
                            errors += 1
                            self._log(f"ERR  {info}  {rel[:55]}")

            elif dec == "restore":
                # Wiederherstellen: von USB (dst im Report) auf Ziel-Rechner
                src = Path(entry.get("dst", ""))
                if not longpath(src).is_file():
                    self._log(f"MISS restore-Quelle  {rel[:60]}")
                    errors += 1
                    continue

                if act == "CONFLICT del Win":
                    targets = self._get_win_roots()
                elif act == "CONFLICT del Mac":
                    targets = self._get_mac_roots()
                else:
                    targets = []

                for t_root in targets:
                    dst = Path(t_root) / rel
                    ok, info = copy_verified(src, dst)
                    if ok:
                        done_restore += 1
                        self._log(f"RST  {t_root}/{rel[:55]}")
                    else:
                        errors += 1
                        self._log(f"ERR  {info}  {rel[:55]}")

        self.root.after(0, lambda: messagebox.showinfo(
            "Löschungen abgeschlossen",
            f"Fertig.\n"
            f"  Gelöscht:         {done_del:,}\n"
            f"  Wiederhergestellt: {done_restore:,}\n"
            f"  Fehler:            {errors:,}"
        ))

    # ------------------------------------------------------------------
    # Ausführung: DELETE (beide gelöscht)
    # ------------------------------------------------------------------

    def _exec_delete(self):
        to_del  = [e for e in self._delete_entries
                   if not self._delete_keep.get(e["rel_path"], False)]
        to_keep = [e for e in self._delete_entries
                   if self._delete_keep.get(e["rel_path"], False)]

        ok = messagebox.askyesno(
            "DELETE bestätigen",
            f"Auf USB löschen:  {len(to_del):,} Dateien\n"
            f"Behalten:         {len(to_keep):,} Dateien\n\n"
            f"Gelöschte Dateien sind danach nur noch in harvest/ vorhanden.\n\n"
            f"Jetzt ausführen?"
        )
        if not ok:
            return

        threading.Thread(
            target=self._run_delete, args=(to_del,), daemon=True
        ).start()

    def _run_delete(self, entries):
        done = errors = 0
        usb_roots = resolve_roots(self._report, self._usb_root)["usb"]

        for entry in entries:
            rel = entry["rel_path"]
            src = find_in_roots(usb_roots, rel)
            if src is None:
                self._log(f"MISS DELETE  {rel[:70]}")
                errors += 1
                continue
            ok, info = delete_file(src)
            if ok:
                done += 1
                self._log(f"DEL  {rel[:70]}")
            else:
                errors += 1
                self._log(f"ERR  {info}  {rel[:60]}")

        self.root.after(0, lambda: messagebox.showinfo(
            "DELETE abgeschlossen",
            f"  Von USB gelöscht: {done:,}\n  Fehler: {errors:,}"
        ))

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
            self.log_btn.config(state=tk.NORMAL)

        self.root.after(0, _upd)

    def _open_log(self):
        if not self._harvest_root:
            return
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        lp = self._harvest_root / f"execution_log_{ts}.txt"
        lp.write_text("".join(self._exec_log), encoding="utf-8")
        self._log_path = lp
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
