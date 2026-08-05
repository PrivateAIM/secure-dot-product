"""
Secure pairwise RBF kernels across N cohorts (not just two), one kernel per data
modality, reusing the two-party Beaver core unchanged.

Why this needs no new cryptography
----------------------------------
The joint kernel over S sites is an S x S block matrix:

    * diagonal block K_ss  -- entirely local, site s computes it in the clear
    * off-diagonal K_st    -- involves EXACTLY two sites

So the multi-cohort problem decomposes into S*(S-1)/2 independent *two-party*
computations. That matters: `beaver_multiply` and especially `_local_truncate`
implement the Mohassel-Zhang **two-party** protocol, which does not generalize to
a genuine N-party product. Because no block ever mixes three cohorts, we never
need one. Every pair runs the same protocol mpc_rbf_proxy runs, with its own
triples, and the aggregator stitches the blocks together.

What had to change relative to mpc_rbf_proxy
-------------------------------------------
1. Shapes are broadcast to every peer, not exchanged with a single partner, so
   every site can derive the same global pair list and block layout.
2. The dealer deals one triple batch per PAIR. It must hand each analyzer a
   single message holding a {pair_key: share} bundle -- `await_intermediate_data`
   keeps only the newest message per (sender, category), so S-1 separate triple
   messages would silently collapse to the last one.
3. The analyzer loops over the pairs it belongs to, in a globally consistent
   order, running one Beaver exchange per pair.

The peer-to-peer categories ("data_share", "masked_values") need no per-pair
scoping: `await_messages` filters on sender and preserves messages from everyone
else, and each unordered pair occurs exactly once, so no (sender, receiver,
category) triple ever repeats.

Within each pair the smaller node id is the x-side, exactly as before. Norms are
folded into the shares rather than shipped, so the aggregator never sees them.

Row alignment
-------------
Row i is patient i in EVERY modality at a given cohort, so all modalities at one
site must carry the same number of rows; only the patient count *between* cohorts
may differ. Round 0 enforces this and fails loudly otherwise. The consequence is
that every modality's kernel has identical axes -- same shape, same row order --
which is what lets them be combined downstream (e.g. weighted-sum multiple-kernel
learning). The axis metadata is therefore returned once, not per kernel.

Note this is positional alignment, not identity-based: nothing carries patient
IDs, so a site whose modality matrices are row-permuted relative to each other
would pass the count check and still be silently misaligned.

Cost: triples grow as sum over pairs of sum over modalities of n_s * n_t * d,
i.e. quadratically in the number of sites.
"""

import math
import pickle
from itertools import combinations
from typing import Any

import numpy as np
from flame.proxy import ProxyAggregator, ProxyAnalyzer, ProxyModelTester
from mpc_core import RING_BITS, BeaverCore, BeaverDealerProxy, tile_x, tile_y
from mpc_rbf_proxy import (
    GAMMA,
    block_start,
    modality_name,
    rbf_kernel_matrix,
    reconstruct_dist_block,
    segment_offsets,
    total_length,
)

PAIR_SEP = "|"


def pair_key(a: str, b: str) -> str:
    """Stable key for the unordered pair {a, b}, smaller id first."""
    lo, hi = (a, b) if a < b else (b, a)
    return f"{lo}{PAIR_SEP}{hi}"


def split_pair(key: str) -> tuple[str, str]:
    """Inverse of `pair_key`; returns (x_side_id, y_side_id)."""
    lo, hi = key.split(PAIR_SEP)
    return lo, hi


def transpose(block: list[list[float]]) -> list[list[float]]:
    """Transpose a rectangular block; K_ts is K_st transposed for a symmetric kernel."""
    return [list(row) for row in zip(*block)]


class PairwiseBeaverDealerProxy(BeaverDealerProxy):
    """Deals one Beaver triple batch per cohort PAIR.

    Each analyzer receives a single message holding every batch it needs, keyed by
    pair. Sending one message per pair instead would be silently lossy: the SDK's
    await keeps only the newest message per (sender, category).
    """

    def proxy_aggregation_method(self, analysis_results: list[Any]) -> Any:
        if self.num_iterations == 0:
            announced = self.flame.await_intermediate_data(self.analyzer_ids, message_category="n_elements")
            # Every analyzer derives the same table, so any one of them will do.
            pair_lengths = next(iter(announced.values()))["pair_lengths"]

            bundles: dict[str, dict[str, dict]] = {nid: {} for nid in self.analyzer_ids}
            for key in sorted(pair_lengths):
                x_id, y_id = split_pair(key)
                share_x, share_y = self._make_triple_shares(pair_lengths[key], 2)
                bundles[x_id][key] = share_x
                bundles[y_id][key] = share_y

            for node_id, bundle in bundles.items():
                self.flame.send_intermediate_data([node_id], bundle, message_category="beaver_triple")
        else:
            self.finished = True
        return None


