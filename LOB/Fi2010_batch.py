"""
Batch runner across all Train/Test fold pairs found in a directory - so
you don't have to invoke anything by hand ~50 times.

Discovery rule: recursively finds every "Train_Dst_*.txt" file anywhere
under the given root, and matches each to its "Test_Dst_*.txt" counterpart
(same filename, "Train"->"Test") found anywhere else under that root -
handles layouts where Train/Test live in separate sibling folders (e.g.
Auction_ZScore_Training/ and Auction_ZScore_Testing/).

PERFORMANCE: loads and Kalman/IMM-filters each file ONCE (via
fi2010_evaluate.load_and_filter_with_raw), then reuses that cached result
across all 5 horizons instead of re-reading and re-filtering the file
from scratch per horizon - a ~5x speedup over calling load_fold() once
per horizon.

THREE-WAY COMPARISON: for every fold x horizon, fits and scores THREE
separate logistic regressions:
  raw       - unfiltered imbalance ratio, straight from observed
              quantities (this is what Gould & Bonart (2015) use)
  filtered  - the Kalman/IMM-smoothed imbalance
  residual  - raw - filtered (the part of each observation the filter
              treated as noise and blended away)
This directly answers "is the filter helping or hurting", across every
fold, instead of eyeballing one file at a time.

NO RESCALING: uses LOB_ssm.py's own R / build_process_noise constants as
defined, unmodified - see fi2010_evaluate.py.

SAFETY: writes each result row to the CSV immediately as it completes,
so if you stop the script partway through, or it crashes, everything
computed so far is already saved to disk.

Usage:
    py fi2010_batch.py <directory> [--out results.csv]

Example:
    py fi2010_batch.py C:\\path\\to\\Auction --out summary.csv
"""

import sys
import glob
import os
import csv
import time

from Fi2010_evaluate import load_and_filter_with_raw, labels_for_horizon, HORIZON_NAMES
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
import numpy as np

SIGNALS = ["raw", "filtered", "residual"]
FIELDNAMES = ["fold", "horizon", "status", "train_n", "test_n"]
for _s in SIGNALS:
    FIELDNAMES += [f"{_s}_auc", f"{_s}_msr", f"{_s}_x0", f"{_s}_x1"]
FIELDNAMES.append("error")


def discover_pairs(directory: str):
    """Finds every Train_Dst_*.txt anywhere under `directory` (recursive),
    and matches each to its Test_Dst_* counterpart by filename, searched
    recursively under the SAME root (wherever it actually lives).
    Returns (pairs, unmatched)."""
    train_files = sorted(glob.glob(os.path.join(directory, "**", "Train_Dst_*.txt"),
                                    recursive=True))

    test_files = glob.glob(os.path.join(directory, "**", "Test_Dst_*.txt"),
                            recursive=True)
    test_by_name = {}
    for t in test_files:
        test_by_name.setdefault(os.path.basename(t), []).append(t)

    pairs = []
    unmatched = []

    for train_path in train_files:
        fname = os.path.basename(train_path)
        test_fname = fname.replace("Train_Dst_", "Test_Dst_", 1)
        label = fname[len("Train_Dst_"):-len(".txt")]

        candidates = test_by_name.get(test_fname, [])
        if len(candidates) == 1:
            pairs.append((label, train_path, candidates[0]))
        elif len(candidates) > 1:
            unmatched.append((train_path, f"ambiguous: {len(candidates)} files "
                                           f"named {test_fname} found"))
        else:
            unmatched.append((train_path, "no matching Test file found"))

    return pairs, unmatched


def fit_and_score(X_train, y_train, X_test, y_test):
    model = LogisticRegression()
    model.fit(X_train, y_train)
    probs = model.predict_proba(X_test)[:, 1]

    msr = float(np.mean((probs - y_test) ** 2))
    try:
        auc = float(roc_auc_score(y_test, probs))
    except ValueError:
        auc = None  # single class in this test split

    return {"auc": auc, "msr": msr,
            "x0": float(model.intercept_[0]), "x1": float(model.coef_[0][0])}


