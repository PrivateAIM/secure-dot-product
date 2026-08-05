"""
Secure pairwise RBF (Gaussian) kernels across two cohorts, one kernel per data
modality, using the proxy trusted dealer and the SAME Beaver core as the
dot-product kernel.

    K_m[i,j] = exp(-gamma * ||X_i - Y_j||^2)
             = exp(-gamma * (||X_i||^2 + ||Y_j||^2 - 2 * X_i . Y_j))

Expanding the squared distance is what makes this federated-friendly: the only
term that mixes data from both cohorts is the cross product X_i . Y_j, which is
exactly what mpc_dot_proxy already computes. The two squared-norm terms never
leave the cohort that owns them, and exp() is a monotone element-wise map applied
after the squared distance is reconstructed.

The norms are folded INTO the shared value rather than sent alongside it: each
analyzer scales its Beaver output by -2 and adds its own encoded row norms into
the matching blocks. Both steps are linear, so they are free on additive shares,
and the aggregator reconstructs ||X_i - Y_j||^2 in one go without ever seeing
either cohort's norms. That matters because the RBF kernel is translation
invariant -- releasing the norms would pin down the origin, which the kernel
itself never reveals. What the aggregator can still infer from the finished
kernel is the point configuration up to a rigid motion, which is exactly the
information content of the output it is meant to produce.

Multiple modalities
-------------------
Each site holds one matrix per modality, keyed by its data file's stem. Within a
modality both sites agree on the column count (features) but row counts (samples)
may differ; across modalities the column count may differ too. Each modality
therefore gets its own independent kernel.

Every modality's tiled vector is concatenated into ONE flat vector, multiplied
with a single Beaver batch, and sliced apart again by the aggregator. This is a
transport detail, not the mental model -- the analyzer and aggregator bodies below
still read as a loop over modalities, with the offset arithmetic confined to
`segment_offsets` and `reconstruct_dot_block`.

The concatenation is what lets this stay a thin protocol file. `BeaverDealerProxy`
deals exactly one triple batch per run, and `await_intermediate_data` keeps only
the newest message per (sender, category) pair -- so M separate announcements or
M separate triple messages would be silently collapsed to the last one. Running
one batch per modality would mean modality-scoped message categories and a
reworked dealer lifecycle in mpc_core.py.

Both sides must walk modalities in the same order for the concatenation to line
up -- everything below iterates `sorted(...)` for that reason.

Round structure (simple_analysis=False, 2 rounds) is identical to mpc_dot_proxy.
"""

import math
import pickle
from pathlib import PurePosixPath
from typing import Any

import numpy as np
from flame.proxy import ProxyAggregator, ProxyAnalyzer, ProxyModelTester
from mpc_core import PRECISION_BITS, RING_BITS, BeaverCore, BeaverDealerProxy, decode, tile_x, tile_y

# Kernel width. Every node runs this same module, so the constant is shared by
# construction; promote it to an analysis parameter if the federation ever needs
# to tune it per run (or make it a dict keyed by modality).
GAMMA = 0.25


def rbf_kernel_matrix(X: np.ndarray, Y: np.ndarray, gamma: float) -> np.ndarray:
    """
    Kernel Matrix Calculation for matrices.

    Args:
        X: Input array of shape (n_samples_X, n_features)
        Y: Input array of shape (n_samples_Y, n_features)
        gamma: RBF kernel parameter (>0)

    Returns:
        Kernel matrix of shape (n_samples_X, n_samples_Y)

    """
    # Squared norm of X and Y
    XX = np.sum(X * X, axis=1, keepdims=True)  # (n, 1)
    YY = np.sum(Y * Y, axis=1, keepdims=True).T  # (1, m)

    # Dot product between X and Y
    XY = X @ Y.T  # (n, m)

    return np.exp(-gamma * (XX + YY - 2.0 * XY))


def modality_name(filename: str) -> str:
    """Modality key for a data file, e.g. 'omics.pkl' -> 'omics'."""
    return PurePosixPath(filename).stem


def segment_offsets(shapes: dict[str, tuple[int, int, int]]) -> dict[str, int]:
    """Start index of each modality's block within the concatenated flat vector.

    Args:
        shapes: Modality -> (n_x, n_y, d), the shapes both cohorts agreed on.

    Returns:
        Modality -> offset. Modalities are walked in sorted order so both
        analyzers and the aggregator derive byte-identical layouts.
    """
    offsets, cursor = {}, 0
    for name in sorted(shapes):
        n_x, n_y, d = shapes[name]
        offsets[name] = cursor
        cursor += n_x * n_y * d
    return offsets


def total_length(shapes: dict[str, tuple[int, int, int]]) -> int:
    """Number of ring elements the concatenated vector spans across all modalities."""
    return sum(n_x * n_y * d for n_x, n_y, d in shapes.values())


