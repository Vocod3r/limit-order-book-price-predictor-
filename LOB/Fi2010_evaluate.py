"""
Reproduces Gould & Bonart (2015) on the FI-2010 benchmark LOB dataset,
routed through the SAME Kalman/IMM filtering pipeline as LOB_ssm.py's
live auction path - this is not a separate reimplementation of queue
imbalance, it calls LOB_ssm.run_on_ticks() directly and evaluates the
FILTERED queue_imbalance it produces, not a raw ratio.

FI-2010 file layout (149 rows x N timesteps, each column = one event):
  rows 0-3   : level-1 ask price, ask volume, bid price, bid volume
  rows 4-39  : levels 2-10, same 4-column pattern
  rows 40-143: 104 handcrafted features (unused here)
  rows 144-148: labels for horizons k=10,20,30,50,100, coded
                1=up, 2=stationary, 3=down

Usage:
    py fi2010_evaluate.py <train_file> <test_file> [--horizon 0-4]

NO RESCALING: LOB_ssm.py's Kalman filter noise constants (R, Q, depletion
bias) are used exactly as defined in LOB_ssm.py - tuned for real dollar
prices / real share quantities, not adjusted for FI-2010's normalized
scale. This is deliberate: run the filter as-is and take the results for
what they are, rather than compensating for a scale mismatch with guessed
reference constants.
"""

import sys
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from LOB_ssm import run_on_ticks

ASK_PRICE_ROW, ASK_VOL_ROW, BID_PRICE_ROW, BID_VOL_ROW = 0, 1, 2, 3
LABEL_ROWS_START = 144
HORIZON_NAMES = ["k=10", "k=20", "k=30", "k=50", "k=100"]


def load_matrix(path: str) -> np.ndarray:
    """Reads a FI-2010 file. Uses pandas' C-based whitespace parser when
    available - substantially faster than np.loadtxt on large files
    (np.loadtxt parses row-by-row in pure Python internally, which gets
    very slow on 100MB+ files with tens of thousands of columns).
    Falls back to np.loadtxt if pandas isn't installed."""
    try:
        import pandas as pd
        return pd.read_csv(path, sep=r"\s+", header=None, engine="c").values
    except ImportError:
        return np.loadtxt(path)


def load_and_filter(path: str):
    """The expensive, horizon-independent part of load_fold(): reads the
    file once and runs the level-1 sequence through the actual Kalman/IMM
    filter once - using LOB_ssm.py's own R and build_process_noise as-is,
    no rescaling. Imbalance is computed with safe_imbalance() from the
    filter's own bid_queue_size/ask_queue_size state, NOT LOB_ssm.py's
    queue_imbalance field (which uses an a+b denominator that breaks on
    Z-score normalized data - see safe_imbalance() docstring). Returns
    (imbalance, label_matrix) where label_matrix is ALL 5 horizon label
    rows (144-148), so callers that need multiple horizons for the same
    file (e.g. the batch script) only pay the load+filter cost once
    instead of once per horizon."""
    data = load_matrix(path)

    ticks = [
        {"best_bid": data[BID_PRICE_ROW, i], "best_ask": data[ASK_PRICE_ROW, i],
         "bid_qty": data[BID_VOL_ROW, i], "ask_qty": data[ASK_VOL_ROW, i]}
        for i in range(data.shape[1])
    ]

    feature_rows = run_on_ticks(ticks)  # same Kalman/IMM pipeline as the live path,
                                          # LOB_ssm.R / build_process_noise untouched
    filtered_q_b = np.array([row["bid_queue_size"] for row in feature_rows])
    filtered_q_a = np.array([row["ask_queue_size"] for row in feature_rows])
    imbalance = safe_imbalance(filtered_q_b, filtered_q_a)

    label_matrix = data[LABEL_ROWS_START:LABEL_ROWS_START + 5, 1:]
    return imbalance, label_matrix