def load_completed_folds(out_path: str) -> set:
    """Scans an existing results CSV (from a previous, possibly
    interrupted run) and returns the set of fold labels that already
    have all 5 horizons present with status='ok'. Used to skip
    re-computing folds that finished successfully last time, so an
    interrupted run can resume instead of starting over from pair 1.
    Returns an empty set if the file doesn't exist or can't be read."""
    if not os.path.exists(out_path):
        return set()

    ok_counts = {}
    try:
        with open(out_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") == "ok":
                    fold = row["fold"]
                    ok_counts[fold] = ok_counts.get(fold, 0) + 1
    except Exception as e:
        print(f"Could not read existing {out_path} to resume ({e}) - starting fresh.")
        return set()

    completed = {fold for fold, count in ok_counts.items() if count >= 5}
    return completed


def main(directory: str, out_path: str):
    pairs, unmatched = discover_pairs(directory)

    if not pairs:
        print(f"No Train_Dst_*.txt / Test_Dst_*.txt pairs found under {directory} "
              f"(searched recursively).")
        if unmatched:
            print(f"({len(unmatched)} Train file(s) found with no usable Test match)")
            for u, reason in unmatched:
                print(f"  {u}  [{reason}]")
        return

    completed = load_completed_folds(out_path)
    if completed:
        print(f"Found {len(completed)} already-completed fold(s) in {out_path} - "
              f"these will be SKIPPED, not recomputed:")
        for f in sorted(completed):
            print(f"  {f}")
        print()
        pairs = [(label, tr, te) for label, tr, te in pairs if label not in completed]

    print(f"Found {len(pairs)} fold pair(s) remaining to run x 5 horizons x 3 signals "
          f"(raw/filtered/residual) = {len(pairs) * 5} rows.\n")
    if unmatched:
        print(f"WARNING: {len(unmatched)} Train file(s) skipped:")
        for u, reason in unmatched:
            print(f"  {u}  [{reason}]")
        print()

    if not pairs:
        print("Nothing left to do - all folds already completed.")
        return

    file_exists = os.path.exists(out_path)
    csv_file = open(out_path, "a", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
    if not file_exists:
        writer.writeheader()
        csv_file.flush()

    n_ok = 0
    all_aucs = {s: [] for s in SIGNALS}
    t_start = time.time()

    for pair_idx, (label, train_path, test_path) in enumerate(pairs, start=1):
        t_pair_start = time.time()
        print(f"[{pair_idx}/{len(pairs)}] {label}: loading + filtering "
              f"train file...", flush=True)
        try:
            raw_tr, filt_tr, resid_tr, labels_tr = load_and_filter_with_raw(train_path)
        except Exception as e:
            print(f"  FAILED to load/filter train file: {e}")
            for h in range(5):
                writer.writerow({"fold": label, "horizon": HORIZON_NAMES[h],
                                  "status": "error",
                                  "error": f"train load/filter failed: {e}"})
            csv_file.flush()
            continue

        print(f"[{pair_idx}/{len(pairs)}] {label}: loading + filtering "
              f"test file...", flush=True)
        try:
            raw_te, filt_te, resid_te, labels_te = load_and_filter_with_raw(test_path)
        except Exception as e:
            print(f"  FAILED to load/filter test file: {e}")
            for h in range(5):
                writer.writerow({"fold": label, "horizon": HORIZON_NAMES[h],
                                  "status": "error",
                                  "error": f"test load/filter failed: {e}"})
            csv_file.flush()
            continue

        train_signals = {"raw": raw_tr, "filtered": filt_tr, "residual": resid_tr}
        test_signals = {"raw": raw_te, "filtered": filt_te, "residual": resid_te}

        for h in range(5):
            row = {"fold": label, "horizon": HORIZON_NAMES[h], "status": "ok"}
            row_ok = True
            summary_bits = []

            for sig in SIGNALS:
                X_train, y_train = labels_for_horizon(train_signals[sig], labels_tr, h)
                X_test, y_test = labels_for_horizon(test_signals[sig], labels_te, h)

                if sig == SIGNALS[0]:
                    row["train_n"] = len(y_train)
                    row["test_n"] = len(y_test)

                try:
                    r = fit_and_score(X_train, y_train, X_test, y_test)
                except Exception as e:
                    row_ok = False
                    row["status"] = "error"
                    row["error"] = f"{sig}: {e}"
                    break

                row[f"{sig}_auc"] = r["auc"]
                row[f"{sig}_msr"] = r["msr"]
                row[f"{sig}_x0"] = r["x0"]
                row[f"{sig}_x1"] = r["x1"]

                auc_str = f"{r['auc']:.3f}" if r["auc"] is not None else "n/a"
                summary_bits.append(f"{sig}={auc_str}")
                if r["auc"] is not None:
                    all_aucs[sig].append(r["auc"])

            print(f"  [{HORIZON_NAMES[h]}] " + "  ".join(summary_bits) if row_ok
                  else f"  [{HORIZON_NAMES[h]}] FAILED: {row.get('error')}")

            writer.writerow(row)
            csv_file.flush()
            if row_ok:
                n_ok += 1

        elapsed_pair = time.time() - t_pair_start
        elapsed_total = time.time() - t_start
        avg_per_pair = elapsed_total / pair_idx
        remaining = avg_per_pair * (len(pairs) - pair_idx)
        print(f"  ({elapsed_pair:.0f}s for this pair, "
              f"~{remaining/60:.1f} min remaining)\n", flush=True)

    csv_file.close()
    print(f"Done. Wrote {n_ok} successful row(s) to {out_path}")
    for sig in SIGNALS:
        if all_aucs[sig]:
            print(f"Mean {sig} AUC across {len(all_aucs[sig])} evaluations: "
                  f"{sum(all_aucs[sig])/len(all_aucs[sig]):.3f} (null baseline: 0.500)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: py fi2010_batch.py <directory> [--out results.csv]")
        sys.exit(1)

    directory = sys.argv[1]
    out_path = "results.csv"
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]

    main(directory, out_path)