def block_start(offset: int, i: int, j: int, n_y: int, d: int) -> int:
    """Index where block (i, j) of a modality begins, matching the tile_x / tile_y layout."""
    return offset + (i * n_y + j) * d


def reconstruct_dist_block(w: list[int], offset: int, n_x: int, n_y: int, d: int, ring: int) -> list[list[float]]:
    """Block-sum one modality's slice into squared distances.

    Each entry is a run of `d` consecutive ring elements. The analyzers already
    folded the -2 scaling and their own row norms into those runs, so the sum is
    ||X_i - Y_j||^2 rather than a bare dot product.

    Args:
        w: Reconstructed (summed, still encoded) share vector for all modalities.
        offset: This modality's start index in `w`.
        n_x: Row count of the x-side cohort for this modality.
        n_y: Row count of the y-side cohort for this modality.
        d: Feature count for this modality.
        ring: Ring size, 2**RING_BITS.

    Returns:
        The n_x by n_y matrix of squared distances, decoded to floats.
    """
    block = []
    for i in range(n_x):
        row = []
        for j in range(n_y):
            start = block_start(offset, i, j, n_y, d)
            row.append(decode(sum(w[start : start + d]) % ring, PRECISION_BITS))
        block.append(row)
    return block


class RBFAnalyzer(ProxyAnalyzer, BeaverCore):
    def analysis_method(self, data, aggregator_results) -> Any:
        mats = {modality_name(fn): pickle.loads(blob) for fn, blob in data[0].items()}
        partner_id = self.partner_analyzer_id()  # this assumes only 3 analyzers
        is_x = self.id < partner_id

        if self.num_iterations == 0:  # round 0: shapes + setup
            local = {m: (len(X), len(X[0])) for m, X in mats.items()}
            self.flame.send_intermediate_data([partner_id], local, message_category="shape_info")
            partner = self.flame.await_intermediate_data([partner_id], message_category="shape_info")[partner_id]
            if set(local) != set(partner):
                raise ValueError(f"modality mismatch: {sorted(local)} vs {sorted(partner)}")

            # Row counts may differ per modality and per cohort; column counts must
            # match within a modality but are free to differ across modalities.
            self.shapes = {}
            for m in sorted(local):
                n_local, d_local = local[m]
                n_partner, d_partner = partner[m]
                if d_local != d_partner:
                    raise ValueError(f"[{m}] feature mismatch: {d_local} vs {d_partner}")
                n_x, n_y = (n_local, n_partner) if is_x else (n_partner, n_local)
                self.shapes[m] = (n_x, n_y, d_local)

            self.announce_length(total_length(self.shapes))  # one triple batch for every modality
            return None

        flat = []  # round 1: compute
        for m in sorted(self.shapes):
            n_x, n_y, _ = self.shapes[m]
            flat.extend(tile_x(mats[m], n_y) if is_x else tile_y(mats[m], n_x))
        triple = self.flame.await_intermediate_data([self.proxy_id], message_category="beaver_triple")[self.proxy_id]
        z_i = self.beaver_multiply(flat, triple, partner_id, is_x)

        # Fold the squared-distance expansion into the share instead of shipping the
        # norms: scale the cross term by -2, then add this cohort's OWN row norms into
        # each block. Both are linear, so they cost nothing on additive shares, and the
        # aggregator ends up reconstructing ||X_i - Y_j||^2 without seeing either side's
        # norms. Every block gets its norm at the block's first index; which index is
        # arbitrary as long as it lands inside the run the aggregator sums.
        dist_share = [(-2 * v) % self.ringsize for v in z_i]
        offsets = segment_offsets(self.shapes)
        for m in sorted(self.shapes):
            n_x, n_y, d = self.shapes[m]
            own_norms = [sum(v * v for v in row) for row in mats[m]]  # indexed by i if x-side, else by j
            for i in range(n_x):
                for j in range(n_y):
                    start = block_start(offsets[m], i, j, n_y, d)
                    dist_share[start] = (dist_share[start] + self.encode(own_norms[i if is_x else j])) % self.ringsize

        # Diagonal blocks are plain in-the-clear RBFs over our own rows. Being already
        # exponentiated, they carry no norm information (their diagonal is all ones).
        K_local = {m: rbf_kernel_matrix(np.asarray(X), np.asarray(X), GAMMA).tolist() for m, X in mats.items()}

        self.flame.send_intermediate_data(
            [self.flame.get_aggregator_id()],
            {
                "dist_share": dist_share,
                "K_local": K_local,
                "is_x": is_x,
                "shapes": self.shapes,
            },
            message_category="final_shares",
        )
        self.finished = True
        return None