class MultiCohortRBFAnalyzer(ProxyAnalyzer, BeaverCore):
    def analyzer_peers(self) -> list[str]:
        """Every other analyzer, sorted. Generalizes BeaverCore.partner_analyzer_id."""
        aggregator_id = self.flame.get_aggregator_id()
        return sorted(nid for nid in self.partner_node_ids if nid not in (aggregator_id, self.proxy_id))

    def analysis_method(self, data, aggregator_results) -> Any:
        mats = {modality_name(fn): pickle.loads(blob) for fn, blob in data[0].items()}
        peers = self.analyzer_peers()

        if self.num_iterations == 0:  # round 0: broadcast shapes, derive layout
            local = {m: (len(X), len(X[0])) for m, X in mats.items()}
            self.flame.send_intermediate_data(peers, local, message_category="shape_info")
            others = self.flame.await_intermediate_data(peers, message_category="shape_info")

            all_shapes = dict(others)
            all_shapes[self.id] = local
            modalities = sorted(local)
            self.patients = {}
            for site, shapes in all_shapes.items():
                if set(shapes) != set(local):
                    raise ValueError(f"[{site}] modality mismatch: {sorted(shapes)} vs {modalities}")
                for m in modalities:
                    if shapes[m][1] != local[m][1]:
                        raise ValueError(f"[{site}][{m}] feature mismatch: {shapes[m][1]} vs {local[m][1]}")

                # Row i is patient i in EVERY modality at a site, so all modalities
                # must report the same row count. Cohorts may still differ from each
                # other. Without this the kernels come out with mismatched axes and
                # cannot be combined downstream (e.g. weighted-sum MKL).
                counts = {m: shapes[m][0] for m in modalities}
                if len(set(counts.values())) != 1:
                    raise ValueError(f"[{site}] modalities disagree on patient count: {counts}")
                self.patients[site] = counts[modalities[0]]

            self.site_ids = sorted(all_shapes)
            features = {m: local[m][1] for m in modalities}

            # Global pair list, identical on every site, so all of them (and the
            # aggregator) agree on both the block layout and the visiting order.
            # Row counts come from the per-site patient count, never per modality.
            self.pair_shapes = {}
            for a, b in combinations(self.site_ids, 2):
                self.pair_shapes[pair_key(a, b)] = {
                    m: (self.patients[a], self.patients[b], features[m]) for m in modalities
                }

            lengths = {key: total_length(shapes) for key, shapes in self.pair_shapes.items()}
            self.flame.send_intermediate_data([self.proxy_id], {"pair_lengths": lengths}, message_category="n_elements")
            return None

        # round 1: one Beaver exchange per pair this site belongs to
        triples = self.flame.await_intermediate_data([self.proxy_id], message_category="beaver_triple")[self.proxy_id]
        dist_shares = {}
        for key in sorted(self.pair_shapes):
            x_id, y_id = split_pair(key)
            if self.id not in (x_id, y_id):
                continue
            is_x = self.id == x_id  # smaller id is the x-side, as in the 2-party file
            partner_id = y_id if is_x else x_id
            shapes = self.pair_shapes[key]

            flat = []
            for m in sorted(shapes):
                n_x, n_y, _ = shapes[m]
                flat.extend(tile_x(mats[m], n_y) if is_x else tile_y(mats[m], n_x))
            z_i = self.beaver_multiply(flat, triples[key], partner_id, is_x)
            dist_shares[key] = self._fold_norms(z_i, shapes, mats, is_x)

        # Diagonal blocks are plain in-the-clear RBFs over our own rows. Being already
        # exponentiated, they carry no norm information (their diagonal is all ones).
        K_local = {m: rbf_kernel_matrix(np.asarray(X), np.asarray(X), GAMMA).tolist() for m, X in mats.items()}

        self.flame.send_intermediate_data(
            [self.flame.get_aggregator_id()],
            {
                "site_id": self.id,
                "dist_shares": dist_shares,
                "K_local": K_local,
                "site_ids": self.site_ids,
                "patients": self.patients,
                "pair_shapes": self.pair_shapes,
            },
            message_category="final_shares",
        )
        self.finished = True
        return None

    def _fold_norms(self, z_i: list[int], shapes: dict, mats: dict, is_x: bool) -> list[int]:
        """Scale the cross term by -2 and add this cohort's own row norms.

        Both operations are linear, so they are free on additive shares. The result
        reconstructs to ||X_i - Y_j||^2 directly, meaning the aggregator never sees
        either cohort's norms -- which the translation-invariant RBF would not have
        revealed on its own.

        Args:
            z_i: This party's truncated Beaver output for one pair, all modalities.
            shapes: Modality -> (n_x, n_y, d) for that pair.
            mats: This site's own matrices, keyed by modality.
            is_x: Whether this site is the x-side of the pair.

        Returns:
            The folded share vector, same length as `z_i`.
        """
        folded = [(-2 * v) % self.ringsize for v in z_i]
        offsets = segment_offsets(shapes)
        for m in sorted(shapes):
            n_x, n_y, d = shapes[m]
            own_norms = [sum(v * v for v in row) for row in mats[m]]  # indexed by i if x-side, else by j
            for i in range(n_x):
                for j in range(n_y):
                    start = block_start(offsets[m], i, j, n_y, d)
                    folded[start] = (folded[start] + self.encode(own_norms[i if is_x else j])) % self.ringsize
        return folded