def safe_imbalance(numerator_a: np.ndarray, numerator_b: np.ndarray) -> np.ndarray:
    """Computes (a - b) / (|a| + |b| + eps) instead of the textbook
    (a - b) / (a + b + eps).

    Why: the textbook ratio assumes a, b >= 0 (real order-book queue
    sizes always are). But Z-score normalized data centers volumes
    around 0, so roughly half of a, b can be NEGATIVE - which makes
    (a + b) flip sign or collapse toward zero, blowing the ratio up to
    values in the hundreds or thousands (confirmed on this dataset: 57%
    of ticks had a negative denominator, 34% of resulting ratios fell
    outside the valid [-1, 1] range). Using |a| + |b| in the denominator
    keeps it non-negative regardless of the sign of the inputs, so the
    result stays properly bounded in [-1, 1] - a - b still correctly
    reflects which side dominates and by how much."""
    return (numerator_a - numerator_b) / (np.abs(numerator_a) + np.abs(numerator_b) + 1e-9)


def load_and_filter_with_raw(path: str):
    """Like load_and_filter(), but also returns the RAW (unfiltered)
    imbalance ratio computed directly from the observed bid/ask
    quantities - the same quantity LOB_ssm.run_on_ticks() feeds INTO the
    filter, before any smoothing. This lets you isolate what the filter
    actually changes, by comparing:

      raw_imbalance       - what Gould & Bonart's own method uses directly
      filtered_imbalance  - the Kalman/IMM-smoothed version
      residual            - raw - filtered: the part of each observation
                             the filter treated as noise and blended away

    Both raw and filtered use safe_imbalance() (|a|+|b| denominator), NOT
    LOB_ssm.py's own queue_imbalance field, which uses the textbook
    a+b denominator and is therefore equally corrupted by Z-score's
    negative values - LOB_ssm.py itself is left untouched, but its
    bid_queue_size/ask_queue_size state (the filtered q_b, q_a) is pulled
    out and the imbalance is recomputed safely here instead.

    Returns (raw_imbalance, filtered_imbalance, residual, label_matrix),
    all aligned to the same tick indices as load_and_filter()."""
    data = load_matrix(path)

    ticks = [
        {"best_bid": data[BID_PRICE_ROW, i], "best_ask": data[ASK_PRICE_ROW, i],
         "bid_qty": data[BID_VOL_ROW, i], "ask_qty": data[ASK_VOL_ROW, i]}
        for i in range(data.shape[1])
    ]

    feature_rows = run_on_ticks(ticks)
    filtered_q_b = np.array([row["bid_queue_size"] for row in feature_rows])
    filtered_q_a = np.array([row["ask_queue_size"] for row in feature_rows])
    filtered_imbalance = safe_imbalance(filtered_q_b, filtered_q_a)

    bid_qty = np.array([t["bid_qty"] for t in ticks[1:]])
    ask_qty = np.array([t["ask_qty"] for t in ticks[1:]])
    raw_imbalance = safe_imbalance(bid_qty, ask_qty)

    residual = raw_imbalance - filtered_imbalance

    label_matrix = data[LABEL_ROWS_START:LABEL_ROWS_START + 5, 1:]
    return raw_imbalance, filtered_imbalance, residual, label_matrix


