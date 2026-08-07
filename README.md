# README: Ternary Phase Diagram Explorer

![icon](icon.png)

Query the [Materials Project](https://materialsproject.org) for all ternary
compounds formed from three user-defined element groups (e.g. *A* = rare
earths, *M* = transition metals, *X* = anions), plus the bounding binaries.
Results are written to CSV --- including space groups, ICSD IDs, and
stability/formation energies --- and plotted as one ternary composition diagram
per system, along with a summary grid of every diagram and a corresponding key figure.

## Setup: GUI (recommended)

1. **Download this repository**: click the green **Code** button at the top of 
   [this page](https://github.com/seshadri-group/MP-query-ternaries), choose 
   **Download ZIP**, then unzip it somewhere convenient (e.g. your Desktop, not a 
   cloud-synced folder).
2. **Install [conda](https://conda-forge.org/download/)** if you don't have it.
3. **Get a free Materials Project API key** from https://materialsproject.org/api
4. **Double-click the launcher** inside the unzipped folder:
   `run_gui.command` (macOS) or `run_gui.bat`
   (Windows). No environment setup needed because the first launch builds the conda
   environment automatically. On macOS, if you get an "unidentified
   developer" warning: open the Apple menu () → **System Settings** →
   **Privacy & Security**, scroll down, and click **Open Anyway**, then
   double-click the launcher again.
5. **Paste your API key** into the GUI's "API key" field and press **Save**.
   It's stored privately in `~/.mp_api_key` on your machine.

Then fill in your element groups, press *Run workflow*, and watch the log.
*Open output folder* takes you to the results.

## Outputs

Everything lands in one folder named after your element groups, e.g. with
group 1 = Ta Ho, group 2 = Co, group 3 = N Si:

```
output_Ta-Ho_Co_N-Si/
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
Setting `out_dir` in `f.args` overrides the automatic folder name.

## Configuration

All behavior is driven by `f.args` — see `f.args.example` for every option
with comments. The GUI reads and writes `f.args`, and preserves any keys you
add by hand (e.g. custom CSV paths), so GUI and manual editing mix freely.

## Tips

* The `[timing]` lines in the log show where time goes; the Materials
  Project query is normally the dominant cost.
* First run in a fresh environment is slower (matplotlib font cache,
  bytecode compilation).
* Working inside cloud-synced folders (Box, Dropbox) can slow large runs;
  point `out_dir` at a local folder if runs feel sluggish.

## Setup: Command line & headless

This section is optional. If you prefer the command line:

**Clone the repository**

```
git clone https://github.com/seshadri-group/MP-query-ternaries.git
cd MP-query-ternaries
```

**Set up the environment manually**

```
conda env create -f environment.yml
conda activate mp-ternaries
```

(prefer venv? `pip install -r requirements.txt` works too)

**Launch the GUI from a terminal**

```
conda activate mp-ternaries
python gui.py
```

**Headless (no GUI)** — copy `f.args.example` to `f.args`, edit it, then:

```
conda activate mp-ternaries
python run_workflow.py
```

For headless use you can supply your key as an `MP_API_KEY` environment
variable instead of the GUI's saved file; it takes precedence over the saved
file if both exist.

* **macOS**: it must go in `~/.zprofile`, not `~/.zshrc`, to be visible to
  the double-click launcher.
* **Windows**: set `MP_API_KEY` as a user environment variable
  (Control Panel → User Environment Variables).
---

*Developed with assistance from Claude (Anthropic).*
