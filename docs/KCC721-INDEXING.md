# KCC721 v0.2 indexing profile

This profile describes a permissionless indexer target. The current DevTools
registry follows only transactions prepared through its own service.

## Input stream

An indexer should consume Kaspa virtual-chain changes from a trusted local node,
including accepted transactions in added blocks and removed-chain notifications.
It must persist a cursor plus block-scoped undo records atomically with indexed
state.

## Validation order

For every candidate genesis or transition:

1. Decode the payload and require protocol `kcc-721`, version `0.2.0`.
2. Validate field encoding, token bounds, metadata digest, and mint mode.
3. Recompile or otherwise verify the exact covenant program and initial state.
4. Recompute Covenant IDs and output bindings.
5. For a transition, require the spent outpoint to be the current lineage tip.
6. Execute or independently reproduce all covenant transition checks.
7. Apply ticker canonicalization only after the genesis is otherwise valid.

For an atomic NFT batch, validate every spent lineage tip and every successor
binding before committing any owner update. All updated lineage tips belong in
the same database transaction so the indexed view cannot expose a partial batch.

Unknown versions must not be interpreted using v0.2 rules.

## State tables

At minimum, persist collections, controller tips, tickets, NFT lineage tips,
current owners, canonical ticker assignments, processed virtual-chain blocks,
and undo records. Ownership is the owner key/address decoded from the live NFT
covenant state, never a payload recipient or migration source record.

## Reorganizations

Removed virtual-chain blocks are rolled back in reverse accepted order. Added
blocks are then applied in accepted order. A rollback can invalidate a ticker's
first genesis, restore a spent controller or NFT tip, and remove a mint or
migration issue. All derived views must be updated in the same database
transaction as the rollback.

## Migration views

The migration Merkle root and remaining count belong to controller state. An
indexer may expose unissued source IDs as a planning view for the deployer, but
must label them as custody/not issued. Only an accepted NFT covenant genesis
creates a live KCC721 owner.