def load_diagnostics(path: str):
    """Like load_and_filter_with_raw(), but also pulls the IMM's regime
    probabilities (p_stable, p_bid_depleting, p_ask_depleting) per tick -
    needed to check whether large residuals cluster around moments the
    filter suspects a depletion event, i.e. whether 'what got filtered
    out' concentrates around regime changes rather than being spread
    uniformly. Uses safe_imbalance() throughout - see its docstring.
    Returns a dict of aligned arrays."""
    data = load_matrix(path)

    ticks = [
        {"best_bid": data[BID_PRICE_ROW, i], "best_ask": data[ASK_PRICE_ROW, i],
         "bid_qty": data[BID_VOL_ROW, i], "ask_qty": data[ASK_VOL_ROW, i]}
        for i in range(data.shape[1])
    ]

    feature_rows = run_on_ticks(ticks)
    filtered_q_b = np.array([row["bid_queue_size"] for row in feature_rows])
    filtered_q_a = np.array([row["ask_queue_size"] for row in feature_rows])
    filtered_imbalance = safe_imbalance(filtered_q_b, filtered_q_a)
    p_bid_depleting = np.array([row["p_bid_depleting"] for row in feature_rows])
    p_ask_depleting = np.array([row["p_ask_depleting"] for row in feature_rows])
    p_stable = np.array([row["p_stable"] for row in feature_rows])

    bid_qty = np.array([t["bid_qty"] for t in ticks[1:]])
    ask_qty = np.array([t["ask_qty"] for t in ticks[1:]])
    raw_imbalance = safe_imbalance(bid_qty, ask_qty)

    residual = raw_imbalance - filtered_imbalance
    label_matrix = data[LABEL_ROWS_START:LABEL_ROWS_START + 5, 1:]

    return {
        "raw": raw_imbalance, "filtered": filtered_imbalance, "residual": residual,
        "p_stable": p_stable, "p_bid_depleting": p_bid_depleting,
        "p_ask_depleting": p_ask_depleting, "label_matrix": label_matrix,
    }


def labels_for_horizon(imbalance: np.ndarray, label_matrix: np.ndarray, horizon: int):
    """The cheap, horizon-specific part: pick the label row for this
    horizon, drop 'stationary' (2) ticks, and align with the imbalance
    array. No file I/O or filtering here - safe to call once per horizon
    on already-loaded/filtered data."""
    labels = label_matrix[horizon]
    mask = labels != 2
    y = (labels[mask] == 1).astype(int)
    X = imbalance[mask].reshape(-1, 1)
    return X, y


def load_fold(path: str, horizon: int):
    """Runs the FI-2010 level-1 sequence through the actual Kalman/IMM
    filter (LOB_ssm.run_on_ticks), using LOB_ssm.py's own R and
    build_process_noise constants unmodified, then pairs the filtered
    queue_imbalance from each tick with FI-2010's real label for that
    same tick. Rows where the label is 'stationary' (2) are dropped,
    matching the paper's own treatment of y (only defined when price
    actually moved).

    Kept for single-horizon / standalone use (this is what the CLI below
    calls). If you need multiple horizons for the same file, call
    load_and_filter() once and labels_for_horizon() per horizon instead -
    that's what the batch script does, to avoid refiltering the same
    file 5 times."""
    imbalance, label_matrix = load_and_filter(path)
    return labels_for_horizon(imbalance, label_matrix, horizon)


def evaluate(train_path: str, test_path: str, horizon: int):
    X_train, y_train = load_fold(train_path, horizon)
    X_test, y_test = load_fold(test_path, horizon)

    print(f"Horizon {HORIZON_NAMES[horizon]}: train n={len(y_train)} "
          f"({y_train.sum()} up, {len(y_train) - y_train.sum()} down), "
          f"test n={len(y_test)} ({y_test.sum()} up, {len(y_test) - y_test.sum()} down)")

    model = LogisticRegression()
    model.fit(X_train, y_train)
    probs = model.predict_proba(X_test)[:, 1]

    model_msr = np.mean((probs - y_test) ** 2)
    model_auc = roc_auc_score(y_test, probs)
    null_msr, null_auc = 0.25, 0.5

    print(f"AUC-ROC:              model={model_auc:.3f}   null_baseline={null_auc:.3f}")
    print(f"Mean squared residual: model={model_msr:.3f}   null_baseline={null_msr:.3f}"
          f"   ({(null_msr - model_msr) / null_msr * 100:+.1f}% vs. baseline)")
    print(f"Fitted coefficients: x0={model.intercept_[0]:.3f}  x1={model.coef_[0][0]:.3f}\n")

    return {"model_auc": model_auc, "model_msr": model_msr}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: py fi2010_evaluate.py <train_file> <test_file> [--horizon 0-4]")
        sys.exit(1)

    train_path, test_path = sys.argv[1], sys.argv[2]
    horizon = 0
    if "--horizon" in sys.argv:
        horizon = int(sys.argv[sys.argv.index("--horizon") + 1])

    evaluate(train_path, test_path, horizon)