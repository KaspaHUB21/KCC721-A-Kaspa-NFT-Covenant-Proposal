# KCC721 v0.2 security notes

## Protected invariants

- NFT collection ID, token ID, and metadata digest do not change on transfer.
- A transfer requires a P2PK co-spend by the current owner.
- Native mint positions advance once through a singleton controller.
- Reveal token IDs must belong to the genesis shuffle commitment.
- Migration IDs are 1-based, may be issued in any order, and cannot be issued
  twice from a valid controller lineage.
- Migration issue requires deployer authorization and binds the NFT recipient.

## Operational risks

The native blind mint depends on private reveal artifacts. Operators should
replicate and test restoration before accepting payments. A malicious or
unavailable reveal operator cannot redirect a ticket, but can delay or prevent
reveal.

Migration does not freeze KRC721. Source ownership shown by an interface can
change independently after a KCC721 issue. The two ownership records must remain
separate.

Metadata and images can be unavailable despite immutable IPFS addressing. Pin
collection data with more than one provider and avoid using metadata as an
authorization input.

## Hosted service boundary

Kaspa Dev Tools prepares transactions and records accepted operations, but does
not hold wallet keys. Its SQLite registry is not a permissionless source of
truth. Users and independent indexers should verify transactions, Covenant IDs,
script state, and unspent lineage tips against their own Kaspa node.

## Verification status

The reference implementation includes executable positive and negative
covenant tests plus registry regression tests. Mainnet use remains experimental
while KCC721 is a draft protocol.