class MultiCohortRBFAggregator(ProxyAggregator):
    ring = 1 << RING_BITS

    def aggregation_method(self, proxy_results):
        if self.num_iterations == 0:
            return None
        res = self.flame.await_intermediate_data(self.analyzer_ids, message_category="final_shares")
        by_site = {r["site_id"]: r for r in res.values()}

        reference = by_site[min(by_site)]
        site_ids, patients, pair_shapes = reference["site_ids"], reference["patients"], reference["pair_shapes"]
        if set(site_ids) != set(by_site):
            raise ValueError(f"expected shares from {sorted(site_ids)}, got {sorted(by_site)}")
        for site, r in by_site.items():
            if r["pair_shapes"] != pair_shapes:
                raise ValueError(f"[{site}] disagrees on the pair layout")

        # Reconstruct every off-diagonal block, one pair at a time.
        cross: dict[tuple[str, str, str], list[list[float]]] = {}
        for key in sorted(pair_shapes):
            x_id, y_id = split_pair(key)
            share_x, share_y = by_site[x_id]["dist_shares"][key], by_site[y_id]["dist_shares"][key]
            shapes = pair_shapes[key]
            if len(share_x) != total_length(shapes) or len(share_y) != total_length(shapes):
                raise ValueError(f"[{key}] share length does not match layout {total_length(shapes)}")

            w = [(a + b) % self.ring for a, b in zip(share_x, share_y)]
            offsets = segment_offsets(shapes)
            for m in sorted(shapes):
                n_x, n_y, d = shapes[m]
                # Already ||X_i - Y_j||^2; fixed-point error can push a near-zero
                # distance negative, which would give exp(+x) > 1, so clamp first.
                dist_sq = reconstruct_dist_block(w, offsets[m], n_x, n_y, d, self.ring)
                cross[(x_id, y_id, m)] = [
                    [math.exp(-GAMMA * max(0.0, dist_sq[i][j])) for j in range(n_y)] for i in range(n_x)
                ]

        # Every modality now shares one set of axes, so the axis metadata is returned
        # once at the top level rather than repeated per kernel.
        kernels = {}
        for m in sorted(reference["K_local"]):
            kernel = []
            for s in site_ids:
                blocks = []
                for t in site_ids:
                    if s == t:
                        blocks.append(by_site[s]["K_local"][m])
                    elif (s, t, m) in cross:
                        blocks.append(cross[(s, t, m)])
                    else:
                        blocks.append(transpose(cross[(t, s, m)]))
                for i in range(patients[s]):
                    kernel.append([value for block in blocks for value in block[i]])
            kernels[m] = kernel

        return {"kernels": kernels, "site_ids": site_ids, "patients": patients}

    def has_converged(self, result, last_result):
        return self.num_iterations >= 1


