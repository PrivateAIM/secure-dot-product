"""
Secure element-wise multiplication of two N-D arrays via a proxy trusted dealer.

Only the analyzer's data prep and the aggregator's reconstruction are specific
to "element-wise multiply"; the Beaver protocol and the dealer come from
mpc_core, unchanged.

Round structure (simple_analysis=False, 2 rounds):
  round 0  analyzers announce vector length  ->  dealer generates+distributes triples
  round 1  analyzers run Beaver + send z_i straight to the aggregator
"""

import pickle
from typing import Any

from flame.proxy import ProxyAnalyzer, ProxyAggregator, ProxyModelTester
from mpc_core import BeaverCore, BeaverDealerProxy, RING_BITS, PRECISION_BITS, flatten, reshape, infer_shape, decode


class MulAnalyzer(ProxyAnalyzer, BeaverCore):
    def analysis_method(self, data, aggregator_results) -> Any:
        values_nd = pickle.loads(next(iter(data[0].values())))
        flat = flatten(values_nd)

        if self.num_iterations == 0:  # round 0: setup
            self.announce_length(len(flat))
            return None

        partner_id = self.partner_analyzer_id()  # round 1: compute
        is_x = self.id < partner_id
        triple = self.flame.await_intermediate_data([self.proxy_id], message_category="beaver_triple")[self.proxy_id]
        z_i = self.beaver_multiply(flat, triple, partner_id, is_x)

        self.flame.send_intermediate_data(
            [self.flame.get_aggregator_id()],
            {"z_i": z_i, "shape": infer_shape(values_nd)},
            message_category="final_shares",
        )
        self.finished = True  # stop the analyzer loop
        return None  # nothing sensitive to the dealer


class MulAggregator(ProxyAggregator):
    ring = 1 << RING_BITS

    def aggregation_method(self, proxy_results):
        if self.num_iterations == 0:
            return None
        shares = self.flame.await_intermediate_data(self.analyzer_ids, message_category="final_shares")
        ids = sorted(self.analyzer_ids)
        shape = shares[ids[0]]["shape"]
        z = [sum(col) % self.ring for col in zip(*[shares[i]["z_i"] for i in ids])]
        return {"product": reshape([decode(v, PRECISION_BITS) for v in z], shape)}

    def has_converged(self, result, last_result):
        return self.num_iterations >= 1


if __name__ == "__main__":
    import random
    from pathlib import Path

    rng = random.Random(0)
    SHAPE = (2, 3)  # try (5,), (2,2,2), ...

    def make(shape):
        if len(shape) == 1:
            return [rng.uniform(-5, 5) for _ in range(shape[0])]
        return [make(shape[1:]) for _ in range(shape[0])]

    X, Y = make(SHAPE), make(SHAPE)
    Path("results").mkdir(exist_ok=True)
    rp = "results/mul_result.pkl"
    ProxyModelTester(
        data_splits=[[{"m.pkl": pickle.dumps(X)}], [{"m.pkl": pickle.dumps(Y)}]],
        analyzer=MulAnalyzer,
        proxy=BeaverDealerProxy,
        aggregator=MulAggregator,
        data_type="s3",
        num_proxy_nodes=1,
        simple_analysis=False,
        output_type="pickle",
        filename=rp,
    )
    got = pickle.loads(Path(rp).read_bytes())["product"]
    exp = reshape([a * b for a, b in zip(flatten(X), flatten(Y))], SHAPE)
    print("\nGot     :", got)
    print("Expected:", exp)
