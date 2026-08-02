import os
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

TEST_RUNTIME = tempfile.mkdtemp(prefix="kcc721-tests-")
os.environ["KASPA_DEVTOOLS_RUNTIME_DIR"] = TEST_RUNTIME
import server

VALID_IPFS = "ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3wljymbk7buz4m5v2l3k4m5aa"


class Kcc721RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "devtools.sqlite3"
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.db_patch.start()
        server.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def operation(self, **overrides):
        operation = {
            "id": "a" * 32,
            "walletAddress": "kaspa:test",
            "kind": "public-mint",
            "collectionId": "b" * 64,
            "expectedTxid": "c" * 64,
            "txid": None,
            "status": "prepared",
            "createdAt": server.now_iso(),
            "updatedAt": server.now_iso(),
        }
        operation.update(overrides)
        return operation

    def test_transaction_lookup_returns_persisted_operation(self):
        operation = self.operation(txid="c" * 64, status="submitted")
        server.db_save_kcc721_operation(operation)

        stored = server.db_get_kcc721_operation_by_txid("c" * 64)

        self.assertEqual(stored["id"], operation["id"])
        self.assertEqual(stored["status"], "submitted")

    def test_submitted_operation_never_loses_its_reservation(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        server.db_save_kcc721_operation(self.operation(status="submitted", createdAt=old))

        self.assertTrue(server.db_has_active_kcc721_operation("b" * 64, "public-mint"))

    def test_stale_unsubmitted_operation_releases_its_reservation(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        server.db_save_kcc721_operation(self.operation(status="prepared", createdAt=old))

        self.assertFalse(server.db_has_active_kcc721_operation("b" * 64, "public-mint"))

    def test_atomic_batch_reserves_every_nft(self):
        operation = self.operation(
            kind="nft-batch-transfer",
            nftId="1" * 64,
            nftIds=["1" * 64, "2" * 64, "3" * 64],
            status="prepared",
        )
        server.db_save_kcc721_operation(operation)

        self.assertTrue(server.db_has_active_kcc721_nft_operation("1" * 64))
        self.assertTrue(server.db_has_active_kcc721_nft_operation("2" * 64))
        self.assertTrue(server.db_has_active_kcc721_nft_operation("3" * 64))
        self.assertFalse(server.db_has_active_kcc721_nft_operation("4" * 64))

    def test_cancelled_batch_releases_every_nft(self):
        wallet = "kaspa:qptest"
        operation = self.operation(
            kind="nft-batch-transfer",
            walletAddress=wallet,
            nftId="1" * 64,
            nftIds=["1" * 64, "2" * 64],
            status="prepared",
        )
        server.db_save_kcc721_operation(operation)

        with patch.object(server, "clean_kaspa_address", return_value=wallet):
            result = server.cancel_kcc721_operation({
                "operationId": operation["id"],
                "walletAddress": wallet,
            })

        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(server.db_has_active_kcc721_nft_operation("1" * 64))
        self.assertFalse(server.db_has_active_kcc721_nft_operation("2" * 64))

    def test_repreparing_same_wallet_batch_releases_previous_reservation(self):
        wallet = "kaspa:qptest"
        operation = self.operation(
            kind="nft-batch-transfer",
            walletAddress=wallet,
            nftId="1" * 64,
            nftIds=["1" * 64, "2" * 64],
            status="prepared",
        )
        server.db_save_kcc721_operation(operation)

        cancelled = server.db_cancel_matching_prepared_kcc721_batches(wallet, {"2" * 64, "3" * 64})

        self.assertEqual(cancelled, 1)
        self.assertEqual(server.db_get_kcc721_operation(operation["id"])["status"], "cancelled")
        self.assertFalse(server.db_has_active_kcc721_nft_operation("1" * 64))

    def test_mint_queue_is_fifo_and_waits_for_active_commit(self):
        collection_id = "b" * 64
        first = self.operation(
            id="1" * 32,
            kind="mint-queue",
            collectionId=collection_id,
            walletAddress="kaspa:first",
            status="queued",
            ticker="QUEUE",
        )
        second = self.operation(
            id="2" * 32,
            kind="mint-queue",
            collectionId=collection_id,
            walletAddress="kaspa:second",
            status="queued",
            ticker="QUEUE",
            createdAt=(datetime.now(timezone.utc) + timedelta(milliseconds=1)).isoformat(),
        )
        server.db_save_kcc721_operation(first)
        server.db_save_kcc721_operation(second)

        self.assertEqual(server.db_kcc721_mint_queue_position(first), 1)
        self.assertEqual(server.db_kcc721_mint_queue_position(second), 2)
        self.assertTrue(server.kcc721_mint_queue_response(first)["ready"])

        active = self.operation(
            id="3" * 32,
            kind="blind-mint-commit",
            collectionId=collection_id,
            walletAddress="kaspa:active",
            status="submitted",
        )
        server.db_save_kcc721_operation(active)
        self.assertFalse(server.kcc721_mint_queue_response(first)["ready"])

    def test_abandoned_mint_queue_entry_expires(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        operation = self.operation(
            id="1" * 32,
            kind="mint-queue",
            walletAddress="kaspa:gone",
            status="queued",
            createdAt=old,
            updatedAt=old,
        )
        server.db_save_kcc721_operation(operation)

        self.assertEqual(server.db_expire_stale_kcc721_mint_queue("b" * 64), 1)
        self.assertEqual(server.db_get_kcc721_operation(operation["id"])["status"], "expired")

    def test_accepted_collection_is_visible_in_indexer(self):
        operation = self.operation(
            kind="collection-genesis",
            collectionId="b" * 64,
            txid="c" * 64,
            status="accepted",
            nftIds=[],
            manifest={
                "ticker": "HUBNFT",
                "deploymentPublicKey": "d" * 64,
                "maxSupply": 21,
                "metadataUri": "ipfs://example",
                "metadataDigest": "e" * 64,
                "mintPriceSompi": 100_000_000,
                "mintDaaScore": 0,
            },
        )
        server.db_save_kcc721_operation(operation)
        server.db_index_kcc721_operation(operation)

        rows = server.db_list_kcc721_collections("hub")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "HUBNFT")
        self.assertEqual(rows[0]["indexed_nfts"], 0)

    def test_migration_plan_uses_one_based_merkle_issuance(self):
        source = {
            "tick": "LEGACY",
            "supply": "21",
            "minted": "21",
            "premint": "3",
            "mintDaaScore": "100",
            "royaltySompi": "500000000",
            "metadataUrl": VALID_IPFS,
            "deployer": "kaspa:qptest",
            "deployTransactionId": "f" * 64,
            "eligible": True,
        }
        with patch.object(server, "clean_kaspa_address", return_value="kaspa:qptest"), patch.object(
            server, "kcc721_migration_preview", return_value=source
        ):
            plan = server.build_kcc721_plan(
                {
                    "mode": "migrate",
                    "walletAddress": "kaspa:qptest",
                    "ticker": "LEGACY",
                    "publicKey": "a" * 64,
                }
            )

        self.assertEqual(plan["mode"], "migrate")
        self.assertEqual(plan["premintAllocation"], 0)
        self.assertEqual(plan["mintPriceSompi"], 0)
        self.assertEqual(plan["version"], "0.2.0")
        self.assertEqual(plan["tokenIdBase"], 1)
        self.assertEqual(plan["mintMode"], "migration-merkle-issue")
        self.assertEqual(plan["migration"]["status"], "genesis-ready / manual-issue")
        self.assertIsNone(plan["migration"]["holderSnapshotRoot"])

    def test_native_plan_uses_blind_mint_v2_and_no_premint(self):
        with patch.object(server, "clean_kaspa_address", return_value="kaspa:qptest"):
            plan = server.build_kcc721_plan(
                {
                    "mode": "deploy",
                    "walletAddress": "kaspa:qptest",
                    "ticker": "BLIND",
                    "publicKey": "a" * 64,
                    "supply": "287",
                    "metadataUrl": VALID_IPFS,
                    "premint": "3",
                }
            )

        self.assertEqual(plan["version"], "0.2.0")
        self.assertEqual(plan["tokenIdBase"], 1)
        self.assertEqual(plan["mintMode"], "commit-reveal")
        self.assertEqual(plan["premintAllocation"], 0)

    def test_hosted_blind_mint_rejects_unbounded_shuffle_supply(self):
        with patch.object(server, "clean_kaspa_address", return_value="kaspa:qptest"):
            with self.assertRaisesRegex(server.BadRequest, "NFT supply is too large"):
                server.build_kcc721_plan(
                    {
                        "mode": "deploy",
                        "walletAddress": "kaspa:qptest",
                        "ticker": "HUGE",
                        "publicKey": "a" * 64,
                        "supply": "25001",
                        "metadataUrl": "ipfs://huge",
                    }
                )

    def test_invalid_ipfs_cid_is_rejected_before_deployment(self):
        with self.assertRaisesRegex(server.BadRequest, "invalid IPFS CID"):
            server.clean_ipfs_uri("ipfs://notarealcid")

    def test_metadata_validation_checks_first_and_last_nft(self):
        with patch.object(
            server,
            "fetch_ipfs_json",
            return_value={"name": "NFT", "image": f"{VALID_IPFS}/image.png"},
        ) as fetch:
            server.validate_kcc721_metadata(VALID_IPFS, 287)

        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [f"{VALID_IPFS}/1.json", f"{VALID_IPFS}/287.json"],
        )

    def test_metadata_without_immutable_image_is_rejected(self):
        with patch.object(server, "fetch_ipfs_json", return_value={"image": "https://example.com/1.png"}):
            with self.assertRaisesRegex(server.BadRequest, "invalid image URI"):
                server.validate_kcc721_metadata(VALID_IPFS, 1)

    def test_shuffle_is_complete_one_based_and_each_proof_matches_root(self):
        artifact = server.build_kcc721_shuffle(287)
        token_ids = [entry["tokenId"] for entry in artifact["entries"]]

        self.assertEqual(sorted(token_ids), list(range(1, 288)))
        self.assertEqual(len(set(token_ids)), 287)
        for entry in artifact["entries"]:
            node = hashlib.sha256(
                int(entry["mintIndex"]).to_bytes(8, "little")
                + int(entry["tokenId"]).to_bytes(8, "little")
                + bytes.fromhex(entry["salt"])
            ).digest()
            for sibling_hex, direction in zip(entry["siblings"], entry["directions"]):
                sibling = bytes.fromhex(sibling_hex)
                node = hashlib.sha256(node + sibling if direction == 0 else sibling + node).digest()
            self.assertEqual(node.hex(), artifact["shuffleRoot"])

    def test_migration_issue_proof_updates_arbitrary_id_once(self):
        artifact = server.build_kcc721_migration_artifact(21)
        issue = server.build_kcc721_migration_issue(artifact, 17)
        live = hashlib.sha256((17).to_bytes(8, "little") + b"\x01").digest()
        spent = hashlib.sha256((17).to_bytes(8, "little") + b"\x00").digest()
        for sibling_hex, direction in zip(issue["siblings"], issue["directions"]):
            sibling = bytes.fromhex(sibling_hex)
            live = hashlib.sha256(live + sibling if direction == 0 else sibling + live).digest()
            spent = hashlib.sha256(spent + sibling if direction == 0 else sibling + spent).digest()
        self.assertEqual(live.hex(), issue["currentUnissuedRoot"])
        self.assertEqual(spent.hex(), issue["nextUnissuedRoot"])
        self.assertIn(17, issue["nextArtifact"]["issuedTokenIds"])
        with self.assertRaisesRegex(server.BadRequest, "already been issued"):
            server.build_kcc721_migration_issue(issue["nextArtifact"], 17)

    def test_unissued_migration_tokens_are_visible_in_deployer_custody(self):
        collection_id = "7" * 64
        deployer = "kaspa:deployer"
        manifest = {
            "version": "0.2.0",
            "ticker": "MIGRATE",
            "deploymentPublicKey": "d" * 64,
            "maxSupply": 3,
            "metadataUri": VALID_IPFS,
            "metadataDigest": "e" * 64,
            "mintPriceSompi": 0,
            "mintDaaScore": 0,
            "mintMode": "migration-merkle-issue",
            "tokenIdBase": 1,
            "migration": {
                "sourceTicker": "MIGRATE",
                "mintedAtPreview": 3,
                "status": "genesis-ready / manual-issue",
            },
        }
        genesis = self.operation(
            kind="migration-genesis",
            collectionId=collection_id,
            txid="8" * 64,
            walletAddress=deployer,
            status="accepted",
            nftIds=[],
            manifest=manifest,
        )
        server.db_index_kcc721_operation(genesis)
        server.save_kcc721_shuffle(collection_id, server.build_kcc721_migration_artifact(3))

        rows, total = server.db_list_kcc721_migration_custody(deployer, 0, 100)

        self.assertEqual(total, 3)
        self.assertEqual([row["token_id"] for row in rows], [1, 2, 3])
        self.assertTrue(all(row["custody_state"] == "migration custody / not issued" for row in rows))

    def test_accepted_migration_issue_moves_token_to_real_recipient_owner(self):
        collection_id = "7" * 64
        deployer = "kaspa:deployer"
        recipient = "kaspa:recipient"
        manifest = {
            "version": "0.2.0",
            "ticker": "MIGRATE",
            "deploymentPublicKey": "d" * 64,
            "maxSupply": 3,
            "metadataUri": VALID_IPFS,
            "metadataDigest": "e" * 64,
            "mintPriceSompi": 0,
            "mintDaaScore": 0,
            "mintMode": "migration-merkle-issue",
            "tokenIdBase": 1,
            "migration": {
                "sourceTicker": "MIGRATE",
                "mintedAtPreview": 3,
                "status": "genesis-ready / manual-issue",
            },
        }
        genesis = self.operation(
            kind="migration-genesis",
            collectionId=collection_id,
            txid="8" * 64,
            walletAddress=deployer,
            status="accepted",
            nftIds=[],
            manifest=manifest,
        )
        server.db_index_kcc721_operation(genesis)
        artifact = server.build_kcc721_migration_artifact(3)
        server.save_kcc721_shuffle(collection_id, artifact)
        proof = server.build_kcc721_migration_issue(artifact, 2)
        issue = self.operation(
            id="9" * 32,
            kind="migration-issue",
            collectionId=collection_id,
            txid="a" * 64,
            status="accepted",
            tokenId=2,
            nftId="b" * 64,
            recipientAddress=recipient,
            nextUnissuedRoot=proof["nextUnissuedRoot"],
        )

        server.db_index_kcc721_operation(issue)

        custody, custody_total = server.db_list_kcc721_migration_custody(deployer, 0, 100)
        real, real_total = server.db_list_kcc721_wallet_nfts(recipient, 0, 100)
        self.assertEqual(custody_total, 2)
        self.assertEqual([row["token_id"] for row in custody], [1, 3])
        self.assertEqual(real_total, 1)
        self.assertEqual(real[0]["token_id"], 2)
        self.assertEqual(real[0]["owner_address"], recipient)

    def test_v2_commit_advances_position_and_reveal_indexes_random_token(self):
        collection_id = "8" * 64
        manifest = {
            "version": "0.2.0",
            "ticker": "BLIND",
            "deploymentPublicKey": "d" * 64,
            "maxSupply": 287,
            "metadataUri": "ipfs://blind",
            "metadataDigest": "e" * 64,
            "mintPriceSompi": 0,
            "mintDaaScore": 0,
            "shuffleRoot": "f" * 64,
        }
        genesis = self.operation(
            kind="collection-genesis", collectionId=collection_id, txid="9" * 64,
            status="accepted", nftIds=[], manifest=manifest,
        )
        server.db_index_kcc721_operation(genesis)
        self.assertEqual(server.db_get_kcc721_collection(collection_id)["next_token_id"], 1)

        commit = self.operation(
            id="1" * 32, kind="blind-mint-commit", collectionId=collection_id,
            txid="2" * 64, status="accepted", mintIndex=1, manifest=manifest,
        )
        server.db_index_kcc721_operation(commit)
        self.assertEqual(server.db_get_kcc721_collection(collection_id)["next_token_id"], 2)

        reveal = self.operation(
            id="3" * 32, kind="blind-mint-reveal", collectionId=collection_id,
            txid="4" * 64, status="accepted", nftId="5" * 64, tokenId=173,
            mintIndex=1, walletAddress="kaspa:winner", ownerPublicKey="6" * 64,
        )
        server.db_index_kcc721_operation(reveal)
        nft = server.db_get_kcc721_nft_by_token(collection_id, 173)
        self.assertEqual(nft["owner_address"], "kaspa:winner")
        self.assertEqual(nft["outpoint_index"], 0)

    def test_first_accepted_collection_owns_ticker_case_insensitively(self):
        first = self.operation(
            kind="collection-genesis",
            collectionId="7" * 64,
            txid="8" * 64,
            status="accepted",
            nftIds=[],
            manifest={
                "ticker": "UNIQUE",
                "deploymentPublicKey": "d" * 64,
                "maxSupply": 21,
                "metadataUri": VALID_IPFS,
                "metadataDigest": "e" * 64,
                "mintPriceSompi": 0,
                "mintDaaScore": 0,
            },
        )
        server.db_save_kcc721_operation(first)
        server.db_index_kcc721_operation(first)
        self.assertTrue(server.db_kcc721_ticker_taken("unique"))

        duplicate = self.operation(
            id="9" * 32,
            kind="collection-genesis",
            collectionId="a" * 64,
            txid="b" * 64,
            status="accepted",
            nftIds=[],
            manifest={**first["manifest"], "ticker": "unique"},
        )
        server.db_save_kcc721_operation(duplicate)
        server.db_index_kcc721_operation(duplicate)

        stored = server.db_get_kcc721_operation(duplicate["id"])
        self.assertEqual(stored["status"], "noncanonical")
        self.assertIn("already assigned", stored["registryError"])
        self.assertFalse(server.db_get_kcc721_collection(duplicate["collectionId"]))

    def test_nft_owner_tracks_latest_accepted_transfer(self):
        collection_id = "b" * 64
        genesis = self.operation(
            kind="collection-genesis",
            collectionId=collection_id,
            txid="c" * 64,
            status="accepted",
            nftIds=[],
            walletAddress="kaspa:deployer",
            manifest={
                "ticker": "HUBNFT",
                "deploymentPublicKey": "d" * 64,
                "maxSupply": 21,
                "metadataUri": "ipfs://example",
                "metadataDigest": "e" * 64,
                "mintPriceSompi": 0,
                "mintDaaScore": 0,
            },
        )
        server.db_index_kcc721_operation(genesis)
        mint = self.operation(
            id="1" * 32,
            collectionId=collection_id,
            txid="2" * 64,
            status="accepted",
            nftId="3" * 64,
            tokenId=7,
            walletAddress="kaspa:first-owner",
            ownerPublicKey="4" * 64,
        )
        server.db_index_kcc721_operation(mint)

        indexed = server.db_get_kcc721_nft_by_token(collection_id, 7)
        self.assertEqual(indexed["owner_address"], "kaspa:first-owner")
        self.assertEqual(indexed["outpoint_txid"], "2" * 64)
        held, total = server.db_list_kcc721_wallet_nfts("kaspa:first-owner", 0, 100)
        self.assertEqual(total, 1)
        self.assertEqual(held[0]["nft_id"], "3" * 64)

        transfer = self.operation(
            id="5" * 32,
            kind="nft-transfer",
            collectionId=collection_id,
            txid="6" * 64,
            status="accepted",
            nftId="3" * 64,
            recipientAddress="kaspa:next-owner",
            recipientPublicKey="7" * 64,
        )
        server.db_index_kcc721_operation(transfer)

        indexed = server.db_get_kcc721_nft_by_token(collection_id, 7)
        self.assertEqual(indexed["owner_address"], "kaspa:next-owner")
        self.assertEqual(indexed["outpoint_txid"], "6" * 64)
        old_wallet, old_total = server.db_list_kcc721_wallet_nfts("kaspa:first-owner", 0, 100)
        new_wallet, new_total = server.db_list_kcc721_wallet_nfts("kaspa:next-owner", 0, 100)
        self.assertEqual((old_wallet, old_total), ([], 0))
        self.assertEqual(new_total, 1)
        self.assertEqual(new_wallet[0]["nft_id"], "3" * 64)
        history = server.db_list_kcc721_nft_history("3" * 64)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["eventType"], "mint")
        self.assertEqual(history[0]["outpoint"], f"{'2' * 64}:1")
        self.assertIsNone(history[0]["previousOutpoint"])
        self.assertFalse(history[0]["isCurrent"])
        self.assertEqual(history[1]["eventType"], "transfer")
        self.assertEqual(history[1]["previousOutpoint"], f"{'2' * 64}:1")
        self.assertEqual(history[1]["outpoint"], f"{'6' * 64}:0")
        self.assertEqual(history[1]["fromAddress"], "kaspa:test")
        self.assertEqual(history[1]["ownerAddress"], "kaspa:next-owner")
        self.assertTrue(history[1]["isCurrent"])

    def test_accepted_atomic_batch_updates_all_owner_outpoints(self):
        collection_id = "b" * 64
        now = server.now_iso()
        with server.db_lock, server.db_connect() as conn:
            for index, nft_id in enumerate(("1" * 64, "2" * 64)):
                conn.execute(
                    """
                    INSERT INTO kcc721_nfts(
                        nft_id, collection_id, token_id, owner_address, owner_public_key,
                        outpoint_txid, outpoint_index, status, updated_at, data
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'live', ?, ?)
                    """,
                    (nft_id, collection_id, index + 1, "kaspa:old", "a" * 64,
                     str(index + 7) * 64, 0, now, "{}"),
                )
        operation = self.operation(
            kind="nft-batch-transfer",
            collectionId=collection_id,
            nftId="1" * 64,
            nftIds=["1" * 64, "2" * 64],
            txid="9" * 64,
            status="accepted",
            recipientAddress="kaspa:new",
            recipientPublicKey="c" * 64,
            items=[
                {"nftId": "1" * 64, "outputIndex": 0, "nftOutput": {"value": 50_000_000}},
                {"nftId": "2" * 64, "outputIndex": 1, "nftOutput": {"value": 50_000_000}},
            ],
        )

        server.db_index_kcc721_operation(operation)

        first = server.db_get_kcc721_nft("1" * 64)
        second = server.db_get_kcc721_nft("2" * 64)
        self.assertEqual((first["owner_address"], first["outpoint_index"]), ("kaspa:new", 0))
        self.assertEqual((second["owner_address"], second["outpoint_index"]), ("kaspa:new", 1))
        self.assertEqual(first["outpoint_txid"], "9" * 64)
        self.assertEqual(second["outpoint_txid"], "9" * 64)
        first_history = server.db_list_kcc721_nft_history("1" * 64)
        second_history = server.db_list_kcc721_nft_history("2" * 64)
        self.assertEqual(first_history[0]["outpoint"], f"{'9' * 64}:0")
        self.assertEqual(second_history[0]["outpoint"], f"{'9' * 64}:1")
        self.assertEqual(first_history[0]["eventType"], "atomic batch transfer")

    def test_history_backfill_rebuilds_saved_accepted_lineage_idempotently(self):
        mint = self.operation(
            id="1" * 32,
            collectionId="b" * 64,
            txid="2" * 64,
            status="accepted",
            nftId="3" * 64,
            tokenId=7,
            walletAddress="kaspa:first-owner",
        )
        transfer = self.operation(
            id="4" * 32,
            kind="nft-transfer",
            collectionId="b" * 64,
            txid="5" * 64,
            status="accepted",
            nftId="3" * 64,
            tokenId=7,
            walletAddress="kaspa:first-owner",
            recipientAddress="kaspa:second-owner",
        )
        server.db_save_kcc721_operation(mint)
        server.db_save_kcc721_operation(transfer)

        self.assertEqual(server.backfill_kcc721_nft_history(), 2)
        self.assertEqual(server.backfill_kcc721_nft_history(), 0)
        history = server.db_list_kcc721_nft_history("3" * 64)
        self.assertEqual([entry["eventType"] for entry in history], ["mint", "transfer"])
        self.assertEqual(history[1]["previousOutpoint"], f"{'2' * 64}:1")


if __name__ == "__main__":
    unittest.main()