class RBFAggregator(ProxyAggregator):
    ring = 1 << RING_BITS

    def aggregation_method(self, proxy_results):
        if self.num_iterations == 0:
            return None
        res = self.flame.await_intermediate_data(self.analyzer_ids, message_category="final_shares")
        results = list(res.values())
        x_res = next(r for r in results if r["is_x"])
        y_res = next(r for r in results if not r["is_x"])

        # A layout disagreement would silently misalign every modality after the
        # offending one, so fail loudly instead of returning plausible garbage.
        shapes = x_res["shapes"]
        if shapes != y_res["shapes"]:
            raise ValueError(f"analyzers disagree on layout: {shapes} vs {y_res['shapes']}")
        w = [sum(col) % self.ring for col in zip(*[r["dist_share"] for r in results])]
        if len(w) != total_length(shapes):
            raise ValueError(f"share length {len(w)} does not match layout {total_length(shapes)}")

        offsets = segment_offsets(shapes)
        kernels = {}
        for m in sorted(shapes):
            n_x, n_y, d = shapes[m]

            # The analyzers already folded in the -2 and their own norms, so this is
            # ||X_i - Y_j||^2 directly. Fixed-point error can push a near-zero distance
            # slightly negative, which would give exp(+x) > 1, so clamp first.
            dist_sq = reconstruct_dist_block(w, offsets[m], n_x, n_y, d, self.ring)
            K_XY = [[math.exp(-GAMMA * max(0.0, dist_sq[i][j])) for j in range(n_y)] for i in range(n_x)]

            K_XX, K_YY = x_res["K_local"][m], y_res["K_local"][m]
            K_YX = [[K_XY[i][j] for i in range(n_x)] for j in range(n_y)]
            kernel = [K_XX[i] + K_XY[i] for i in range(n_x)] + [K_YX[j] + K_YY[j] for j in range(n_y)]
            kernels[m] = {"kernel_matrix": kernel, "n_x": n_x, "n_y": n_y, "d": d}

        return {"kernels": kernels}

    def has_converged(self, result, last_result):
        return self.num_iterations >= 1


if __name__ == "__main__":
    import random
    from pathlib import Path

    # modality -> (rows at cohort A, rows at cohort B, n_features).
    # Rows differ per modality AND per cohort; features differ across modalities
    # but match within one.
    SPEC = {
        "omics": (2, 3, 4),
        "imaging": (3, 2, 2),
        "labs": (4, 2, 5),
    }

    rng = random.Random(0)

    def make(n: int, d: int) -> list[list[float]]:
        # Kept small relative to GAMMA so the kernels span a useful range instead
        # of collapsing to ~0 everywhere, which would make the check vacuous.
        return [[rng.uniform(-1.5, 1.5) for _ in range(d)] for _ in range(n)]

    A = {m: make(n_a, d) for m, (n_a, _, d) in SPEC.items()}
    B = {m: make(n_b, d) for m, (_, n_b, d) in SPEC.items()}

    Path("results").mkdir(exist_ok=True)
    rp = "results/rbf_kernel_result.pkl"
    ProxyModelTester(
        data_splits=[
            [{f"{m}.pkl": pickle.dumps(mat) for m, mat in A.items()}],
            [{f"{m}.pkl": pickle.dumps(mat) for m, mat in B.items()}],
        ],
        analyzer=RBFAnalyzer,
        proxy=BeaverDealerProxy,
        aggregator=RBFAggregator,
        data_type="s3",
        num_proxy_nodes=1,
        simple_analysis=False,
        output_type="pickle",
        filename=rp,
    )
    kernels = pickle.loads(Path(rp).read_bytes())["kernels"]

    # Which cohort became the x-side is decided by node id, so it is random per run
    # but GLOBAL across modalities. Infer it once from a modality whose two row
    # counts differ, then apply that orientation everywhere.
    probe = next(m for m, (n_a, n_b, _) in SPEC.items() if n_a != n_b)
    a_is_x = kernels[probe]["n_x"] == SPEC[probe][0]

    worst = 0.0
    for m in sorted(SPEC):
        K = np.array(kernels[m]["kernel_matrix"])
        Aa, Bb = np.array(A[m]), np.array(B[m])
        X_, Y_ = (Aa, Bb) if a_is_x else (Bb, Aa)
        Z = np.vstack([X_, Y_])
        expected = rbf_kernel_matrix(Z, Z, GAMMA)
        err = float(np.max(np.abs(K - expected)))
        worst = max(worst, err)
        print(f"\n[{m}] kernel {K.shape}, d={kernels[m]['d']}, max abs error {err:.3e}")
        print(np.round(K, 4))
        np.testing.assert_allclose(K, expected, atol=0.05)

    print(f"\nPASS — {len(SPEC)} modalities, worst max abs error {worst:.3e}")
