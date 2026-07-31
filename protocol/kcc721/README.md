# KCC721 protocol reference

This directory contains the covenant sources and native transaction builder for
KCC721 v0.2 controlled Mainnet tests. Transactions are real and irreversible;
KCC721 remains a draft proposal.

## Components

- `kcc721-v2-collection.sil`: singleton blind-mint controller.
- `kcc721-v2-ticket.sil`: committed reveal ticket.
- `kcc721-v2-nft.sil`: transferable UTXO-native NFT.
- `kcc721-v2-migration.sil`: arbitrary one-time issue of 1-based KRC721 IDs.
- `engine/`: Kasware Safe JSON builder and covenant execution tests.

The browser at `https://devtools.kaslab.space/kcc721` selects a normal wallet
UTXO, asks the native engine to construct a transaction, and lets Kasware sign
the P2PK authorization input. Private keys never reach the server.

The hosted registry persists accepted operations and current outpoints in
SQLite. It follows transactions made through this deployment; a permissionless
BlockDAG indexer remains future work.

## Build and test

The engine pins SilverScript to an upstream Git revision and the Rust toolchain
in `engine/rust-toolchain.toml`, so no sibling checkout is required.

```bash
cd protocol/kcc721/engine
cargo test
cargo build --release
```

Python registry tests run from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The normative draft is in `docs/KCC721-DRAFT.md`.
