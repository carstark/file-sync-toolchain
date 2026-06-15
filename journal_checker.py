#!/usr/bin/env python3
"""
journal_checker.py - Vergleicht bis zu drei Journals (Mac, Win, USB)
======================================================================
Eingabe:  Bis zu 3 Journal-JSON-Dateien aus current_journal.py
Ausgabe:  change_directive_{timestamp}.json

Logik:
  - Gleiche Dateien (Pfad + Größe + mtime identisch auf allen Quellen)
    → still überspringen.
  - Datei existiert nur auf einem Gerät (neu oder gelöscht)
    → ACTION_COPY oder ACTION_DELETE (je nach Kontext).
  - Datei existiert auf mehreren Geräten, aber Größe oder mtime abweichend
    → CONFLICT → Nutzer muss entscheiden.

Das Ergebnis ist ein strukturiertes change_directive, das Script 3
(file_harvester.py) direkt einlesen kann.
"""

import json
import sys
import threading
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ==============================================================================
# DATENSTRUKTUREN
# ==============================================================================

DEVICE_SLOTS = ["mac", "win", "usb"]  # logische Namen

# Aktions-Konstanten
ACTION_OK       = "OK"         # Überall identisch → kein Handlungsbedarf
ACTION_COPY     = "COPY"       # Nur auf einer Quelle → auf fehlende Ziele kopieren
ACTION_CONFLICT = "CONFLICT"   # Auf mehreren Quellen, aber unterschiedlich → Nutzer entscheiden


# ==============================================================================
# JOURNAL LESEN
# ==============================================================================

def load_journal(path: Path) -> dict:
    """Liest ein Journal-JSON und gibt es als dict zurück."""
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def flatten_journal(journal: dict) -> dict[str, dict]:
    """
    Extrahiert aus dem Journal eine flache Datei-Map:
    { "relativer/pfad/datei.ext": {"size": int, "mtime": float} }

    Der relative Pfad kommt direkt aus dem Journal (so wie current_journal.py
    ihn gesetzt hat: relativ zum gescannten Ordner).
    """
    flat = {}
    for folder_abs, folder_data in journal.get("folders", {}).items():
        for rel_path, meta in folder_data.get("files", {}).items():
            # Pfad-Separator normalisieren (Windows vs Unix)
            normalized = rel_path.replace("\\", "/")
            flat[normalized] = {
                "size":  meta["size"],
                "mtime": meta["mtime"],
            }
    return flat


# ==============================================================================
# VERGLEICHSLOGIK
# ==============================================================================

