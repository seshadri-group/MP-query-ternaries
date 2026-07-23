#!/usr/bin/env python3
"""
run_workflow.py — query the Materials Project and generate ternary phase diagrams.
All settings are read from f.args. Requires MP_API_KEY environment variable.

Query output flags (under [query] in f.args):
  write_all = true/false   # write _all CSV files (default: true)
  write_exp = true/false   # write _exp CSV files (default: true)

Plot primary-dataset flag (under [query] in f.args):
  experimental = true/false  # which files the plotter treats as primary (default: true)

Element group name labels (under [elements] in f.args):
  groups = A M X           # short placeholder names for the three groups;
                           # shown at the triangle vertices of the key figure
                           # (group_1 → bottom-left, group_2 → top, group_3 → bottom-right)
"""

from itertools import product
from pathlib import Path
import configparser
import csv
import io
import os
import re
import sys
import time

API_KEY = os.getenv("MP_API_KEY")

# Legend marker size, as a fraction of the actual plotted marker size, used
# consistently by every legend in the script (per-diagram legends and the
# key figure). Passed straight to matplotlib's legend(markerscale=...).
LEGEND_MARKER_SCALE = 0.9

# Number of attempts (with a short pause between them) for each batched
# Materials Project query before giving up. Because the query is batched and
# the CSV is only written after the query succeeds, a network failure can no
# longer leave a partially-written CSV behind.
QUERY_RETRIES = 3
QUERY_RETRY_WAIT_S = 5


def load_config(path="f.args"):
    config = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if not Path(path).exists():
        sys.exit(f"Error: '{path}' not found.")
    config.read(path)
    return config


def parse_elements(config, key):
    return [e.strip() for e in config.get("elements", key, fallback="").split() if e.strip()]


def output_path(config, key, default_name):
    """
    Resolve an output file path. All outputs default to living inside a
    single run-output directory ([output] out_dir, default "output") so that
    CSVs and the phase_diagrams/ folder end up together in one place that's
    easy to open, zip, or point the GUI at.

    Precedence:
      * if the key (e.g. ternary_exp_csv) is explicitly set in f.args, that
        value is used verbatim — hand-customized configs keep working exactly
        as before;
      * otherwise the default filename is placed under out_dir.
    The parent directory is created if needed.
    """
    out_dir  = Path(config.get("output", "out_dir", fallback="output"))
    explicit = config.get("output", key, fallback=None)
    path     = Path(explicit) if explicit else out_dir / default_name
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def parse_group_names(config):
    """
    Read the three short placeholder names for the element groups from f.args.
    Falls back to ['1', '2', '3'] when the key is absent or malformed.

    Example f.args entry:
        groups = A M X
    """
    raw   = config.get("elements", "groups", fallback="1 2 3")
    parts = [p.strip() for p in raw.split() if p.strip()]
    if len(parts) != 3:
        print(f"Warning: 'groups' should have exactly 3 names, got {parts!r}. "
              "Falling back to ['1', '2', '3'].")
        parts = ["1", "2", "3"]
    return parts


def validate_groups(group_1, group_2, group_3, group_names):
    """
    Reject configurations where the same element appears in more than one
    group. Overlapping groups break the workflow in two ways: the chemsys
    string degenerates (e.g. 'Si-N-Si' is not a valid ternary system), and
    _formula_to_ternary would silently double-count the shared element when
    computing fractions. Failing loudly here is much friendlier than either.
    Duplicates *within* a single group are also collapsed, with a warning.
    """
    groups = [group_1, group_2, group_3]

    for name, group in zip(group_names, groups):
        if len(set(group)) != len(group):
            deduped = list(dict.fromkeys(group))  # preserves order
            print(f"Warning: duplicate elements within group '{name}' "
                  f"({group}) — collapsing to {deduped}.")
            group[:] = deduped

    for (name_a, ga), (name_b, gb) in [
        ((group_names[0], group_1), (group_names[1], group_2)),
        ((group_names[0], group_1), (group_names[2], group_3)),
        ((group_names[1], group_2), (group_names[2], group_3)),
    ]:
        overlap = set(ga) & set(gb)
        if overlap:
            sys.exit(
                f"Error: element(s) {sorted(overlap)} appear in both group "
                f"'{name_a}' and group '{name_b}'. Each element may belong to "
                "only one group — overlapping groups produce degenerate "
                "chemical systems and mis-computed ternary fractions."
            )


