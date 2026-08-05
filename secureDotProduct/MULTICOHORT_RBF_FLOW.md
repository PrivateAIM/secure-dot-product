# Multi-Cohort RBF Kernels — Evaluation Flow

How `mpc_rbf_proxy_multicohort.py` gets from raw per-site matrices to one finished
kernel per modality, without any cohort seeing another's data.

Everything below uses the worked example from the file's `__main__`:

| | |
|---|---|
| **Cohorts** | 5 — `site_a` 3 patients, `site_b` 5, `site_c` 4, `site_d` 3, `site_e` 5 → **N = 20** |
| **Modalities** | 3 — `imaging` d=2, `labs` d=5, `omics` d=4 → Σd = 11 |
| **Pairs** | 10 |
| **Output** | 3 kernels, each 20 × 20, sharing identical axes |

Note `site_a`/`site_d` and `site_b`/`site_e` share a patient count. That is deliberate
and must work: the protocol keys cohorts by **node id**, never by row count.

---

## 0. The cast

```mermaid
flowchart TB
    subgraph COHORTS[Data holders - raw patient features never leave]
        A[site_a<br/>3 patients<br/>3 matrices]
        B[site_b<br/>5 patients<br/>3 matrices]
        C[site_c<br/>4 patients<br/>3 matrices]
        D[site_d<br/>3 patients<br/>3 matrices]
        E[site_e<br/>5 patients<br/>3 matrices]
    end

    P[Beaver Dealer<br/>PairwiseBeaverDealerProxy<br/>learns only 10 pair lengths<br/>never sees data, masks or output]
    G[Aggregator<br/>MultiCohortRBFAggregator<br/>sums shares, exponentiates,<br/>stitches blocks]

    P -.triples per pair.-> A
    P -.triples per pair.-> B
    P -.triples per pair.-> C
    P -.triples per pair.-> D
    P -.triples per pair.-> E

    A --- B
    A --- C
    A --- D
    A --- E
    B --- C
    B --- D
    B --- E
    C --- D
    C --- E
    D --- E

    A --> G
    B --> G
    C --> G
    D --> G
    E --> G

    G --> OUT[3 kernels<br/>each 20 x 20]
```

The ten lines between cohorts are the ten **peer-to-peer** Beaver exchanges. They
never pass through the dealer. The dealer's only input is a table of ten integers.
Their density is the point: exchanges grow as S², which is the dominant cost here.

---

## 1. Why this is ten 2-party problems, not one 5-party problem

The joint kernel is a 5 × 5 block matrix. Look at where each block's data comes from:

Rows and columns are grouped by cohort, so each cell below is a whole block:

| | **site_a** | **site_b** | **site_c** | **site_d** | **site_e** |
|---|---|---|---|---|---|
| **site_a** | `LOCAL` | `MPC` | `MPC` | `MPC` | `MPC` |
| **site_b** | transpose | `LOCAL` | `MPC` | `MPC` | `MPC` |
| **site_c** | transpose | transpose | `LOCAL` | `MPC` | `MPC` |
| **site_d** | transpose | transpose | transpose | `LOCAL` | `MPC` |
| **site_e** | transpose | transpose | transpose | transpose | `LOCAL` |

Where the work goes:

```mermaid
flowchart LR
    K[Kernel block K_st]
    K --> Q{s equals t}
    Q -- yes --> L[LOCAL<br/>one cohort's own rows<br/>plaintext RBF, no protocol]
    Q -- no, s before t --> M[MPC<br/>exactly two cohorts<br/>one 2-party Beaver run]
    Q -- no, t before s --> T[TRANSPOSE<br/>kernel is symmetric<br/>free, no computation]
```

- **Diagonal blocks** need only one cohort's own rows → computed in the clear, locally.
- **Off-diagonal blocks** touch exactly **two** cohorts → a 2-party secure computation.
- **Lower triangle** is the transpose of the upper — the kernel is symmetric, so it is free.

No block ever mixes three cohorts. That is the whole reason no new cryptography is
needed: `beaver_multiply`, and in particular the Mohassel–Zhang `_local_truncate`,
implement a strictly **2-party** protocol. It is reused unchanged, ten times.

Accounting for the 20 × 20 = 400 entries per modality:

| source | entries |
|---|---|
| diagonal blocks, computed locally in the clear | 3² + 5² + 4² + 3² + 5² = **84** |
| off-diagonal, from 158 secure patient-pairs, mirrored | 158 × 2 = **316** |

---

## 2. Round 0 — agree on a layout, order the triples

```mermaid
sequenceDiagram
    autonumber
    participant A as site_a
    participant B as site_b
    participant C as site_c
    participant E as site_e
    participant P as Dealer

    Note over A,E: every site broadcasts its shapes to every peer
    A->>B: shape_info
    A->>C: shape_info
    A->>E: shape_info
    Note over B,E: and symmetrically, all 20 directed messages across the 5 sites

    Note over A,E: each site now independently derives the SAME global picture
    Note over A,E: validate same modality set everywhere, same d per modality across sites, one patient count per site
    Note over A,E: site_ids sorted, 10 pairs enumerated, pair_lengths computed

    A->>P: n_elements with all 10 pair lengths
    B->>P: n_elements with all 10 pair lengths
    C->>P: n_elements with all 10 pair lengths
    E->>P: n_elements with all 10 pair lengths

    Note over P: tables are identical so take any one. For each pair generate that many triples, split each into 2 additive shares
    P->>A: beaver_triple - ONE message, bundle keyed by pair
    P->>B: beaver_triple - ONE message, bundle keyed by pair
    P->>C: beaver_triple - ONE message, bundle keyed by pair
    P->>E: beaver_triple - ONE message, bundle keyed by pair
```

*(`site_d` is omitted from the diagram only to keep it legible; it behaves exactly
like the others.)*

Three things are load-bearing here.

**No coordinator.** Every site derives `site_ids`, the pair list, and the block layout
from the same broadcast data using the same sorted ordering. They agree by
construction rather than by negotiation.

**The patient-count check.** Row *i* must be patient *i* in every modality at a site, so
all modalities there must report the same row count. This is what makes the three
kernels come out with identical axes and therefore combinable downstream. Cohorts
may still differ from each other — and two cohorts may well share the same count, as
`site_a`/`site_d` and `site_b`/`site_e` do here. Cohorts are told apart by node id.

**The dealer sends one message, not ten.** `await_intermediate_data` keeps only the
newest message per sender-and-category pair, so ten separate `beaver_triple` messages
would silently collapse to the last one. Bundling them into a single dict avoids this.

### Triples ordered, for the example

| pair | patients | × Σd | triples |
|---|---|---|---|
| a–b | 3 × 5 = 15 | 11 | 165 |
| a–c | 3 × 4 = 12 | 11 | 132 |
| a–d | 3 × 3 = 9 | 11 | 99 |
| a–e | 3 × 5 = 15 | 11 | 165 |
| b–c | 5 × 4 = 20 | 11 | 220 |
| b–d | 5 × 3 = 15 | 11 | 165 |
| b–e | 5 × 5 = 25 | 11 | 275 |
| c–d | 4 × 3 = 12 | 11 | 132 |
| c–e | 4 × 5 = 20 | 11 | 220 |
| d–e | 3 × 5 = 15 | 11 | 165 |
| | | | **1738** |

Listed by cohort name for readability. At runtime the ordering is by node id, which
is assigned randomly per run.

---

## 3. Round 1 — one Beaver exchange per pair

Each site walks the global pair list in sorted order and skips pairs it is not in.
`site_a` therefore runs four exchanges; every site runs `S-1 = 4`.

```mermaid
sequenceDiagram
    autonumber
    participant X as x-side, smaller node id
    participant Y as y-side
    Note over X,Y: repeated once per pair

    Note over X: flat = concat over modalities of tile_x
    Note over Y: flat = concat over modalities of tile_y

    rect rgb(232, 240, 254)
    Note over X,Y: input sharing
    X->>Y: data_share
    Y->>X: data_share
    end

    rect rgb(252, 238, 232)
    Note over X,Y: Beaver masking
    X->>Y: masked_values d and e
    Y->>X: masked_values d and e
    Note over X,Y: z = c + e*a + d*b, plus d*e on the x-side only, then Mohassel-Zhang local truncation
    end

    Note over X,Y: fold in - multiply by -2, add OWN squared norms
```

The two peer categories need no per-pair scoping. `await_messages` filters on sender
and preserves everyone else's traffic, and each unordered pair exists once, so no
sender/receiver/category combination ever repeats.