def compare_journals(journals: dict[str, dict]) -> dict:
    """
    Vergleicht die übergebenen Journals.

    journals = {
        "mac": <journal_dict>,
        "win": <journal_dict>,   # optional
        "usb": <journal_dict>,   # optional
    }

    Gibt ein change_directive zurück:
    {
        "meta": { ... },
        "sources": { "mac": {...}, "win": {...}, ... },
        "entries": [
            {
                "rel_path": "...",
                "action": "OK" | "COPY" | "CONFLICT",
                "presence": { "mac": True/False, "win": True/False, ... },
                "per_source": { "mac": {"size":..,"mtime":..}, ... },
                "resolution": null,   # wird von Script 3/4 befüllt
            },
            ...
        ],
        "summary": {
            "total_files":   int,
            "ok":            int,
            "copy":          int,
            "conflict":      int,
        }
    }
    """
    slot_names = list(journals.keys())

    # Journals flach machen
    flat: dict[str, dict[str, dict]] = {
        slot: flatten_journal(j) for slot, j in journals.items()
    }

    # Alle bekannten Pfade sammeln
    all_paths: set[str] = set()
    for files in flat.values():
        all_paths.update(files.keys())

    entries = []

    for rel_path in sorted(all_paths):
        presence   = {slot: (rel_path in flat[slot]) for slot in slot_names}
        per_source = {slot: flat[slot][rel_path]
                      for slot in slot_names if rel_path in flat[slot]}

        present_slots = [s for s, p in presence.items() if p]

        # --- Überall gleich? ---
        if len(present_slots) == len(slot_names):
            # Datei ist auf allen Quellen vorhanden → Werte vergleichen
            values = list(per_source.values())
            all_same = all(
                v["size"] == values[0]["size"] and
                abs(v["mtime"] - values[0]["mtime"]) < 2.0   # 2s Toleranz für FAT32/exFAT
                for v in values
            )
            if all_same:
                action = ACTION_OK
            else:
                action = ACTION_CONFLICT
        elif len(present_slots) == 1:
            # Nur auf einer Quelle → Kopieraktion
            action = ACTION_COPY
        else:
            # Auf mehreren, aber nicht allen Quellen → Konflikt
            action = ACTION_CONFLICT

        entry = {
            "rel_path":   rel_path,
            "action":     action,
            "presence":   presence,
            "per_source": per_source,
            "resolution": None,  # Platzhalter für Script 4
        }
        entries.append(entry)

    # --- Zusammenfassung ---
    summary = {
        "total_files": len(entries),
        "ok":          sum(1 for e in entries if e["action"] == ACTION_OK),
        "copy":        sum(1 for e in entries if e["action"] == ACTION_COPY),
        "conflict":    sum(1 for e in entries if e["action"] == ACTION_CONFLICT),
    }

    # --- Quell-Metadaten ---
    sources_meta = {}
    for slot, j in journals.items():
        sources_meta[slot] = {
            "device":    j.get("device", "?"),
            "timestamp": j.get("timestamp", "?"),
            "files":     j.get("summary", {}).get("total_files", 0),
        }

    directive = {
        "meta": {
            "created":  datetime.now().isoformat(),
            "tool":     "journal_checker.py",
            "version":  "1.0",
        },
        "sources":  sources_meta,
        "slots":    slot_names,
        "entries":  entries,
        "summary":  summary,
    }

    return directive


def save_directive(directive: dict, output_path: Path) -> Path:
    """Speichert die change_directive als JSON (UTF-8)."""
    text = json.dumps(directive, indent=2, ensure_ascii=False)
    output_path.write_text(text, encoding="utf-8")
    return output_path


# ==============================================================================
# GUI
# ==============================================================================