if __name__ == "__main__":
    import random
    from pathlib import Path

    # Features are per modality and shared by every site. Patients are per SITE and
    # shared by every modality at that site -- row i is patient i everywhere.
    FEATURES = {"omics": 4, "imaging": 2, "labs": 5}
    PATIENTS = {"site_a": 3, "site_b": 5, "site_c": 4, "site_d": 3, "site_e": 5}
    MODALITIES = sorted(FEATURES)

    rng = random.Random(0)

    def make(n: int, d: int) -> list[list[float]]:
        # Kept small relative to GAMMA so the kernels span a useful range instead
        # of collapsing to ~0 everywhere, which would make the check vacuous.
        return [[rng.uniform(-1.5, 1.5) for _ in range(d)] for _ in range(n)]

    cohorts = {name: {m: make(n, FEATURES[m]) for m in MODALITIES} for name, n in PATIENTS.items()}

    def identify_sites(probe: str) -> dict[str, str]:
        """Map runtime node ids back to the cohort names this harness generated.

        ProxyModelTester assigns random UUIDs, so the harness has to work out which
        node received which matrices before it can build a plaintext reference.
        Matching on the in-the-clear diagonal block does that from data content, so
        it holds however the patient counts fall -- in particular it does NOT need
        them to be unique. Two sites with the same number of patients are fine.

        Nothing here reflects a protocol constraint: the protocol keys sites by node
        id, which is unique by construction, and never inspects row counts to tell
        cohorts apart.

        Args:
            probe: Modality whose diagonal blocks are used as the fingerprint.

        Returns:
            Node id -> cohort name.
        """
        K = np.array(kernels[probe])
        offset, resolved, claimed = 0, {}, set()
        for site in site_ids:
            n = patients[site]
            block = K[offset : offset + n, offset : offset + n]
            offset += n
            for name, own in cohorts.items():
                if name in claimed or len(own[probe]) != n:
                    continue
                X = np.array(own[probe])
                if np.allclose(block, rbf_kernel_matrix(X, X, GAMMA), atol=1e-9):
                    resolved[site] = name
                    claimed.add(name)
                    break
            else:
                raise SystemExit(f"could not identify node {site}")
        return resolved

    Path("results").mkdir(exist_ok=True)
    rp = "results/rbf_multicohort_result.pkl"
    ProxyModelTester(
        data_splits=[[{f"{m}.pkl": pickle.dumps(mat) for m, mat in cohorts[name].items()}] for name in sorted(cohorts)],
        analyzer=MultiCohortRBFAnalyzer,
        proxy=PairwiseBeaverDealerProxy,
        aggregator=MultiCohortRBFAggregator,
        data_type="s3",
        num_proxy_nodes=1,
        simple_analysis=False,
        output_type="pickle",
        filename=rp,
    )
    result = pickle.loads(Path(rp).read_bytes())
    kernels, site_ids, patients = result["kernels"], result["site_ids"], result["patients"]

    id_to_name = identify_sites(MODALITIES[0])
    print("\nsite order:", [id_to_name[s] for s in site_ids])

    total = sum(PATIENTS.values())
    shapes = {m: np.array(kernels[m]).shape for m in MODALITIES}
    if set(shapes.values()) != {(total, total)}:
        raise SystemExit(f"kernels must share one set of axes, got {shapes}")
    print(f"all {len(MODALITIES)} kernels share axes {(total, total)}")

    worst = 0.0
    for m in MODALITIES:
        K = np.array(kernels[m])
        Z = np.vstack([np.array(cohorts[id_to_name[s]][m]) for s in site_ids])
        expected = rbf_kernel_matrix(Z, Z, GAMMA)
        err = float(np.max(np.abs(K - expected)))
        worst = max(worst, err)
        print(f"\n[{m}] kernel {K.shape}, d={FEATURES[m]}, max abs error {err:.3e}")
        np.testing.assert_allclose(K, expected, atol=0.05)

    # Aligned axes are what make a weighted-sum combination meaningful at all.
    beta = {m: 1.0 / len(MODALITIES) for m in MODALITIES}
    K_mkl = sum(beta[m] * np.array(kernels[m]) for m in MODALITIES)
    print(f"\nweighted-sum combination K_mkl {K_mkl.shape}, diagonal {np.round(np.diag(K_mkl), 3)}")

    n_pairs = len(PATIENTS) * (len(PATIENTS) - 1) // 2
    print(f"\nPASS — {len(PATIENTS)} cohorts, {n_pairs} pairs, {len(MODALITIES)} modalities")
    print(f"worst max abs error {worst:.3e}")
