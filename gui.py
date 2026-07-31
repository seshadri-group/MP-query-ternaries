#!/usr/bin/env python3
"""
gui.py — a small tkinter front-end for run_workflow.py.

Design notes
------------
* The GUI is a thin wrapper around f.args: it pre-fills the form from an
  existing f.args (if present), writes f.args when you press Run, and then
  launches run_workflow.py as a *subprocess*, streaming its stdout into the
  log pane. The GUI never imports the workflow, so the two can't break each
  other and the script remains fully usable headless from a terminal.
* Only the standard library is used (tkinter, subprocess, threading, queue),
  so the GUI adds no dependencies to the environment.
* The subprocess runs on a background thread; stdout lines are pushed onto a
  queue.Queue and drained into the log pane by a Tk `after()` poll, since Tk
  widgets must only be touched from the main thread.

Usage:  python gui.py
"""

import configparser
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

CONFIG_PATH = "f.args"
WORKFLOW = Path(__file__).parent / "run_workflow.py"


# --------------------------------------------------------------------------
# f.args I/O
# --------------------------------------------------------------------------

# (section, key, default) for every setting the form exposes.
TEXT_FIELDS = [
    ("elements", "group_1", ""),
    ("elements", "group_2", ""),
    ("elements", "group_3", ""),
    ("elements", "groups", "A M X"),
    ("plot", "markersize", "10"),
    # out_dir defaults to blank: when empty, run_workflow.py names the
    # folder after the element groups (e.g. output_Ta-Ho_Co_N-Si).
    ("output", "out_dir", ""),
]

BOOL_FIELDS = [
    ("query", "run_query", True, "Query Materials Project"),
    ("query", "write_all", True, "Write _all CSVs (incl. predicted)"),
    ("query", "write_exp", True, "Write _exp CSVs (ICSD only)"),
    ("query", "experimental", True, "Plot experimental data as primary"),
    ("plot", "run_plot", True, "Generate individual diagrams"),
    ("plot", "run_summary", True, "Generate summary grid + key"),
    ("plot", "theory", True, "Overlay predicted compounds (hollow markers)"),
    ("plot", "no_formulas", False, "Hide formula annotations"),
    ("plot", "no_labels", False, "Hide titles/legends/corner labels"),
]


def read_config():
    config = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if Path(CONFIG_PATH).exists():
        config.read(CONFIG_PATH)
    return config


