"""
Shared secure-multiplication core for the FLAME ProxyModel pattern.

Everything protocol-agnostic lives here so individual analyses (element-wise
multiply, dot-product kernel, ...) stay thin:

  * fixed-point encode/decode + Mohassel-Zhang local truncation
  * shape helpers (flatten / reshape / infer_shape) -> N-D element-wise multiply
  * tile_x / tile_y                                 -> broadcasting for kernels
  * BeaverCore.beaver_multiply(...)                 -> the 2-party Beaver step
  * BeaverDealerProxy                               -> the trusted dealer node
"""

import secrets
from typing import Any, Optional

from flame.proxy import Proxy

RING_BITS = 64
MASK = (1 << RING_BITS) - 1
PRECISION_BITS = 16  # fixed-point fractional bits (f)


# --- shape helpers -----------------------------------------------------------
def infer_shape(nested) -> tuple:
    shape = []
    x = nested
    while isinstance(x, (list, tuple)):
        shape.append(len(x))
        x = x[0] if len(x) else None
    return tuple(shape)


def flatten(nested) -> list:
    if not isinstance(nested, (list, tuple)):
        return [nested]
    out = []
    for item in nested:
        out.extend(flatten(item))
    return out


def reshape(flat: list, shape: tuple):
    if len(shape) <= 1:
        return list(flat)
    stride = 1
    for s in shape[1:]:
        stride *= s
    return [reshape(flat[i * stride : (i + 1) * stride], shape[1:]) for i in range(shape[0])]


# --- broadcasting for pairwise dot-product kernels ---------------------------
def tile_x(X: list[list[float]], n_y: int) -> list[float]:
    """Repeat each X-row n_y times: length n_x * n_y * d (x-side / smaller id)."""
    return [X[i][k] for i in range(len(X)) for _ in range(n_y) for k in range(len(X[0]))]


def tile_y(Y: list[list[float]], n_x: int) -> list[float]:
    """Cycle all Y-rows n_x times: length n_x * n_y * d (y-side / larger id)."""
    return [Y[j][k] for _ in range(n_x) for j in range(len(Y)) for k in range(len(Y[0]))]


def decode(val: int, f: int = PRECISION_BITS) -> float:
    if val & (1 << (RING_BITS - 1)):  # two's-complement negative
        val -= 1 << RING_BITS
    return float(val) / (1 << f)