class CheckerApp:
    """tkinter GUI für journal_checker."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🔍 Journal Checker")
        self.root.geometry("800x660")
        self.root.resizable(True, True)

        # Journal-Pfade je Slot (mac, win, usb)
        self.journal_vars: dict[str, tk.StringVar] = {
            slot: tk.StringVar() for slot in DEVICE_SLOTS
        }
        self.save_dir_var = tk.StringVar(value=str(Path.home()))
        self.cancel_event = threading.Event()

        self._build_ui()

    # ------------------------------------------------------------------
    # UI-Aufbau
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg="#2c3e50", height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="🔍 Journal Checker – Vergleich",
                 font=("Helvetica", 16, "bold"),
                 bg="#2c3e50", fg="white").pack(pady=15)

        # Journal-Auswahl
        sel_frame = tk.LabelFrame(self.root, text="Journals auswählen",
                                  padx=10, pady=10)
        sel_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        labels = {"mac": "Mac  ", "win": "Win  ", "usb": "USB  "}
        for slot in DEVICE_SLOTS:
            row = tk.Frame(sel_frame)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=f"{labels[slot]}:",
                     font=("Helvetica", 11, "bold"), width=6,
                     anchor="w").pack(side=tk.LEFT)
            tk.Entry(row, textvariable=self.journal_vars[slot],
                     font=("Courier", 9)).pack(side=tk.LEFT,
                                               fill=tk.X, expand=True)
            tk.Button(row, text="📂",
                      command=lambda s=slot: self._pick_journal(s),
                      width=3).pack(side=tk.LEFT, padx=(4, 0))
            tk.Button(row, text="✖",
                      command=lambda s=slot: self.journal_vars[s].set(""),
                      width=3).pack(side=tk.LEFT, padx=2)

        tk.Label(sel_frame, text="(Mindestens 2 Journals erforderlich. "
                                  "Leere Felder werden ignoriert.)",
                 font=("Helvetica", 9), fg="#555").pack(anchor="w", pady=(6, 0))

        # Speicherort
        save_frame = tk.LabelFrame(self.root, text="Speicherort für change_directive",
                                   padx=10, pady=5)
        save_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Entry(save_frame, textvariable=self.save_dir_var,
                 font=("Courier", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(save_frame, text="📂",
                  command=self._choose_save_dir,
                  width=3).pack(side=tk.RIGHT, padx=5)

        # Ergebnis-Tabelle (Notebook mit zwei Tabs)
        result_nb = ttk.Notebook(self.root)
        result_nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Tab 1: Zusammenfassung
        self.summary_text = tk.Text(result_nb, font=("Courier", 10),
                                    state=tk.DISABLED, height=6)
        result_nb.add(self.summary_text, text="Zusammenfassung")

        # Tab 2: Einträge
        tree_frame = tk.Frame(result_nb)
        result_nb.add(tree_frame, text="Einträge")

        columns = ("action", "presence", "rel_path")
        self.tree = ttk.Treeview(tree_frame, columns=columns,
                                 show="headings", selectmode="browse")
        self.tree.heading("action",   text="Aktion")
        self.tree.heading("presence", text="Quellen")
        self.tree.heading("rel_path", text="Relativer Pfad")
        self.tree.column("action",   width=90,  anchor="center", stretch=False)
        self.tree.column("presence", width=130, anchor="center", stretch=False)
        self.tree.column("rel_path", width=560)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                             command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal",
                             command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(fill=tk.BOTH, expand=True)

        # Farben für Aktionen
        self.tree.tag_configure("OK",       background="#d5f5e3")
        self.tree.tag_configure("COPY",     background="#d6eaf8")
        self.tree.tag_configure("CONFLICT", background="#fde8d8")

        # Statuszeile
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

        self._directive = None  # wird nach Vergleich befüllt

    # ------------------------------------------------------------------
    # Datei-Auswahl
    # ------------------------------------------------------------------

    def _pick_journal(self, slot: str):
        path = filedialog.askopenfilename(
            title=f"Journal für {slot.upper()} wählen...",
            filetypes=[("JSON", "*.json"), ("Alle Dateien", "*.*")]
        )
        if path:
            self.journal_vars[slot].set(path)

    def _choose_save_dir(self):
        d = filedialog.askdirectory(
            title="Speicherort wählen...",
            initialdir=self.save_dir_var.get() or str(Path.home())
        )
        if d:
            self.save_dir_var.set(d)

    # ------------------------------------------------------------------
    # Vergleich
    # ------------------------------------------------------------------

    def _start_check(self):
        # Journals einlesen
        journals = {}
        for slot in DEVICE_SLOTS:
            p = self.journal_vars[slot].get().strip()
            if not p:
                continue
            path = Path(p)
            if not path.is_file():
                messagebox.showerror("Fehler",
                                     f"Journal nicht gefunden:\n{p}")
                return
            try:
                journals[slot] = load_journal(path)
            except Exception as exc:
                messagebox.showerror("Lesefehler",
                                     f"Konnte {path.name} nicht lesen:\n{exc}")
                return

        if len(journals) < 2:
            messagebox.showwarning("Zu wenig Journals",
                                   "Bitte mindestens 2 Journals angeben.")
            return

        self.run_btn.config(state=tk.DISABLED)
        self.status_var.set("⏳ Vergleiche Journals …")
        self._directive = None
        self.save_btn.config(state=tk.DISABLED)

        thread = threading.Thread(
            target=self._run_check, args=(journals,), daemon=True
        )
        thread.start()

    def _run_check(self, journals: dict):
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
        summary = directive["summary"]

        # Zusammenfassung
        s = directive["sources"]
        lines = ["Verglichene Quellen:"]
        for slot, meta in s.items():
            lines.append(
                f"  {slot.upper():4s}  Gerät: {meta['device']:<20s}  "
                f"Dateien: {meta['files']:>6,}  "
                f"Stand: {meta['timestamp'][:19]}"
            )
        lines += [
            "",
            f"Gesamt:    {summary['total_files']:>7,}  Dateien",
            f"OK:        {summary['ok']:>7,}  (überall identisch)",
            f"COPY:      {summary['copy']:>7,}  (fehlt auf mindestens einer Quelle)",
            f"CONFLICT:  {summary['conflict']:>7,}  (abweichende Version)",
        ]
        self.summary_text.config(state=tk.NORMAL)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert(tk.END, "\n".join(lines))
        self.summary_text.config(state=tk.DISABLED)

        # Tabelle füllen (OK-Einträge zuletzt, damit COPYs + CONFLICTs oben stehen)
        self.tree.delete(*self.tree.get_children())
        slot_names = directive["slots"]

        def presence_str(presence: dict) -> str:
            parts = []
            for slot in slot_names:
                icon = "✔" if presence.get(slot) else "✖"
                parts.append(f"{slot}:{icon}")
            return "  ".join(parts)

        # Sortierung: CONFLICT → COPY → OK
        order = {ACTION_CONFLICT: 0, ACTION_COPY: 1, ACTION_OK: 2}
        sorted_entries = sorted(
            directive["entries"],
            key=lambda e: (order[e["action"]], e["rel_path"])
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
            f"✅ Vergleich abgeschlossen – "
            f"OK: {summary['ok']}  |  "
            f"COPY: {summary['copy']}  |  "
            f"CONFLICT: {summary['conflict']}"
        )
        self.run_btn.config(state=tk.NORMAL)
        self.save_btn.config(state=tk.NORMAL)

    # ------------------------------------------------------------------
    # Speichern
    # ------------------------------------------------------------------

    def _save_directive(self):
        if not self._directive:
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"change_directive_{ts}.json"
        output_path = Path(self.save_dir_var.get()) / filename

        try:
            save_directive(self._directive, output_path)
        except Exception as exc:
            messagebox.showerror("Speicherfehler", str(exc))
            return

        messagebox.showinfo(
            "Gespeichert",
            f"change_directive gespeichert:\n\n{output_path}"
        )
        self.status_var.set(f"💾 Gespeichert: {filename}")

    def run(self):
        self.root.mainloop()


# ==============================================================================
# CLI MODE
# ==============================================================================

def cli_mode(args: list[str]):
    """
    Kommandozeilen-Modus.

    Beispiel:
        python journal_checker.py \\
            --mac journal_MacBook_2025.json \\
            --win journal_WinPC_2025.json \\
            --usb journal_USB_2025.json \\
            --output /path/to/output/
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="journal_checker.py – Vergleicht bis zu 3 Journals"
    )
    parser.add_argument("--mac",    help="Journal-Datei für Mac")
    parser.add_argument("--win",    help="Journal-Datei für Windows")
    parser.add_argument("--usb",    help="Journal-Datei für USB")
    parser.add_argument("--output", help="Ausgabeordner (Standard: aktuelles Verzeichnis)")

    parsed = parser.parse_args(args)

    journals = {}
    slot_map = {"mac": parsed.mac, "win": parsed.win, "usb": parsed.usb}
    for slot, path_str in slot_map.items():
        if path_str:
            p = Path(path_str)
            if not p.is_file():
                print(f"❌ Datei nicht gefunden: {p}")
                sys.exit(1)
            print(f"📖 Lese {slot.upper()}: {p.name}")
            journals[slot] = load_journal(p)

    if len(journals) < 2:
        print("❌ Mindestens 2 Journals erforderlich.")
        sys.exit(1)

    print("\n⏳ Vergleiche …")
    directive = compare_journals(journals)
    s = directive["summary"]

    print(f"\n{'='*60}")
    print(f"  Gesamt:    {s['total_files']:>7,}")
    print(f"  OK:        {s['ok']:>7,}")
    print(f"  COPY:      {s['copy']:>7,}")
    print(f"  CONFLICT:  {s['conflict']:>7,}")
    print(f"{'='*60}")

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = Path(parsed.output) if parsed.output else Path.cwd()
    output_path = out_dir / f"change_directive_{ts}.json"
    save_directive(directive, output_path)
    print(f"\n✅ Gespeichert: {output_path}")


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
