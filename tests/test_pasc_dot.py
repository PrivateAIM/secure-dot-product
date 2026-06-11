import os
import pickle
import tempfile

import numpy as np
from flame.star import StarModelTester

from secureDotProduct.mpc_dot_pasc import PASCAggregator, PASCAnalyzer


def _run_star_tester(X, Y) -> dict:
    """Run StarModelTester and return the aggregator result dict.

    StarModelTester does not expose the result directly; we capture it via
    a temporary pickle file (the SDK writes to the exact path when filename
    is a plain string with no multiple_results).
    """
    fd, path = tempfile.mkstemp(dir=os.environ.get("TMPDIR", tempfile.gettempdir()))
    os.close(fd)
    try:
        StarModelTester(
            data_splits=[[{"matrix.pkl": pickle.dumps(X)}], [{"matrix.pkl": pickle.dumps(Y)}]],
            analyzer=PASCAnalyzer,
            aggregator=PASCAggregator,
            data_type="s3",
            simple_analysis=False,
            output_type="pickle",
            filename=path,
        )
        with open(path, "rb") as f:
            return pickle.loads(f.read())
    finally:
        os.unlink(path)


def _assert_full_kernel(result: dict, X: list, Y: list, tol: float = 0.05) -> None:
    """Assert that the full symmetric kernel is correct.

    The kernel has shape (n_a+n_b) × (n_a+n_b) with x-side patients in rows
    0..n_a-1 and y-side patients in rows n_a..n_a+n_b-1.  Which cohort (X or Y)
    is the x-side is determined at runtime by UUID; we resolve it from
    n_patients_x in the result.

    Args:
        result: Aggregator result dict containing "kernel_matrix", "n_patients_x",
            and "n_patients_y".
        X: Data matrix for one cohort.
        Y: Data matrix for the other cohort.
        tol: Absolute tolerance for fixed-point rounding.
    """
    K = np.array(result["kernel_matrix"])
    n_a = result["n_patients_x"]
    n_b = result["n_patients_y"]
    Xa, Ya = np.array(X), np.array(Y)

    assert K.shape == (n_a + n_b, n_a + n_b), f"Expected ({n_a + n_b},{n_a + n_b}), got {K.shape}"
    np.testing.assert_allclose(K, K.T, atol=tol, err_msg="Kernel not symmetric")

    # Resolve orientation: x-side rows come first in K.
    if n_a == len(X) and n_b == len(Y):
        A, B = Xa, Ya
    elif n_a == len(Y) and n_b == len(X):
        A, B = Ya, Xa
    else:
        raise AssertionError(
            f"n_patients_x={n_a}, n_patients_y={n_b} inconsistent with len(X)={len(X)}, len(Y)={len(Y)}"
        )

    expected = np.block([[A @ A.T, A @ B.T], [B @ A.T, B @ B.T]])
    np.testing.assert_allclose(K, expected, atol=tol)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_kernel_small():
    """Full 5×5 symmetric kernel is correct for a 2-patient vs 3-patient case."""
    X = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    Y = [[1.0, 0.0, 1.0], [2.0, 1.0, 0.0], [0.0, 1.0, 2.0]]

    result = _run_star_tester(X, Y)
    _assert_full_kernel(result, X, Y)


def test_full_kernel_larger():
    """Full symmetric kernel is correct for a 3-patient vs 5-patient case."""
    X = [[1.0, -1.0, 2.0, 0.5], [0.0, 3.0, -1.0, 1.0], [-2.0, 1.0, 0.0, 2.0]]
    Y = [
        [2.0, 0.0, -1.0, 1.0],
        [1.0, 1.0, 1.0, 1.0],
        [0.0, -1.0, 2.0, -0.5],
        [-1.0, 2.0, 0.5, -1.0],
        [3.0, -1.0, 0.0, 0.5],
    ]

    result = _run_star_tester(X, Y)
    _assert_full_kernel(result, X, Y)


def test_full_kernel_shape_and_symmetry():
    """Kernel shape is (n_a+n_b)×(n_a+n_b) and the matrix is symmetric."""
    import random

    n_a, n_b, d = 4, 3, 6
    rng = random.Random(99)
    X = [[rng.uniform(-2.0, 2.0) for _ in range(d)] for _ in range(n_a)]
    Y = [[rng.uniform(-2.0, 2.0) for _ in range(d)] for _ in range(n_b)]

    result = _run_star_tester(X, Y)
    _assert_full_kernel(result, X, Y)
