# KCC721: A Kaspa NFT Covenant Proposal

KCC721 is an experimental NFT protocol for Kaspa that represents collections,
mint commitments, and individual NFTs as covenant UTXOs. Ownership is not an
entry in an inscription database: it is the owner key committed to the current
unspent NFT covenant state.

This repository contains the full v0.2 proposal and the implementation used by
the public Mainnet lab:

- SilverScript covenant contracts
- a native Rust transaction builder for Kasware Safe JSON transactions
- blind commit/reveal minting with random 1-based token IDs
- direct KRC721-to-KCC721 migration issuance
- transfer support with wallet authorization
- deterministic test vectors and executable covenant tests
- a SQLite reference registry and browser test interface
- indexing and security profiles

> KCC721 is a draft proposal. The test page creates real, irreversible Mainnet
> transactions. It has not received an independent security audit.

## Live test page

**[Open the KCC721 Mainnet lab](https://devtools.kaslab.space/kcc721)**

The lab connects to Kasware. It can deploy a native collection, prepare a blind
mint, transfer a live KCC721 NFT, or deploy and issue a KRC721 migration
collection. Kasware signs the normal P2PK authorization input; the service never
receives a private key.

## Why a covenant NFT?

KRC721 ownership is reconstructed by an indexer from protocol operations.
KCC721 instead gives every NFT a covenant lineage and a live UTXO. The covenant
enforces the immutable NFT identity and permits only an authorized owner change.

The intended properties are:

1. **UTXO-native ownership.** A live NFT outpoint is the ownership record.
2. **Immutable identity.** Collection ID, token ID, and metadata digest cannot
   change during transfer.
3. **Wallet-compatible authorization.** Transfers co-spend a normal P2PK UTXO
   belonging to the owner, which Kasware can sign through its PSKT flow.
4. **Auditable blind minting.** Token assignments are committed before genesis
   and disclosed only after payment is accepted.
5. **One-time migration issuance.** Existing 1-based KRC721 IDs can be issued in
   arbitrary order, but a valid controller can never issue the same ID twice.

## Repository layout

```text
.
├── docs/
│   ├── KCC721-DRAFT.md              Normative v0.2 proposal
│   ├── KCC721-INDEXING.md           Permissionless indexer profile
│   ├── KCC721-SECURITY.md           Invariants and threat model
│   └── KCC721-V0.2-BLIND-MINT.md    Commit/reveal design
├── protocol/kcc721/
│   ├── kcc721-v2-collection.sil     Native collection controller
│   ├── kcc721-v2-ticket.sil         Blind mint reveal ticket
│   ├── kcc721-v2-nft.sil            NFT transfer covenant
│   ├── kcc721-v2-migration.sil      Migration issue controller
│   ├── engine/                       Rust transaction builder and tests
│   └── test-vectors/                 Deterministic proof fixtures
└── reference/
    ├── server.py                     API and persistent registry reference
    ├── public/                       Mainnet browser test interface
    └── tests/                        Registry regression tests
```

The two non-v2 SilverScript files and corresponding engine commands are retained
only to document and read early native experiments. New collection deployments
and every migration use v0.2.

## Protocol model

### Collection identity

A collection is identified by the Covenant ID of its controller genesis output.
Its descriptor commits to the ticker, maximum supply, immutable IPFS metadata
root, metadata digest, deployer key, mint mode, and mode-specific commitment.

Token IDs are integers from `1` through `maxSupply`. Metadata for token `n`
resolves to:

```text
{metadata_uri}/{n}.json
```

Ticker uniqueness is an indexer validity rule because one covenant cannot inspect
the entire BlockDAG. Conforming indexers assign a case-insensitive ticker to the
first valid v0.2 genesis in canonical accepted-transaction order and roll that
decision back during a reorganization.

### NFT state

Each v0.2 NFT stores:

```text
collectionId:   byte[32]
tokenId:        uint64
metadataDigest: byte[32]
owner:          pubkey
```

The transfer covenant preserves the first three fields. It verifies that a
separate transaction input is a P2PK UTXO controlled by the current owner and
that the successor keeps the NFT Covenant ID and required cell value.

### Native blind mint

Before deployment, the builder shuffles `1..maxSupply`, creates a random salt
for every mint position, and commits the Merkle root in the collection genesis:

```text
leaf = SHA256(LE64(mintIndex) || LE64(tokenId) || salt32)
```

Minting is split into two transactions:

1. **Commit:** the buyer advances the singleton controller, satisfies the mint
   price and DAA rule, and creates a ticket bound to the buyer and mint index.
   The token ID is not disclosed.
2. **Reveal:** the service supplies the committed token ID, salt, and Merkle
   path. The ticket verifies the proof and creates the NFT directly for the
   committed recipient.

The reveal transaction cannot redirect the NFT. Availability of the private
reveal artifact is an operational trust boundary, so it must be backed up and
should be published after the final mint for independent verification.

### KRC721 migration

A migration deployment is accepted only when the connected wallet is the source
KRC721 deployment address and the source collection is fully minted. Source IDs
remain exactly 1-based.

The controller starts with a Merkle tree of unissued IDs:

```text
unissued(id) = SHA256(LE64(id) || 0x01)
issued(id)   = SHA256(LE64(id) || 0x00)
```

An issue transaction proves that one ID is still unissued, changes only that
leaf, decrements the remaining count, and creates the matching NFT directly for
the selected recipient. The deployer authorizes each issue through a P2PK
co-spend. IDs may be issued in any order, which makes manual one-by-one airdrops
possible without pretending that planned recipients already own KCC721 UTXOs.

KRC721 remains an independent protocol and can continue moving after migration.
Interfaces therefore keep **KCC721 owner** and **KRC721 owner** as separate
fields.

## Transaction building and fees

The Rust engine compiles SilverScript contracts and creates Toccata Safe JSON
transactions for Kasware. It calculates compute mass, transient mass, normalized
fee mass, and storage mass. Mainnet fees use `100 sompi/gram`.

The reference API selects one confirmed plain wallet UTXO, gives the transaction
to Kasware for review and signing, broadcasts it, and waits for explicit Mainnet
acceptance before indexing the new controller or NFT outpoint.

Collection controllers are singleton UTXOs. Concurrent requests for one
collection must therefore be queued and rebuilt against the newest accepted
controller outpoint. Independent collections and NFT transfers can execute in
parallel.

## Indexing and ownership

The included SQLite registry follows transactions prepared through the reference
service. It validates acceptance and tracks current controller and NFT outpoints.
It is useful for live tests, but it is not a permissionless network indexer.

A complete indexer must consume virtual-chain additions and removals from a
Kaspa node, validate every covenant program and transition, persist undo data,
follow each Covenant ID lineage, and derive ownership only from the current live
NFT state. The full algorithm is described in
[`docs/KCC721-INDEXING.md`](docs/KCC721-INDEXING.md).

## Build the native engine

The project pins Rust and the upstream SilverScript revision. No sibling source
checkout is needed.

```bash
cd protocol/kcc721/engine
cargo test
cargo build --release
./target/release/kcc721-engine compile-info
```

The expected compile-info response identifies protocol version `0.2.0`, the
SilverScript compiler version, NFT template hash, and fee rate.

## Run the reference test page locally

Build the engine first, then:

```bash
cd reference
python3 server.py
```

Open `http://127.0.0.1:8112/kcc721`. Kasware normally requires an approved web
origin and Mainnet connectivity, so the hosted HTTPS page is the practical
end-to-end environment.

Runtime data is created under `reference/runtime/` and is ignored by Git.

## Tests

Run the covenant execution suite:

```bash
cd protocol/kcc721/engine
cargo test
```

Run the registry and migration regression suite:

```bash
cd reference
python3 -m unittest discover -s tests -v
```

The tests cover contract compilation, blind commit/reveal execution, transfers,
arbitrary 1-based migration issuance, duplicate issue rejection, ticker
canonicalization, owner updates, deployer custody views, malformed IPFS input,
and Merkle proof verification.

## Current status

The implementation supports real Mainnet genesis, blind mint, transfer,
migration genesis, and direct migration issue transactions. Before KCC721 should
move beyond Draft, it still needs:

- an independent implementation
- a permissionless full-BlockDAG indexer
- external security review
- wallet interoperability testing beyond the current Kasware flow
- broader review of covenant cell values and long-term availability assumptions

Discussion, reproducible test results, adversarial test cases, and alternative
indexer implementations are welcome.

## License

MIT. See [`LICENSE`](LICENSE).
