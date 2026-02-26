#!/usr/bin/env python3
"""
current_journal.py - Erstellt ein Metadaten-Journal für ausgewählte Ordner
===========================================================================
Nur: relativer Pfad, Dateigröße, mtime (kein Hashing).
Perfekt als schneller Snapshot für spätere Vergleiche.

Ausgabe: journal_{device}_{datum}.json
"""

import json
import platform
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

# --- GUI Imports ---
import tkinter as tk
from tkinter import filedialog, messagebox

# ==============================================================================
# CONFIG
# ==============================================================================

DEVICE_NAME = platform.node()
TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H%M%S")
JOURNAL_FILENAME = f"journal_{DEVICE_NAME}_{TIMESTAMP}.json"

# Dateien/Ordner, die ignoriert werden
IGNORE_PATTERNS = [
    '.DS_Store',
    'Thumbs.db',
    '*.tmp',
    '*.lock',
    '__pycache__',
    '.sync',
    '.git',
    '*.partial',
    '$RECYCLE.BIN',
    '.Trash',
    '.Spotlight-V100',
    '.fseventsd',
    'desktop.ini',
]

# ==============================================================================
# IGNORE LOGIC
# ==============================================================================

def should_ignore(filepath: Path) -> bool:
    """Prüfe ob Datei/Ordner ignoriert werden soll."""
    name = filepath.name
    filepath_str = str(filepath)

    for pattern in IGNORE_PATTERNS:
        if pattern.startswith('*'):
            # Wildcard: *.tmp → prüfe Endung
            if name.endswith(pattern[1:]):
                return True
        elif pattern in filepath_str or name == pattern:
            return True
    return False


# ==============================================================================
# SCANNING
# ==============================================================================

def scan_folder(folder_path: Path,
                progress_callback=None,
                cancel_event=None) -> dict:
    """
    Scanne einen Ordner und erfasse Metadaten aller Dateien.

    Returns:
        dict: {relative_path: {"size": int, "mtime": float}}
    """
    files = {}
    file_list = []

    # Erst alle Dateien sammeln (für Fortschrittsanzeige)
    for filepath in folder_path.rglob('*'):
        if cancel_event and cancel_event.is_set():
            return files
        if filepath.is_file() and not should_ignore(filepath):
            file_list.append(filepath)

    total = len(file_list)

    for i, filepath in enumerate(file_list):
        if cancel_event and cancel_event.is_set():
            return files

        try:
            stat = filepath.stat()
            rel_path = str(filepath.relative_to(folder_path))

            entry = {
                'size': stat.st_size,
                'mtime': stat.st_mtime,
            }

            files[rel_path] = entry

        except (IOError, OSError, PermissionError):
            pass  # Unlesbare Dateien überspringen

        # Progress Callback (ordnerintern)
        if progress_callback and (i % 50 == 0 or i == total - 1):
            progress_callback(i + 1, total, filepath.name)

    return files


# ==============================================================================
# JOURNAL
# ==============================================================================

def create_journal(folders: list[Path],
                   folder_progress_callback=None,
                   file_progress_callback=None,
                   cancel_event=None) -> dict:
    """
    Erstelle ein vollständiges Journal über alle Ordner.

    Returns:
        dict: Das komplette Journal als Dictionary
    """
    journal = {
        'device': DEVICE_NAME,
        'timestamp': datetime.now().isoformat(),
        'hash_algorithm': 'none',
        'folders': {},
        'summary': {
            'total_files': 0,
            'total_size_bytes': 0,
        }
    }

    total_folders = len(folders)

    for idx, folder in enumerate(folders):
        if cancel_event and cancel_event.is_set():
            break

        folder_name = folder.name
        if folder_progress_callback:
            folder_progress_callback(idx + 1, total_folders, folder_name)

        files = scan_folder(
            folder,
            progress_callback=file_progress_callback,
            cancel_event=cancel_event
        )

        journal['folders'][str(folder)] = {
            'folder_name': folder_name,
            'file_count': len(files),
            'files': files
        }

        journal['summary']['total_files'] += len(files)
        journal['summary']['total_size_bytes'] += sum(
            f['size'] for f in files.values()
        )

    # Menschenlesbare Größe
    total_bytes = journal['summary']['total_size_bytes']
    if total_bytes > 1e12:
        journal['summary']['total_size_human'] = f"{total_bytes/1e12:.2f} TB"
    elif total_bytes > 1e9:
        journal['summary']['total_size_human'] = f"{total_bytes/1e9:.2f} GB"
    elif total_bytes > 1e6:
        journal['summary']['total_size_human'] = f"{total_bytes/1e6:.2f} MB"
    else:
        journal['summary']['total_size_human'] = f"{total_bytes/1e3:.2f} KB"

    return journal


