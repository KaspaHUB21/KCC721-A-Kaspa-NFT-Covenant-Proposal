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

The v0.2 engine supports true atomic transfers of 2 to 22 NFTs. Every NFT
covenant is consumed and recreated in one transaction, while one final P2PK
funding input authorizes the complete batch. Kasware therefore shows one
approval. Consensus accepts either every transition or none of them. The
22-NFT ceiling is enforced before signing because a 23-NFT reference batch
exceeds Mainnet's 500,000 Storage Mass limit.

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
