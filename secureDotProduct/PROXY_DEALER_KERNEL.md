# Proxy-Dealer Setup for the Pairwise Dot-Product Kernel

How `mpc_dot_proxy.py` + `mpc_core.py` compute the full kernel matrix

```
        ┌ K_XX  K_XY ┐
    K = │            │        K_XY[i,j] = X_i · Y_j
        └ K_YX  K_YY ┘
```

across **two cohorts that never see each other's data**, using a FLAME proxy node
as a **trusted Beaver-triple dealer**.

---

## 1. The cast

```mermaid
flowchart TB
    subgraph COHORTS[Data holders - hold raw patient features]
        A1[Analyzer X<br/>KernelAnalyzer<br/>matrix X is n_x by d<br/>role is_x = True<br/>smaller node id]
        A2[Analyzer Y<br/>KernelAnalyzer<br/>matrix Y is n_y by d<br/>role is_x = False<br/>larger node id]
    end

    P[Proxy = Beaver Dealer<br/>BeaverDealerProxy<br/>learns only n = n_x n_y d<br/>never sees X, Y, masks or z]
    AGG[Aggregator<br/>KernelAggregator<br/>sums shares mod 2^64<br/>decodes, assembles blocks]

    P -.beaver_triple a_i b_i c_i.-> A1
    P -.beaver_triple a_j b_j c_j.-> A2

    A1 == data_share and masked_values ==> A2
    A2 == peer-to-peer, bypasses the dealer ==> A1

    A1 -- final_shares z_i and K_XX --> AGG
    A2 -- final_shares z_j and K_YY --> AGG

    AGG --> OUT[kernel_matrix<br/>n_x+n_y square]
```

**Key structural point:** the FLAME `ProxyModel` pattern normally relays
`analyzers → proxy → aggregator`. Here that relay path carries **nothing** — every
`analysis_method` returns `None`. All real traffic goes over explicit side channels
identified by `message_category`, so the dealer is architecturally in the middle but
cryptographically on the sidelines.

---

## 2. Why a kernel needs more than an element-wise multiply

`K[i,j] = X_i · Y_j` is a *contraction*, not a Hadamard product. The Beaver core only
knows how to multiply two flat vectors element-wise, so the kernel adds exactly two
pieces of logic on top:

```mermaid
flowchart LR
    IN[X is n_x by d<br/>Y is n_y by d] --> B[1. Broadcast<br/>tile_x and tile_y<br/>two flat vectors of<br/>length n_x n_y d]
    B --> M[2. Element-wise<br/>Beaver multiply<br/>unchanged core]
    M --> S[3. Block-sum<br/>add each run of d<br/>consecutive products]
    S --> K[K_XY is n_x by n_y]

    style B fill:#e8f0fe,stroke:#4285f4
    style S fill:#e8f0fe,stroke:#4285f4
    style M fill:#f1f3f4,stroke:#9aa0a6
```

Only the blue boxes are kernel-specific. Triples, masking and truncation are reused
verbatim from `mpc_mul_proxy.py`.

### The tiling layout

Both sides agree on one flat index ordering, so that position
`(i·n_y + j)·d + k` holds `X[i][k]` on one side and `Y[j][k]` on the other:

```mermaid
flowchart TB
    subgraph TX[tile_x - repeat each X row n_y times]
        TXR[X0 X0 X0 -- X1 X1 X1]
    end
    subgraph TY[tile_y - cycle all Y rows n_x times]
        TYR[Y0 Y1 Y2 -- Y0 Y1 Y2]
    end
    TXR --> PROD[element-wise product<br/>X0Y0 X0Y1 X0Y2 -- X1Y0 X1Y1 X1Y2]
    TYR --> PROD
    PROD --> SUM[sum each block of d<br/>K_XY 0,0 then 0,1 then 0,2 -- then 1,0 and on]
```

*(example with n_x = 2, n_y = 3; each symbol above is a length-`d` run)*

---

## 3. Full protocol flow — 2 rounds

```mermaid
sequenceDiagram
    autonumber
    participant X as Analyzer X - is_x True
    participant Y as Analyzer Y - is_x False
    participant D as Proxy Dealer
    participant G as Aggregator

    Note over X,Y: ROUND 0 - setup, num_iterations == 0

    X->>Y: shape_info with n_x and d
    Y->>X: shape_info with n_y and d
    Note over X,Y: assert d matches, both derive n_x and n_y
    X->>D: n_elements, n = n_x n_y d
    Y->>D: n_elements, n = n_x n_y d

    Note over D: for each of n elements: A and B random in ring 2^64, C = A B un-truncated, split additively into 2 shares
    D->>X: beaver_triple a_i b_i c_i
    D->>Y: beaver_triple a_j b_j c_j

    Note over X,Y: ROUND 1 - compute, num_iterations == 1

    Note over X: flat = tile_x of X with n_y
    Note over Y: flat = tile_y of Y with n_x

    rect rgb(232, 240, 254)
    Note over X,Y: input sharing - each party splits its OWN encoded operand in two
    X->>Y: data_share, X remote share
    Y->>X: data_share, Y remote share
    Note over X,Y: now both hold a share of x and of y
    end

    rect rgb(252, 238, 232)
    Note over X,Y: Beaver masking
    Note over X: d_i = x_i - a_i and e_i = y_i - b_i
    Note over Y: d_j = x_j - a_j and e_j = y_j - b_j
    X->>Y: masked_values d_i and e_i
    Y->>X: masked_values d_j and e_j
    Note over X,Y: d = d_i + d_j, e = e_i + e_j, z = c + e a + d b plus d e on X only, then local truncation by f bits
    end

    Note over X: K_XX = X times X transpose in cleartext, own data
    Note over Y: K_YY = Y times Y transpose in cleartext, own data
    X->>G: final_shares z_i, K_XX, is_x, n_x, n_y, d
    Y->>G: final_shares z_j, K_YY, is_x, n_x, n_y, d
    Note over X,Y: finished = True

    Note over G: z = z_i + z_j mod 2^64, block-sum and decode to K_XY, K_YX is K_XY transposed, assemble the block matrix
    G->>G: has_converged, stop
```