### The flat vector for one pair

Modalities are concatenated into a single vector so the pair needs only one Beaver
exchange. For pair a–b, with 3 × 5 patients:

```mermaid
flowchart LR
    S1[imaging<br/>offset 0<br/>3*5*2 = 30] --> S2[labs<br/>offset 30<br/>3*5*5 = 75] --> S3[omics<br/>offset 105<br/>3*5*4 = 60]
    S3 --> T[total 165]
```

Inside one modality segment, block `i,j` starts at `offset + i*n_y + j` times `d` and
runs for `d` elements — the same tiling the 2-cohort file uses, shifted by the
segment offset.

### The fold-in

Rather than shipping squared norms to the aggregator, each side folds them into its
share. Both operations are linear, so they are free on additive shares:

```
w_i  =  -2 * z_i        then add encode of own norms into each block
```

Summing the two sides' shares over a `d`-run gives

```
-2*dot[i,j] + norm_x[i] + norm_y[j]  =  squared distance between X_i and Y_j
```

The aggregator reconstructs the distance directly and never sees either cohort's
norms — which matters because the RBF is translation invariant, so the norms would
have revealed absolute position that the kernel itself hides.

---

## 4. Assembly — from shares to three kernels

```mermaid
flowchart TB
    IN[final_shares from all 5 sites<br/>dist_shares per pair, K_local per modality]
    IN --> CHK[validate all sites present,<br/>all agree on pair layout,<br/>share lengths match layout]

    CHK --> LOOP[for each of the 10 pairs]
    LOOP --> SUM[w = share_x + share_y mod 2^64]
    SUM --> SLICE[slice w per modality using segment offsets]
    SLICE --> BLK[block-sum each run of d, then decode]
    BLK --> CLAMP[clamp negatives to 0<br/>fixed-point error can undershoot]
    CLAMP --> EXP[exp of -gamma times squared distance]
    EXP --> CROSS[cross block K_st]

    CROSS --> ASM[assemble per modality]
    LOCAL[K_local diagonal blocks<br/>already exponentiated] --> ASM
    ASM --> OUT[kernels, site_ids, patients<br/>3 matrices, each 20 x 20]
```

Assembly walks `site_ids` in sorted order for both rows and columns. For block `s,t`
it takes the local block when `s == t`, the reconstructed cross block when the pair
was computed as `s,t`, and the transpose otherwise.

Because every modality now shares one set of axes, the axis metadata — `site_ids` and
`patients` — is returned **once** at the top level rather than repeated per kernel.

---

## 5. What each party learns

```mermaid
flowchart LR
    subgraph DEALER[Dealer sees]
        D1[10 pair lengths only]
    end
    subgraph SITE[A cohort sees]
        S1[its own data]
        S2[uniform-looking shares from peers]
        S3[masked values d and e]
    end
    subgraph AGG[Aggregator sees]
        G1[squared distances across cohorts]
        G2[the finished kernels]
    end
```

| party | can reconstruct | cannot |
|---|---|---|
| Dealer | the 10 pair lengths | any data, mask, or output |
| Cohort | its own rows | peers' rows — shares are uniform in the ring |
| Aggregator | the kernels, and geometry up to a rigid motion | absolute positions, raw features |

The aggregator's residual knowledge is inherent to producing a plaintext kernel:
from a full distance matrix, classical MDS recovers the point configuration up to
rotation, reflection and translation. Closing that would mean keeping the kernel
secret-shared and approximating `exp` inside MPC.

---

## 6. Cost

| quantity | scaling | example |
|---|---|---|
| pairs | S(S−1)/2 | 10 |
| triples | Σ over pairs, Σ over modalities of n_s·n_t·d | 1738 |
| peer round trips per site | 2(S−1) | 8 |
| dealer messages | S, one bundle each | 5 |
| rounds | 2, independent of S and of modality count | 2 |

Triples grow quadratically in the number of sites. Rounds do not — adding cohorts or
modalities never adds a round trip to the protocol's depth.

---

## Running it

```bash
python mpc_rbf_proxy_multicohort.py
```

Verifies all three kernels against a plaintext `rbf_kernel_matrix` over the stacked
cohorts, asserts they share axes, and demonstrates a weighted-sum combination.