def _normalize_chemsys(chemsys):
    """Return the MP-canonical (alphabetically sorted) form of a chemsys string."""
    return "-".join(sorted(chemsys.split("-")))


def _batched_search(mpr, chemsys_list, exp_mode, fields):
    """
    Run one batched summary search covering every chemsys at once — the MP
    client accepts a list for `chemsys`, and a single batched request is far
    faster than one round-trip per system for large element grids.

    Retries the whole request a few times on failure; since nothing has been
    written to disk yet, a failure after all retries aborts cleanly with no
    partial output.
    """
    last_exc = None
    for attempt in range(1, QUERY_RETRIES + 1):
        try:
            t = time.perf_counter()
            docs = mpr.materials.summary.search(
                chemsys=chemsys_list,
                **({"theoretical": False} if exp_mode else {}),
                fields=fields,
            )
            print(f"  [timing] batched request ({len(chemsys_list)} systems): "
                  f"{time.perf_counter() - t:.1f}s")
            return docs
        except Exception as exc:  # network hiccups, transient API errors, ...
            last_exc = exc
            if attempt < QUERY_RETRIES:
                print(f"  Query attempt {attempt} failed ({exc}); "
                      f"retrying in {QUERY_RETRY_WAIT_S}s...")
                time.sleep(QUERY_RETRY_WAIT_S)
    raise RuntimeError(
        f"Materials Project query failed after {QUERY_RETRIES} attempts: {last_exc}"
    )


def _query_and_write(mpr, chemsys_list, output_path, exp_mode):
    # Deduplicate and canonicalize up front: overlapping group_1/group_3
    # pairings (and MP's own alphabetical normalization) can otherwise
    # produce the same system twice, wasting API calls and duplicating rows.
    chemsys_list = sorted({_normalize_chemsys(cs) for cs in chemsys_list})

    fields = [
        "material_id", "chemsys", "formula_pretty", "symmetry", "theoretical",
        "database_IDs", "is_stable", "energy_above_hull",
        "formation_energy_per_atom", "equilibrium_reaction_energy_per_atom",
    ]

    docs = _batched_search(mpr, chemsys_list, exp_mode, fields)

    # Group the batched results back by system so the CSV stays organized
    # per-chemsys and zero-result systems still get reported below.
    by_chemsys = {cs: [] for cs in chemsys_list}
    for doc in docs:
        by_chemsys.setdefault(_normalize_chemsys(doc.chemsys), []).append(doc)

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "material_id", "chemsys", "formula", "space_group", "is_theoretical",
            "icsd_id(s)", "is_stable", "E_hull (eV/atom)",
            "E_formation (eV/atom)", "E_decomp (eV/atom)",
        ])
        for chemsys in chemsys_list:
            sys_docs = by_chemsys.get(chemsys, [])

            # One row per material_id (not aggregated by formula). A given
            # formula can have several polymorphs/entries in MP; sorting by
            # (formula, e_above_hull) here just keeps the CSV readable —
            # polymorphs of the same formula end up grouped together with
            # the most stable one listed first.
            def _sort_key(doc):
                e_hull = doc.energy_above_hull
                return (doc.formula_pretty, e_hull is None, e_hull if e_hull is not None else 0)

            for doc in sorted(sys_docs, key=_sort_key):
                # A single material_id can itself have multiple matching
                # ICSD entries, so icsd_id stays a "; "-joined list — but
                # everything else here is now a single value per row.
                icsd = doc.database_IDs.get("icsd", []) if doc.database_IDs else []
                ids  = "; ".join(str(i) for i in sorted(icsd))
                sg   = doc.symmetry.symbol if doc.symmetry is not None and doc.symmetry.symbol else ""

                writer.writerow([
                    doc.material_id,
                    chemsys,
                    doc.formula_pretty,
                    sg,
                    doc.theoretical,
                    ids,
                    doc.is_stable,
                    doc.energy_above_hull,
                    doc.formation_energy_per_atom,
                    doc.equilibrium_reaction_energy_per_atom,
                ])

            n_formulas = len({doc.formula_pretty for doc in sys_docs})
            print(f"  {chemsys}: {len(sys_docs)} entries ({n_formulas} unique compositions)")
    print(f"Wrote {Path(output_path).resolve()}")


