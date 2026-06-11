"""Secure dot product kernel across cohorts (local simulation with dummy data).

Two cohorts each hold a patient matrix (n_patients × n_features). The protocol
computes the full dot-product kernel K[i,j] = X_i · Y_j without either cohort
learning the other's patient vectors.
"""

import pickle
import random
import secrets
from pathlib import Path

from flame.star import StarAggregator, StarAnalyzer, StarModelTester

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RESULTS_DIR = Path("results")
RING_BITS = 64
MASK = (1 << RING_BITS) - 1
PRECISION_BITS = 16

# ---------------------------------------------------------------------------
# Analyzer  (runs on each cohort node)
# ---------------------------------------------------------------------------


class PASCAnalyzer(StarAnalyzer):
    """Computes a share of the dot-product kernel for this cohort's patients.

    Each node holds a matrix X (n_patients × n_features) and participates in
    a two-party Beaver triple protocol to securely compute all pairwise dot
    products with the partner cohort.
    """

    def __init__(self, flame):
        super().__init__(flame)
        self.max_iter = 1
        self.f = PRECISION_BITS
        self.mask = MASK
        self.ringsize = 1 << RING_BITS
        self.triple = None

        self.partner_id = self.partner_node_ids
        self.partner_id.remove(self.flame.get_aggregator_id())  # Assuming only 2 nodes for simplicity

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _tile_x(self, X: list[list[float]], n_y: int) -> list[float]:
        """Tile X rows for batched kernel computation (x-side / smaller ID node).

        For each patient i in X, repeat the row n_y times so that it aligns
        with every patient j in Y when both flat vectors are multiplied
        element-wise.

        Args:
            X: Patient matrix, shape (n_x, d).
            n_y: Number of patients in the partner cohort.

        Returns:
            Flat float list of length n_x * n_y * d.
        """
        return [X[i][k] for i in range(len(X)) for _ in range(n_y) for k in range(len(X[0]))]

    def _tile_y(self, Y: list[list[float]], n_x: int) -> list[float]:
        """Tile Y rows for batched kernel computation (y-side / larger ID node).

        Cycle through all Y rows n_x times so that each patient j in Y aligns
        with every patient i in X when both flat vectors are multiplied
        element-wise.

        Args:
            Y: Patient matrix, shape (n_y, d).
            n_x: Number of patients in the partner cohort.

        Returns:
            Flat float list of length n_x * n_y * d.
        """
        return [Y[j][k] for _ in range(n_x) for j in range(len(Y)) for k in range(len(Y[0]))]

    # ------------------------------------------------------------------
    # Beaver triple helpers
    # ------------------------------------------------------------------

    def encode(self, val: float) -> int:
        # Use round() before int() to prevent precision loss (e.g., 0.1 -> 0.0999)
        # Applying & self.mask ensures it fits the 2^64 ring (handles negatives too)
        return int(round(val * (1 << self.f))) & self.mask

    def _get_beaver_triple(self) -> dict:
        # TODO get this from the trusted proxy in the real implementation.
        # Simulating the proxy locally: both parties derive the SAME (A,B,C)
        # and share split from a shared seed, then each returns only its own
        # share. With secrets.randbits each party would generate a different
        # triple and the shares would not reconstruct.
        rng = random.Random(0xBEA4_BEA4)
        A = rng.getrandbits(64) & self.mask
        B = rng.getrandbits(64) & self.mask
        # C = A * B in the ring. FX truncation is applied locally to z_i after
        # multiplication; baking >>f into C here would conflict with that.
        C = (A * B) & self.mask
        print(f"Generated Beaver triple (A, B, C): ({A}, {B}, {C})")
        a_0 = rng.getrandbits(64) & self.mask
        b_0 = rng.getrandbits(64) & self.mask
        c_0 = rng.getrandbits(64) & self.mask
        a_1 = (A - a_0) & self.mask
        b_1 = (B - b_0) & self.mask
        c_1 = (C - c_0) & self.mask
        if self.id < self.partner_id[0]:
            return {"a_i": a_0, "b_i": b_0, "c_i": c_0}
        else:
            return {"a_i": a_1, "b_i": b_1, "c_i": c_1}

    def _local_truncate(self, z_i: int) -> int:
        """Mohassel-Zhang local truncation for 2 parties.

        Party 0 logical-shifts; party 1 negates, shifts, negates back.
        Reconstructs z >> f mod 2^N with failure prob ~|z|/2^N;
        on failure the error is exactly 2^(N-f), so it's not silent.

        Args:
            z_i: Un-truncated product share in the ring.

        Returns:
            Truncated share (divided by 2^f in the ring).
        """
        if self.id < self.partner_id[0]:
            return (z_i >> self.f) & self.mask
        else:
            return (-(((-z_i) & self.mask) >> self.f)) & self.mask

    def _create_additive_shares(self, secret: list[float]) -> list[list[int]]:
        shares = []
        for val in secret:
            val_encoded = self.encode(val)
            share_0 = secrets.randbits(RING_BITS)
            share_1 = (val_encoded - share_0) & self.mask
            shares.append([share_0, share_1])

        return shares

    # ------------------------------------------------------------------
    # Main analysis
    # ------------------------------------------------------------------

    def analysis_method(self, data: list[dict], aggregator_results):
        """Execute the secure dot-product kernel protocol for this node.

        Computes:
        - K_local: the self-kernel of this cohort's patients (plaintext, local only).
        - z_i: additive shares of the cross-kernel elements (via Beaver triples).

        The aggregator combines both to assemble the full symmetric kernel.

        Args:
            data: FLAME s3 data format — list of datasource dicts, each mapping
                dataset name to bytes. The patient matrix is unpickled from the
                first value of the first datasource dict.
            aggregator_results: Unused for iteration 0.

        Returns:
            Dict with z_i shares, local self-kernel, side flag, and shape metadata.
        """
        # FLAME delivers data as list[dict] with bytes values for s3 datasources.
        data: list[list[float]] = pickle.loads(next(iter(data[0].values())))
        n_local = len(data)
        d = len(data[0])

        # Local Kernel
        K_local = [[sum(data[i][k] * data[j][k] for k in range(d)) for j in range(n_local)] for i in range(n_local)]

        # ---- Step 0: Exchange shapes for data tiling ---------------------
        self.flame.send_intermediate_data(
            receivers=self.partner_id,
            data={"n_patients": n_local, "n_features": d},
            message_category="shape_info",
        )
        partner_shape = self.flame.await_intermediate_data(self.partner_id, message_category="shape_info")[
            self.partner_id[0]
        ]
        n_partner = partner_shape["n_patients"]
        d_partner = partner_shape["n_features"]

        if d != d_partner:
            raise ValueError(f"Feature dimension mismatch: local={d}, partner={d_partner}")

        # Determine which side (x or y) this node contributes.
        is_x = self.id < self.partner_id[0]
        n_x = n_local if is_x else n_partner
        n_y = n_partner if is_x else n_local

        if is_x:
            flat_data = self._tile_x(data, n_y)
        else:
            flat_data = self._tile_y(data, n_x)

        # ---- Step 1: Create and exchange additive shares -----------------
        self.triple = self._get_beaver_triple()
        shares = self._create_additive_shares(flat_data)
        share_local = [s[0] for s in shares]
        share_remote = [s[1] for s in shares]

        if is_x:
            x_i = share_local
        else:
            y_i = share_local

        self.flame.send_intermediate_data(receivers=self.partner_id, data=share_remote, message_category="data_share")
        received_share = self.flame.await_intermediate_data(self.partner_id, message_category="data_share")[
            self.partner_id[0]
        ]

        if is_x:
            y_i = received_share
        else:
            x_i = received_share

        # ---- Step 2: Compute and exchange Beaver masked values (d_i, e_i) -
        d_i = [(x - self.triple["a_i"]) % self.ringsize for x in x_i]
        e_i = [(y - self.triple["b_i"]) % self.ringsize for y in y_i]

        self.flame.send_intermediate_data(
            receivers=self.partner_id,
            data={"d_i": d_i, "e_i": e_i},
            message_category="intermediate_results",
        )
        partner_results = self.flame.await_intermediate_data(self.partner_id, message_category="intermediate_results")[
            self.partner_id[0]
        ]
        d_j = partner_results["d_i"]
        e_j = partner_results["e_i"]

        # ----Step 3: Reconstruct d, e and compute the z_i product share ---
        length = len(d_i)
        d_full = [(d_i[k] + d_j[k]) % self.ringsize for k in range(length)]
        e_full = [(e_i[k] + e_j[k]) % self.ringsize for k in range(length)]

        z_i = []
        for k in range(length):
            term_ea = (e_full[k] * self.triple["a_i"]) & self.mask
            term_db = (d_full[k] * self.triple["b_i"]) & self.mask
            term_de = (d_full[k] * e_full[k]) & self.mask

            z_val = (self.triple["c_i"] + term_ea + term_db) % self.ringsize
            if is_x:
                z_val = (z_val + term_de) % self.ringsize

            z_i.append(self._local_truncate(z_val))

        return {
            "z_i": z_i,
            "K_local": K_local,
            "is_x": is_x,
            "n_patients_x": n_x,
            "n_patients_y": n_y,
            "n_features": d,
        }


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class PASCAggregator(StarAggregator):
    """Assembles the complete dot-product kernel matrix from the two z_i shares.

    Receives z_i shares (flat, length n_x * n_y * d) from both analyzer nodes,
    sums them in the ring, then sums each d-block to recover each K[i,j].
    """

    def __init__(self, flame):
        super().__init__(flame)
        self.max_iter = 1
        self.f = PRECISION_BITS
        self.mask = MASK
        self.ring = 1 << RING_BITS

    def decode(self, val: int) -> float:
        """Decode a fixed-point ring element back to a float.

        Args:
            val: Unsigned 64-bit ring element (may represent a negative value
                 via two's complement).

        Returns:
            Decoded float value.
        """
        if val & (1 << 63):
            val -= 1 << 64
        return float(val) / (1 << self.f)

    def aggregation_method(self, analysis_results):
        """Reconstruct the full symmetric kernel matrix.

        Assembles:
          K = [ K_XX  K_XY ]
              [ K_YX  K_YY ]

        K_XY is reconstructed from the Beaver z_i shares, and K_YX = K_XY^T.

        Args:
            analysis_results: List of result dicts from both PASCAnalyzer nodes,
                each containing "z_i", "K_local", "is_x", "n_patients_x",
                "n_patients_y", "n_features".

        Returns:
            Dict with "kernel_matrix" (shape (n_x+n_y) × (n_x+n_y)),
            "n_patients_x", and "n_patients_y".
        """
        x_res = next(r for r in analysis_results if r["is_x"])
        y_res = next(r for r in analysis_results if not r["is_x"])

        n_x = x_res["n_patients_x"]
        n_y = x_res["n_patients_y"]
        d = x_res["n_features"]
        K_XX = x_res["K_local"]
        K_YY = y_res["K_local"]

        # Reconstruct cross-kernel K_XY from the two z_i shares.
        z_lists = [res["z_i"] for res in analysis_results]
        z = [(z_lists[0][k] + z_lists[1][k]) % self.ring for k in range(len(z_lists[0]))]

        K_XY = []
        for i in range(n_x):
            row = []
            for j in range(n_y):
                start = (i * n_y + j) * d
                block_sum = sum(z[start : start + d]) % self.ring
                row.append(self.decode(block_sum))
            K_XY.append(row)

        # K_YX = K_XY^T (dot product is symmetric).
        K_YX = [[K_XY[i][j] for i in range(n_x)] for j in range(n_y)]

        kernel = [K_XX[i] + K_XY[i] for i in range(n_x)] + [K_YX[j] + K_YY[j] for j in range(n_y)]

        return {"kernel_matrix": kernel, "n_patients_x": n_x, "n_patients_y": n_y}

    def has_converged(self, result, last_result):
        return self.num_iterations >= self.max_iter - 1