def write_config(text_values, bool_values):
    """Write the form state to f.args, preserving any sections/keys the form
    doesn't know about (e.g. custom CSV paths someone added by hand)."""
    config = read_config()
    for (section, key, _default), value in zip(TEXT_FIELDS, text_values):
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, value)
    for (section, key, _default, _label), value in zip(BOOL_FIELDS, bool_values):
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, "true" if value else "false")
    with open(CONFIG_PATH, "w") as f:
        config.write(f)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class WorkflowGUI:
    def __init__(self, root):
        self.root = root
        root.title("Ternary Phase Diagram Workflow")
        root.minsize(560, 640)

        # Window icon (icon.png next to this script). tk.PhotoImage reads PNG
        # natively on Tk ≥ 8.6; keep a reference on self so it isn't garbage-
        # collected. Wrapped in try/except so a missing or unreadable icon
        # never prevents the GUI from launching. Note: on macOS the titlebar
        # shows no icon by design — there this sets the Dock icon instead
        # (on sufficiently recent Tk builds).
        try:
            self._icon = tk.PhotoImage(file=str(Path(__file__).parent / "icon.png"))
            root.iconphoto(True, self._icon)
        except Exception:
            pass

        self.proc = None
        self.log_queue = queue.Queue()

        config = read_config()

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # --- Elements ------------------------------------------------------
        elem_frame = ttk.LabelFrame(main, text="Element groups (space-separated)", padding=8)
        elem_frame.pack(fill="x", pady=(0, 8))
        elem_frame.columnconfigure(1, weight=1)

        self.text_vars = []
        labels = {
            "group_1": "Group 1 (bottom-left)",
            "group_2": "Group 2 (top)",
            "group_3": "Group 3 (bottom-right)",
            "groups": "Group placeholder names",
            "markersize": "Marker size",
            "out_dir": "Output directory (blank = auto-named from groups)",
        }
        row = 0
        for section, key, default in TEXT_FIELDS[:4]:
            var = tk.StringVar(value=config.get(section, key, fallback=default))
            self.text_vars.append(var)
            ttk.Label(elem_frame, text=labels[key]).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(elem_frame, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2)
            row += 1

        # --- Options -------------------------------------------------------
        opt_frame = ttk.LabelFrame(main, text="Options", padding=8)
        opt_frame.pack(fill="x", pady=(0, 8))
        opt_frame.columnconfigure(1, weight=1)

        self.bool_vars = []
        for i, (section, key, default, label) in enumerate(BOOL_FIELDS):
            var = tk.BooleanVar(value=config.getboolean(section, key, fallback=default))
            self.bool_vars.append(var)
            ttk.Checkbutton(opt_frame, text=label, variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)

        # --- Plot settings -------------------------------------------------
        plot_frame = ttk.LabelFrame(main, text="Plot settings", padding=8)
        plot_frame.pack(fill="x", pady=(0, 8))
        plot_frame.columnconfigure(1, weight=1)
        for i, (section, key, default) in enumerate(TEXT_FIELDS[4:]):
            value = config.get(section, key, fallback=default)
            # Migration: older GUI versions saved out_dir = output into
            # f.args, which would forever override the new auto-naming.
            # Treat that legacy literal as "unset" so those configs pick up
            # group-based folder names; anything else the user typed is kept.
            if key == "out_dir" and value.strip() == "output":
                value = ""
            var = tk.StringVar(value=value)
            self.text_vars.append(var)
            ttk.Label(plot_frame, text=labels[key]).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(plot_frame, textvariable=var).grid(row=i, column=1, sticky="ew", pady=2)

        # --- API key -------------------------------------------------------
        # The key is resolved like the workflow does: MP_API_KEY env var
        # first, then ~/.mp_api_key. The entry + Save button writes the file
        # (chmod 600), because macOS runs double-clicked .command launchers
        # in non-interactive shells that don't source ~/.zshrc — so relying
        # on an exported variable breaks for GUI users.
        key_frame = ttk.LabelFrame(main, text="Materials Project API key", padding=8)
        key_frame.pack(fill="x", pady=(0, 8))
        key_frame.columnconfigure(0, weight=1)

        self.key_var = tk.StringVar()
        key_entry = ttk.Entry(key_frame, textvariable=self.key_var, show="•")
        key_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(key_frame, text="Save key", command=self.save_key).grid(row=0, column=1)
        self.key_status = ttk.Label(key_frame, text="")
        self.key_status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._refresh_key_status()

        # --- Buttons -------------------------------------------------------
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(0, 8))
        self.run_btn = ttk.Button(btn_frame, text="Run workflow", command=self.run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Save f.args only", command=self.save_only).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Open output folder", command=self.open_output).pack(side="left", padx=6)

        # --- Log -----------------------------------------------------------
        log_frame = ttk.LabelFrame(main, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(log_frame, height=16, state="disabled",
                                             font=("Courier", 10))
        self.log.pack(fill="both", expand=True)

        self.root.after(100, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- API key ------------------------------------------------------------

    KEY_FILE = Path.home() / ".mp_api_key"

    @classmethod
    def resolve_key(cls):
        """Same precedence as the workflow: env var, then ~/.mp_api_key."""
        key = os.getenv("MP_API_KEY")
        if key:
            return key.strip(), "environment variable"
        if cls.KEY_FILE.exists():
            key = cls.KEY_FILE.read_text().strip()
            if key:
                return key, str(cls.KEY_FILE)
        return None, None

    def save_key(self):
        key = self.key_var.get().strip()
        if not key:
            messagebox.showerror("Empty key", "Paste your API key first "
                                 "(from https://materialsproject.org/api).")
            return
        self.KEY_FILE.write_text(key + "\n")
        self.KEY_FILE.chmod(0o600)  # private to this user
        self.key_var.set("")
        self._refresh_key_status()
        self._append_log(f"API key saved to {self.KEY_FILE}\n")

    def _refresh_key_status(self):
        key, source = self.resolve_key()
        if key:
            self.key_status.config(
                text=f"Key found ✓  (from {source})", foreground="green")
        else:
            self.key_status.config(
                text="No key found — paste your key above and press Save "
                     "(get one free at materialsproject.org/api)",
                foreground="red")

    # -- actions ------------------------------------------------------------

    def save_only(self):
        self._write_form()
        self._append_log(f"Saved {Path(CONFIG_PATH).resolve()}\n")

    def _write_form(self):
        write_config([v.get() for v in self.text_vars],
                     [v.get() for v in self.bool_vars])

    def run(self):
        if self.proc is not None:
            return
        # Minimal pre-flight: non-empty groups (the workflow does the real
        # validation — overlaps etc. — and its error messages land in the log).
        for var, (_, key, _d) in zip(self.text_vars, TEXT_FIELDS):
            if key.startswith("group_") and not var.get().strip():
                messagebox.showerror("Missing input", f"{key} must not be empty.")
                return
        self._write_form()
        key, _source = self.resolve_key()
        if not key:
            messagebox.showerror(
                "No API key",
                "No Materials Project API key found. Paste your key in the "
                "API key field and press Save (free at materialsproject.org/api).")
            return
        self._append_log("── Starting workflow ──\n")
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        # -u forces unbuffered stdout so the log streams line-by-line instead
        # of arriving in one lump when the process exits. The resolved key is
        # injected explicitly so the workflow sees it regardless of how the
        # GUI itself was launched (terminal, double-click, etc.).
        env = dict(os.environ, MP_API_KEY=key)
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(WORKFLOW)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(WORKFLOW.parent),
            env=env,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            self._append_log("── Terminated by user ──\n")

    def _default_out_dir(self):
        """
        Mirror run_workflow.py's _default_out_dir naming (elements joined
        with "-" within a group, groups joined with "_"), built from the
        current form fields. Duplicated rather than imported to preserve the
        design rule that the GUI never imports the workflow — if the naming
        scheme changes in run_workflow.py, update this to match.
        """
        groups = [self.text_vars[i].get().split() for i in range(3)]
        if not all(groups):
            return "output"
        return "output_" + "_".join("-".join(g) for g in groups)

    def open_output(self):
        # out_dir is the last text field (see TEXT_FIELDS order); it holds
        # the CSVs directly and the diagrams under phase_diagrams/. A blank
        # field means the auto-named, group-based folder.
        out_dir = Path(self.text_vars[-1].get().strip() or self._default_out_dir())
        if not out_dir.exists():
            messagebox.showinfo("Not found", f"{out_dir} does not exist yet.")
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(out_dir)])
        elif sys.platform.startswith("win"):
            os.startfile(str(out_dir))  # noqa — Windows only
        else:
            subprocess.Popen(["xdg-open", str(out_dir)])

    # -- subprocess plumbing -------------------------------------------------

    def _reader(self):
        """Background thread: forward subprocess stdout lines to the queue."""
        for line in self.proc.stdout:
            self.log_queue.put(line)
        code = self.proc.wait()
        self.log_queue.put(f"── Workflow exited with code {code} ──\n")
        self.log_queue.put(None)  # sentinel: process finished

    def _drain_log(self):
        """Main thread: move queued lines into the log widget."""
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line is None:
                    self.proc = None
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _append_log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_close(self):
        if self.proc is not None:
            if not messagebox.askyesno("Workflow running",
                                       "A workflow is still running. Stop it and quit?"):
                return
            self.proc.terminate()
        self.root.destroy()


def main():
    root = tk.Tk()
    WorkflowGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
