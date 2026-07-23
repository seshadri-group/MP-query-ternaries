# Ternary Phase Diagram Explorer

Query the [Materials Project](https://materialsproject.org) for all known and
predicted compounds across a grid of ternary chemical systems, and render
them as ternary phase diagrams — individually and as a stitched summary grid.

![icon](icon.png)

Solid markers are experimentally known compounds (ICSD); hollow markers are
DFT-predicted. Blue circles are ternaries, red squares are the bounding
binaries.

## Setup (once)

1. Install [conda](https://conda-forge.org/download/) if you don't have it.
2. Get a free Materials Project API key from
   https://materialsproject.org/api and set it in your shell:
   ```
   export MP_API_KEY="your-key-here"          # bash/zsh — add to ~/.zshrc
   ```
   On Windows, set `MP_API_KEY` as a user environment variable instead
   (System Properties → Environment Variables).
   Never commit your key or paste it into any file in this repo.

That's it — the launcher below creates the conda environment automatically
on first run. To set it up manually instead (or for headless use):
```
conda env create -f environment.yml
conda activate mp-ternaries
```
(prefer venv? `pip install -r requirements.txt` works too)

## Use

**GUI** — double-click `run_gui.command` (macOS) or `run_gui.bat` (Windows).
No environment activation needed; the first launch builds the environment
if it doesn't exist yet. (macOS may show an "unidentified developer" prompt
the first time — right-click → Open.)

Fill in your element groups, press *Run workflow*, and watch the log.
*Open output folder* takes you to the results.

From a terminal, the equivalent is:
```
conda run --no-capture-output -n mp-ternaries python gui.py
```

**Headless**: copy `f.args.example` to `f.args`, edit it, then
```
conda activate mp-ternaries
python run_workflow.py
```

## Outputs

Everything lands in one folder (default `output/`, configurable):

```
output/
├── compounds_ternary_exp.csv    # experimentally known ternaries
├── compounds_ternary_all.csv    # + predicted
├── compounds_binary_exp.csv     # bounding binaries
├── compounds_binary_all.csv
└── phase_diagrams/
    ├── <A>-<M>-<X>.png          # one diagram per ternary system
    ├── summary.png              # all diagrams in one grid
    ├── key.png                  # legend + group definitions
    └── blank.png                # empty template triangle
```

CSVs contain one row per Materials Project entry (polymorphs included) with
space group, ICSD IDs, stability, and formation/decomposition energies.

## Configuration

All behavior is driven by `f.args` — see `f.args.example` for every option
with comments. The GUI reads and writes `f.args`, and preserves any keys you
add by hand (e.g. custom CSV paths), so GUI and manual editing mix freely.

## Tips

* The `[timing]` lines in the log show where time goes; the Materials
  Project query is normally the dominant cost.
* First run in a fresh environment is slower (matplotlib font cache,
  bytecode compilation) — that's normal and happens once.
* Working inside cloud-synced folders (Box, Dropbox) can slow large runs;
  point `out_dir` at a local folder if runs feel sluggish.

---

*Developed by Anya Mulligan, Seshadri Group, UCSB, with assistance from Claude (Anthropic). July 2026.*