def run_ternary_query(config, group_1, group_2, group_3):
    from mp_api.client import MPRester
    if not API_KEY:
        raise ValueError("MP_API_KEY not set.")

    write_all = config.getboolean("query", "write_all", fallback=True)
    write_exp = config.getboolean("query", "write_exp", fallback=True)

    if not write_all and not write_exp:
        print("[Ternary] write_all and write_exp are both False — nothing to write.")
        return

    chemsys_list = [f"{a}-{b}-{c}" for a, b, c in product(group_1, group_2, group_3)]

    with MPRester(API_KEY) as mpr:
        if write_all:
            out = output_path(config, "ternary_all_csv", "compounds_ternary_all.csv")
            print(f"\n[Ternary — all] → {out}")
            _query_and_write(mpr, chemsys_list, out, exp_mode=False)
        if write_exp:
            out = output_path(config, "ternary_exp_csv", "compounds_ternary_exp.csv")
            print(f"\n[Ternary — exp] → {out}")
            _query_and_write(mpr, chemsys_list, out, exp_mode=True)


def run_binary_query(config, group_1, group_2, group_3):
    from mp_api.client import MPRester
    if not API_KEY:
        raise ValueError("MP_API_KEY not set.")

    write_all = config.getboolean("query", "write_all", fallback=True)
    write_exp = config.getboolean("query", "write_exp", fallback=True)

    if not write_all and not write_exp:
        print("[Binary] write_all and write_exp are both False — nothing to write.")
        return

    # Duplicate pairs (e.g. the same group_3 element paired with several
    # group_1/group_2 elements' shared partners) are collapsed inside
    # _query_and_write, so building them naively here is fine.
    raw_pairs = (
        [(a, c) for a in group_1 for c in group_3] +
        [(b, c) for b in group_2 for c in group_3]
    )
    chemsys_list = ["-".join(sorted([e1, e2])) for e1, e2 in raw_pairs]

    with MPRester(API_KEY) as mpr:
        if write_all:
            out = output_path(config, "binary_all_csv", "compounds_binary_all.csv")
            print(f"\n[Binary — all] → {out}")
            _query_and_write(mpr, chemsys_list, out, exp_mode=False)
        if write_exp:
            out = output_path(config, "binary_exp_csv", "compounds_binary_exp.csv")
            print(f"\n[Binary — exp] → {out}")
            _query_and_write(mpr, chemsys_list, out, exp_mode=True)


def _preprocess_name(name):
    from pymatgen.core import Composition
    comp = Composition(name)
    parts = []
    for el in comp.elements:
        n = int(round(comp[el]))
        parts.append(str(el) + (str(n) if n > 1 else ""))
    # Braces are required around multi-digit runs so mathtext subscripts the
    # whole number (e.g. "13") rather than just its first character — without
    # them, "$_13$" only subscripts the "1" and renders the "3" as normal-size
    # text, which is why two-digit-and-up counts looked unformatted.
    return re.sub(r'(\d+)', r'$_{\1}$', "".join(parts))


def _formula_to_ternary(formula, elements):
    from pymatgen.core import Composition
    comp = Composition(formula)
    total = sum(comp[el] for el in elements)
    return tuple(comp[el] / total for el in elements)


def _ternary_to_xy(a, b, c):
    """
    Maps ternary fractions to Cartesian coordinates.
    Vertex positions: a → bottom-left, b → bottom-right, c → top.
    """
    import numpy as np
    x = 0.5 * (2 * b + c) / (a + b + c)
    y = (np.sqrt(3) / 2) * c / (a + b + c)
    return x, y


def _draw_grid(ax, steps=10):
    for i in range(1, steps):
        f = i / steps
        for p1, p2 in [
            (_ternary_to_xy(1-f, f,   0), _ternary_to_xy(0,   f, 1-f)),
            (_ternary_to_xy(1-f, 0,   f), _ternary_to_xy(0, 1-f,   f)),
            (_ternary_to_xy(f,   1-f, 0), _ternary_to_xy(f,   0, 1-f)),
        ]:
            ax.plot([p1[0], p2[0]], [p1[1], p2[1]], "k-", lw=0.3, alpha=0.3)


