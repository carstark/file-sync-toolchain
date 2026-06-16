#!/usr/bin/env python3
"""
journal_checker.py - Vergleicht bis zu drei Journals (Mac, Win, USB)
======================================================================
Version 2.0 – USB als Ground Truth

USB ist der letzte bekannte gemeinsame Stand (Mittler).
Mac und Win sind die Arbeitsflächen seit dem letzten Sync.

Aktionsklassen:
  OK               – Überall identisch, kein Handlungsbedarf
  COPY MAC         – Neu auf Mac, auf USB+Win kopieren
  COPY WIN         – Neu auf Win, auf USB+Mac kopieren
  COPY             – Neu auf beiden Rechnern, identisch → auf USB kopieren
  DELETE           – Auf beiden Rechnern gelöscht → von USB entfernen
  CONFLICT modified – Auf Mac UND Win geändert, aber unterschiedlich
  CONFLICT new     – Neu auf Mac UND Win, aber unterschiedlich
  CONFLICT del Mac – Auf Mac gelöscht, auf Win aber geändert (oder umgekehrt)
  CONFLICT del Win – Auf Win gelöscht, auf Mac aber geändert (oder umgekehrt)

Alle CONFLICT-Einträge enthalten 'needs_resolution': true für Script 4.
"""

import json
import sys
import threading
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ==============================================================================
# AKTIONSKLASSEN
# ==============================================================================

A_OK           = "OK"
A_COPY_MAC     = "COPY MAC"
A_COPY_WIN     = "COPY WIN"
A_COPY         = "COPY"           # neu auf beiden, identisch
A_DELETE       = "DELETE"         # auf beiden Rechnern gelöscht
A_CONF_MOD     = "CONFLICT modified"
A_CONF_NEW     = "CONFLICT new"
A_CONF_DEL_MAC = "CONFLICT del Mac"
A_CONF_DEL_WIN = "CONFLICT del Win"

# Sortierpriorität für die Tabelle (kleinste Zahl = oben)
ACTION_ORDER = {
    A_CONF_MOD:     0,
    A_CONF_NEW:     1,
    A_CONF_DEL_MAC: 2,
    A_CONF_DEL_WIN: 3,
    A_COPY_MAC:     4,
    A_COPY_WIN:     5,
    A_COPY:         6,
    A_DELETE:       7,
    A_OK:           8,
}

# Farben für die Treeview-Tabelle
ACTION_COLORS = {
    A_CONF_MOD:     "#fde8d8",   # orange
    A_CONF_NEW:     "#fde8d8",
    A_CONF_DEL_MAC: "#fde8d8",
    A_CONF_DEL_WIN: "#fde8d8",
    A_COPY_MAC:     "#d6eaf8",   # blau
    A_COPY_WIN:     "#d6eaf8",
    A_COPY:         "#d6eaf8",
    A_DELETE:       "#fadbd8",   # rot
    A_OK:           "#d5f5e3",   # grün
}

DEVICE_SLOTS = ["mac", "win", "usb"]

MTIME_TOLERANCE = 2.0   # Sekunden – FAT32/exFAT rundet auf 2s


# ==============================================================================
# HILFSFUNKTIONEN
# ==============================================================================

