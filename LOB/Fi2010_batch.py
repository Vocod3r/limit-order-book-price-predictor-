"""
Batch runner for fi2010_evaluate.py across all Train/Test fold pairs found
in a directory - so you don't have to invoke the script by hand ~50 times.

Discovery rule: for every "Train_Dst_*.txt" file, looks for the matching
"Test_Dst_*.txt" (same suffix, "Train" -> "Test"). Files without a match
are skipped and reported at the end, not silently dropped.

For each matched pair, runs all 5 horizons (k=10/20/30/50/100) through
the same Kalman/IMM-filtered pipeline as fi2010_evaluate.py, and collects
results into one summary table (printed + written to CSV), similar in
spirit to the paper's Table 4/5 layout.

Usage:
    py fi2010_batch.py <directory> [--out results.csv]

Example:
    py fi2010_batch.py C:\\path\\to\\fi2010_files --out summary.csv
"""

import sys
import glob
import os
import csv

from Fi2010_evaluate import load_fold, HORIZON_NAMES
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
import numpy as np


def discover_pairs(directory: str):
    """Finds every Train_Dst_*.txt anywhere under `directory` (recursive -
    handles layouts like Auction_Zscore_Train/ and Auction_Zscore_Test/
    living in separate sibling folders, not just flat in one directory),
    and matches each to its Test_Dst_* counterpart by filename, searched
    recursively under the SAME root (wherever it actually lives).
    Returns (pairs, unmatched) where pairs is a list of
    (label, train_path, test_path) and unmatched is a list of train
    files that had no corresponding test file found anywhere under the
    root - reported, not skipped silently."""
    train_files = sorted(glob.glob(os.path.join(directory, "**", "Train_Dst_*.txt"),
                                    recursive=True))

    # Build a lookup of every Test_Dst_*.txt under the root, keyed by
    # filename, so a Train file in one subfolder can find its Test
    # counterpart in a completely different sibling subfolder.
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
        label = fname[len("Train_Dst_"):-len(".txt")]  # e.g. "Auction_ZScore_CF_1"

        candidates = test_by_name.get(test_fname, [])
        if len(candidates) == 1:
            pairs.append((label, train_path, candidates[0]))
        elif len(candidates) > 1:
            # Same filename found in more than one subfolder - ambiguous,
            # don't guess which one is "right".
            unmatched.append((train_path, f"ambiguous: {len(candidates)} files "
                                           f"named {test_fname} found"))
        else:
            unmatched.append((train_path, "no matching Test file found"))

    return pairs, unmatched


def run_one(train_path: str, test_path: str, horizon: int):
    """Same as fi2010_evaluate.evaluate(), but returns the numbers
    instead of just printing them, so batch mode can collect them into
    a table."""
    X_train, y_train = load_fold(train_path, horizon)
    X_test, y_test = load_fold(test_path, horizon)

    model = LogisticRegression()
    model.fit(X_train, y_train)
    probs = model.predict_proba(X_test)[:, 1]

    model_msr = float(np.mean((probs - y_test) ** 2))
    try:
        model_auc = float(roc_auc_score(y_test, probs))
    except ValueError:
        model_auc = None  # single class in test split for this fold/horizon

    return {
        "train_n": len(y_train), "test_n": len(y_test),
        "model_auc": model_auc, "model_msr": model_msr,
        "x0": float(model.intercept_[0]), "x1": float(model.coef_[0][0]),
    }


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

    print(f"Found {len(pairs)} fold pair(s) to run x 5 horizons "
          f"= {len(pairs) * 5} total evaluations.\n")
    if unmatched:
        print(f"WARNING: {len(unmatched)} Train file(s) skipped:")
        for u, reason in unmatched:
            print(f"  {u}  [{reason}]")
        print()

    rows = []
    for label, train_path, test_path in pairs:
        for h in range(5):
            print(f"[{label}] horizon={HORIZON_NAMES[h]} ...", end=" ", flush=True)
            try:
                r = run_one(train_path, test_path, h)
            except Exception as e:
                print(f"FAILED: {e}")
                rows.append({"fold": label, "horizon": HORIZON_NAMES[h],
                             "status": "error", "error": str(e)})
                continue

            auc_str = f"{r['model_auc']:.3f}" if r["model_auc"] is not None else "n/a"
            print(f"AUC={auc_str} MSR={r['model_msr']:.3f}")

            rows.append({
                "fold": label, "horizon": HORIZON_NAMES[h], "status": "ok",
                "train_n": r["train_n"], "test_n": r["test_n"],
                "model_auc": r["model_auc"], "model_msr": r["model_msr"],
                "x0": r["x0"], "x1": r["x1"],
            })

    fieldnames = ["fold", "horizon", "status", "train_n", "test_n",
                  "model_auc", "model_msr", "x0", "x1", "error"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"\nWrote {len(rows)} row(s) to {out_path}")

    ok_rows = [r for r in rows if r["status"] == "ok" and r["model_auc"] is not None]
    if ok_rows:
        avg_auc = sum(r["model_auc"] for r in ok_rows) / len(ok_rows)
        print(f"Mean AUC across all {len(ok_rows)} successful evaluations: {avg_auc:.3f} "
              f"(null baseline: 0.500)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: py fi2010_batch.py <directory> [--out results.csv]")
        sys.exit(1)

    directory = sys.argv[1]
    out_path = "results.csv"
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]

    main(directory, out_path)