def _draw_triangle(ax, elements=None, show_labels=True, italic_labels=False):
    """
    Draws the triangle and, if show_labels, places element labels at:
      elements[0] (group_1) → bottom-left
      elements[1] (group_2) → top
      elements[2] (group_3) → bottom-right

    italic_labels : render the vertex labels in italics. Used for the key
                    figure, where the labels are the A/M/X-style placeholder
                    variable names rather than concrete element symbols.
    """
    import numpy as np
    corners = np.array([[0, 0], [1, 0], [0.5, np.sqrt(3) / 2], [0, 0]])
    ax.plot(corners[:, 0], corners[:, 1], "k-", lw=1)
    _draw_grid(ax)
    if show_labels and elements:
        offset = 0.05
        fontstyle = "italic" if italic_labels else "normal"
        ax.text(0 - offset,              0 - offset,          elements[0], ha="center", fontsize=11, fontstyle=fontstyle)  # bottom-left
        ax.text(0.5,       np.sqrt(3) / 2 + 0.03,             elements[1], ha="center", fontsize=11, fontstyle=fontstyle)  # top
        ax.text(1 + offset,              0 - offset,          elements[2], ha="center", fontsize=11, fontstyle=fontstyle)  # bottom-right


def _parse_float(value):
    """Parse a CSV numeric cell; empty strings and 'None' become None."""
    if value is None:
        return None
    value = value.strip()
    if value in ("", "None"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _load_systems(filepath):
    """
    Load one CSV into {chemsys: {formula: record}}, where record is a dict:
        material_id, space_group, is_theoretical, e_hull, e_formation

    A formula can appear on multiple rows (one per polymorph/material_id).
    The record kept for each formula is the one with the *lowest*
    energy_above_hull, selected explicitly by comparing parsed values —
    never by trusting CSV row order — so a re-sorted or hand-edited file
    still yields the most stable polymorph's metadata. Rows with a missing
    E_hull are only kept when no row with a numeric E_hull exists.

    Downstream code that only needs the composition can iterate the inner
    dict's keys; marker styling by stability (etc.) can read the records.
    """
    systems = {}
    if not Path(filepath).exists():
        print(f"  Warning: {filepath} not found — skipping.")
        return systems
    with open(filepath, newline="") as f:
        for row in csv.DictReader(f):
            key     = "-".join(sorted(row["chemsys"].split("-")))
            records = systems.setdefault(key, {})
            formula = row["formula"]
            e_hull  = _parse_float(row.get("E_hull (eV/atom)"))

            record = dict(
                material_id=row.get("material_id", ""),
                space_group=row.get("space_group", ""),
                is_theoretical=row.get("is_theoretical", "").strip() == "True",
                e_hull=e_hull,
                e_formation=_parse_float(row.get("E_formation (eV/atom)")),
            )

            existing = records.get(formula)
            if existing is None:
                records[formula] = record
            else:
                # Explicit stability comparison: numeric E_hull always beats
                # None; between two numeric values, keep the lower.
                old, new = existing["e_hull"], e_hull
                if new is not None and (old is None or new < old):
                    records[formula] = record
    return systems


def _plot_compounds(ax, compounds, elements, marker, color, label, show_formulas, texts, markersize, hollow=False):
    """
    compounds : mapping of formula → record (as produced by _load_systems),
                where record carries the most stable polymorph's metadata
                (material_id, space_group, is_theoretical, e_hull,
                e_formation). Only the formula (key) is used for positioning;
                the record is looked up per formula so future extensions can
                style markers by stability, e.g.:
                    e_hull = compounds[formula]["e_hull"]
                    color  = cmap(norm(e_hull))
    elements  : [group_1, group_2, group_3].
    Fractions are unpacked as (g1, g2, g3) then passed to _ternary_to_xy as
    (g1, g3, g2) so that group_1 → bottom-left, group_2 → top, group_3 → bottom-right.
    """
    zorder = 4 if hollow else 5
    for formula in compounds:
        try:
            g1, g2, g3 = _formula_to_ternary(formula, elements)
            x, y       = _ternary_to_xy(g1, g3, g2)   # g2 → top (c slot), g3 → bottom-right (b slot)
            mfc        = "none" if hollow else color
            ax.plot(x, y, marker, color=color, mfc=mfc, markersize=markersize, zorder=zorder, label=label)
            label = "_nolegend_"
            if show_formulas:
                texts.append(ax.text(
                    x, y + 0.03, _preprocess_name(formula),
                    ha="center", fontsize=7, zorder=10,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.7),
                ))
        except Exception as exc:
            # Never fail the whole diagram over one bad formula, but never
            # swallow it silently either — a systematic problem (e.g. a
            # typo'd element list) would otherwise just look like an
            # inexplicably empty triangle.
            print(f"  Warning: could not plot '{formula}' on "
                  f"{'-'.join(elements)}: {exc}")
            continue