if __name__ == "__main__":
    import numpy as np

    N_PATIENTS_X = 2
    N_PATIENTS_Y = 3
    N_FEATURES = 3

    rng_x = random.Random(1)
    rng_y = random.Random(2)
    # Random arrays for cohorts
    X = [[rng_x.uniform(-3.0, 3.0) for _ in range(N_FEATURES)] for _ in range(N_PATIENTS_X)]
    Y = [[rng_y.uniform(-3.0, 3.0) for _ in range(N_FEATURES)] for _ in range(N_PATIENTS_Y)]

    print(f"Cohort X: {N_PATIENTS_X} patients × {N_FEATURES} features")
    print(f"Cohort Y: {N_PATIENTS_Y} patients × {N_FEATURES} features")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIR / "kernel_result.pkl"

    StarModelTester(
        data_splits=[[{"matrix.pkl": pickle.dumps(X)}], [{"matrix.pkl": pickle.dumps(Y)}]],
        analyzer=PASCAnalyzer,
        aggregator=PASCAggregator,
        data_type="s3",
        simple_analysis=False,
        output_type="pickle",
        filename=str(result_path),
    )
    with open(result_path, "rb") as f:
        result = pickle.loads(f.read())

    Xa, Ya = np.array(X), np.array(Y)
    print(f"\nOriginal data shapes: Xa {Xa.shape}, Ya {Ya.shape}")
    print(f"Xa sample:\n{Xa}")
    print(f"Ya sample:\n{Ya}")
    n_x = result["n_patients_x"]

    K = np.array(result["kernel_matrix"])
    print(f"\nSecure kernel matrix (K) shape: {K.shape}")
    print(K[:5, :5])

    # Full expected symmetric kernel; x-side rows come first.
    if n_x == N_PATIENTS_X:
        X_, Y_ = Xa, Ya
    else:
        X_, Y_ = Ya, Xa
    expected = np.block([[X_ @ X_.T, X_ @ Y_.T], [Y_ @ X_.T, Y_ @ Y_.T]])
    print(f"\nExpected kernel shape: {expected.shape}")
    print(expected[:5, :5])

    tol = 0.05
    np.testing.assert_allclose(K, expected, atol=tol)