# --- Beaver core mixin (used by analyzer nodes) ------------------------------
class BeaverCore:
    f = PRECISION_BITS
    mask = MASK
    ringsize = 1 << RING_BITS

    def encode(self, val: float) -> int:
        return int(round(val * (1 << self.f))) & self.mask

    def _local_truncate(self, z_i: int, is_x: bool) -> int:
        # Mohassel-Zhang 2-party local truncation of z >> f (mod 2^N).
        if is_x:
            return (z_i >> self.f) & self.mask
        return (-(((-z_i) & self.mask) >> self.f)) & self.mask

    def _create_additive_shares(self, values: list) -> tuple[list, list]:
        local, remote = [], []
        for val in values:
            enc = self.encode(val)
            s0 = secrets.randbits(RING_BITS)
            local.append(s0)
            remote.append((enc - s0) & self.mask)
        return local, remote

    def beaver_multiply(self, values: list, triple: dict, partner_id: str, is_x: bool) -> list:
        """Element-wise secure multiply over a flat vector.

        `triple` is this party's *vectorised* additive share {a_i,b_i,c_i}
        (one entry per element) of triples with C = A*B (un-truncated).
        Returns this party's flat z_i share vector (already truncated)."""
        share_local, share_remote = self._create_additive_shares(values)
        self.flame.send_intermediate_data([partner_id], share_remote, message_category="data_share")
        received = self.flame.await_intermediate_data([partner_id], message_category="data_share")[partner_id]
        if is_x:
            x_i, y_i = share_local, received
        else:
            x_i, y_i = received, share_local

        a, b, c = triple["a_i"], triple["b_i"], triple["c_i"]
        d_i = [(x_i[k] - a[k]) % self.ringsize for k in range(len(x_i))]
        e_i = [(y_i[k] - b[k]) % self.ringsize for k in range(len(y_i))]
        self.flame.send_intermediate_data([partner_id], {"d_i": d_i, "e_i": e_i}, message_category="masked_values")
        partner = self.flame.await_intermediate_data([partner_id], message_category="masked_values")[partner_id]
        d_j, e_j = partner["d_i"], partner["e_i"]

        z_i = []
        for k in range(len(d_i)):
            d = (d_i[k] + d_j[k]) % self.ringsize
            e = (e_i[k] + e_j[k]) % self.ringsize
            z_val = (c[k] + (e * a[k]) + (d * b[k])) % self.ringsize
            if is_x:  # exactly one party adds d*e
                z_val = (z_val + d * e) % self.ringsize
            z_i.append(self._local_truncate(z_val, is_x))
        return z_i

    # -- helper shared by every proxy-pattern analyzer --
    # TODO: current version only assumes 2 analyzers. change this
    def partner_analyzer_id(self) -> str:
        """The single other analyzer (2-party setting)."""
        aggregator_id = self.flame.get_aggregator_id()
        others = [nid for nid in self.partner_node_ids if nid != aggregator_id and nid != self.proxy_id]
        # self.partner_id.append(self.id)
        # self.partner_id = sorted(self.partner_id)  # Ensure consistent ordering
        # single_partner_id = self.partner_id[
        # (self.partner_id.index(self.id) + 1) % len(self.partner_id)]  # Get the other node's ID, always the next one
        return others[0]

    def announce_length(self, n: int) -> None:
        """Round-0 hook: tell the dealer how many triples to generate."""
        self.flame.send_intermediate_data([self.proxy_id], {"n": n}, message_category="n_elements")


# --- Reusable trusted dealer -------------------------------------------------
class BeaverDealerProxy(Proxy):
    """Generates one Beaver triple (A,B,C=A*B, un-truncated) per element and
    hands each analyzer an additive share. Sees neither the masked values nor
    the product shares, so it cannot reconstruct inputs or output."""

    mask = MASK

    def __init__(self, flame):
        super().__init__(flame)
        self.n_elements: Optional[int] = None

    def _make_triple_shares(self, n: int, num_parties: int) -> list[dict]:
        shares = [{"a_i": [], "b_i": [], "c_i": []} for _ in range(num_parties)]
        for _ in range(n):
            A = secrets.randbits(RING_BITS) & self.mask
            B = secrets.randbits(RING_BITS) & self.mask
            C = (A * B) & self.mask
            a_rest = b_rest = c_rest = 0
            for p in range(num_parties - 1):
                a, b, c = (secrets.randbits(RING_BITS) for _ in range(3))
                shares[p]["a_i"].append(a)
                shares[p]["b_i"].append(b)
                shares[p]["c_i"].append(c)
                a_rest = (a_rest + a) & self.mask
                b_rest = (b_rest + b) & self.mask
                c_rest = (c_rest + c) & self.mask
            shares[-1]["a_i"].append((A - a_rest) & self.mask)
            shares[-1]["b_i"].append((B - b_rest) & self.mask)
            shares[-1]["c_i"].append((C - c_rest) & self.mask)
        return shares

    def proxy_aggregation_method(self, analysis_results: list[Any]) -> Any:
        if self.num_iterations == 0:
            n = self.flame.await_intermediate_data(self.analyzer_ids, message_category="n_elements")
            n = next(iter(n.values()))["n"]
            analyzers = sorted(self.analyzer_ids)
            for node_id, share in zip(analyzers, self._make_triple_shares(n, len(analyzers))):
                self.flame.send_intermediate_data([node_id], share, message_category="beaver_triple")
        else:
            self.finished = True
        return None