def _legend_handles(markersize):
    """
    Build the fixed set of four legend handles — ternary (ICSD), binary
    (ICSD), ternary (predicted), binary (predicted) — used identically by
    every legend in the script (the key figure and each per-diagram plot).
    Handles are proxy artists, not tied to what's actually plotted, so all
    four entries always appear even on diagrams where a given category has
    no compounds to show.
    """
    import matplotlib.lines as mlines
    return [
        mlines.Line2D([], [], color="steelblue", marker="o", linestyle="none",
                      markersize=markersize, label="ternary (ICSD)"),
        mlines.Line2D([], [], color="tomato",    marker="s", linestyle="none",
                      markersize=markersize, label="binary (ICSD)"),
        mlines.Line2D([], [], color="steelblue", marker="o", linestyle="none",
                      markersize=markersize, mfc="none", label="ternary (predicted)"),
        mlines.Line2D([], [], color="tomato",    marker="s", linestyle="none",
                      markersize=markersize, mfc="none", label="binary (predicted)"),
    ]


def _make_blank(output_dir):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.set_aspect("equal")
    ax.axis("off")
    _draw_triangle(ax, elements=None, show_labels=False)
    out_path = Path(output_dir) / "blank.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved {out_path}")


def _make_key_figure(group_names, group_1, group_2, group_3, data, output_dir):
    """
    Save key.png: a blank labeled triangle whose vertices show the group
    placeholder names (group_names[0] → bottom-left, group_names[1] → top,
    group_names[2] → bottom-right) plus a two-column legend defining all four
    marker styles, plus a line spelling out which real elements each
    placeholder stands for (e.g. "M = Co    A = Ta, Ho    X = N, Si").
    The legend is always drawn with all four types regardless of whether
    theory mode is active, since the key is a static reference.

    The vertex labels and the placeholder letters in the definition line are
    italicized to mark them as variables, matching the convention that A/M/X
    stand in for whichever elements are configured in f.args.
    """
    import matplotlib.pyplot as plt

    # Handles are built at the actual plotted marker size; legend()'s
    # markerscale (shared LEGEND_MARKER_SCALE) applies the same slight
    # reduction used by every other legend in the script.
    handles = _legend_handles(data["markersize"])

    fig, ax = plt.subplots(figsize=(5, 4.7))
    ax.set_aspect("equal")
    ax.axis("off")
    _draw_triangle(ax, elements=group_names, show_labels=True, italic_labels=True)
    ax.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.04),
        ncol=2,
        columnspacing=1.0,
        handletextpad=0.25,
        labelspacing=0.6,
        fontsize=10,
        frameon=False,
        markerscale=LEGEND_MARKER_SCALE,
    )

    # Spell out which real elements each placeholder name stands for, e.g.
    # "M = Co    A = Ta, Ho    X = N, Si", read from group_1/2/3 in f.args.
    # The placeholder letter (before "=") is italicized to match the vertex
    # labels; the concrete element symbols after "=" stay upright since they
    # are literal values, not variables.
    # Uses ax.set_title (anchored to the axes, with an exact pad in points)
    # rather than fig.suptitle (anchored to the whole figure), since the
    # latter's distance from the triangle isn't reliably controllable once
    # bbox_inches='tight' recomputes the crop.
    definition = "    ".join(
        r"$\it{" + name + r"}$" + f" = {', '.join(elements)}"
        for name, elements in zip(group_names, [group_1, group_2, group_3])
    )
    ax.set_title(definition, fontsize=12, pad=18)

    out_path = Path(output_dir) / "key.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved {out_path}")