def save_journal(journal: dict, output_path: Path) -> Path:
    """Speichere Journal als JSON."""
    output_path.write_text(json.dumps(journal, indent=2, ensure_ascii=False))
    return output_path


# ==============================================================================
# GUI
# ==============================================================================

class JournalApp:
    """tkinter GUI für current_journal"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"📁 Current Journal - {DEVICE_NAME}")
        self.root.geometry("750x600")
        self.root.resizable(True, True)

        self.selected_folders: list[str] = []
        self.cancel_event = threading.Event()

        self._build_ui()

    def _build_ui(self):
        """Baue die GUI-Elemente"""

        # --- Header ---
        header = tk.Frame(self.root, bg="#2c3e50", height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="📁 Current Journal Creator",
                 font=("Helvetica", 16, "bold"), bg="#2c3e50", fg="white"
                 ).pack(pady=15)

        # --- Ordner-Auswahl ---
        folder_frame = tk.LabelFrame(self.root, text="Zu scannende Ordner",
                                     padx=10, pady=10)
        folder_frame.pack(fill=tk.X, expand=False, padx=10, pady=(10, 5))

        # Listbox für Ordner (sichtbar ca. 8–10 Einträge)
        list_frame = tk.Frame(folder_frame)
        list_frame.pack(fill=tk.X, expand=False)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.folder_listbox = tk.Listbox(list_frame, font=("Courier", 10),
                                         height=10,
                                         yscrollcommand=scrollbar.set,
                                         selectmode=tk.SINGLE)
        self.folder_listbox.pack(fill=tk.X, expand=False)
        scrollbar.config(command=self.folder_listbox.yview)

        # Buttons für Ordner
        btn_frame = tk.Frame(folder_frame)
        btn_frame.pack(fill=tk.X, pady=(5, 0))

        tk.Button(btn_frame, text="➕ Ordner hinzufügen",
                  command=self._add_folder, width=20).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="➖ Entfernen",
                  command=self._remove_folder, width=15).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="🗑️ Alle entfernen",
                  command=self._clear_folders, width=15).pack(side=tk.LEFT, padx=5)

        # --- Speicherort ---
        save_frame = tk.LabelFrame(self.root, text="Speicherort", padx=10, pady=5)
        save_frame.pack(fill=tk.X, padx=10, pady=5)

        self.save_dir_var = tk.StringVar(value=str(Path.home()))
        tk.Entry(save_frame, textvariable=self.save_dir_var,
                 font=("Courier", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(save_frame, text="📂", command=self._choose_save_dir,
                  width=3).pack(side=tk.RIGHT, padx=5)

        # --- Fortschritt (nur Text) ---
        progress_frame = tk.LabelFrame(self.root, text="Fortschritt", padx=10, pady=5)
        progress_frame.pack(fill=tk.BOTH, padx=10, pady=5, expand=True)

        self.folder_progress_label = tk.Label(progress_frame,
                                              text="Bereit.", anchor='w',
                                              font=("Helvetica", 11))
        self.folder_progress_label.pack(fill=tk.X)

        self.file_progress_label = tk.Label(progress_frame,
                                            text="", anchor='w',
                                            font=("Courier", 9))
        self.file_progress_label.pack(fill=tk.X)

        # --- Start / Cancel ---
        action_frame = tk.Frame(self.root)
        action_frame.pack(fill=tk.X, padx=10, pady=10)

        self.start_btn = tk.Button(action_frame, text="▶️  Journal erstellen",
                                   command=self._start_scan,
                                   font=("Helvetica", 12, "bold"),
                                   bg="#27ae60", fg="white",
                                   height=2, width=25)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.cancel_btn = tk.Button(action_frame, text="⏹ Abbrechen",
                                    command=self._cancel_scan,
                                    font=("Helvetica", 12),
                                    state=tk.DISABLED,
                                    height=2, width=15)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)

    # --- Ordner-Verwaltung ---

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Ordner auswählen")
        if folder and folder not in self.selected_folders:
            self.selected_folders.append(folder)
            self.folder_listbox.insert(tk.END, folder)

    def _remove_folder(self):
        selection = self.folder_listbox.curselection()
        if selection:
            idx = selection[0]
            self.selected_folders.pop(idx)
            self.folder_listbox.delete(idx)

    def _clear_folders(self):
        self.selected_folders.clear()
        self.folder_listbox.delete(0, tk.END)

    def _choose_save_dir(self):
        directory = filedialog.askdirectory(
            title="Zielordner für Journal auswählen...",
            initialdir=self.save_dir_var.get() or str(Path.home())
        )
        if directory:
            self.save_dir_var.set(directory)

    # --- Scan ---

    def _start_scan(self):
        if not self.selected_folders:
            messagebox.showwarning(
                "Keine Ordner",
                "Bitte mindestens einen Ordner auswählen."
            )
            return

        self.cancel_event.clear()
        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)

        # Scan in separatem Thread (UI bleibt responsiv)
        thread = threading.Thread(target=self._run_scan, daemon=True)
        thread.start()

    def _cancel_scan(self):
        self.cancel_event.set()
        self.folder_progress_label.config(text="⏹ Abgebrochen.")
        self.start_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)

    def _run_scan(self):
        """Scan-Thread"""
        folders = [Path(f) for f in self.selected_folders]
        start_time = time.time()

        def folder_progress(current, total, name):
            self.root.after(0, lambda: self.folder_progress_label.config(
                text=f"📂 Ordner {current}/{total}: {name}"
            ))

        def file_progress(current, total, name):
            self.root.after(0, lambda: self.file_progress_label.config(
                text=f"   📄 Datei {current}/{total} in diesem Ordner: {name[:60]}"
            ))

        journal = create_journal(
            folders=folders,
            folder_progress_callback=folder_progress,
            file_progress_callback=file_progress,
            cancel_event=self.cancel_event
        )

        if self.cancel_event.is_set():
            return

        # Speichern
        output_path = Path(self.save_dir_var.get()) / JOURNAL_FILENAME
        save_journal(journal, output_path)

        elapsed = time.time() - start_time
        summary = journal['summary']

        # UI Update im Main Thread
        self.root.after(0, lambda: self._scan_complete(
            output_path, summary, elapsed
        ))

    def _scan_complete(self, output_path: Path, summary: dict, elapsed: float):
        self.start_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)

        msg = (
            f"✅ Journal erstellt!\n\n"
            f"📄 Datei: {output_path.name}\n"
            f"📂 Speicherort: {output_path.parent}\n\n"
            f"📊 Zusammenfassung:\n"
            f"   Dateien: {summary['total_files']:,}\n"
            f"   Größe:   {summary['total_size_human']}\n"
            f"   Dauer:   {elapsed:.1f} Sekunden\n\n"
            f"Dieses Journal kann nun für den Vergleich\n"
            f"mit journal_checker.py verwendet werden."
        )

        self.folder_progress_label.config(
            text=f"✅ Fertig! {summary['total_files']:,} Dateien in {elapsed:.1f}s"
        )
        self.file_progress_label.config(text="")

        # eigenes, breiteres Dialogfenster mit etwas größerer Schrift
        dialog = tk.Toplevel(self.root)
        dialog.title("Journal erstellt")
        dialog.geometry("750x260")
        dialog.transient(self.root)
        dialog.grab_set()

        text_widget = tk.Message(dialog, text=msg, width=700,
                                 font=("Helvetica", 11))
        text_widget.pack(padx=20, pady=20, fill=tk.BOTH, expand=True)

        tk.Button(dialog, text="OK", command=dialog.destroy,
                  width=12, font=("Helvetica", 11)).pack(pady=(0, 15))

    def run(self):
        self.root.mainloop()


# ==============================================================================
# CLI MODE (ohne GUI)
# ==============================================================================

def cli_mode(folders: list[str], output: str = None):
    """
    Kommandozeilen-Modus für Automatisierung.

    Beispiel:
        python current_journal.py --cli /path/to/Photos /path/to/Videos
    """
    folder_paths = [Path(f) for f in folders]
    output_path = Path(output) if output else Path.cwd() / JOURNAL_FILENAME

    print(f"📁 Current Journal - {DEVICE_NAME}")
    print(f"{'='*60}")
    print(f"   Ordner:     {len(folder_paths)}")
    print(f"   Hash:       none")
    print(f"   Ausgabe:    {output_path}")
    print(f"{'='*60}\n")

    def folder_progress(current, total, name):
        print(f"\n📂 Ordner {current}/{total}: {name}")

    def file_progress(current, total, name):
        print(f"\r   Datei {current}/{total} in diesem Ordner: {name[:60]}",
              end='', flush=True)

    journal = create_journal(
        folders=folder_paths,
        folder_progress_callback=folder_progress,
        file_progress_callback=file_progress,
    )

    save_journal(journal, output_path)

    summary = journal['summary']
    print(f"\n\n{'='*60}")
    print(f"✅ Journal gespeichert: {output_path.name}")
    print(f"   Dateien: {summary['total_files']:,}")
    print(f"   Größe:   {summary['total_size_human']}")
    print(f"{'='*60}")


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == '__main__':
    if '--cli' in sys.argv:
        # CLI Modus: python current_journal.py --cli /folder1 /folder2
        args = sys.argv[1:]
        args.remove('--cli')

        output = None
        if '--output' in args:
            idx = args.index('--output')
            output = args[idx + 1]
            args.pop(idx)  # --output
            args.pop(idx)  # value

        if not args:
            print("Usage: python current_journal.py --cli /folder1 /folder2 "
                  "[--output path]")
            sys.exit(1)

        cli_mode(args, output=output)
    else:
        # GUI Modus (Standard)
        app = JournalApp()
        app.run()
