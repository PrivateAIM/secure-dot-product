import secrets
from flame.star import StarAnalyzer, StarAggregator

# --- CONFIGURATION ---
RING_BITS = 64
MASK = (1 << RING_BITS) - 1
PRECISION_BITS = 16  # Fixed-point precision (f)
# RINGSIZE = 10000  # Define a smaller ring size for testing purposes


class BeaverMultiplicationAnalyzer(StarAnalyzer):
    def __init__(self, flame):
        super().__init__(flame)
        self.max_iter = 1
        self.f = PRECISION_BITS
        self.mask = MASK
        self.triple = None  # Will hold (a_i, b_i, c_i) aka Beaver triple shares triples will be generated and its shares will be distributed by the proxy in the real implementation

        # self.ringsize = RINGSIZE  #Actual value: 1<<RING_BITS
        self.ringsize = 1 << RING_BITS

        self.partner_id = self.partner_node_ids
        self.partner_id.remove(self.flame.get_aggregator_id())  # Assuming only 2 nodes for simplicity

    def encode(self, val: float) -> int:
        # Use round() before int() to prevent precision loss (e.g., 0.1 -> 0.0999)
        # Applying & self.mask ensures it fits the 2^64 ring (handles negatives too)
        return int(round(val * (1 << self.f))) & self.mask

    def _get_beaver_triple(self):
        # Beaver triple components: (A=12, B=6, C=72)
        # TODO get this from the trusted proxy in the real implementation
        if self.id < self.partner_id[0]:
            return {"a_i": 6, "b_i": 3, "c_i": 54}
        else:
            return {"a_i": 6, "b_i": 3, "c_i": 18}

    def _create_additive_shares(self, secret):
        shares = []
        for val in secret:
            # share_0 = secrets.randbelow(self.ringsize)
            # share_1 = (val - share_0) % self.ringsize
            val_encoded = self.encode(val)
            share_0 = secrets.randbits(RING_BITS)
            share_1 = (val_encoded - share_0) & self.mask
            shares.append([share_0, share_1])

        return shares

    def analysis_method(self, data, aggregator_results):
        # Step 1: Create and exchange additive shares
        shares = self._create_additive_shares(data)
        self.triple = self._get_beaver_triple()  # In the real implementation the triples will come from the proxy
        share_local = [s[0] for s in shares]
        share_remote = [s[1] for s in shares]
        # The node with the smaller id is the X source, the other node is the Y source
        if self.id < self.partner_id[0]:
            x_i = share_local
        else:
            y_i = share_local

        print(f"{self.id} - sending data: {share_remote} to {self.partner_id}")
        self.flame.send_intermediate_data(receivers=self.partner_id, data=share_remote, message_category="data_share")
        received_share = self.flame.await_intermediate_data(self.partner_id, message_category="data_share")[
            self.partner_id[0]
        ]
        print(f"{self.id} - received messages: {received_share}")

        if self.id < self.partner_id[0]:
            y_i = received_share
        else:
            x_i = received_share

        print(f"{self.id} aligned - x_i: {x_i}, y_i: {y_i}")

        # Step 2: Compute and exchange Beaver masked values (d_i, e_i)
        d_i = [(x - self.triple["a_i"]) % self.ringsize for x in x_i]
        e_i = [(y - self.triple["b_i"]) % self.ringsize for y in y_i]

        self.flame.send_intermediate_data(
            receivers=self.partner_id, data={"d_i": d_i, "e_i": e_i}, message_category="intermediate_results"
        )
        print(f"{self.id} - sent d_i: {d_i}, e_i: {e_i}")

        partner_results = self.flame.await_intermediate_data(self.partner_id, message_category="intermediate_results")[
            self.partner_id[0]
        ]
        print(f"{self.id} - received d_i and e_i from partner node: {partner_results}")
        d_j = partner_results["d_i"]
        e_j = partner_results["e_i"]

        # Step 3: Reconstruct d, e and compute z_i share
        d = [(d_i[i] + d_j[i]) % self.ringsize for i in range(len(d_j))]
        e = [(e_i[i] + e_j[i]) % self.ringsize for i in range(len(e_j))]
        print(f"{self.id} - d: {d}, e: {e}")

        z_i = []
        for i in range(len(d)):
            term_ea = (e[i] * self.triple["a_i"]) >> self.f
            term_db = (d[i] * self.triple["b_i"]) >> self.f
            term_de = (d[i] * e[i]) >> self.f

            z_val = (self.triple["c_i"] + term_ea + term_db) % self.ringsize

            if self.id < self.partner_id[0]:
                z_val = (z_val + term_de) % self.ringsize

            z_i.append(z_val)
        print(f"{self.id} - z_i: {z_i}")
        return {"z_i": z_i}


class BeaverAggregator(StarAggregator):
    def __init__(self, flame):
        super().__init__(flame)
        self.max_iter = 1
        self.f = PRECISION_BITS
        self.mask = MASK
        self.ring = 1 << RING_BITS

    def decode(self, val: int) -> float:
        if val & (1 << 63):  # Handle Two's Complement: Convert unsigned 64-bit int back to signed Python int
            val -= 1 << 64
        return float(val) / (1 << self.f)  # Rescale: Divide by 2^f using float division.

    def aggregation_method(self, analysis_results):
        z_list = [res["z_i"] for res in analysis_results]
        num_elements = len(z_list[0])
        z = [sum(z_list[j][i] for j in range(2)) % self.ring for i in range(num_elements)]
        print(f"Aggregator - combined z values: {z}")
        z_decoded = [self.decode(val) for val in z]
        return {"final_product": z_decoded}

    def has_converged(self, result, last_result):
        return self.num_iterations >= self.max_iter - 1


# if __name__ == "__main__":
#     secret_1 = [random.randint(1, 100) for _ in range(3)] # dynamic random secret values for node 0
#     secret_2 = [random.randint(1, 100) for _ in range(3)] # dynamic random secret values for node 1
#
#     StarModelTester(
#         data_splits=[secret_1, secret_2],
#         analyzer=BeaverMultiplicationAnalyzer,
#         aggregator=BeaverAggregator,
#         data_type='s3',
#         simple_analysis=False
#     )
#
#     print(f"Expected: {[a * b for a, b in zip(secret_1, secret_2)]}")