# Module-level guard so the theory/experimental conflict warning prints only
# once per run, even though _load_diagram_data may be called by both
# run_plotting and run_summary_figure.
_theory_conflict_warned = False


def _load_diagram_data(config):
    """
    Load all CSV data and plotting settings into a single dict so that
    both run_plotting and run_summary_figure share identical inputs without
    reading the files twice when both are called in the same run.
    """
    global _theory_conflict_warned

    experimental = config.getboolean("query", "experimental", fallback=True)
    theory       = config.getboolean("plot",  "theory",       fallback=True)
    no_formulas  = config.getboolean("plot",  "no_formulas",  fallback=False)
    no_labels    = config.getboolean("plot",  "no_labels",    fallback=False)
    markersize   = config.getint(    "plot",  "markersize",   fallback=10)
    out_dir      = config.get("output", "out_dir",  fallback="output")
    output_dir   = config.get("output", "plot_dir", fallback=str(Path(out_dir) / "phase_diagrams"))

    exp_ternary_path = output_path(config, "ternary_exp_csv", "compounds_ternary_exp.csv")
    all_ternary_path = output_path(config, "ternary_all_csv", "compounds_ternary_all.csv")
    exp_binary_path  = output_path(config, "binary_exp_csv", "compounds_binary_exp.csv")
    all_binary_path  = output_path(config, "binary_all_csv", "compounds_binary_all.csv")

    if theory and not experimental:
        if not _theory_conflict_warned:
            print("Warning: 'theory = true' requires 'experimental = true' as baseline. Disabling theory overlay.")
            _theory_conflict_warned = True
        theory = False

    primary_ternary = _load_systems(exp_ternary_path if experimental else all_ternary_path)
    primary_binary  = _load_systems(exp_binary_path  if experimental else all_binary_path)
    all_ternary     = _load_systems(all_ternary_path) if theory else {}
    all_binary      = _load_systems(all_binary_path)  if theory else {}

    show_formulas = not no_formulas and not no_labels
    bin_label     = "binary (ICSD)"  if theory else "binary"
    tern_label    = "ternary (ICSD)" if theory else "ternary"

    return dict(
        primary_ternary=primary_ternary,
        primary_binary=primary_binary,
        all_ternary=all_ternary,
        all_binary=all_binary,
        theory=theory,
        bin_label=bin_label,
        tern_label=tern_label,
        show_formulas=show_formulas,
        no_labels=no_labels,
        markersize=markersize,
        output_dir=output_dir,
    )


