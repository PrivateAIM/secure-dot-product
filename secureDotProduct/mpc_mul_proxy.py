"""
Secure Multiplication using Beaver Triples with Proxy-based Architecture.

This implementation integrates the Beaver triple-based secure multiplication
into the Proxy model structure, where:
- Proxy: Generates and distributes Beaver triples to analyzer nodes
- Analyzer: Performs local computations using received triples
- Aggregator: Combines the final shares to reveal the result
"""

import secrets
from typing import Any, Optional

from flame.proxy import ProxyModel, ProxyAnalyzer, Proxy, ProxyAggregator

# --- CONFIGURATION ---
RING_BITS = 64
MASK = (1 << RING_BITS) - 1
PRECISION_BITS = 16  # Fixed-point precision (f)


class BeaverMultiplicationAnalyzer(ProxyAnalyzer):
    """Analyzer node that performs secure multiplication using Beaver triples."""

    def __init__(self, flame):
        super().__init__(flame)
        self.f = PRECISION_BITS
        self.mask = MASK
        self.ringsize = 1 << RING_BITS

        self.triple = None  # Beaver triple (a_i, b_i, c_i) from proxy

        # Get partner analyzer IDs (excluding self and aggregator)
        self.partner_analyzer_ids: Optional[list[str]] = None

    def encode(self, val: float) -> int:
        """Encode a float value to fixed-point integer in the ring."""
        return int(round(val * (1 << self.f))) & self.mask

    def _create_additive_shares(self, secret: list) -> list:
        """Create 2-party additive shares for each secret value."""
        shares = []
        for val in secret:
            val_encoded = self.encode(val)
            share_0 = secrets.randbits(RING_BITS)
            share_1 = (val_encoded - share_0) & self.mask
            shares.append([share_0, share_1])
        return shares

    def analysis_method(self, data, aggregator_results) -> Any:
        # Initialize partner IDs on first call
        if self.partner_analyzer_ids is None:
            self.partner_analyzer_ids = [nid for nid in self.analyzer_ids if nid != self.id]

        if self.num_iterations == 0:
            # Iteration 0: Proxy distributes Beaver triples during this iteration,
            # analyzer just passes through (triples not available yet)
            return None

        if self.num_iterations == 1:
            # Receive Beaver triple from proxy (sent during iteration 0)
            proxy_data = self.flame.await_intermediate_data(senders=[self.proxy_id], message_category="beaver_triple")
            self.triple = proxy_data[self.proxy_id]
            print(f"{self.id} - Received Beaver triple from proxy: {self.triple}")

        if self.num_iterations > 0:
            # --- Step 1: Create and exchange additive shares ---
            shares = self._create_additive_shares(data)
            share_local = [s[0] for s in shares]
            share_remote = [s[1] for s in shares]

            if self.id < self.partner_analyzer_ids[0]:
                x_i = share_local
            else:
                y_i = share_local

            print(f"{self.id} - Sending share to {self.partner_analyzer_ids}")
            self.flame.send_intermediate_data(
                receivers=self.partner_analyzer_ids, data=share_remote, message_category="data_share"
            )
            received_share = self.flame.await_intermediate_data(
                senders=self.partner_analyzer_ids, message_category="data_share"
            )[self.partner_analyzer_ids[0]]
            print(f"{self.id} - Received share from partner: {received_share}")

            if self.id < self.partner_analyzer_ids[0]:
                y_i = received_share
            else:
                x_i = received_share

            print(f"{self.id} - Aligned shares: x_i={x_i}, y_i={y_i}")

            # --- Step 2: Compute and exchange Beaver masked values ---
            d_i = [(x - self.triple["a_i"]) % self.ringsize for x in x_i]
            e_i = [(y - self.triple["b_i"]) % self.ringsize for y in y_i]

            self.flame.send_intermediate_data(
                receivers=self.partner_analyzer_ids, data={"d_i": d_i, "e_i": e_i}, message_category="masked_values"
            )
            print(f"{self.id} - Sent d_i={d_i}, e_i={e_i}")

            partner_results = self.flame.await_intermediate_data(
                senders=self.partner_analyzer_ids, message_category="masked_values"
            )[self.partner_analyzer_ids[0]]
            print(f"{self.id} - Received masked values from partner: {partner_results}")

            d_j = partner_results["d_i"]
            e_j = partner_results["e_i"]

            # --- Step 3: Reconstruct d, e and compute z_i share ---
            d = [(d_i[i] + d_j[i]) % self.ringsize for i in range(len(d_j))]
            e = [(e_i[i] + e_j[i]) % self.ringsize for i in range(len(e_j))]
            print(f"{self.id} - Reconstructed d={d}, e={e}")

            z_i = []
            for i in range(len(d)):
                term_ea = (e[i] * self.triple["a_i"]) >> self.f
                term_db = (d[i] * self.triple["b_i"]) >> self.f
                term_de = (d[i] * e[i]) >> self.f

                z_val = (self.triple["c_i"] + term_ea + term_db) % self.ringsize

                if self.id < self.partner_analyzer_ids[0]:
                    z_val = (z_val + term_de) % self.ringsize

                z_i.append(z_val)

            print(f"{self.id} - Computed z_i={z_i}")

            # Send final share to aggregator
            self.flame.send_intermediate_data(
                receivers=[self.flame.get_aggregator_id()], data={"z_i": z_i}, message_category="final_shares"
            )
            return {"z_i": z_i}

        return None


