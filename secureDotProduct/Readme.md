# Proxy-dealer secure multiplication & kernel (FLAME `proxy-nodes`)

Secure two-party element-wise multiply and pairwise dot-product kernel over two  cohorts, using a FLAME **proxy node as a trusted Beaver-triple dealer**. Fixed-point arithmetic over the ring 2^64, Mohassel–Zhang local truncation bla bla.

> Requires the `proxy-nodes` branch of `python-sdk-patterns` (the `flame.proxy` package does not exist on `main`).
> Run with `num_proxy_nodes=1`.

## Files

| File | Role |
|------|------|
| `mpc_core.py` | Shared library. Everything reusable lives here: encoding/truncation, the Beaver step, input sharing, the dealer, and the tiling/reshape helpers. |
| `mpc_mul_proxy.py` | Thin protocol file: element-wise product of two N-D arrays. |
| `mpc_dot_proxy.py` | Thin protocol file: pairwise dot-product kernel K[i,j] = X_i · Y_j across two cohorts. |

Both protocol files import from `mpc_core`, so keep the three together (same directory or on `PYTHONPATH`).

## Protocol (2 rounds, `simple_analysis=False`)

**Topology:** the framework relays analyzers → proxy → aggregator, but the proxy here is *only* a triple dealer. Sensitive data bypasses it via explicit side channels (message categories), so analyzers return `None`.

Round 0 (setup)
1. Each analyzer determines the length it needs (`n` for multiply; `n_x·n_y·d` after exchanging shapes for the kernel) and announces it to the dealer.
2. Dealer generates one Beaver triple per element, additively splits it, and sends each analyzer its vectorised share (`beaver_triple`).

Round 1 (compute)
1. Each analyzer shares its own operand with its partner (`data_share`) — this is the input-sharing step.
2. The two analyzers exchange masked values `d, e` **peer-to-peer** (`masked_values`), never through the dealer.
3. Each computes its un-truncated `z_i`, applies local truncation, and sends its share **straight to the aggregator** (`final_shares`), then sets `finished = True`.
4. Aggregator sums the shares mod 2^64 and decodes. For the kernel it also block-sums each length-`d` run — `start = (i·n_y + j)·d` — to recover K[i,j],  sets K_YX = K_XYᵀ, and assembles the full symmetric block matrix.


[//]: # (## `mpc_core.py)

[//]: # ()
[//]: # (- **Encoding / decoding** — `encode`, `decode`: float ↔ fixed-point ring element &#40;`PRECISION_BITS = 16`&#41;.)

[//]: # (- **Input sharing** — `_create_additive_shares`: splits a party's *own operand* into two additive shares &#40;keep `local`, send `remote` to the partner&#41;. Called inside `beaver_multiply`.)

[//]: # (- **Beaver multiply** — `beaver_multiply`: the 2-party masked-value exchange &#40;`d = x − a`, `e = y − b`&#41;, local `z_i`, then `_local_truncate`. Operates on a flat vector.)

[//]: # (- **Local truncation** — `_local_truncate`: Mohassel–Zhang 2-party truncation applied to `z_i` at the end &#40;NOT baked into the triple&#41;.)

[//]: # (- **Triple sharing** — `_make_triple_shares` inside `BeaverDealerProxy`: generates `&#40;A, B, C = A·B&#41;` per element and additively splits each into one share per analyzer. `C` is stored **un-truncated** on purpose.)

[//]: # (- **Dealer loop** — `BeaverDealerProxy.proxy_aggregation_method`: round 0 deals triples, round 1 finishes. Sees only the element count — never inputs, masked values, or outputs.)

[//]: # (- **Tiling** — `tile_x`, `tile_y`: broadcast the two matrices against each other for the kernel &#40;length `n_x · n_y · d`&#41;. Kernel-only; the element-wise multiply doesn't need them.)

[//]: # (- **Shape helpers** — `infer_shape`, `flatten`, `reshape`: make the element-wise multiply shape-agnostic.)


## Running (local tester)

```bash
python mpc_mul_proxy.py     # element-wise product, prints max abs error
python mpc_dot_proxy.py     # kernel, verified against a plaintext np.block
```

## Relation to the original files

- `mpc_mul_local.py` (working baseline): same math, but the triple came from a
  shared seed — an insecure simulation. Replaced by the dealer.
- `mpc_dot_pasc.py` (colleague's kernel): tiling formulas and the aggregator
  index reconstruction are preserved verbatim; tiling moved into `mpc_core`, the
  shared-seed triple replaced by the dealer. Nothing dropped.

## TODO

- [ ] **Extend beyond 2 analyzers.** Currently hard-wired to a single pair.
      Generalize to N cohorts by pairing each analyzer with the next one
      (`i → i+1`, cyclically) so every pair gets covered.
- [ ] **Avoid collisions.** With several pairwise exchanges running at once, give
      each pair its own message channel so Beaver triples and masked values don't
      get mixed up between pairs (silent share mismatch / race conditions).