---

## 4. Aggregator reconstruction

```mermaid
flowchart TB
    Z1[z_i from X] --> ADD[z = z_i + z_j mod 2^64<br/>additive shares collapse]
    Z2[z_j from Y] --> ADD
    ADD --> BS[block-sum<br/>start = i n_y + j times d<br/>sum the next d entries]
    BS --> DEC[decode two's-complement<br/>then divide by 2^16]
    DEC --> KXY[K_XY is n_x by n_y]
    KXY --> TR[K_YX = K_XY transposed<br/>no extra MPC]

    KXX[K_XX<br/>cleartext from X] --> ASM
    KYY[K_YY<br/>cleartext from Y] --> ASM
    KXY --> ASM
    TR --> ASM
    ASM[assemble block matrix] --> FINAL[kernel_matrix<br/>n_x+n_y square]

    style KXY fill:#e8f0fe,stroke:#4285f4
    style FINAL fill:#e6f4ea,stroke:#34a853
```

Only the **off-diagonal** block `K_XY` costs an MPC protocol. The diagonal blocks are
each computed locally in the clear — a cohort learns nothing new by taking inner
products within its own data.

---

## 5. Why the dealer is safe

```mermaid
flowchart LR
    subgraph SEE[What the dealer receives]
        S1[n_elements - a single count n]
    end
    subgraph NOTSEE[What never reaches it]
        N1[raw X and Y]
        N2[data_share - input shares]
        N3[masked_values d and e]
        N4[final z_i shares]
        N5[the kernel itself]
    end
    S1 --> CONC[dealer learns only<br/>n_x times n_y times d]

    style SEE fill:#fef7e0,stroke:#f9ab00
    style NOTSEE fill:#fce8e6,stroke:#d93025
    style CONC fill:#e6f4ea,stroke:#34a853
```

| Node | Sees | Cannot reconstruct |
|---|---|---|
| Dealer (proxy) | the element count `n` | inputs, masked values, output |
| Analyzer X | own `X`, one additive share of `Y`, masked `d`/`e` | `Y` (shares are uniform in the ring) |
| Analyzer Y | own `Y`, one additive share of `X`, masked `d`/`e` | `X` |
| Aggregator | both `z` shares → the kernel | the raw feature matrices |

The masked values `d = x − a` and `e = y − b` are uniformly random to each party because
`a` and `b` are fresh, uniform, and known only in shares — that is exactly the Beaver
guarantee. The dealer is the only entity that ever knows `A` and `B` in the clear, and it
never sees `d` or `e`, so it cannot invert the mask.

---

## 6. Implementation details worth knowing

**Role assignment is by node id.** `is_x = self.id < partner_id`. The cohort with the
smaller id becomes the "x side". Which physical cohort that is varies per run, which is
why the `__main__` harness reorders its plaintext reference with
`X_, Y_ = (Xa, Ya) if result["n_x"] == N_X else (Ya, Xa)`.

**`is_x` drives three asymmetries:**

```mermaid
flowchart TB
    ISX{is_x}
    ISX -- True --> T1[tile_x - repeat rows]
    ISX -- True --> T2[holds x_i as own share<br/>y_i as received]
    ISX -- True --> T3[adds d times e to z<br/>exactly one party must]
    ISX -- True --> T4[truncate z right-shift f]
    ISX -- False --> F1[tile_y - cycle rows]
    ISX -- False --> F2[holds x_i as received<br/>y_i as own share]
    ISX -- False --> F3[does NOT add d times e]
    ISX -- False --> F4[truncate negated<br/>Mohassel-Zhang]
```

**Truncation is applied to `z`, not baked into the triple.** The dealer stores
`C = A·B` un-truncated on purpose; each party truncates its own `z_i` at the end. The
two-party Mohassel–Zhang scheme is correct up to a small probability of a large error
and introduces ±1 ulp noise — hence the `atol=0.05` tolerance in the self-test rather
than exact equality.

**Fixed point:** ring `2^64`, `PRECISION_BITS = 16`. Feature values must stay well
inside the ring after the `d`-term accumulation, or products wrap around silently.

**Round dispatch is by `self.num_iterations`.** Round 0 does shape exchange + triple
request; round 1 does the multiply. `has_converged` returns `True` at
`num_iterations >= 1`, and analyzers set `self.finished = True` to leave the loop.

---

## 7. Known limitations

- **Hard-wired to 2 analyzers.** `partner_analyzer_id()` picks `others[0]`, so a third
  cohort breaks the pairing. Generalizing means pairing `i → i+1` cyclically.
- **Message categories are global.** With several pairs exchanging at once,
  `beaver_triple` / `masked_values` would collide across pairs. Each pair needs its own
  channel before scaling past two cohorts.
- **The dealer is trusted.** It knows `A` and `B`; a dealer colluding with either
  analyzer breaks privacy. Replacing it with OT- or HE-based triple generation removes
  that assumption.

---

## Running it

```bash
python mpc_dot_proxy.py   # kernel, verified against a plaintext np.block
python mpc_mul_proxy.py   # the element-wise multiply this was built from
```

Requires the `proxy-nodes` branch of `python-sdk-patterns` (`flame.proxy` does not
exist on `main`), and `num_proxy_nodes=1`.
