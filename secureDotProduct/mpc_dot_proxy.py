"""
Secure pairwise dot-product kernel across two cohorts, using the proxy trusted
dealer and the SAME Beaver core as the element-wise multiply.

A kernel K[i,j] = X_i . Y_j is a *contraction*, not an element-wise product, so
it still needs (a) broadcasting the two matrices against each other and (b) a
sum over the feature axis. Those two steps are the only kernel-specific logic:
  * broadcasting  -> mpc_core.tile_x / tile_y  (shared, not hand-rolled per node)
  * feature sum   -> block-sum in the aggregator
Everything else (triples, masking, truncation) is reused verbatim.
"""

import pickle
from typing import Any

from flame.proxy import ProxyAnalyzer, ProxyAggregator, ProxyModelTester
from mpc_core import BeaverCore, BeaverDealerProxy, RING_BITS, PRECISION_BITS, tile_x, tile_y, decode


class KernelAnalyzer(ProxyAnalyzer, BeaverCore):
    def analysis_method(self, data, aggregator_results) -> Any:
        X = pickle.loads(next(iter(data[0].values())))
        n_local, d = len(X), len(X[0])
        partner_id = self.partner_analyzer_id()  # this assumes only 3 analyzers
        is_x = self.id < partner_id

        if self.num_iterations == 0:  # round 0: shapes + setup
            self.flame.send_intermediate_data([partner_id], {"n": n_local, "d": d}, message_category="shape_info")
            partner = self.flame.await_intermediate_data([partner_id], message_category="shape_info")[partner_id]
            if d != partner["d"]:
                raise ValueError(f"feature mismatch: {d} vs {partner['d']}")
            self.n_x = n_local if is_x else partner["n"]
            self.n_y = partner["n"] if is_x else n_local
            self.announce_length(self.n_x * self.n_y * d)  # tiled length -> dealer
            return None

        flat = tile_x(X, self.n_y) if is_x else tile_y(X, self.n_x)  # round 1: compute
        triple = self.flame.await_intermediate_data([self.proxy_id], message_category="beaver_triple")[self.proxy_id]
        z_i = self.beaver_multiply(flat, triple, partner_id, is_x)

        K_local = [[sum(X[i][k] * X[j][k] for k in range(d)) for j in range(n_local)] for i in range(n_local)]
        self.flame.send_intermediate_data(
            [self.flame.get_aggregator_id()],
            {"z_i": z_i, "K_local": K_local, "is_x": is_x, "n_x": self.n_x, "n_y": self.n_y, "d": d},
            message_category="final_shares",
        )
        self.finished = True
        return None


class KernelAggregator(ProxyAggregator):
    ring = 1 << RING_BITS

    def aggregation_method(self, proxy_results):
        if self.num_iterations == 0:
            return None
        res = self.flame.await_intermediate_data(self.analyzer_ids, message_category="final_shares")
        results = list(res.values())
        x_res = next(r for r in results if r["is_x"])
        y_res = next(r for r in results if not r["is_x"])
        n_x, n_y, d = x_res["n_x"], x_res["n_y"], x_res["d"]

        z = [sum(col) % self.ring for col in zip(*[r["z_i"] for r in results])]
        K_XY = [
            [decode(sum(z[(i * n_y + j) * d : (i * n_y + j) * d + d]) % self.ring, PRECISION_BITS) for j in range(n_y)]
            for i in range(n_x)
        ]
        K_XX, K_YY = x_res["K_local"], y_res["K_local"]
        K_YX = [[K_XY[i][j] for i in range(n_x)] for j in range(n_y)]
        kernel = [K_XX[i] + K_XY[i] for i in range(n_x)] + [K_YX[j] + K_YY[j] for j in range(n_y)]
        return {"kernel_matrix": kernel, "n_x": n_x, "n_y": n_y}

    def has_converged(self, result, last_result):
        return self.num_iterations >= 1


if __name__ == "__main__":
    import random
    import numpy as np
    from pathlib import Path

    N_X, N_Y, D = 2, 3, 3
    rx, ry = random.Random(1), random.Random(2)
    X = [[rx.uniform(-3, 3) for _ in range(D)] for _ in range(N_X)]
    Y = [[ry.uniform(-3, 3) for _ in range(D)] for _ in range(N_Y)]

    Path("results").mkdir(exist_ok=True)
    rp = "results/kernel_result.pkl"
    ProxyModelTester(
        data_splits=[[{"m.pkl": pickle.dumps(X)}], [{"m.pkl": pickle.dumps(Y)}]],
        analyzer=KernelAnalyzer,
        proxy=BeaverDealerProxy,
        aggregator=KernelAggregator,
        data_type="s3",
        num_proxy_nodes=1,
        simple_analysis=False,
        output_type="pickle",
        filename=rp,
    )
    result = pickle.loads(Path(rp).read_bytes())
    K = np.array(result["kernel_matrix"])
    Xa, Ya = np.array(X), np.array(Y)
    # x-side is whichever cohort got the smaller node id (random); order the
    # expected block matrix to match, exactly like the original harness did.
    X_, Y_ = (Xa, Ya) if result["n_x"] == N_X else (Ya, Xa)
    expected = np.block([[X_ @ X_.T, X_ @ Y_.T], [Y_ @ X_.T, Y_ @ Y_.T]])
    print("\nSecure kernel:\n", np.round(K, 4))
    print("max abs error vs plaintext:", np.max(np.abs(K - expected)))
    np.testing.assert_allclose(K, expected, atol=0.05)
    print("PASS (within fixed-point tolerance)")
