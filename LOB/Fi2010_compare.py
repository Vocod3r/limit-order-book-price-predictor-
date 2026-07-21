"""
Isolates what the Kalman/IMM filter actually changes, by comparing THREE
versions of imbalance against the same real FI-2010 labels:

  raw       - (bid_qty - ask_qty)/(bid_qty + ask_qty), computed directly
              from observed quantities, no filtering at all. This is
              exactly what Gould & Bonart (2015) use.
  filtered  - the Kalman/IMM-smoothed imbalance from LOB_ssm.run_on_ticks().
  residual  - raw - filtered: the part of each tick's raw signal the
              filter treated as noise and blended away. A direct proxy
              for "what got filtered out" without touching LOB_ssm.py's
              internals.

If raw clearly beats filtered, that's evidence the smoothing itself is
suppressing the paper's signal. If residual ALSO predicts well (better
than filtered, comparable to raw), that specifically confirms the
signal lives in the high-frequency part the filter discards - not
somewhere else entirely.

Usage:
    py fi2010_compare.py <train_file> <test_file> [--horizon 0-4]
"""

import sys
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from Fi2010_evaluate import load_and_filter_with_raw, labels_for_horizon, HORIZON_NAMES


def score(X_train, y_train, X_test, y_test):
    model = LogisticRegression()
    model.fit(X_train, y_train)
    probs = model.predict_proba(X_test)[:, 1]
    msr = float(np.mean((probs - y_test) ** 2))
    try:
        auc = float(roc_auc_score(y_test, probs))
    except ValueError:
        auc = None
    return auc, msr, float(model.intercept_[0]), float(model.coef_[0][0])


def compare(train_path: str, test_path: str, horizon: int):
    print(f"Loading + filtering train file ({train_path})...")
    raw_train, filt_train, resid_train, labels_train = load_and_filter_with_raw(train_path)
    print(f"Loading + filtering test file ({test_path})...")
    raw_test, filt_test, resid_test, labels_test = load_and_filter_with_raw(test_path)

    print(f"\n=== Horizon {HORIZON_NAMES[horizon]} ===\n")
    print(f"{'signal':<10} {'AUC':>8} {'null':>8} {'MSR':>8} {'x0':>8} {'x1':>8}")

    results = {}
    for name, train_signal, test_signal in [
        ("raw", raw_train, raw_test),
        ("filtered", filt_train, filt_test),
        ("residual", resid_train, resid_test),
    ]:
        X_train, y_train = labels_for_horizon(train_signal, labels_train, horizon)
        X_test, y_test = labels_for_horizon(test_signal, labels_test, horizon)

        auc, msr, x0, x1 = score(X_train, y_train, X_test, y_test)
        results[name] = auc
        auc_str = f"{auc:.3f}" if auc is not None else "n/a"
        print(f"{name:<10} {auc_str:>8} {'0.500':>8} {msr:>8.3f} {x0:>8.3f} {x1:>8.3f}")

    print()
    if results["raw"] is not None and results["filtered"] is not None:
        diff = results["raw"] - results["filtered"]
        if diff > 0.01:
            print(f"raw beats filtered by {diff:+.3f} AUC - consistent with the filter "
                  f"suppressing real signal, not just noise.")
        elif diff < -0.01:
            print(f"filtered beats raw by {-diff:+.3f} AUC - filtering is HELPING here, "
                  f"opposite of the smoothing hypothesis.")
        else:
            print(f"raw and filtered are within 0.01 AUC of each other - no clear "
                  f"difference on this fold/horizon.")

    if results["residual"] is not None and results["filtered"] is not None:
        if results["residual"] > results["filtered"] + 0.01:
            print(f"residual beats filtered ({results['residual']:.3f} vs "
                  f"{results['filtered']:.3f}) - the high-frequency part the filter "
                  f"discards carries more signal than what it kept.")

    return results


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: py fi2010_compare.py <train_file> <test_file> [--horizon 0-4]")
        sys.exit(1)

    train_path, test_path = sys.argv[1], sys.argv[2]
    horizon = 0
    if "--horizon" in sys.argv:
        horizon = int(sys.argv[sys.argv.index("--horizon") + 1])

    compare(train_path, test_path, horizon)