def _make_diagram_figure(elements, data, show_labels, show_formulas=None, markersize=None):
    """
    Render one ternary phase diagram and return the matplotlib Figure object
    without saving it — the caller decides what to do with it.

    show_labels   : draw corner element labels, title, and legend.
    show_formulas : annotate points with formula names; falls back to
                    data['show_formulas'] when not explicitly set, allowing
                    the summary path to force False independently of user config.
    markersize    : override data['markersize'] when set explicitly, e.g. to
                    scale markers up for the summary figure.
    """
    import matplotlib.pyplot as plt
    from adjustText import adjust_text

    if show_formulas is None:
        show_formulas = data["show_formulas"]
    if markersize is None:
        markersize = data["markersize"]

    a, b, c  = elements
    chemsys  = "-".join(sorted(elements))
    filename = f"{a}-{b}-{c}"

    primary_formulas = data["primary_ternary"].get(chemsys, {})

    fig, ax = plt.subplots(figsize=(6, 5))
    ax.set_aspect("equal")
    ax.axis("off")
    _draw_triangle(ax, elements=elements, show_labels=show_labels)

    texts = []

    for e1, e2 in [(a, b), (b, c), (a, c)]:
        binary_key = "-".join(sorted([e1, e2]))
        prim_bin   = data["primary_binary"].get(binary_key, {})

        if data["theory"]:
            all_bin    = data["all_binary"].get(binary_key, {})
            theory_bin = {f: r for f, r in all_bin.items() if f not in prim_bin}
            _plot_compounds(ax, theory_bin, elements,
                            marker="s", color="tomato",
                            label="binary (predicted)",
                            show_formulas=show_formulas, texts=texts,
                            markersize=markersize, hollow=True)

        _plot_compounds(ax, prim_bin, elements,
                        marker="s", color="tomato",
                        label=data["bin_label"],
                        show_formulas=show_formulas, texts=texts,
                        markersize=markersize)

    if data["theory"]:
        all_tern        = data["all_ternary"].get(chemsys, {})
        theory_formulas = {f: r for f, r in all_tern.items() if f not in primary_formulas}
        _plot_compounds(ax, theory_formulas, elements,
                        marker="o", color="steelblue",
                        label="ternary (predicted)",
                        show_formulas=show_formulas, texts=texts,
                        markersize=markersize, hollow=True)

    _plot_compounds(ax, primary_formulas, elements,
                    marker="o", color="steelblue",
                    label=data["tern_label"],
                    show_formulas=show_formulas, texts=texts,
                    markersize=markersize)

    if show_formulas and texts:
        adjust_text(texts, ax=ax, arrowprops=dict(arrowstyle="-", color="gray", lw=0.5))

    if show_labels:
        # Always show the same fixed four-entry legend — ternary (ICSD),
        # binary (ICSD), ternary (predicted), binary (predicted) — matching
        # the key figure, even on diagrams where some of those categories
        # have no compounds plotted.
        ax.legend(handles=_legend_handles(markersize),
                  loc="upper right",
                  handletextpad=0.25,
                  labelspacing=0.6,
                  fontsize=10,
                  frameon=False,
                  markerscale=LEGEND_MARKER_SCALE,
                  bbox_to_anchor=(1.15, 1.1))
        ax.set_title(filename, pad=20, fontsize=12)

    return fig