def load_journal(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def flatten_journal(journal: dict) -> dict[str, dict]:
    """
    Gibt eine flache Map zurück:
    { "relativer/pfad/datei.ext": {"size": int, "mtime": float} }
    """
    flat = {}
    for folder_data in journal.get("folders", {}).values():
        for rel_path, meta in folder_data.get("files", {}).items():
            normalized = rel_path.replace("\\", "/")
            flat[normalized] = {"size": meta["size"], "mtime": meta["mtime"]}
    return flat


def same(a: dict, b: dict) -> bool:
    """Vergleicht zwei Metadaten-Einträge (Größe + mtime mit Toleranz)."""
    return (
        a["size"] == b["size"] and
        abs(a["mtime"] - b["mtime"]) < MTIME_TOLERANCE
    )


# ==============================================================================
# KERNLOGIK – USB ALS GROUND TRUTH
# ==============================================================================

def classify(presence: dict, per_source: dict, has_usb: bool) -> str:
    """
    Bestimmt die Aktionsklasse für eine Datei.

    presence   = {"mac": bool, "win": bool, "usb": bool}
    per_source = {slot: {"size":..,"mtime":..}} für vorhandene Slots
    has_usb    = ob ein USB-Journal geladen ist
    """
    on_mac = presence.get("mac", False)
    on_win = presence.get("win", False)
    on_usb = presence.get("usb", False)

    # ---------------------------------------------------------------
    # Modus: alle drei Journals vorhanden
    # ---------------------------------------------------------------
    if has_usb:

        # Alle drei vorhanden
        if on_mac and on_win and on_usb:
            mac_usb = same(per_source["mac"], per_source["usb"])
            win_usb = same(per_source["win"], per_source["usb"])
            if mac_usb and win_usb:
                return A_OK
            if mac_usb and not win_usb:
                return A_COPY_WIN   # Win hat geändert → Win-Version verteilen
            if win_usb and not mac_usb:
                return A_COPY_MAC   # Mac hat geändert → Mac-Version verteilen
            # Beide von USB abweichend
            return A_CONF_MOD

        # Nur auf USB (beide Rechner haben gelöscht)
        if not on_mac and not on_win and on_usb:
            return A_DELETE

        # Neu auf Mac (nicht auf USB, nicht auf Win)
        if on_mac and not on_win and not on_usb:
            return A_COPY_MAC

        # Neu auf Win (nicht auf USB, nicht auf Mac)
        if not on_mac and on_win and not on_usb:
            return A_COPY_WIN

        # Neu auf beiden, aber nicht auf USB
        if on_mac and on_win and not on_usb:
            if same(per_source["mac"], per_source["win"]):
                return A_COPY       # identisch → einfach auf USB
            return A_CONF_NEW       # verschieden → Nutzer entscheiden

        # Auf Mac + USB, aber nicht auf Win
        if on_mac and not on_win and on_usb:
            mac_usb = same(per_source["mac"], per_source["usb"])
            if mac_usb:
                # Win hat gelöscht, Mac ist unverändert → echte Löschung auf Win
                return A_CONF_DEL_WIN   # sicher melden, User bestätigt
            else:
                # Mac hat geändert UND Win hat gelöscht → echter Konflikt
                return A_CONF_DEL_WIN

        # Auf Win + USB, aber nicht auf Mac
        if not on_mac and on_win and on_usb:
            win_usb = same(per_source["win"], per_source["usb"])
            if win_usb:
                return A_CONF_DEL_MAC
            else:
                return A_CONF_DEL_MAC

        # Fallback (sollte nicht vorkommen)
        return A_CONF_MOD

    # ---------------------------------------------------------------
    # Modus: nur zwei Journals (ohne USB)
    # ---------------------------------------------------------------
    else:
        # Beide vorhanden
        if on_mac and on_win:
            if same(per_source["mac"], per_source["win"]):
                return A_OK
            return A_CONF_MOD

        if on_mac and not on_win:
            return A_COPY_MAC

        if not on_mac and on_win:
            return A_COPY_WIN

        return A_CONF_MOD  # Fallback


# ==============================================================================
# VERGLEICH
# ==============================================================================

def compare_journals(journals: dict[str, dict]) -> dict:
    """
    Vergleicht die übergebenen Journals und gibt ein change_directive zurück.
    """
    has_usb = "usb" in journals
    slot_names = list(journals.keys())

    flat: dict[str, dict[str, dict]] = {
        slot: flatten_journal(j) for slot, j in journals.items()
    }

    all_paths: set[str] = set()
    for files in flat.values():
        all_paths.update(files.keys())

    entries = []

    for rel_path in sorted(all_paths):
        presence   = {slot: (rel_path in flat[slot]) for slot in slot_names}
        per_source = {slot: flat[slot][rel_path]
                      for slot in slot_names if rel_path in flat[slot]}

        action = classify(presence, per_source, has_usb)

        entry = {
            "rel_path":        rel_path,
            "action":          action,
            "needs_resolution": action.startswith("CONFLICT"),
            "presence":        presence,
            "per_source":      per_source,
            "resolution":      None,   # befüllt durch Script 4
        }
        entries.append(entry)

    # Zusammenfassung
    all_actions = [e["action"] for e in entries]
    summary = {
        "total_files":      len(entries),
        "ok":               all_actions.count(A_OK),
        "copy_mac":         all_actions.count(A_COPY_MAC),
        "copy_win":         all_actions.count(A_COPY_WIN),
        "copy":             all_actions.count(A_COPY),
        "delete":           all_actions.count(A_DELETE),
        "conflict_modified":  all_actions.count(A_CONF_MOD),
        "conflict_new":       all_actions.count(A_CONF_NEW),
        "conflict_del_mac":   all_actions.count(A_CONF_DEL_MAC),
        "conflict_del_win":   all_actions.count(A_CONF_DEL_WIN),
        "total_conflicts":  sum(1 for e in entries if e["needs_resolution"]),
        "total_copies":     sum(1 for e in entries
                                if e["action"] in (A_COPY_MAC, A_COPY_WIN, A_COPY)),
    }

    sources_meta = {
        slot: {
            "device":    j.get("device", "?"),
            "timestamp": j.get("timestamp", "?"),
            "files":     j.get("summary", {}).get("total_files", 0),
            # Absolute Wurzelpfade – werden von Script 3 direkt genutzt
            "roots":     list(j.get("folders", {}).keys()),
        }
        for slot, j in journals.items()
    }

    return {
        "meta": {
            "created": datetime.now().isoformat(),
            "tool":    "journal_checker.py",
            "version": "2.1",
            "usb_as_ground_truth": has_usb,
        },
        "sources":  sources_meta,
        "slots":    slot_names,
        "entries":  entries,
        "summary":  summary,
    }


def save_directive(directive: dict, output_path: Path) -> Path:
    output_path.write_text(
        json.dumps(directive, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return output_path


# ==============================================================================
# GUI
# ==============================================================================

class CheckerApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🔍 Journal Checker v2")
        self.root.geometry("900x700")
        self.root.resizable(True, True)

        self.journal_vars   = {slot: tk.StringVar() for slot in DEVICE_SLOTS}
        self._directive     = None
        self._auto_save_dir = None   # wird aus USB-Journal abgeleitet

        self._build_ui()
        self._auto_find_journals()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg="#2c3e50", height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="🔍 Journal Checker – USB als Ground Truth",
                 font=("Helvetica", 15, "bold"),
                 bg="#2c3e50", fg="white").pack(pady=15)

        # Journal-Auswahl
        sel_frame = tk.LabelFrame(self.root, text="Journals auswählen",
                                  padx=10, pady=8)
        sel_frame.pack(fill=tk.X, padx=10, pady=(10, 4))

        labels = {"mac": "Mac ", "win": "Win ", "usb": "USB "}
        for slot in DEVICE_SLOTS:
            row = tk.Frame(sel_frame)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=f"{labels[slot]}:",
                     font=("Helvetica", 11, "bold"),
                     width=5, anchor="w").pack(side=tk.LEFT)
            tk.Entry(row, textvariable=self.journal_vars[slot],
                     font=("Courier", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Button(row, text="📂",
                      command=lambda s=slot: self._pick_journal(s),
                      width=3).pack(side=tk.LEFT, padx=(4, 0))
            tk.Button(row, text="✖",
                      command=lambda s=slot: self.journal_vars[s].set(""),
                      width=3).pack(side=tk.LEFT, padx=2)

        tk.Label(sel_frame,
                 text="USB ist Ground Truth. Mac/Win sind die Arbeitsflächen. "
                      "Min. 2 Journals erforderlich.",
                 font=("Helvetica", 9), fg="#555").pack(anchor="w", pady=(4, 0))

        # Speicherort – wird automatisch aus USB-Journal abgeleitet
        save_frame = tk.LabelFrame(self.root,
                                   text="Speicherort für change_directive  "
                                        "(automatisch aus USB-Journal)",
                                   padx=10, pady=4)
        save_frame.pack(fill=tk.X, padx=10, pady=4)
        self.save_display_var = tk.StringVar(value="(wird nach Journal-Auswahl gesetzt)")
        tk.Label(save_frame, textvariable=self.save_display_var,
                 font=("Courier", 9), anchor="w", fg="#555"
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(save_frame, text="✏ ändern",
                  command=self._choose_save_dir,
                  width=10).pack(side=tk.RIGHT, padx=4)

        # Notebook: Zusammenfassung + Einträge
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # Tab 1: Zusammenfassung
        self.summary_text = tk.Text(nb, font=("Courier", 10),
                                    state=tk.DISABLED)
        nb.add(self.summary_text, text="Zusammenfassung")

        # Tab 2: Einträge
        tree_frame = tk.Frame(nb)
        nb.add(tree_frame, text="Einträge")

        cols = ("action", "presence", "rel_path")
        self.tree = ttk.Treeview(tree_frame, columns=cols,
                                 show="headings", selectmode="browse")
        self.tree.heading("action",   text="Aktion")
        self.tree.heading("presence", text="Quellen")
        self.tree.heading("rel_path", text="Relativer Pfad")
        self.tree.column("action",   width=160, anchor="center", stretch=False)
        self.tree.column("presence", width=140, anchor="center", stretch=False)
        self.tree.column("rel_path", width=580)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                             command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal",
                             command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(fill=tk.BOTH, expand=True)

        for action, color in ACTION_COLORS.items():
            self.tree.tag_configure(action, background=color)

        # Statusleiste
        self.status_var = tk.StringVar(value="Bereit.")
        tk.Label(self.root, textvariable=self.status_var,
                 anchor="w", font=("Helvetica", 10),
                 relief=tk.SUNKEN).pack(fill=tk.X, padx=10, pady=(0, 2))

        # Buttons
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=10, pady=8)

        self.run_btn = tk.Button(btn_frame, text="▶️  Journals vergleichen",
                                  command=self._start_check,
                                  font=("Helvetica", 12, "bold"),
                                  bg="#2980b9", fg="white",
                                  height=2, width=25)
        self.run_btn.pack(side=tk.LEFT, padx=5)

        self.save_btn = tk.Button(btn_frame, text="💾  Directive speichern",
                                   command=self._save_directive,
                                   font=("Helvetica", 12),
                                   state=tk.DISABLED,
                                   height=2, width=22)
        self.save_btn.pack(side=tk.LEFT, padx=5)

    # ------------------------------------------------------------------
    # Datei-Picker & Auto-Suche
    # ------------------------------------------------------------------

    def _auto_find_journals(self):
        """
        Sucht beim Start automatisch nach Journals im _sync-Ordner des USB.
        Erkennt USB-Laufwerke anhand von _sync-Unterordnern.
        Nimmt je Slot das neueste Journal.
        """
        candidates = []

        # Typische Mount-Punkte je Plattform durchsuchen
        import platform
        system = platform.system()
        if system == "Darwin":
            search_roots = list(Path("/Volumes").iterdir()) if Path("/Volumes").exists() else []
        elif system == "Windows":
            import string
            search_roots = [Path(f"{d}:\\") for d in string.ascii_uppercase
                            if Path(f"{d}:\\").exists()]
        else:
            search_roots = list(Path("/media").rglob("*")) + list(Path("/mnt").rglob("*"))

        for root in search_roots:
            sync = root / "_sync"
            if sync.is_dir():
                # Neuestes Datum-Unterverzeichnis
                dated = sorted(
                    [d for d in sync.iterdir() if d.is_dir()],
                    reverse=True
                )
                for d in dated:
                    for jfile in sorted(d.glob("journal_*.json"), reverse=True):
                        candidates.append(jfile)
                break  # erstes USB mit _sync nehmen

        # Auch neben dem Script selbst suchen (falls Directive schon da liegt)
        script_dir = Path(__file__).parent
        for jfile in sorted(script_dir.rglob("journal_*.json"), reverse=True):
            if jfile not in candidates:
                candidates.append(jfile)

        # Journals den Slots zuordnen (anhand Dateiname: journal_<device>_<datum>.json)
        assigned = set()
        for slot in DEVICE_SLOTS:
            for jfile in candidates:
                name_lower = jfile.stem.lower()
                if slot in name_lower and jfile not in assigned:
                    self.journal_vars[slot].set(str(jfile))
                    assigned.add(jfile)
                    break

        # Speicherort aus USB-Journal ableiten
        self._derive_save_dir()

    def _derive_save_dir(self):
        """Leitet den Speicherpfad für die Directive aus dem USB-Journal ab."""
        usb_path = self.journal_vars["usb"].get().strip()
        if usb_path and Path(usb_path).is_file():
            self._auto_save_dir = Path(usb_path).parent
            self.save_display_var.set(str(self._auto_save_dir))
        else:
            # Fallback: aus irgendeinem gefundenen Journal
            for slot in DEVICE_SLOTS:
                p = self.journal_vars[slot].get().strip()
                if p and Path(p).is_file():
                    self._auto_save_dir = Path(p).parent
                    self.save_display_var.set(
                        str(self._auto_save_dir) + "  (kein USB-Journal gefunden)"
                    )
                    break

    def _pick_journal(self, slot: str):
        # Startverzeichnis: bereits gesetztes Journal oder _sync auf USB
        current = self.journal_vars[slot].get().strip()
        init = str(Path(current).parent) if current else str(Path.home())

        path = filedialog.askopenfilename(
            title=f"Journal für {slot.upper()} wählen...",
            initialdir=init,
            filetypes=[("JSON", "*.json"), ("Alle Dateien", "*.*")]
        )
        if path:
            self.journal_vars[slot].set(path)
            self._derive_save_dir()

    def _choose_save_dir(self):
        d = filedialog.askdirectory(
            title="Speicherort wählen...",
            initialdir=str(self._auto_save_dir) if self._auto_save_dir else str(Path.home())
        )
        if d:
            self._auto_save_dir = Path(d)
            self.save_display_var.set(str(self._auto_save_dir))

    # ------------------------------------------------------------------
    # Vergleich
    # ------------------------------------------------------------------

    def _start_check(self):
        journals = {}
        for slot in DEVICE_SLOTS:
            p = self.journal_vars[slot].get().strip()
            if not p:
                continue
            path = Path(p)
            if not path.is_file():
                messagebox.showerror("Fehler", f"Journal nicht gefunden:\n{p}")
                return
            try:
                journals[slot] = load_journal(path)
            except Exception as exc:
                messagebox.showerror("Lesefehler",
                                     f"Konnte {Path(p).name} nicht lesen:\n{exc}")
                return

        if len(journals) < 2:
            messagebox.showwarning("Zu wenig Journals",
                                   "Bitte mindestens 2 Journals angeben.")
            return

        self.run_btn.config(state=tk.DISABLED)
        self.save_btn.config(state=tk.DISABLED)
        self._directive = None
        self.status_var.set("⏳ Vergleiche Journals …")

        threading.Thread(
            target=self._run_check, args=(journals,), daemon=True
        ).start()

    def _run_check(self, journals):
        try:
            directive = compare_journals(journals)
        except Exception as exc:
            self.root.after(0, lambda: messagebox.showerror(
                "Fehler", f"Vergleich fehlgeschlagen:\n{exc}"
            ))
            self.root.after(0, lambda: self.run_btn.config(state=tk.NORMAL))
            return
        self.root.after(0, lambda: self._show_results(directive))

    def _show_results(self, directive: dict):
        self._directive = directive
        s  = directive["summary"]
        sr = directive["sources"]

        # Zusammenfassung aufbauen
        lines = ["Verglichene Quellen:"]
        for slot, meta in sr.items():
            lines.append(
                f"  {slot.upper():4s}  Gerät: {meta['device']:<22s}  "
                f"Dateien: {meta['files']:>7,}  "
                f"Stand: {meta['timestamp'][:19]}"
            )

        usb_hint = ("✔ USB als Ground Truth aktiv"
                    if directive["meta"]["usb_as_ground_truth"]
                    else "⚠ Kein USB-Journal – symmetrischer Vergleich")

        lines += [
            "",
            usb_hint,
            "",
            f"Gesamt:              {s['total_files']:>7,}",
            f"OK:                  {s['ok']:>7,}",
            f"─" * 38,
            f"COPY MAC:            {s['copy_mac']:>7,}",
            f"COPY WIN:            {s['copy_win']:>7,}",
            f"COPY (beide gleich): {s['copy']:>7,}",
            f"DELETE:              {s['delete']:>7,}",
            f"─" * 38,
            f"CONFLICT modified:   {s['conflict_modified']:>7,}",
            f"CONFLICT new:        {s['conflict_new']:>7,}",
            f"CONFLICT del Mac:    {s['conflict_del_mac']:>7,}",
            f"CONFLICT del Win:    {s['conflict_del_win']:>7,}",
            f"─" * 38,
            f"Gesamt Conflicts:    {s['total_conflicts']:>7,}",
            f"Gesamt Copies:       {s['total_copies']:>7,}",
        ]

        self.summary_text.config(state=tk.NORMAL)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert(tk.END, "\n".join(lines))
        self.summary_text.config(state=tk.DISABLED)

        # Tabelle füllen
        self.tree.delete(*self.tree.get_children())
        slot_names = directive["slots"]

        def presence_str(presence):
            return "  ".join(
                f"{sl}:{'✔' if presence.get(sl) else '✖'}"
                for sl in slot_names
            )

        sorted_entries = sorted(
            directive["entries"],
            key=lambda e: (ACTION_ORDER.get(e["action"], 99), e["rel_path"])
        )

        for entry in sorted_entries:
            self.tree.insert(
                "", tk.END,
                values=(
                    entry["action"],
                    presence_str(entry["presence"]),
                    entry["rel_path"],
                ),
                tags=(entry["action"],)
            )

        self.status_var.set(
            f"✅ Abgeschlossen – "
            f"OK: {s['ok']}  |  "
            f"Copies: {s['total_copies']}  |  "
            f"Conflicts: {s['total_conflicts']}  |  "
            f"Delete: {s['delete']}"
        )
        self.run_btn.config(state=tk.NORMAL)
        self.save_btn.config(state=tk.NORMAL)

    # ------------------------------------------------------------------
    # Speichern
    # ------------------------------------------------------------------

    def _save_directive(self):
        if not self._directive:
            return

        if not self._auto_save_dir:
            messagebox.showwarning(
                "Kein Speicherort",
                "Kein Speicherort ermittelt.\n"
                "Bitte mindestens ein Journal laden."
            )
            return

        ts       = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"change_directive_{ts}.json"
        out_path = Path(self._auto_save_dir) / filename
        try:
            save_directive(self._directive, out_path)
        except Exception as exc:
            messagebox.showerror("Speicherfehler", str(exc))
            return
        messagebox.showinfo("Gespeichert",
                            f"change_directive gespeichert:\n\n{out_path}")
        self.status_var.set(f"💾 Gespeichert: {filename}")

    def run(self):
        self.root.mainloop()


# ==============================================================================
# CLI
# ==============================================================================

def cli_mode(args: list[str]):
    import argparse
    parser = argparse.ArgumentParser(
        description="journal_checker.py v2 – USB als Ground Truth"
    )
    parser.add_argument("--mac",    help="Journal Mac")
    parser.add_argument("--win",    help="Journal Windows")
    parser.add_argument("--usb",    help="Journal USB (Ground Truth)")
    parser.add_argument("--output", help="Ausgabeordner")
    parsed = parser.parse_args(args)

    journals = {}
    for slot, path_str in {"mac": parsed.mac,
                            "win": parsed.win,
                            "usb": parsed.usb}.items():
        if path_str:
            p = Path(path_str)
            if not p.is_file():
                print(f"❌ Nicht gefunden: {p}"); sys.exit(1)
            print(f"📖 Lese {slot.upper()}: {p.name}")
            journals[slot] = load_journal(p)

    if len(journals) < 2:
        print("❌ Mindestens 2 Journals erforderlich."); sys.exit(1)

    print("\n⏳ Vergleiche …")
    directive = compare_journals(journals)
    s = directive["summary"]

    print(f"\n{'='*45}")
    print(f"  Gesamt:              {s['total_files']:>7,}")
    print(f"  OK:                  {s['ok']:>7,}")
    print(f"  COPY MAC:            {s['copy_mac']:>7,}")
    print(f"  COPY WIN:            {s['copy_win']:>7,}")
    print(f"  COPY (beide gleich): {s['copy']:>7,}")
    print(f"  DELETE:              {s['delete']:>7,}")
    print(f"  CONFLICT modified:   {s['conflict_modified']:>7,}")
    print(f"  CONFLICT new:        {s['conflict_new']:>7,}")
    print(f"  CONFLICT del Mac:    {s['conflict_del_mac']:>7,}")
    print(f"  CONFLICT del Win:    {s['conflict_del_win']:>7,}")
    print(f"{'='*45}")

    ts       = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir  = Path(parsed.output) if parsed.output else Path.cwd()
    out_path = out_dir / f"change_directive_{ts}.json"
    save_directive(directive, out_path)
    print(f"\n✅ Gespeichert: {out_path}")


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    if "--cli" in sys.argv:
        args = sys.argv[1:]
        args.remove("--cli")
        cli_mode(args)
    else:
        app = CheckerApp()
        app.run()