class BeaverTripleProxy(Proxy):
    """Proxy node that generates and distributes Beaver triples to analyzer nodes."""

    def __init__(self, flame):
        super().__init__(flame)
        self.f = PRECISION_BITS
        self.mask = MASK
        self.ringsize = 1 << RING_BITS

    def generate_beaver_triple_shares(self, num_parties: int = 2) -> list[dict]:
        """
        Generate Beaver triple (A, B, C) where C = A * B,
        and create additive shares for each party.
        """
        # Generate random triple values
        A = secrets.randbits(RING_BITS) & self.mask
        B = secrets.randbits(RING_BITS) & self.mask
        # C = A * B, scaled properly for fixed-point arithmetic
        C = ((A * B) >> self.f) & self.mask

        print(f"Proxy - Generated Beaver triple: A={A}, B={B}, C={C}")

        # Create additive shares for each party
        shares = []
        a_sum, b_sum, c_sum = 0, 0, 0

        for i in range(num_parties - 1):
            a_i = secrets.randbits(RING_BITS) & self.mask
            b_i = secrets.randbits(RING_BITS) & self.mask
            c_i = secrets.randbits(RING_BITS) & self.mask
            shares.append({"a_i": a_i, "b_i": b_i, "c_i": c_i})
            a_sum = (a_sum + a_i) & self.mask
            b_sum = (b_sum + b_i) & self.mask
            c_sum = (c_sum + c_i) & self.mask

        # Last party gets the remaining share to make the sum equal to original
        shares.append({"a_i": (A - a_sum) & self.mask, "b_i": (B - b_sum) & self.mask, "c_i": (C - c_sum) & self.mask})

        return shares

    def proxy_aggregation_method(self, analysis_results: list[Any]) -> Any:
        if self.num_iterations == 0:
            # Generate and distribute Beaver triples to all analyzer nodes
            num_analyzers = len(self.analyzer_ids)
            triple_shares = self.generate_beaver_triple_shares(num_parties=num_analyzers)

            print(f"Proxy - Distributing Beaver triple shares to {self.analyzer_ids}")

            # Send each analyzer its share of the Beaver triple
            for i, node_id in enumerate(sorted(self.analyzer_ids)):
                self.flame.send_intermediate_data(
                    receivers=[node_id], data=triple_shares[i], message_category="beaver_triple"
                )
                print(f"Proxy - Sent triple share to {node_id}: {triple_shares[i]}")

        return None


class BeaverMultiplicationAggregator(ProxyAggregator):
    """Aggregator node that combines final shares to reveal the multiplication result."""

    def __init__(self, flame):
        super().__init__(flame)
        self.max_iter = 2
        self.f = PRECISION_BITS
        self.mask = MASK
        self.ring = 1 << RING_BITS

    def decode(self, val: int) -> float:
        """Decode a fixed-point integer back to float."""
        if val & (1 << 63):  # Handle Two's Complement
            val -= 1 << 64
        return float(val) / (1 << self.f)

    def aggregation_method(self, analysis_results):
        print(f"Aggregator - Iteration {self.num_iterations}")

        if self.num_iterations == 0:
            # Iteration 0: Pass through (proxy distributes triples)
            return None

        # Iteration 1: Combine final shares from all analyzers
        final_shares = self.flame.await_intermediate_data(senders=self.analyzer_ids, message_category="final_shares")
        print(f"Aggregator - Received final shares: {final_shares}")

        # Extract z_i values from each analyzer
        z_list = [final_shares[node_id]["z_i"] for node_id in sorted(self.analyzer_ids)]
        num_elements = len(z_list[0])

        # Sum all shares to reconstruct the final result
        z = [sum(z_list[j][i] for j in range(len(z_list))) % self.ring for i in range(num_elements)]
        print(f"Aggregator - Combined z values: {z}")

        # Decode back to float values
        z_decoded = [self.decode(val) for val in z]
        return {"final_product": z_decoded}

    def has_converged(self, result, last_result):
        return self.num_iterations >= self.max_iter - 1


def main():
    # # Generate random test data for two analyzer nodes
    # secret_1 = [random.randint(1, 100) for _ in range(3)]
    # secret_2 = [random.randint(1, 100) for _ in range(3)]

    # print(f"Input secrets:")
    # print(f"  Node 1: {secret_1}")
    # print(f"  Node 2: {secret_2}")
    # print(f"Expected product: {[a * b for a, b in zip(secret_1, secret_2)]}")
    # print("-" * 50)

    ProxyModel(
        analyzer=BeaverMultiplicationAnalyzer,  # Custom analyzer class
        proxy=BeaverTripleProxy,  # Custom proxy class for Beaver triple generation
        aggregator=BeaverMultiplicationAggregator,  # Custom aggregator class
        data_type="s3",  # Type of data source ('fhir' or 's3')
        query="",  # Query (not used in this example)
        num_proxy_nodes=1,  # Number of proxy nodes
        simple_analysis=False,  # Multi-iterative analysis
        output_type="str",  # Output format for the final result
        multiple_results=False,  # Single result output
        analyzer_kwargs=None,
        proxy_kwargs=None,
        aggregator_kwargs=None,
    )


if __name__ == "__main__":
    main()