def run_plotting(config, group_1, group_2, group_3):
    import matplotlib.pyplot as plt

    data       = _load_diagram_data(config)
    output_dir = data["output_dir"]
    no_labels  = data["no_labels"]

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    _make_blank(output_dir)

    for a, b, c in product(group_1, group_2, group_3):
        elements = [a, b, c]
        filename = f"{a}-{b}-{c}"
        fig = _make_diagram_figure(elements, data, show_labels=not no_labels)
        fig.savefig(Path(output_dir) / f"{filename}.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"Saved {filename}.png")

    print(f"\nDiagrams written to {Path(output_dir).resolve()}")


def run_summary_figure(config, group_1, group_2, group_3, group_names):
    """
    Stitch all individual phase diagrams into a single summary grid, then
    save key.png alongside it.

    Columns = group_2 elements, rows = group_1 × group_3 pairs.
    Saves summary.png and key.png inside plot_dir.

    group_names : three short placeholder labels read from 'groups' in f.args
                  (e.g. ['A', 'M', 'X']); used as vertex labels on key.png.

    The summary always uses clean (label-free, formula-free) diagram cells:
      - no_labels = false (default): clean versions are rendered in-memory via
        BytesIO and never written to disk; the permanent PNGs keep their labels.
      - no_labels = true: the permanent PNGs are already label-free, so they are
        loaded directly without re-rendering.

    Markers are always rendered at 4/3 × the markersize set in f.args so that
    they remain clearly visible at the reduced cell size of the summary grid.
    To opt out, set markersize in f.args to a value scaled accordingly.
    """
    import matplotlib.pyplot as plt
    import matplotlib.image as mpimg
    from matplotlib.gridspec import GridSpec

    data       = _load_diagram_data(config)
    output_dir = data["output_dir"]
    no_labels  = data["no_labels"]
    summary_ms = data["markersize"] * 4 / 3

    row_pairs = [(a, c) for a in group_1 for c in group_3]
    cols      = group_2
    n_rows    = len(row_pairs)
    n_cols    = len(cols)

    cell_w      = 2.2
    cell_h      = 2.0
    label_col_w = 0.9
    header_h    = 0.45

    fig_w = label_col_w + n_cols * cell_w
    fig_h = header_h    + n_rows * cell_h

    fig = plt.figure(figsize=(fig_w, fig_h))
    gs  = GridSpec(
        n_rows + 1, n_cols + 1,
        figure=fig,
        height_ratios=[header_h] + [cell_h] * n_rows,
        width_ratios=[label_col_w] + [cell_w] * n_cols,
        hspace=0.02,
        wspace=0.02,
        left=0, right=1, top=1, bottom=0,
    )

    # Top-left corner (blank)
    fig.add_subplot(gs[0, 0]).axis("off")

    # Column headers (blue, matching the reference figure)
    for col_idx, b in enumerate(cols):
        ax = fig.add_subplot(gs[0, col_idx + 1])
        ax.set_facecolor("#AED6F1")
        ax.text(0.5, 0.5, b, ha="center", va="center", fontsize=11,
                transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)

    # Row labels and diagram cells
    for row_idx, (a, c) in enumerate(row_pairs):
        bg_color = "#E0E0E0" if row_idx % 2 == 0 else "#C8C8C8"
        ax_label = fig.add_subplot(gs[row_idx + 1, 0])
        ax_label.set_facecolor(bg_color)
        ax_label.text(0.5, 0.5, f"{a}–{c}", ha="center", va="center", fontsize=10,
                      transform=ax_label.transAxes)
        ax_label.set_xticks([])
        ax_label.set_yticks([])
        for spine in ax_label.spines.values():
            spine.set_visible(False)

        for col_idx, b in enumerate(cols):
            ax       = fig.add_subplot(gs[row_idx + 1, col_idx + 1])
            elements = [a, b, c]

            if no_labels:
                # Permanent PNGs are already label-free — load directly.
                filepath = Path(output_dir) / f"{a}-{b}-{c}.png"
                if filepath.exists():
                    ax.imshow(mpimg.imread(str(filepath)))
                else:
                    ax.text(0.5, 0.5, f"{a}-{b}-{c}\n(missing)",
                            ha="center", va="center", transform=ax.transAxes,
                            fontsize=8, color="gray")
            else:
                # Render a clean version in-memory; never touches the filesystem.
                cell_fig = _make_diagram_figure(
                    elements, data,
                    show_labels=False,
                    show_formulas=False,
                    markersize=summary_ms,
                )
                buf = io.BytesIO()
                cell_fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
                plt.close(cell_fig)
                buf.seek(0)
                ax.imshow(mpimg.imread(buf))

            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_linewidth(0.5)

    out_path = Path(output_dir) / "summary.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nSaved summary figure: {out_path.resolve()}")

    # Always generate the key alongside the summary.
    _make_key_figure(group_names, group_1, group_2, group_3, data, output_dir)


def main():
    config = load_config("f.args")

    group_1     = parse_elements(config, "group_1")
    group_2     = parse_elements(config, "group_2")
    group_3     = parse_elements(config, "group_3")
    group_names = parse_group_names(config)

    if not all([group_1, group_2, group_3]):
        sys.exit("Error: group_1, group_2, and group_3 must all be non-empty in f.args.")

    validate_groups(group_1, group_2, group_3, group_names)

    print(f"Group 1 ({group_names[0]}): {group_1}")
    print(f"Group 2 ({group_names[1]}): {group_2}")
    print(f"Group 3 ({group_names[2]}): {group_3}")

    run_query   = config.getboolean("query", "run_query",   fallback=True)
    run_plot    = config.getboolean("plot",  "run_plot",    fallback=True)
    run_summary = config.getboolean("plot",  "run_summary", fallback=True)

    t0 = time.perf_counter()

    if run_query:
        print("\n── Querying Materials Project ──")
        run_ternary_query(config, group_1, group_2, group_3)
        run_binary_query(config, group_1, group_2, group_3)
        t1 = time.perf_counter()
        print(f"[timing] query phase: {t1 - t0:.1f}s")
    t1 = time.perf_counter()

    if run_plot:
        print("\n── Generating phase diagrams ───")
        run_plotting(config, group_1, group_2, group_3)
        t2 = time.perf_counter()
        print(f"[timing] plotting phase: {t2 - t1:.1f}s")
    t2 = time.perf_counter()

    if run_summary:
        print("\n── Generating summary figure ────")
        run_summary_figure(config, group_1, group_2, group_3, group_names)
        print(f"[timing] summary phase: {time.perf_counter() - t2:.1f}s")

    print(f"\nDone. Total: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
