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

AUTOSCALING: LOB_ssm.py's Kalman filter noise constants (R, Q, depletion
bias) were tuned assuming real dollar prices / real share quantities.
FI-2010's normalized values sit on a completely different scale, so
those constants would badly miscalibrate the filter if used as-is. This
script computes each file's actual price/quantity variance and rescales
R/Q/depletion-bias proportionally before running the filter - applied
only for the duration of this script (via monkey-patching LOB_ssm's
module-level R and build_process_noise), never touching the live
pipeline's own values.
"""

import sys
import math
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

import LOB_ssm
from LOB_ssm import run_on_ticks

ASK_PRICE_ROW, ASK_VOL_ROW, BID_PRICE_ROW, BID_VOL_ROW = 0, 1, 2, 3
LABEL_ROWS_START = 144
HORIZON_NAMES = ["k=10", "k=20", "k=30", "k=50", "k=100"]

# The reference scale LOB_ssm.py's constants were originally tuned for -
# real dollar prices (~$0.05 measurement noise std) and real share
# quantities (~1 share measurement noise std, ~depleting over ~50 ticks
# from a queue around a few hundred shares). Used only to compute a
# RATIO for rescaling, not copied as-is.
REFERENCE_QTY_VAR = 1.0
REFERENCE_BASE_Q = 0.05
REFERENCE_DEPLETION_Q = 2.0
REFERENCE_DEPLETION_BIAS = -8.0
R_NOISE_FRACTION = 0.05  # assume ~5% of raw variance is pure measurement noise


def autoscale_filter_constants(ticks):
    """Computes this file's actual price/qty variance and monkey-patches
    LOB_ssm.R and LOB_ssm.build_process_noise so the filter's noise
    assumptions match THIS data's scale, not the live pipeline's
    real-dollar assumptions. Returns the values it applied, for
    transparency."""
    prices = np.array([t["best_bid"] for t in ticks] + [t["best_ask"] for t in ticks])
    qtys = np.array([t["bid_qty"] for t in ticks] + [t["ask_qty"] for t in ticks])
    price_var = max(np.var(prices), 1e-9)
    qty_var = max(np.var(qtys), 1e-9)

    new_R = np.diag([price_var * R_NOISE_FRACTION, price_var * R_NOISE_FRACTION,
                      qty_var * R_NOISE_FRACTION, qty_var * R_NOISE_FRACTION])

    qty_ratio = qty_var / REFERENCE_QTY_VAR
    new_base_q = REFERENCE_BASE_Q * qty_ratio
    new_depletion_q = REFERENCE_DEPLETION_Q * qty_ratio
    new_depletion_bias = REFERENCE_DEPLETION_BIAS * math.sqrt(qty_ratio)

    LOB_ssm.R = new_R

    def scaled_build_process_noise(regime, base_q=new_base_q,
                                    depletion_q=new_depletion_q,
                                    depletion_bias=new_depletion_bias):
        return LOB_ssm._orig_build_process_noise(
            regime, base_q=base_q, depletion_q=depletion_q, depletion_bias=depletion_bias)

    if not hasattr(LOB_ssm, "_orig_build_process_noise"):
        LOB_ssm._orig_build_process_noise = LOB_ssm.build_process_noise
    LOB_ssm.build_process_noise = scaled_build_process_noise

    return {"price_var": price_var, "qty_var": qty_var, "qty_ratio": qty_ratio,
            "base_q": new_base_q, "depletion_q": new_depletion_q,
            "depletion_bias": new_depletion_bias}


def load_fold(path: str, horizon: int):
    """Runs the FI-2010 level-1 sequence through the actual Kalman/IMM
    filter (LOB_ssm.run_on_ticks), with R/Q autoscaled to this file's
    real variance, then pairs the filtered queue_imbalance from each
    tick with FI-2010's real label for that same tick. Rows where the
    label is 'stationary' (2) are dropped, matching the paper's own
    treatment of y (only defined when price actually moved)."""
    data = np.loadtxt(path)

    ticks = [
        {"best_bid": data[BID_PRICE_ROW, i], "best_ask": data[ASK_PRICE_ROW, i],
         "bid_qty": data[BID_VOL_ROW, i], "ask_qty": data[ASK_VOL_ROW, i]}
        for i in range(data.shape[1])
    ]

    scale = autoscale_filter_constants(ticks)
    print(f"  autoscaled: price_var={scale['price_var']:.4f} qty_var={scale['qty_var']:.4f} "
          f"qty_ratio={scale['qty_ratio']:.4f} -> base_q={scale['base_q']:.4f} "
          f"depletion_q={scale['depletion_q']:.4f} depletion_bias={scale['depletion_bias']:.4f}")

    feature_rows = run_on_ticks(ticks)  # same Kalman/IMM pipeline as the live path

    # run_on_ticks consumes tick[0] as the seed state, so feature_rows[i]
    # corresponds to ticks[i+1] / data column (i+1) - align labels the same way
    labels = data[LABEL_ROWS_START + horizon, 1:]

    imbalance = np.array([row["queue_imbalance"] for row in feature_rows])
    mask = labels != 2
    y = (labels[mask] == 1).astype(int)
    X = imbalance[mask].reshape(-1, 1)
    return X, y


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