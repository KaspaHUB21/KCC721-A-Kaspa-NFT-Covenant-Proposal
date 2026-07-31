# KCC721: UTXO-native NFTs on Kaspa

```text
KIP: TBD
Layer: Applications
Title: KCC721 UTXO-native non-fungible tokens
Author: HUB21
Status: Draft
Version: 0.2.0
```

## Abstract

KCC721 represents collection controllers, mint tickets, and NFTs as Kaspa
covenant UTXOs. An NFT's live covenant outpoint is its ownership record. Its
collection ID, 1-based token ID, and metadata digest are immutable; transfer
changes only the owner.

The proposal builds on [KIP-20 Covenant IDs](https://github.com/kaspanet/kips/blob/master/kip-0020.md)
and the KCC1/SilverScript covenant ABI. An inscription indexer is not an
authority for KCC721 ownership.

## Terminology

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are normative.

- **Collection ID:** Covenant ID of the collection controller genesis.
- **NFT ID:** Covenant ID of an NFT genesis output.
- **Live owner:** Owner public key in the current unspent NFT covenant state.
- **Canonical genesis:** First valid accepted v0.2 genesis for a ticker.

## Collection genesis

A conforming v0.2 descriptor commits to:

```text
protocol:          "kcc-721"
version:           "0.2.0"
ticker:            ASCII[A-Z0-9], length 1..10
max_supply:        uint64 > 0
metadata_uri:      immutable ipfs:// collection root
metadata_digest:   SHA256(canonical metadata_uri)
token_id_base:     1
mint_mode:         "commit-reveal" | "migration-merkle-issue"
deployer_pubkey:   x-only public key
```

Native collections additionally commit to `shuffle_root`, `mint_price_sompi`,
and `mint_daa_score`. Migration collections additionally commit to the source
KRC721 deployment descriptor and the initial `unissued_root`.

The collection ID, not its ticker, is the cryptographic identity.

## NFT state and transfer

Each NFT state contains:

```text
collection_id:    byte[32]
token_id:         uint64 in 1..max_supply
metadata_digest:  byte[32]
owner:            pubkey
```

A transfer MUST preserve the first three fields and MAY replace only `owner`.
It MUST co-spend a signed P2PK input belonging to the current owner. The
successor output MUST preserve the NFT Covenant ID and required cell value.

An indexer MUST derive ownership from the latest accepted, unspent covenant
outpoint. Planned recipients, KRC721 owners, metadata, or hosted registry rows
MUST NOT override that state.

## Native blind mint

Native token IDs are a secret permutation of `1..max_supply`. Leaf `i` is:

```text
SHA256(LE64(mint_index) || LE64(token_id) || salt32)
```

Genesis commits to the Merkle root. A commit transition atomically advances
the singleton controller, enforces mint timing and price, and creates a ticket
bound to the buyer public key and mint index. It does not disclose the token
ID. A reveal consumes that ticket, verifies its Merkle proof, and creates the
NFT directly for the committed recipient.

Every reveal artifact SHOULD be replicated before public minting. The complete
permutation and salts SHOULD be published after the final reveal so anyone can
audit the collection.

## KRC721 migration

Migration is allowed only when the connected deployment key equals the source
KRC721 deployment key and the source collection is fully minted. KCC721 does
not freeze or modify the KRC721 collection.

Source IDs remain exactly `1..source_supply`. The migration controller stores
a Merkle root whose leaves are:

```text
unissued(token_id) = SHA256(LE64(token_id) || 0x01)
issued(token_id)   = SHA256(LE64(token_id) || 0x00)
```

The tree is padded to the next power of two with
`SHA256(LE64(0) || 0x00)`. An issue transition proves an unissued leaf, replaces
it with its issued leaf, decrements `remaining`, and creates one NFT with that
same token ID directly for the specified recipient. The deployer MUST authorize
the transaction with a P2PK co-spend. IDs MAY be issued in any order and each ID
MUST be issued no more than once.

Before issue, an interface MAY display an NFT as **deployer custody / not
issued**, but MUST distinguish this from a live NFT owner. It MAY separately
display the current KRC721 owner for migration planning.

## Metadata

Metadata for token `n` resolves to `{metadata_uri}/{n}.json`. The metadata URI
and digest are immutable. Images SHOULD use content-addressed `ipfs://` URIs.

KCC721 imposes no byte-size limit on an image. Hosted gateways and applications
MAY impose documented response limits on metadata JSON for availability and
resource protection; such limits are not consensus or protocol rules.

## Canonical ticker rule

Ticker comparison is case-insensitive. Conforming indexers MUST assign a ticker
to the first valid v0.2 genesis in canonical accepted-transaction order and
mark later duplicates noncanonical. They MUST apply and roll back this decision
with virtual-chain changes. Local preparation reservations are only a user
experience measure and do not establish canonical ownership.

Experimental v0.1 migration descriptors are not canonical v0.2 migration
collections and MUST NOT reserve a v0.2 ticker.

## Permissionless indexing

A conforming indexer MUST:

1. Consume accepted virtual-chain transactions in canonical order.
2. Validate descriptor, covenant program, state, Covenant ID, and output binding.
3. Follow each controller, ticket, and NFT lineage by Covenant ID.
4. Reject transitions whose previous outpoint is not the current live outpoint.
5. Derive NFT ownership only from validated live NFT state.
6. Persist enough undo data to roll back removed virtual-chain blocks.
7. Re-evaluate ticker assignment after every rollback.

The hosted DevTools registry currently follows transactions prepared through
its own builder. It is a live-test registry, not yet a permissionless network
indexer.

## Concurrency and scaling

The native controller and migration controller are singleton UTXOs. Consensus
therefore serializes transitions per collection. Services MAY queue competing
requests, but MUST rebuild against the newest accepted controller outpoint
before signing. Different collections and independent NFT transfers can proceed
concurrently.

## Security considerations

- Lost blind-reveal artifacts can strand tickets; replicate and export them.
- A migration deployer can choose recipients, but cannot issue an ID twice or
  alter immutable NFT identity fields.
- KRC721 can continue moving after migration; interfaces must show both states.
- Metadata availability depends on IPFS pinning and gateways.
- Hosted registries can omit transactions; ownership claims require independent
  covenant validation.
- Mainnet transactions are irreversible once accepted.

## Backward compatibility

Native experimental v0.1 lineages may remain readable for controlled tests.
New deployments and all KRC721 migrations MUST use v0.2. No v0.1 migration is
promoted, repaired, or silently reinterpreted as v0.2.

## Reference implementation status

Kaspa Dev Tools can build and broadcast v0.2 Mainnet collection genesis,
blind commit/reveal mint, NFT transfer, migration genesis, and arbitrary
one-time migration issue transactions through Kasware. The native builder
calculates normalized Toccata fees at 100 sompi per gram and never receives a
private key.

Before a standards submission moves beyond Draft, the project still requires
an independent implementation, permissionless BlockDAG indexer, external
security review, and public interoperability vectors from more than one wallet.
