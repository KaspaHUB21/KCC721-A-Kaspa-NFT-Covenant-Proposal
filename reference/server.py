import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
RUNTIME_DIR = Path(os.environ.get("KASPA_DEVTOOLS_RUNTIME_DIR", str(BASE_DIR / "runtime")))
RUNTIME_DIR.mkdir(exist_ok=True)
VAULT_DIR = RUNTIME_DIR / "vaults"
VAULT_DIR.mkdir(exist_ok=True)
KRC20_SNAPSHOT_DIR = RUNTIME_DIR / "krc20-snapshots"
KRC20_SNAPSHOT_DIR.mkdir(exist_ok=True)
KCC721_SHUFFLE_DIR = RUNTIME_DIR / "kcc721-shuffles"
KCC721_SHUFFLE_DIR.mkdir(exist_ok=True)
KRC20_SNAPSHOT_SCRIPT = BASE_DIR / "scripts" / "krc20_snapshot.py"
KCC721_ENGINE = Path(
    os.environ.get(
        "KASPA_DEVTOOLS_KCC721_ENGINE",
        str(BASE_DIR / "protocol" / "kcc721" / "engine" / "target" / "release" / "kcc721-engine"),
    )
)
DB_PATH = RUNTIME_DIR / "devtools.sqlite3"
PAYMENT_SESSION_SECRET_PATH = RUNTIME_DIR / "payment_session_secret"
if PAYMENT_SESSION_SECRET_PATH.exists():
    PAYMENT_SESSION_SECRET = PAYMENT_SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()
else:
    PAYMENT_SESSION_SECRET = uuid.uuid4().hex + uuid.uuid4().hex
    PAYMENT_SESSION_SECRET_PATH.write_text(PAYMENT_SESSION_SECRET, encoding="utf-8")

PORT = int(os.environ.get("KASPA_DEVTOOLS_PORT", "8112"))
KASPLEX_API = "https://api.kasplex.org/v1"
KRC721_API = "https://krc721-indexer.kaspa.com/api/v1/krc721/mainnet"
KRC721_T10_API = os.environ.get(
    "KASPA_DEVTOOLS_KRC721_T10_API",
    "http://127.0.0.1:8810/api/v1/krc721/testnet-10",
)
KASPA_API = "https://api.kaspa.org"
KASPA_T10_API = "https://api-tn10.kaspa.org"
IPFS_GATEWAYS = tuple(
    item.strip().rstrip("/")
    for item in os.environ.get(
        "KASPA_DEVTOOLS_IPFS_GATEWAYS",
        "https://ipfs.io/ipfs,https://dweb.link/ipfs",
    ).split(",")
    if item.strip()
)
PAYMENT_ADDRESS = os.environ.get(
    "KASPA_DEVTOOLS_PAYMENT_ADDRESS",
    "kaspa:qraed0llukpgfvnnhsctmrsrm4m9vkeqyvg6ek82vtq7gmh99c8qcz9atrgz6",
)
SNAPSHOT_PRICE_KAS = 21
ADVANCED_KRC721_PRICE_KAS = 10
XRAY_PRICE_KAS = 5
XRAY_KRC20_PRICE_KAS = 5
BATCH_TRANSFER_PRICE_KAS = 0
TOCCATA_FEE_RATE_SOMPI_PER_G = 100
KRC20_DECIMALS = 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("kaspa-devtools")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
vault_lock = threading.Lock()
db_lock = threading.Lock()
submitted_jobs: set[str] = set()
submitted_jobs_lock = threading.Lock()
rate_limit_lock = threading.Lock()
rate_limits: dict[str, list[float]] = {}
krc20_snapshot_lock = threading.Lock()
kcc721_prepare_lock = threading.Lock()
kcc721_deploy_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=int(os.environ.get("KASPA_DEVTOOLS_WORKERS", "3")))
JOB_CREATE_LIMIT_PER_HOUR = int(os.environ.get("KASPA_DEVTOOLS_JOB_CREATE_LIMIT_PER_HOUR", "20"))
FREE_JOB_LIMIT_PER_HOUR = int(os.environ.get("KASPA_DEVTOOLS_FREE_JOB_LIMIT_PER_HOUR", "5"))


class BadRequest(ValueError):
    pass


class PaymentRequired(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                wallet_address TEXT,
                status TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL,
                params TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kcc721_operations (
                id TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                kind TEXT NOT NULL,
                collection_id TEXT,
                nft_id TEXT,
                txid TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kcc721_wallet_updated ON kcc721_operations(wallet_address, updated_at DESC)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_kcc721_txid ON kcc721_operations(txid) WHERE txid IS NOT NULL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kcc721_collections (
                collection_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                deployer_address TEXT NOT NULL,
                deployer_public_key TEXT NOT NULL,
                max_supply INTEGER NOT NULL,
                metadata_uri TEXT NOT NULL,
                metadata_digest TEXT NOT NULL,
                mint_price_sompi INTEGER NOT NULL,
                mint_daa_score INTEGER NOT NULL,
                next_token_id INTEGER NOT NULL,
                controller_txid TEXT NOT NULL,
                controller_output_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_kcc721_ticker_unique "
            "ON kcc721_collections(ticker COLLATE NOCASE)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kcc721_nfts (
                nft_id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL,
                token_id INTEGER NOT NULL,
                owner_address TEXT NOT NULL,
                owner_public_key TEXT NOT NULL,
                outpoint_txid TEXT NOT NULL,
                outpoint_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                data TEXT NOT NULL,
                UNIQUE(collection_id, token_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kcc721_nfts_owner ON kcc721_nfts(owner_address, collection_id, token_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kcc721_nft_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nft_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                token_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                txid TEXT NOT NULL,
                output_index INTEGER NOT NULL,
                previous_txid TEXT,
                previous_output_index INTEGER,
                from_address TEXT,
                owner_address TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                data TEXT NOT NULL,
                UNIQUE(nft_id, txid, output_index)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kcc721_nft_history_lineage "
            "ON kcc721_nft_history(nft_id, id)"
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "params" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN params TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_wallet_updated ON jobs(wallet_address, updated_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                txid TEXT PRIMARY KEY,
                wallet_address TEXT,
                job_id TEXT,
                amount_sompi INTEGER NOT NULL,
                expected_sompi INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )


def migrate_vault_indexes_to_db() -> None:
    for index_path in VAULT_DIR.glob("*/index.json"):
        try:
            items = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            # Vault indexes are a compatibility mirror. Once a job exists in
            # SQLite, the database owns its current status and resume data.
            if db_get_job(item["id"]):
                continue
            job = dict(item)
            job.setdefault("createdAt", now_iso())
            job.setdefault("updatedAt", job.get("createdAt"))
            job.setdefault("type", job.get("type") or "unknown")
            job.setdefault("status", job.get("status") or "complete")
            db_save_job(job)


def migrate_job_payments_to_db() -> None:
    with db_lock, db_connect() as conn:
        rows = conn.execute("SELECT id, wallet_address, data FROM jobs").fetchall()
    migrated = 0
    for row in rows:
        try:
            job = json.loads(row["data"])
        except ValueError:
            continue
        txid = extract_txid(job.get("paymentTxid"))
        if not txid or db_payment_used(txid):
            continue
        db_mark_payment_used(txid, job.get("walletAddress") or row["wallet_address"] or "", row["id"], {"migratedFromJob": True})
        migrated += 1
    if migrated:
        logger.info("Marked %s existing job payments as used.", migrated)


def migrate_orphan_jobs_to_single_wallet() -> None:
    wallets = db_distinct_wallets()
    if len(wallets) != 1:
        return
    wallet_address = wallets[0]
    migrated = 0
    for job in db_orphan_jobs():
        job_id = job.get("id")
        if not job_id:
            continue
        output_path = RUNTIME_DIR / f"{job_id}.txt"
        result_path = RUNTIME_DIR / f"{job_id}.json"
        if not output_path.exists() and not result_path.exists():
            continue
        job["walletAddress"] = wallet_address
        db_save_job(job)
        vault = vault_dir(wallet_address)
        if output_path.exists():
            shutil.copyfile(output_path, vault / f"{job_id}.txt")
        if result_path.exists():
            shutil.copyfile(result_path, vault / f"{job_id}.json")
        migrated += 1
    if migrated:
        logger.info("Assigned %s legacy orphan jobs to wallet vault %s.", migrated, short_address(wallet_address))


def db_save_job(job: dict) -> None:
    if not job.get("id"):
        return
    params_json = json.dumps(job.get("params"), sort_keys=True) if isinstance(job.get("params"), dict) else None
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs(id, wallet_address, status, type, created_at, updated_at, data, params)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                wallet_address = excluded.wallet_address,
                status = excluded.status,
                type = excluded.type,
                updated_at = excluded.updated_at,
                data = excluded.data,
                params = COALESCE(excluded.params, jobs.params)
            """,
            (
                job["id"],
                job.get("walletAddress") or "",
                job.get("status") or "",
                job.get("type") or "",
                job.get("createdAt") or now_iso(),
                job.get("updatedAt") or now_iso(),
                json.dumps(public_job(job), sort_keys=True),
                params_json,
            ),
        )


def db_get_job(job_id: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except ValueError:
        return {}


def db_get_job_params(job_id: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT params FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or not row["params"]:
        return {}
    try:
        params = json.loads(row["params"])
    except ValueError:
        return {}
    return params if isinstance(params, dict) else {}


def db_list_wallet_jobs(wallet_address: str) -> list[dict]:
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT data FROM jobs WHERE wallet_address = ? ORDER BY updated_at DESC LIMIT 500",
            (wallet_address,),
        ).fetchall()
    items = []
    for row in rows:
        try:
            items.append(json.loads(row["data"]))
        except ValueError:
            continue
    return items


def db_distinct_wallets() -> list[str]:
    with db_lock, db_connect() as conn:
        rows = conn.execute("SELECT DISTINCT wallet_address FROM jobs WHERE wallet_address != ''").fetchall()
    return [row["wallet_address"] for row in rows if row["wallet_address"]]


def db_orphan_jobs() -> list[dict]:
    with db_lock, db_connect() as conn:
        rows = conn.execute("SELECT data FROM jobs WHERE wallet_address = '' ORDER BY created_at ASC").fetchall()
    items = []
    for row in rows:
        try:
            items.append(json.loads(row["data"]))
        except ValueError:
            continue
    return items


def db_payment_used(txid: str) -> bool:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT txid FROM payments WHERE txid = ?", (txid,)).fetchone()
    return bool(row)


def db_payment_job(txid: str) -> str:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT job_id FROM payments WHERE txid = ?", (txid,)).fetchone()
    return str(row["job_id"] or "") if row else ""


def db_save_payment(txid: str, wallet_address: str, job_id: str, amount_sompi: int, expected_sompi: int, tx_data: dict) -> None:
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO payments(txid, wallet_address, job_id, amount_sompi, expected_sompi, created_at, data)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (txid, wallet_address or "", job_id or "", amount_sompi, expected_sompi, now_iso(), json.dumps(tx_data, sort_keys=True)),
        )


def db_mark_payment_used(txid: str, wallet_address: str, job_id: str, tx_data: dict | None = None) -> None:
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO payments(txid, wallet_address, job_id, amount_sompi, expected_sompi, created_at, data)
            VALUES(?, ?, ?, 0, 0, ?, ?)
            """,
            (txid, wallet_address or "", job_id or "", now_iso(), json.dumps(tx_data or {}, sort_keys=True)),
        )


def db_save_kcc721_operation(operation: dict) -> None:
    with db_lock, db_connect() as conn:
        conn.execute(
            """
            INSERT INTO kcc721_operations(
                id, wallet_address, kind, collection_id, nft_id, txid,
                status, created_at, updated_at, data
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                txid = COALESCE(excluded.txid, kcc721_operations.txid),
                status = excluded.status,
                updated_at = excluded.updated_at,
                data = excluded.data
            """,
            (
                operation["id"],
                operation.get("walletAddress") or "",
                operation.get("kind") or "",
                operation.get("collectionId"),
                operation.get("nftId"),
                operation.get("txid"),
                operation.get("status") or "prepared",
                operation.get("createdAt") or now_iso(),
                operation.get("updatedAt") or now_iso(),
                json.dumps(operation, sort_keys=True),
            ),
        )


def db_get_kcc721_operation(operation_id: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT data FROM kcc721_operations WHERE id = ?", (operation_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except ValueError:
        return {}


def db_get_kcc721_operation_by_txid(txid: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT data FROM kcc721_operations WHERE txid = ?", (txid,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except ValueError:
        return {}


def db_pending_kcc721_operations() -> list[dict]:
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT data FROM kcc721_operations WHERE status = 'submitted' ORDER BY updated_at ASC LIMIT 100"
        ).fetchall()
    operations = []
    for row in rows:
        try:
            operations.append(json.loads(row["data"]))
        except ValueError:
            continue
    return operations


def db_latest_kcc721_collection_operation(collection_id: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute(
            """
            SELECT data FROM kcc721_operations
            WHERE collection_id = ? AND status = 'accepted'
              AND kind IN ('collection-genesis', 'migration-genesis', 'public-mint', 'blind-mint-commit', 'migration-issue')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (collection_id,),
        ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except ValueError:
        return {}


def db_get_kcc721_collection(collection_id: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM kcc721_collections WHERE collection_id = ?", (collection_id,)).fetchone()
    return dict(row) if row else {}


def db_kcc721_ticker_taken(ticker: str, ignore_operation_id: str = "") -> bool:
    normalized = clean_kcc721_tick(ticker)
    cutoff = datetime.fromtimestamp(time.time() - 900, timezone.utc).isoformat()
    with db_lock, db_connect() as conn:
        existing = conn.execute(
            "SELECT collection_id FROM kcc721_collections WHERE ticker = ? COLLATE NOCASE LIMIT 1",
            (normalized,),
        ).fetchone()
        rows = conn.execute(
            """
            SELECT id, status, created_at, data FROM kcc721_operations
            WHERE kind IN ('collection-genesis', 'migration-genesis')
              AND status IN ('prepared', 'submitted', 'accepted')
            """
        ).fetchall()
    if existing:
        return True
    for row in rows:
        if row["id"] == ignore_operation_id:
            continue
        if row["status"] == "prepared" and row["created_at"] < cutoff:
            continue
        try:
            manifest = (json.loads(row["data"]) or {}).get("manifest") or {}
        except (TypeError, ValueError):
            continue
        if str(manifest.get("ticker") or "").upper() == normalized:
            return True
    return False


def db_list_kcc721_collections(search: str = "") -> list[dict]:
    params: list[str] = []
    where = "WHERE c.status = 'live'"
    if search:
        where += " AND (UPPER(c.ticker) LIKE ? OR c.collection_id LIKE ? OR c.deployer_address LIKE ?)"
        needle = f"%{search}%"
        params.extend((needle.upper(), needle.lower(), needle.lower()))
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT c.*, COUNT(n.nft_id) AS indexed_nfts
            FROM kcc721_collections c
            LEFT JOIN kcc721_nfts n ON n.collection_id = c.collection_id AND n.status = 'live'
            {where}
            GROUP BY c.collection_id
            ORDER BY c.created_at DESC
            LIMIT 250
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def kcc721_collection_migration(collection: dict) -> dict:
    try:
        operation = json.loads(collection.get("data") or "{}")
    except ValueError:
        return {}
    migration = (operation.get("manifest") or {}).get("migration")
    return migration if isinstance(migration, dict) else {}


def kcc721_collection_manifest(collection: dict) -> dict:
    try:
        operation = json.loads(collection.get("data") or "{}")
    except ValueError:
        return {}
    manifest = operation.get("manifest") or {}
    return manifest if isinstance(manifest, dict) else {}


def build_kcc721_shuffle(supply: int) -> dict:
    token_ids = list(range(1, supply + 1))
    secrets.SystemRandom().shuffle(token_ids)
    entries = []
    level = []
    for mint_index, token_id in enumerate(token_ids, 1):
        salt = secrets.token_bytes(32)
        leaf = hashlib.sha256(
            mint_index.to_bytes(8, "little") + token_id.to_bytes(8, "little") + salt
        ).digest()
        entries.append({"mintIndex": mint_index, "tokenId": token_id, "salt": salt.hex()})
        level.append(leaf)
    proofs = [[] for _ in entries]
    directions = [[] for _ in entries]
    indexes = list(range(len(entries)))
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        for entry_index, node_index in enumerate(indexes):
            sibling_index = node_index + 1 if node_index % 2 == 0 else node_index - 1
            proofs[entry_index].append(level[sibling_index].hex())
            directions[entry_index].append(0 if node_index % 2 == 0 else 1)
            indexes[entry_index] = node_index // 2
        level = [hashlib.sha256(level[index] + level[index + 1]).digest() for index in range(0, len(level), 2)]
    for entry, siblings, path in zip(entries, proofs, directions):
        entry["siblings"] = siblings
        entry["directions"] = path
    return {"version": "0.2.0", "supply": supply, "shuffleRoot": level[0].hex(), "entries": entries}


def _kcc721_migration_leaf(token_id: int, issued: bool) -> bytes:
    return hashlib.sha256(token_id.to_bytes(8, "little") + bytes([0 if issued else 1])).digest()


def _kcc721_migration_tree(supply: int, issued_ids: set[int]) -> list[list[bytes]]:
    width = 1
    while width < supply:
        width *= 2
    padding_leaf = hashlib.sha256((0).to_bytes(8, "little") + b"\x00").digest()
    leaves = [
        _kcc721_migration_leaf(token_id, token_id in issued_ids)
        for token_id in range(1, supply + 1)
    ]
    leaves.extend([padding_leaf] * (width - supply))
    levels = [leaves]
    while len(levels[-1]) > 1:
        current = levels[-1]
        levels.append(
            [hashlib.sha256(current[index] + current[index + 1]).digest() for index in range(0, len(current), 2)]
        )
    return levels


def build_kcc721_migration_artifact(supply: int) -> dict:
    levels = _kcc721_migration_tree(supply, set())
    return {
        "version": "0.2.0",
        "mode": "migration-merkle-issue",
        "supply": supply,
        "unissuedRoot": levels[-1][0].hex(),
        "issuedTokenIds": [],
    }


def build_kcc721_migration_issue(artifact: dict, token_id: int) -> dict:
    supply = int(artifact.get("supply") or 0)
    issued_ids = {int(value) for value in artifact.get("issuedTokenIds") or []}
    if token_id < 1 or token_id > supply:
        raise BadRequest("Migration token ID is outside 1..maxSupply.")
    if token_id in issued_ids:
        raise BadRequest("This migration NFT has already been issued.")
    levels = _kcc721_migration_tree(supply, issued_ids)
    index = token_id - 1
    siblings = []
    directions = []
    for level in levels[:-1]:
        sibling_index = index + 1 if index % 2 == 0 else index - 1
        siblings.append(level[sibling_index].hex())
        directions.append(0 if index % 2 == 0 else 1)
        index //= 2
    current_root = levels[-1][0].hex()
    issued_ids.add(token_id)
    next_root = _kcc721_migration_tree(supply, issued_ids)[-1][0].hex()
    return {
        "tokenId": token_id,
        "currentUnissuedRoot": current_root,
        "nextUnissuedRoot": next_root,
        "siblings": siblings,
        "directions": directions,
        "remaining": supply - len(issued_ids) + 1,
        "nextArtifact": {
            **artifact,
            "unissuedRoot": next_root,
            "issuedTokenIds": sorted(issued_ids),
        },
    }


def save_kcc721_shuffle(collection_id: str, artifact: dict) -> None:
    path = KCC721_SHUFFLE_DIR / f"{collection_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def load_kcc721_shuffle(collection_id: str) -> dict:
    path = KCC721_SHUFFLE_DIR / f"{collection_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def db_get_kcc721_nft(nft_id: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM kcc721_nfts WHERE nft_id = ?", (nft_id,)).fetchone()
    return dict(row) if row else {}


def db_get_kcc721_nft_by_token(collection_id: str, token_id: int) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM kcc721_nfts
            WHERE collection_id = ? AND token_id = ? AND status = 'live'
            """,
            (collection_id, token_id),
        ).fetchone()
    return dict(row) if row else {}


def kcc721_operation_nft_events(operation: dict) -> list[dict]:
    kind = operation.get("kind")
    txid = operation.get("txid")
    collection_id = operation.get("collectionId")
    accepted_at = operation.get("updatedAt") or operation.get("createdAt") or now_iso()
    if not txid or not collection_id or operation.get("status") != "accepted":
        return []
    common = {"collectionId": collection_id, "txid": txid, "acceptedAt": accepted_at}
    if kind in ("blind-mint-reveal", "public-mint", "migration-issue"):
        nft_id = operation.get("nftId")
        if not nft_id:
            return []
        owner = operation.get("recipientAddress") if kind == "migration-issue" else operation.get("walletAddress")
        return [{
            **common,
            "nftId": nft_id,
            "tokenId": int(operation.get("tokenId") or 0),
            "eventType": "migration issue" if kind == "migration-issue" else "mint",
            "outputIndex": 1 if kind in ("public-mint", "migration-issue") else 0,
            "fromAddress": "",
            "ownerAddress": owner or "",
            "operationId": operation.get("id"),
        }]
    if kind in ("collection-genesis", "migration-genesis"):
        events = []
        token_base = int((operation.get("manifest") or {}).get("tokenIdBase") or 0)
        for offset, nft_id in enumerate(operation.get("nftIds") or []):
            events.append({
                **common,
                "nftId": nft_id,
                "tokenId": token_base + offset,
                "eventType": "genesis allocation",
                "outputIndex": offset + 1,
                "fromAddress": "",
                "ownerAddress": operation.get("walletAddress") or "",
                "operationId": operation.get("id"),
            })
        return events
    if kind == "nft-transfer" and operation.get("nftId"):
        return [{
            **common,
            "nftId": operation["nftId"],
            "tokenId": int(operation.get("tokenId") or 0),
            "eventType": "transfer",
            "outputIndex": 0,
            "fromAddress": operation.get("walletAddress") or "",
            "ownerAddress": operation.get("recipientAddress") or "",
            "operationId": operation.get("id"),
        }]
    if kind == "nft-batch-transfer":
        return [{
            **common,
            "collectionId": item.get("collectionId") or collection_id,
            "nftId": item.get("nftId") or "",
            "tokenId": int(item.get("tokenId") or 0),
            "eventType": "atomic batch transfer",
            "outputIndex": int(item.get("outputIndex") or 0),
            "fromAddress": operation.get("walletAddress") or "",
            "ownerAddress": operation.get("recipientAddress") or "",
            "operationId": operation.get("id"),
        } for item in operation.get("items") or [] if item.get("nftId")]
    return []


def db_record_kcc721_operation_history(operation: dict) -> int:
    events = kcc721_operation_nft_events(operation)
    if not events:
        return 0
    inserted = 0
    with db_lock, db_connect() as conn:
        for event in events:
            previous = conn.execute(
                """
                SELECT txid, output_index FROM kcc721_nft_history
                WHERE nft_id = ? ORDER BY id DESC LIMIT 1
                """,
                (event["nftId"],),
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO kcc721_nft_history(
                    nft_id, collection_id, token_id, event_type, txid, output_index,
                    previous_txid, previous_output_index, from_address, owner_address,
                    accepted_at, data
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["nftId"], event["collectionId"], event["tokenId"], event["eventType"],
                    event["txid"], event["outputIndex"], previous["txid"] if previous else None,
                    previous["output_index"] if previous else None, event["fromAddress"],
                    event["ownerAddress"], event["acceptedAt"], json.dumps(event, sort_keys=True),
                ),
            )
            inserted += int(cursor.rowcount > 0)
    return inserted


def backfill_kcc721_nft_history() -> int:
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT data FROM kcc721_operations
            WHERE status = 'accepted'
            ORDER BY updated_at ASC, created_at ASC, id ASC
            """
        ).fetchall()
    inserted = 0
    for row in rows:
        try:
            inserted += db_record_kcc721_operation_history(json.loads(row["data"]))
        except ValueError:
            continue
    return inserted


def db_list_kcc721_nft_history(nft_id: str) -> list[dict]:
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM kcc721_nft_history
            WHERE nft_id = ? ORDER BY id ASC
            """,
            (nft_id,),
        ).fetchall()
    history = []
    for index, row in enumerate(rows):
        item = dict(row)
        history.append({
            "step": index + 1,
            "eventType": item["event_type"],
            "transactionId": item["txid"],
            "outputIndex": item["output_index"],
            "outpoint": f"{item['txid']}:{item['output_index']}",
            "previousOutpoint": (
                f"{item['previous_txid']}:{item['previous_output_index']}"
                if item.get("previous_txid") is not None else None
            ),
            "fromAddress": item.get("from_address") or None,
            "ownerAddress": item["owner_address"],
            "acceptedAt": item["accepted_at"],
            "isCurrent": index == len(rows) - 1,
        })
    return history


def db_count_kcc721_collection_nfts(collection_id: str) -> int:
    with db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM kcc721_nfts WHERE collection_id = ? AND status = 'live'",
            (collection_id,),
        ).fetchone()
    return int(row[0])


def db_list_kcc721_wallet_nfts(owner_address: str, offset: int, limit: int) -> tuple[list[dict], int]:
    with db_lock, db_connect() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM kcc721_nfts n
            JOIN kcc721_collections c ON c.collection_id = n.collection_id
            WHERE n.owner_address = ? AND n.status = 'live' AND c.status = 'live'
            """,
            (owner_address,),
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT n.*, c.ticker, c.metadata_uri
            FROM kcc721_nfts n
            JOIN kcc721_collections c ON c.collection_id = n.collection_id
            WHERE n.owner_address = ? AND n.status = 'live' AND c.status = 'live'
            ORDER BY n.updated_at DESC, c.ticker ASC, n.token_id ASC
            LIMIT ? OFFSET ?
            """,
            (owner_address, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows], int(total)


def db_list_kcc721_migration_custody(owner_address: str, offset: int, limit: int) -> tuple[list[dict], int]:
    with db_lock, db_connect() as conn:
        collections = conn.execute(
            """
            SELECT * FROM kcc721_collections
            WHERE deployer_address = ? AND status = 'live'
            ORDER BY created_at DESC
            """,
            (owner_address,),
        ).fetchall()
    virtual = []
    for collection_row in collections:
        collection = dict(collection_row)
        manifest = kcc721_collection_manifest(collection)
        if manifest.get("mintMode") != "migration-merkle-issue":
            continue
        artifact = load_kcc721_shuffle(collection["collection_id"])
        issued_ids = {int(value) for value in artifact.get("issuedTokenIds") or []}
        for token_id in range(1, int(collection["max_supply"]) + 1):
            if token_id in issued_ids:
                continue
            virtual.append(
                {
                    "nft_id": "",
                    "collection_id": collection["collection_id"],
                    "ticker": collection["ticker"],
                    "token_id": token_id,
                    "owner_address": owner_address,
                    "metadata_uri": collection["metadata_uri"],
                    "updated_at": collection["updated_at"],
                    "custody_state": "migration custody / not issued",
                }
            )
    return virtual[offset : offset + limit], len(virtual)


def kcc721_wallet_nft_item(row: dict) -> dict:
    token_id = int(row["token_id"])
    metadata_uri = f"{str(row['metadata_uri']).rstrip('/')}/{token_id}.json"
    metadata = {}
    try:
        metadata = fetch_ipfs_json(metadata_uri, timeout=8)
    except BadRequest:
        pass
    return {
        "nftId": row["nft_id"],
        "collectionId": row["collection_id"],
        "ticker": row["ticker"],
        "tokenId": token_id,
        "ownerAddress": row["owner_address"],
        "name": str(metadata.get("name") or f"{row['ticker']} #{token_id}"),
        "imageUrl": ipfs_gateway_url(metadata.get("image")),
        "metadataUri": metadata_uri,
        "metadataAvailable": bool(metadata),
        "detailUrl": f"/kcc721/nft?id={row['collection_id']}&tokenId={token_id}",
        "updatedAt": row["updated_at"],
        "custodyState": row.get("custody_state") or "live",
    }


def db_has_active_kcc721_operation(collection_id: str, kind: str) -> bool:
    return bool(db_get_active_kcc721_operation(collection_id, kind))


def db_get_active_kcc721_operation(collection_id: str, kind: str, prepared_ttl_seconds: int = 900) -> dict:
    cutoff = datetime.fromtimestamp(time.time() - prepared_ttl_seconds, timezone.utc).isoformat()
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT data FROM kcc721_operations
            WHERE collection_id = ? AND kind = ?
              AND (status = 'submitted' OR (status = 'prepared' AND created_at >= ?))
            ORDER BY created_at DESC
            """,
            (collection_id, kind, cutoff),
        ).fetchall()
    for row in rows:
        try:
            return json.loads(row["data"])
        except ValueError:
            continue
    return {}


def db_supersede_stale_kcc721_operations(collection_id: str, kind: str, prepared_ttl_seconds: int) -> int:
    cutoff = datetime.fromtimestamp(time.time() - prepared_ttl_seconds, timezone.utc).isoformat()
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT data FROM kcc721_operations
            WHERE collection_id = ? AND kind = ? AND status = 'prepared' AND created_at < ?
            """,
            (collection_id, kind, cutoff),
        ).fetchall()
    updated = 0
    for row in rows:
        try:
            operation = json.loads(row["data"])
        except ValueError:
            continue
        operation["status"] = "superseded"
        operation["updatedAt"] = now_iso()
        db_save_kcc721_operation(operation)
        updated += 1
    return updated


def db_expire_stale_kcc721_mint_queue(collection_id: str, ttl_seconds: int = 45) -> int:
    cutoff = datetime.fromtimestamp(time.time() - ttl_seconds, timezone.utc).isoformat()
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT data FROM kcc721_operations
            WHERE collection_id = ? AND kind = 'mint-queue'
              AND status = 'queued' AND updated_at < ?
            """,
            (collection_id, cutoff),
        ).fetchall()
    expired = 0
    for row in rows:
        try:
            operation = json.loads(row["data"])
        except ValueError:
            continue
        operation["status"] = "expired"
        operation["updatedAt"] = now_iso()
        db_save_kcc721_operation(operation)
        expired += 1
    return expired


def db_get_kcc721_mint_queue_entry(operation_id: str) -> dict:
    operation = db_get_kcc721_operation(operation_id)
    if operation.get("kind") != "mint-queue":
        return {}
    return operation


def db_find_kcc721_mint_queue_entry(collection_id: str, wallet_address: str) -> dict:
    with db_lock, db_connect() as conn:
        row = conn.execute(
            """
            SELECT data FROM kcc721_operations
            WHERE collection_id = ? AND wallet_address = ?
              AND kind = 'mint-queue' AND status = 'queued'
            ORDER BY created_at ASC LIMIT 1
            """,
            (collection_id, wallet_address),
        ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["data"])
    except ValueError:
        return {}


def db_kcc721_mint_queue_position(operation: dict) -> int:
    with db_lock, db_connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS ahead FROM kcc721_operations
            WHERE collection_id = ? AND kind = 'mint-queue' AND status = 'queued'
              AND (created_at < ? OR (created_at = ? AND id < ?))
            """,
            (
                operation["collectionId"],
                operation["createdAt"],
                operation["createdAt"],
                operation["id"],
            ),
        ).fetchone()
    return int(row["ahead"] or 0) + 1


def db_touch_kcc721_mint_queue(operation: dict) -> dict:
    operation["updatedAt"] = now_iso()
    db_save_kcc721_operation(operation)
    return operation


def kcc721_mint_queue_response(operation: dict) -> dict:
    position = db_kcc721_mint_queue_position(operation)
    active = db_get_active_kcc721_operation(operation["collectionId"], "blind-mint-commit", 120)
    return {
        "mode": "mint-queued",
        "operationId": operation["id"],
        "collectionId": operation["collectionId"],
        "ticker": operation.get("ticker") or "KCC721",
        "queuePosition": position,
        "ready": position == 1 and not active,
        "status": "ready to prepare" if position == 1 and not active else f"mint queue position {position}",
    }


def db_has_active_kcc721_nft_operation(nft_id: str) -> bool:
    cutoff = datetime.fromtimestamp(time.time() - 900, timezone.utc).isoformat()
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT nft_id, data FROM kcc721_operations
            WHERE kind IN ('nft-transfer', 'nft-batch-transfer')
              AND (status = 'submitted' OR (status = 'prepared' AND created_at >= ?))
            """,
            (cutoff,),
        ).fetchall()
    for row in rows:
        if row["nft_id"] == nft_id:
            return True
        try:
            operation = json.loads(row["data"])
        except ValueError:
            continue
        if nft_id in (operation.get("nftIds") or []):
            return True
    return False


def db_cancel_matching_prepared_kcc721_batches(wallet_address: str, nft_ids: set[str]) -> int:
    with db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT data FROM kcc721_operations
            WHERE wallet_address = ? AND kind = 'nft-batch-transfer' AND status = 'prepared'
            """,
            (wallet_address,),
        ).fetchall()
    cancelled = 0
    for row in rows:
        try:
            operation = json.loads(row["data"])
        except ValueError:
            continue
        if not nft_ids.intersection(operation.get("nftIds") or []):
            continue
        operation["status"] = "cancelled"
        operation["updatedAt"] = now_iso()
        db_save_kcc721_operation(operation)
        cancelled += 1
    return cancelled


def _db_index_kcc721_operation_state(operation: dict) -> None:
    manifest = operation.get("manifest") or {}
    collection_id = operation.get("collectionId")
    txid = operation.get("txid")
    kind = operation.get("kind")
    if not collection_id or not txid:
        return
    now = now_iso()
    if kind == "blind-mint-commit":
        mint_index = int(operation.get("mintIndex") or 0)
        if mint_index < 1:
            return
        with db_lock, db_connect() as conn:
            conn.execute(
                """
                UPDATE kcc721_collections
                SET next_token_id = ?, controller_txid = ?, updated_at = ?, data = ?
                WHERE collection_id = ?
                """,
                (mint_index + 1, txid, now, json.dumps(operation, sort_keys=True), collection_id),
            )
        return
    if kind == "blind-mint-reveal":
        nft_id = operation.get("nftId")
        token_id = int(operation.get("tokenId") or 0)
        if not nft_id or token_id < 1:
            return
        with db_lock, db_connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO kcc721_nfts(
                    nft_id, collection_id, token_id, owner_address,
                    owner_public_key, outpoint_txid, outpoint_index,
                    status, updated_at, data
                ) VALUES(?, ?, ?, ?, ?, ?, 0, 'live', ?, ?)
                """,
                (
                    nft_id,
                    collection_id,
                    token_id,
                    operation.get("walletAddress") or "",
                    operation.get("ownerPublicKey") or "",
                    txid,
                    now,
                    json.dumps(
                        {
                            "revealOperationId": operation.get("id"),
                            "mintIndex": operation.get("mintIndex"),
                            "ticketId": operation.get("ticketId"),
                            "output": operation.get("nftOutput") or {},
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return
    if kind == "public-mint":
        nft_id = operation.get("nftId")
        token_id = int(operation.get("tokenId") or 0)
        if not nft_id:
            return
        with db_lock, db_connect() as conn:
            conn.execute(
                """
                UPDATE kcc721_collections
                SET next_token_id = ?, controller_txid = ?, updated_at = ?, data = ?
                WHERE collection_id = ?
                """,
                (token_id + 1, txid, now, json.dumps(operation, sort_keys=True), collection_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO kcc721_nfts(
                    nft_id, collection_id, token_id, owner_address,
                    owner_public_key, outpoint_txid, outpoint_index,
                    status, updated_at, data
                ) VALUES(?, ?, ?, ?, ?, ?, 1, 'live', ?, ?)
                """,
                (
                    nft_id,
                    collection_id,
                    token_id,
                    operation.get("walletAddress") or "",
                    operation.get("ownerPublicKey") or "",
                    txid,
                    now,
                    json.dumps(
                        {
                            "mintOperationId": operation.get("id"),
                            "output": operation.get("nftOutput") or {},
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return
    if kind == "migration-issue":
        nft_id = operation.get("nftId")
        token_id = int(operation.get("tokenId") or 0)
        if not nft_id or token_id < 1:
            return
        artifact = load_kcc721_shuffle(collection_id)
        issued_ids = {int(value) for value in artifact.get("issuedTokenIds") or []}
        issued_ids.add(token_id)
        artifact["issuedTokenIds"] = sorted(issued_ids)
        artifact["unissuedRoot"] = operation.get("nextUnissuedRoot") or artifact.get("unissuedRoot")
        save_kcc721_shuffle(collection_id, artifact)
        with db_lock, db_connect() as conn:
            conn.execute(
                """
                UPDATE kcc721_collections
                SET controller_txid = ?, updated_at = ?
                WHERE collection_id = ?
                """,
                (txid, now, collection_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO kcc721_nfts(
                    nft_id, collection_id, token_id, owner_address,
                    owner_public_key, outpoint_txid, outpoint_index,
                    status, updated_at, data
                ) VALUES(?, ?, ?, ?, '', ?, 1, 'live', ?, ?)
                """,
                (
                    nft_id,
                    collection_id,
                    token_id,
                    operation.get("recipientAddress") or "",
                    txid,
                    now,
                    json.dumps(
                        {
                            "migrationIssueOperationId": operation.get("id"),
                            "output": operation.get("nftOutput") or {},
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return
    if kind == "nft-transfer":
        nft_id = operation.get("nftId")
        if not nft_id:
            return
        with db_lock, db_connect() as conn:
            conn.execute(
                """
                UPDATE kcc721_nfts
                SET owner_address = ?, owner_public_key = ?, outpoint_txid = ?,
                    outpoint_index = 0, updated_at = ?, data = ?
                WHERE nft_id = ?
                """,
                (
                    operation.get("recipientAddress") or "",
                    operation.get("recipientPublicKey") or "",
                    txid,
                    now,
                    json.dumps(
                        {
                            "transferOperationId": operation.get("id"),
                            "output": operation.get("nftOutput") or {},
                        },
                        sort_keys=True,
                    ),
                    nft_id,
                ),
            )
        return
    if kind == "nft-batch-transfer":
        items = operation.get("items") or []
        if not items:
            return
        with db_lock, db_connect() as conn:
            for item in items:
                nft_id = item.get("nftId")
                output_index = int(item.get("outputIndex") or 0)
                if not nft_id:
                    continue
                conn.execute(
                    """
                    UPDATE kcc721_nfts
                    SET owner_address = ?, owner_public_key = ?, outpoint_txid = ?,
                        outpoint_index = ?, updated_at = ?, data = ?
                    WHERE nft_id = ?
                    """,
                    (
                        operation.get("recipientAddress") or "",
                        operation.get("recipientPublicKey") or "",
                        txid,
                        output_index,
                        now,
                        json.dumps(
                            {
                                "batchTransferOperationId": operation.get("id"),
                                "output": item.get("nftOutput") or {},
                            },
                            sort_keys=True,
                        ),
                        nft_id,
                    ),
                )
        return
    if kind not in ("collection-genesis", "migration-genesis"):
        return
    premint_ids = operation.get("nftIds") or []
    is_v2 = str(manifest.get("version") or "").startswith("0.2")
    try:
        with db_lock, db_connect() as conn:
            conn.execute(
                """
                INSERT INTO kcc721_collections(
                    collection_id, ticker, deployer_address, deployer_public_key,
                    max_supply, metadata_uri, metadata_digest, mint_price_sompi,
                    mint_daa_score, next_token_id, controller_txid,
                    controller_output_index, status, created_at, updated_at, data
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'live', ?, ?, ?)
                ON CONFLICT(collection_id) DO NOTHING
                """,
                (
                    collection_id,
                    manifest.get("ticker") or "",
                    operation.get("walletAddress") or "",
                    manifest.get("deploymentPublicKey") or "",
                    int(manifest.get("maxSupply") or 0),
                    manifest.get("metadataUri") or "",
                    manifest.get("metadataDigest") or "",
                    int(manifest.get("mintPriceSompi") or 0),
                    int(manifest.get("mintDaaScore") or 0),
                    1 if is_v2 else len(premint_ids),
                    txid,
                    now,
                    now,
                    json.dumps(operation, sort_keys=True),
                ),
            )
            for token_id, nft_id in enumerate(premint_ids):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO kcc721_nfts(
                        nft_id, collection_id, token_id, owner_address,
                        owner_public_key, outpoint_txid, outpoint_index,
                        status, updated_at, data
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'live', ?, ?)
                    """,
                    (
                        nft_id,
                        collection_id,
                        token_id,
                        operation.get("walletAddress") or "",
                        manifest.get("deploymentPublicKey") or "",
                        txid,
                        token_id + 1,
                        now,
                        json.dumps(
                            {
                                "genesisOperationId": operation.get("id"),
                                "output": (operation.get("nftOutputs") or [{}])[token_id],
                            },
                            sort_keys=True,
                        ),
                    ),
                )
    except sqlite3.IntegrityError as exc:
        operation["status"] = "noncanonical"
        operation["registryError"] = f"Ticker {manifest.get('ticker') or ''} is already assigned to another KCC721 collection."
        operation["updatedAt"] = now_iso()
        db_save_kcc721_operation(operation)
        logger.warning("Rejected duplicate KCC721 ticker for transaction %s: %s", txid, exc)


def db_index_kcc721_operation(operation: dict) -> None:
    _db_index_kcc721_operation_state(operation)
    db_record_kcc721_operation_history(operation)


def kcc721_indexer_loop() -> None:
    while True:
        try:
            for operation in db_pending_kcc721_operations():
                txid = operation.get("txid")
                if not txid:
                    continue
                try:
                    data = json_get(f"{KASPA_API}/transactions/{txid}?inputs=true&outputs=true", timeout=12)
                except HTTPError as exc:
                    if exc.code in (404, 422):
                        continue
                    raise
                if not isinstance(data, dict) or data.get("is_accepted") is not True:
                    continue
                operation["status"] = "accepted"
                operation["updatedAt"] = now_iso()
                db_save_kcc721_operation(operation)
                db_index_kcc721_operation(operation)
                indexed = db_get_kcc721_operation(operation["id"])
                if indexed.get("registryError"):
                    logger.warning("KCC721 transaction %s is noncanonical: %s", txid, indexed["registryError"])
                else:
                    logger.info("Indexed KCC721 %s transaction %s.", operation.get("kind"), txid)
        except Exception as exc:
            logger.warning("KCC721 indexer pass failed: %s", exc)
        time.sleep(2)
def load_jobs_from_db() -> list[tuple[str, dict, bool]]:
    resumable: list[tuple[str, dict, bool]] = []
    with db_lock, db_connect() as conn:
        rows = conn.execute("SELECT id, data, params FROM jobs ORDER BY updated_at DESC LIMIT 300").fetchall()
    with jobs_lock:
        for row in rows:
            try:
                job = json.loads(row["data"])
            except ValueError:
                continue
            original_status = job.get("status")
            if original_status in ("queued", "running", "validating_payment"):
                params = {}
                if row["params"]:
                    try:
                        params = json.loads(row["params"])
                    except ValueError:
                        params = {}
                if isinstance(params, dict) and params:
                    job["status"] = "queued"
                    job["progress"] = "Job resumed after service restart. Waiting for worker."
                    job["params"] = params
                    job.pop("error", None)
                    resumable.append((job["id"], params, original_status == "validating_payment"))
                else:
                    job["status"] = "failed"
                    job["error"] = "Job was interrupted by a service restart and cannot be resumed because it was created before persistent queue support."
                job["updatedAt"] = now_iso()
                db_save_job(job)
            jobs[job["id"]] = job
    return resumable


def wallet_key(wallet_address: str) -> str:
    normalized = str(wallet_address or "").strip().lower()
    if not normalized:
        raise BadRequest("Wallet address is required.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def vault_dir(wallet_address: str) -> Path:
    path = VAULT_DIR / wallet_key(wallet_address)
    path.mkdir(parents=True, exist_ok=True)
    return path


def vault_index_path(wallet_address: str) -> Path:
    return vault_dir(wallet_address) / "index.json"


def read_vault_index(wallet_address: str) -> list[dict]:
    db_items = db_list_wallet_jobs(wallet_address)
    path = vault_index_path(wallet_address)
    file_items = []
    if not path.exists():
        data = []
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
    if isinstance(data, list):
        file_items = [item for item in data if isinstance(item, dict)]

    merged = {}
    for item in file_items + db_items:
        job_id = item.get("id")
        if not job_id:
            continue
        current = merged.get(job_id, {})
        current.update(item)
        current["downloadUrl"] = f"/api/vault/{job_id}/download"
        merged[job_id] = current
    items = list(merged.values())
    items.sort(key=lambda item: item.get("updatedAt") or item.get("createdAt") or "", reverse=True)
    return items[:500]


def write_vault_index(wallet_address: str, items: list[dict]) -> None:
    path = vault_index_path(wallet_address)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(items, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def snapshot_title(params: dict) -> str:
    snapshot_type = params.get("type")
    if snapshot_type == "krc20":
        return f"KRC20 {params['krc20Tick'].upper()} min {params['krc20Min']:g}"
    if snapshot_type == "krc721":
        return f"KRC721 {params['krc721Tick'].upper()} min {params['krc721Min']}"
    if snapshot_type == "multi721":
        ticks = " + ".join(item["tick"].upper() for item in params["collections"])
        return f"Advanced KRC721 {ticks}"
    ticks = " + ".join(item["tick"].upper() for item in params["collections"])
    return f"Advanced {params['krc20Tick'].upper()} + {ticks}"


def snapshot_summary(params: dict) -> str:
    snapshot_type = params.get("type")
    if snapshot_type == "krc20":
        return f"Minimum balance {params['krc20Min']:g}"
    if snapshot_type == "krc721":
        return "Fixed minimum 1 NFT"
    if snapshot_type == "multi721":
        return ", ".join(f"{item['tick'].upper()} min {item['min']}" for item in params["collections"])
    krc721 = ", ".join(f"{item['tick'].upper()} min {item['min']}" for item in params["collections"])
    return f"{params['krc20Tick'].upper()} min {params['krc20Min']:g}; {krc721}"


def xray_title(params: dict) -> str:
    return f"Wallet X-Ray {params['address']}"


def xray_summary(params: dict) -> str:
    depth = "one-hop" if params.get("depth") == "one-hop" else "direct"
    krc20 = f", KRC20 {params['krc20Tick']}" if params.get("krc20Tick") else ""
    return f"{depth.title()} relationship graph, up to {params['maxTx']:,} transactions{krc20}"


def short_address(address: str) -> str:
    value = str(address or "")
    if len(value) <= 24:
        return value
    return f"{value[:12]}...{value[-8:]}"


def persist_vault_job(job: dict) -> None:
    db_save_job(job)
    wallet_address = job.get("walletAddress")
    if not wallet_address:
        return

    with vault_lock:
        items = read_vault_index(wallet_address)
        existing = {item.get("id"): item for item in items}
        current = existing.get(job["id"], {})
        current.update(
            {
                "id": job["id"],
                "type": job.get("type"),
                "title": job.get("title") or job.get("filename") or "Snapshot",
                "summary": job.get("summary") or "",
                "status": job.get("status"),
                "progress": job.get("progress"),
                "createdAt": job.get("createdAt"),
                "updatedAt": job.get("updatedAt"),
                "walletAddress": wallet_address,
                "walletCount": job.get("walletCount"),
                "filename": job.get("filename"),
                "resultUrl": job.get("resultUrl"),
                "paid": job.get("paid"),
                "paymentTxid": job.get("paymentTxid"),
                "downloadUrl": f"/api/vault/{job['id']}/download",
            }
        )
        if job.get("error"):
            current["error"] = job["error"]
        if job.get("status") == "complete":
            current["progress"] = "Wallet X-Ray complete." if job.get("type") == "xray" else "Snapshot complete."

        next_items = [item for item in items if item.get("id") != job["id"]]
        next_items.insert(0, current)
        write_vault_index(wallet_address, next_items[:500])


def public_job(job: dict) -> dict:
    data = {key: value for key, value in job.items() if key not in ("walletKey", "params")}
    if data.get("type") == "xray" and data.get("progress") == "Snapshot complete.":
        data["progress"] = "Wallet X-Ray complete."
    return data


def submit_job(job_id: str, params: dict, validate_payment: bool = False) -> bool:
    with submitted_jobs_lock:
        if job_id in submitted_jobs:
            return False
        submitted_jobs.add(job_id)
    executor.submit(job_runner, job_id, params, validate_payment)
    return True


def job_runner(job_id: str, params: dict, validate_payment: bool = False) -> None:
    try:
        if validate_payment:
            set_job(job_id, status="validating_payment", progress="Validating payment on-chain.")
            validate_payment_for_params(params, job_id)
            set_job(job_id, paymentTxid=params.get("paymentTxid"))
        run_snapshot(job_id, params)
    except (PaymentRequired, BadRequest) as exc:
        set_job(job_id, status="failed", progress="Payment validation failed.", error=str(exc))
    except Exception as exc:
        logger.exception("Job runner failed: %s", exc)
        set_job(job_id, status="failed", error=str(exc))
    finally:
        with submitted_jobs_lock:
            submitted_jobs.discard(job_id)


def json_get(url: str, timeout: int = 20) -> dict:
    request = Request(url, headers={"accept": "application/json", "user-agent": "KaspaDevTools/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def json_get_with_headers(url: str, timeout: int = 20) -> tuple[dict | list, dict]:
    request = Request(url, headers={"accept": "application/json", "user-agent": "KaspaDevTools/1.0"})
    with urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8-sig"))
        headers = {key.lower(): value for key, value in response.headers.items()}
    return body, headers


def clean_tick(value: str) -> str:
    tick = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9._-]{1,32}", tick):
        raise BadRequest("Invalid ticker.")
    return tick


def clean_kaspa_address(value: str) -> str:
    address = str(value or "").strip().lower()
    if not re.fullmatch(r"kaspa:[a-z0-9]{61,63}", address):
        raise BadRequest("Invalid Kaspa address.")
    return address


def clean_txid(value: str) -> str:
    txid = extract_txid(value)
    if not txid:
        raise BadRequest("Invalid payment transaction id.")
    return txid


def extract_txid(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        candidates = value.values() if isinstance(value, dict) else value
        preferred_keys = ("txid", "txId", "id", "hash", "transactionId", "txHash")
        if isinstance(value, dict):
            for key in preferred_keys:
                if key in value:
                    txid = extract_txid(value[key])
                    if txid:
                        return txid
        for item in candidates:
            txid = extract_txid(item)
            if txid:
                return txid
        return ""
    text = str(value or "").strip()
    if text.startswith("{") or text.startswith("["):
        try:
            txid = extract_txid(json.loads(text))
            if txid:
                return txid
        except ValueError:
            pass
    match = re.search(r"\b[a-fA-F0-9]{64}\b", text)
    return match.group(0).lower() if match else ""


def parse_min_float(value) -> float:
    if value is None or str(value).strip() == "":
        return 0.0
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise BadRequest("Minimum balance must be a number.")
    if parsed < 0:
        raise BadRequest("Minimum balance cannot be negative.")
    return parsed


def parse_min_int(value) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise BadRequest("Minimum NFT count must be an integer.")
    if parsed < 1:
        raise BadRequest("Minimum NFT count must be at least 1.")
    return parsed


def parse_int_range(value, default: int, minimum: int, maximum: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise BadRequest(f"{label} must be an integer.")
    if parsed < minimum or parsed > maximum:
        raise BadRequest(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def clean_kcc721_tick(value) -> str:
    tick = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,10}", tick):
        raise BadRequest("Ticker must contain 1 to 10 ASCII letters or digits.")
    return tick


def clean_ipfs_uri(value) -> str:
    uri = str(value or "").strip()
    if len(uri) > 512 or not re.fullmatch(r"ipfs://[A-Za-z0-9]+(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?", uri):
        raise BadRequest("Metadata URL must be an immutable ipfs:// URI.")
    if "?" in uri or "#" in uri:
        raise BadRequest("IPFS metadata URLs cannot contain query strings or fragments.")
    cid = uri.removeprefix("ipfs://").split("/", 1)[0]
    cid_v0 = re.fullmatch(r"Qm[1-9A-HJ-NP-Za-km-z]{44}", cid)
    cid_v1_base32 = re.fullmatch(r"b[a-z2-7]{20,}", cid)
    cid_v1_base58 = re.fullmatch(r"z[1-9A-HJ-NP-Za-km-z]{20,}", cid)
    if not (cid_v0 or cid_v1_base32 or cid_v1_base58):
        raise BadRequest("Metadata URL contains an invalid IPFS CID.")
    return uri.rstrip("/")


def ipfs_gateway_url(value: str) -> str:
    uri = str(value or "").strip()
    if uri.startswith("ipfs://"):
        return f"https://ipfs.io/ipfs/{uri.removeprefix('ipfs://')}"
    return uri


def fetch_ipfs_json(uri: str, timeout: int = 12, maximum_bytes: int = 16_000_000) -> dict:
    clean_uri = clean_ipfs_uri(uri)
    path = clean_uri.removeprefix("ipfs://")
    last_error = None
    for gateway in IPFS_GATEWAYS:
        try:
            request = Request(
                f"{gateway}/{path}",
                headers={"accept": "application/json", "user-agent": "KaspaDevTools/1.0"},
            )
            with urlopen(request, timeout=timeout) as response:
                content_length = int(response.headers.get("content-length") or 0)
                if content_length > maximum_bytes:
                    raise ValueError("metadata JSON exceeds the 16 MB service safety limit")
                body = response.read(maximum_bytes + 1)
                if len(body) > maximum_bytes:
                    raise ValueError("metadata JSON exceeds the 16 MB service safety limit")
            value = json.loads(body.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("metadata file must contain a JSON object")
            return value
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
    raise BadRequest(f"IPFS metadata is not reachable or valid: {last_error or 'no IPFS gateway configured'}")


def validate_kcc721_metadata(metadata_uri: str, max_supply: int) -> None:
    token_ids = sorted({1, int(max_supply)})
    for token_id in token_ids:
        item_uri = f"{metadata_uri.rstrip('/')}/{token_id}.json"
        metadata = fetch_ipfs_json(item_uri)
        image_uri = metadata.get("image")
        if not isinstance(image_uri, str) or not image_uri.strip():
            raise BadRequest(f"IPFS metadata {token_id}.json must contain an image URI.")
        try:
            clean_ipfs_uri(image_uri)
        except BadRequest as exc:
            raise BadRequest(f"IPFS metadata {token_id}.json contains an invalid image URI.") from exc


def parse_optional_uint(value, label: str, maximum: int = (2**63 - 1)) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if not re.fullmatch(r"[0-9]+", text):
        raise BadRequest(f"{label} must be a non-negative integer.")
    number = int(text)
    if number > maximum:
        raise BadRequest(f"{label} is too large.")
    return number


def parse_kas_to_sompi(value, label: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise BadRequest(f"{label} must be a valid KAS amount.") from exc
    if amount < 0 or amount.as_tuple().exponent < -8:
        raise BadRequest(f"{label} must be non-negative with at most 8 decimal places.")
    sompi_amount = amount * Decimal(100_000_000)
    if sompi_amount != sompi_amount.to_integral_value() or sompi_amount > Decimal(2**63 - 1):
        raise BadRequest(f"{label} is out of range.")
    return int(sompi_amount)


def fetch_krc721_collection(tick: str) -> dict:
    try:
        data = json_get(f"{KRC721_API}/nfts/{tick.lower()}", timeout=30)
    except HTTPError as exc:
        if exc.code == 404:
            raise BadRequest("KRC721 collection was not found.") from exc
        raise
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise BadRequest("KRC721 collection was not found.")
    return result


def kcc721_migration_preview(tick: str, wallet_address: str) -> dict:
    collection = fetch_krc721_collection(tick)
    deployer = str(collection.get("deployer") or "").strip().lower()
    metadata = collection.get("buri")
    if not metadata and isinstance(collection.get("metadata"), dict):
        metadata = collection["metadata"].get("image")
    return {
        "tick": str(collection.get("tick") or tick).upper(),
        "supply": str(collection.get("max") or "0"),
        "minted": str(collection.get("minted") or "0"),
        "premint": str(collection.get("premint") or "0"),
        "mintDaaScore": str(collection.get("daaMintStart") or collection.get("mintDaaScore") or "0"),
        "royaltySompi": str(collection.get("royaltyFee") or "0"),
        "metadataUrl": str(metadata or ""),
        "deployer": deployer,
        "deployTransactionId": str(collection.get("txIdRev") or ""),
        "eligible": bool(deployer and deployer == wallet_address.lower()),
    }


def build_kcc721_plan(payload: dict) -> dict:
    mode = str(payload.get("mode") or "deploy").strip().lower()
    if mode not in ("deploy", "migrate"):
        raise BadRequest("Invalid KCC721 deployment mode.")
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    public_key = str(payload.get("publicKey") or "").strip().lower()
    if public_key and not re.fullmatch(r"(?:02|03)?[0-9a-f]{64}", public_key):
        raise BadRequest("Kasware returned an invalid public key.")
    tick = clean_kcc721_tick(payload.get("ticker"))
    migration = None

    if mode == "migrate":
        source = kcc721_migration_preview(tick, wallet_address)
        if not source["eligible"]:
            raise BadRequest("The connected wallet is not the KRC721 deployment address.")
        metadata_uri = clean_ipfs_uri(source["metadataUrl"])
        max_supply = parse_optional_uint(source["supply"], "KRC721 supply", 1_000_000)
        premint = 0
        mint_daa_score = parse_optional_uint(source["mintDaaScore"], "KRC721 mint DAA score")
        mint_price_sompi = 0
        migration = {
            "status": "genesis-ready / manual-issue",
            "sourceProtocol": "krc-721",
            "sourceNetwork": "mainnet",
            "sourceTicker": source["tick"],
            "sourceDeployTransactionId": clean_txid(source["deployTransactionId"]),
            "sourceDeployer": source["deployer"],
            "sourceRoyaltySompi": str(parse_optional_uint(source["royaltySompi"], "KRC721 royalty")),
            "sourcePremint": parse_optional_uint(source["premint"], "KRC721 source premint", max_supply),
            "sourceMintDaaScore": mint_daa_score,
            "mintedAtPreview": parse_optional_uint(source["minted"], "KRC721 minted supply", max_supply),
            "cutoffDaaScore": None,
            "holderSnapshotRoot": None,
        }
    else:
        max_supply = parse_optional_uint(payload.get("supply"), "NFT supply", 25_000)
        if max_supply < 1:
            raise BadRequest("NFT supply must be at least 1.")
        metadata_uri = clean_ipfs_uri(payload.get("metadataUrl"))
        premint = 0
        mint_daa_score = parse_optional_uint(payload.get("mintDaaScore"), "Mint DAA score")
        mint_price_sompi = parse_kas_to_sompi(payload.get("mintPriceKas"), "Mint price")

    metadata_digest = hashlib.sha256(metadata_uri.encode("utf-8")).hexdigest()
    manifest = {
        "protocol": "kcc-721",
        "version": "0.2.0",
        "network": "mainnet",
        "mode": mode,
        "ticker": tick,
        "maxSupply": max_supply,
        "metadataUri": metadata_uri,
        "metadataDigest": metadata_digest,
        "mintPriceSompi": mint_price_sompi,
        "premintAllocation": premint,
        "mintDaaScore": mint_daa_score,
        "deploymentAddress": wallet_address,
        "deploymentPublicKey": public_key or None,
        "migration": migration,
    }
    if not migration:
        manifest.update({"tokenIdBase": 1, "mintMode": "commit-reveal"})
    else:
        if migration["mintedAtPreview"] != max_supply:
            raise BadRequest("KRC721 migration currently requires a fully minted source collection.")
        manifest.update({"tokenIdBase": 1, "mintMode": "migration-merkle-issue"})
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        **manifest,
        "manifestHash": hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        "status": "mainnet transaction review required",
    }


def run_kcc721_engine(command: str, payload: dict) -> dict:
    if not KCC721_ENGINE.exists() or not os.access(KCC721_ENGINE, os.X_OK):
        raise RuntimeError("KCC721 Mainnet engine is not installed.")
    process = subprocess.run(
        [str(KCC721_ENGINE), command],
        input=json.dumps(payload, separators=(",", ":")),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        message = (process.stderr or process.stdout or "KCC721 transaction preparation failed.").strip().splitlines()[-1]
        raise BadRequest(message.removeprefix("Error: ").strip())
    try:
        result = json.loads(process.stdout)
    except ValueError as exc:
        raise RuntimeError("KCC721 engine returned invalid JSON.") from exc
    if not isinstance(result, dict) or not result.get("txJsonString"):
        raise RuntimeError("KCC721 engine returned an incomplete transaction.")
    return result


def clean_kasware_utxo(value) -> dict:
    if not isinstance(value, dict):
        raise BadRequest("A Kasware funding UTXO is required.")
    txid = clean_txid(value.get("transactionId"))
    index = parse_optional_uint(value.get("index"), "UTXO index", 2**32 - 1)
    amount = parse_optional_uint(value.get("amount"), "UTXO amount", 2**63 - 1)
    daa = parse_optional_uint(value.get("blockDaaScore"), "UTXO DAA score")
    script = str(value.get("scriptPublicKey") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{6,10000}", script) or len(script) % 2:
        raise BadRequest("Kasware returned an invalid UTXO script public key.")
    if amount < 1:
        raise BadRequest("Kasware returned an empty funding UTXO.")
    return {
        "transactionId": txid,
        "index": index,
        "amount": str(amount),
        "scriptPublicKey": script,
        "blockDaaScore": str(daa),
        "isCoinbase": parse_bool(value.get("isCoinbase")),
    }


def prepare_kcc721_deploy(payload: dict) -> dict:
    with kcc721_deploy_lock:
        return prepare_kcc721_deploy_locked(payload)


def prepare_kcc721_deploy_locked(payload: dict) -> dict:
    plan = build_kcc721_plan(payload)
    if db_kcc721_ticker_taken(plan["ticker"]):
        raise BadRequest(f"Ticker {plan['ticker']} is already reserved by another KCC721 collection.")
    validate_kcc721_metadata(plan["metadataUri"], plan["maxSupply"])
    public_key = str(plan.get("deploymentPublicKey") or "")
    if len(public_key) == 66 and public_key[:2] in ("02", "03"):
        public_key = public_key[2:]
    if not re.fullmatch(r"[0-9a-f]{64}", public_key):
        raise BadRequest("Kasware public key is required for Mainnet deployment.")
    artifact = (
        build_kcc721_shuffle(plan["maxSupply"])
        if plan["mode"] == "deploy"
        else build_kcc721_migration_artifact(plan["maxSupply"])
    )
    if plan["mode"] == "deploy":
        plan["shuffleRoot"] = artifact["shuffleRoot"]
    else:
        plan["unissuedRoot"] = artifact["unissuedRoot"]
    if artifact:
        canonical_manifest = {key: value for key, value in plan.items() if key not in ("manifestHash", "status")}
        canonical = json.dumps(canonical_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        plan["manifestHash"] = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    engine_payload = {
            "publicKey": public_key,
            "ticker": plan["ticker"],
            "supply": plan["maxSupply"],
            "metadataUri": plan["metadataUri"],
            "mintPriceSompi": str(plan["mintPriceSompi"]),
            "mintDaaScore": str(plan["mintDaaScore"]),
            "fundingUtxo": clean_kasware_utxo(payload.get("fundingUtxo")),
    }
    if plan["mode"] == "deploy":
        engine_payload["shuffleRoot"] = artifact["shuffleRoot"]
        engine_command = "prepare-v2-deploy"
    else:
        engine_payload.update({"unissuedRoot": artifact["unissuedRoot"], "migration": plan.get("migration")})
        engine_command = "prepare-v2-migration-deploy"
    engine_result = run_kcc721_engine(engine_command, engine_payload)
    if engine_result.get("ownerAddress") != plan["deploymentAddress"]:
        raise BadRequest("Kasware address and public key do not match.")
    operation_id = uuid.uuid4().hex
    safe_transaction = json.loads(engine_result["txJsonString"])
    operation = {
        "id": operation_id,
        "walletAddress": plan["deploymentAddress"],
        "kind": "migration-genesis" if plan["mode"] == "migrate" else "collection-genesis",
        "collectionId": engine_result["collectionId"],
        "nftIds": engine_result.get("premintNftIds") or [],
        "expectedTxid": engine_result["transactionId"],
        "txid": None,
        "status": "prepared",
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
        "manifest": {
            **{key: value for key, value in plan.items() if key != "status"},
            "deploymentPublicKey": public_key,
        },
        "feeSompi": engine_result["feeSompi"],
        "storageMass": engine_result["storageMass"],
        "controllerOutput": safe_transaction["outputs"][0],
        "nftOutputs": safe_transaction["outputs"][1 : 1 + len(engine_result.get("premintNftIds") or [])],
    }
    db_save_kcc721_operation(operation)
    save_kcc721_shuffle(engine_result["collectionId"], artifact)
    return {**plan, **engine_result, "operationId": operation_id, "status": "prepared for Kasware approval"}


def prepare_kcc721_mint(payload: dict) -> dict:
    collection_id = clean_txid(payload.get("collectionId"))
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    public_key = str(payload.get("publicKey") or "").strip().lower()
    if len(public_key) == 66 and public_key[:2] in ("02", "03"):
        public_key = public_key[2:]
    if not re.fullmatch(r"[0-9a-f]{64}", public_key):
        raise BadRequest("Kasware public key is required for Mainnet minting.")
    with kcc721_prepare_lock:
        collection = db_get_kcc721_collection(collection_id)
        if not collection or collection.get("status") != "live":
            raise BadRequest("KCC721 collection is not indexed as live.")
        manifest = kcc721_collection_manifest(collection)
        if manifest.get("migration"):
            raise BadRequest("Migration NFTs are issued individually by the deployer and cannot be publicly minted.")
        if not str(manifest.get("version") or "").startswith("0.2"):
            raise BadRequest("This experimental v0.1 collection does not support public minting.")
        next_mint_index = int(collection["next_token_id"])
        if next_mint_index > int(collection["max_supply"]):
            raise BadRequest("This KCC721 collection is fully minted.")
        db_supersede_stale_kcc721_operations(collection_id, "blind-mint-commit", 120)
        db_expire_stale_kcc721_mint_queue(collection_id)
        queue_operation_id = str(payload.get("queueOperationId") or "").strip()
        queue_operation = db_get_kcc721_mint_queue_entry(queue_operation_id) if queue_operation_id else {}
        if queue_operation and (
            queue_operation.get("collectionId") != collection_id
            or queue_operation.get("walletAddress") != wallet_address
            or queue_operation.get("status") != "queued"
        ):
            raise BadRequest("This mint queue position is no longer active.")
        if not queue_operation:
            queue_operation = db_find_kcc721_mint_queue_entry(collection_id, wallet_address)
        if not queue_operation:
            queue_operation = {
                "id": uuid.uuid4().hex,
                "walletAddress": wallet_address,
                "ownerPublicKey": public_key,
                "kind": "mint-queue",
                "collectionId": collection_id,
                "ticker": collection["ticker"],
                "status": "queued",
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
            }
            db_save_kcc721_operation(queue_operation)
        elif queue_operation.get("ownerPublicKey") != public_key:
            raise BadRequest("Mint queue wallet key does not match this Kasware connection.")
        db_touch_kcc721_mint_queue(queue_operation)

        active_mint = db_get_active_kcc721_operation(collection_id, "blind-mint-commit", 120)
        if active_mint and active_mint.get("walletAddress") == wallet_address and active_mint.get("preparedResponse"):
            queue_operation["status"] = "promoted"
            queue_operation["updatedAt"] = now_iso()
            db_save_kcc721_operation(queue_operation)
            return active_mint["preparedResponse"]
        if active_mint or db_kcc721_mint_queue_position(queue_operation) != 1:
            return kcc721_mint_queue_response(queue_operation)
        latest = db_latest_kcc721_collection_operation(collection_id)
        controller_output = latest.get("controllerOutput") or {}
        controller_txid = latest.get("txid")
        if not controller_txid or not controller_output.get("scriptPublicKey"):
            raise RuntimeError("Indexed controller outpoint is incomplete.")
        engine_result = run_kcc721_engine(
            "prepare-v2-commit",
            {
                "collectionId": collection_id,
                "deployerPublicKey": collection["deployer_public_key"],
                "recipientPublicKey": public_key,
                "supply": int(collection["max_supply"]),
                "metadataUri": collection["metadata_uri"],
                "shuffleRoot": manifest.get("shuffleRoot"),
                "mintPriceSompi": str(collection["mint_price_sompi"]),
                "mintDaaScore": str(collection["mint_daa_score"]),
                "nextMintIndex": next_mint_index,
                "controllerUtxo": {
                    "transactionId": controller_txid,
                    "index": 0,
                    "amount": str(controller_output.get("value") or 0),
                    "scriptPublicKey": controller_output["scriptPublicKey"],
                    "blockDaaScore": "0",
                    "isCoinbase": False,
                },
                "fundingUtxo": clean_kasware_utxo(payload.get("fundingUtxo")),
            },
        )
        if engine_result.get("recipientAddress") != wallet_address:
            raise BadRequest("Kasware address and public key do not match.")
        safe_transaction = json.loads(engine_result["txJsonString"])
        operation_id = uuid.uuid4().hex
        operation = {
            "id": operation_id,
            "walletAddress": wallet_address,
            "ownerPublicKey": public_key,
            "kind": "blind-mint-commit",
            "collectionId": collection_id,
            "ticketId": engine_result["ticketId"],
            "mintIndex": engine_result["mintIndex"],
            "expectedTxid": engine_result["transactionId"],
            "txid": None,
            "status": "prepared",
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "manifest": latest.get("manifest") or {},
            "feeSompi": engine_result["feeSompi"],
            "storageMass": engine_result["storageMass"],
            "controllerOutput": safe_transaction["outputs"][0],
            "ticketOutput": safe_transaction["outputs"][1],
        }
        response = {
        **engine_result,
        "mode": "mint-commit",
        "ticker": collection["ticker"],
        "maxSupply": collection["max_supply"],
        "metadataUri": collection["metadata_uri"],
        "metadataDigest": collection["metadata_digest"],
        "mintPriceSompi": str(collection["mint_price_sompi"]),
        "operationId": operation_id,
        "status": "prepared for Kasware approval",
        }
        operation["preparedResponse"] = response
        db_save_kcc721_operation(operation)
        queue_operation["status"] = "promoted"
        queue_operation["mintOperationId"] = operation_id
        queue_operation["updatedAt"] = now_iso()
        db_save_kcc721_operation(queue_operation)
    return response


def get_kcc721_mint_queue_status(operation_id: str, wallet_address: str) -> dict:
    with kcc721_prepare_lock:
        operation = db_get_kcc721_mint_queue_entry(operation_id)
        if not operation or operation.get("walletAddress") != wallet_address:
            raise BadRequest("Mint queue position was not found for this wallet.")
        if operation.get("status") != "queued":
            return {
                "mode": "mint-queued",
                "operationId": operation_id,
                "collectionId": operation.get("collectionId"),
                "ready": False,
                "expired": operation.get("status") == "expired",
                "status": operation.get("status") or "inactive",
            }
        db_expire_stale_kcc721_mint_queue(operation["collectionId"])
        operation = db_get_kcc721_mint_queue_entry(operation_id)
        if operation.get("status") != "queued":
            return {
                "mode": "mint-queued",
                "operationId": operation_id,
                "collectionId": operation.get("collectionId"),
                "ready": False,
                "expired": True,
                "status": "expired",
            }
        db_supersede_stale_kcc721_operations(operation["collectionId"], "blind-mint-commit", 120)
        db_touch_kcc721_mint_queue(operation)
        return kcc721_mint_queue_response(operation)


def prepare_kcc721_reveal(payload: dict) -> dict:
    commit_operation_id = str(payload.get("commitOperationId") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", commit_operation_id):
        raise BadRequest("Invalid blind mint operation ID.")
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    with kcc721_prepare_lock:
        commit = db_get_kcc721_operation(commit_operation_id)
        if not commit or commit.get("kind") != "blind-mint-commit":
            raise BadRequest("Blind mint commitment was not found.")
        if commit.get("walletAddress") != wallet_address:
            raise BadRequest("Blind mint commitment belongs to a different wallet.")
        if commit.get("status") != "accepted" or not commit.get("txid"):
            raise BadRequest("Blind mint commitment must be accepted before reveal.")
        collection_id = commit["collectionId"]
        collection = db_get_kcc721_collection(collection_id)
        if not collection or collection.get("status") != "live":
            raise BadRequest("KCC721 collection is not indexed as live.")
        manifest = kcc721_collection_manifest(collection)
        shuffle = load_kcc721_shuffle(collection_id)
        mint_index = int(commit.get("mintIndex") or 0)
        entry = next(
            (item for item in shuffle.get("entries") or [] if int(item.get("mintIndex") or 0) == mint_index),
            None,
        )
        if not entry or shuffle.get("shuffleRoot") != manifest.get("shuffleRoot"):
            raise RuntimeError("The committed blind mint reveal data is unavailable.")
        ticket_output = commit.get("ticketOutput") or {}
        if not ticket_output.get("scriptPublicKey"):
            raise RuntimeError("The accepted blind mint ticket outpoint is incomplete.")
        engine_result = run_kcc721_engine(
            "prepare-v2-reveal",
            {
                "collectionId": collection_id,
                "recipientPublicKey": commit["ownerPublicKey"],
                "supply": int(collection["max_supply"]),
                "metadataUri": collection["metadata_uri"],
                "shuffleRoot": manifest["shuffleRoot"],
                "mintIndex": mint_index,
                "tokenId": int(entry["tokenId"]),
                "salt": entry["salt"],
                "siblings": entry["siblings"],
                "directions": entry["directions"],
                "ticketId": commit["ticketId"],
                "ticketUtxo": {
                    "transactionId": commit["txid"],
                    "index": 1,
                    "amount": str(ticket_output.get("value") or 0),
                    "scriptPublicKey": ticket_output["scriptPublicKey"],
                    "blockDaaScore": "0",
                    "isCoinbase": False,
                },
            },
        )
        safe_transaction = json.loads(engine_result["txJsonString"])
        operation_id = uuid.uuid4().hex
        operation = {
            "id": operation_id,
            "walletAddress": wallet_address,
            "ownerPublicKey": commit["ownerPublicKey"],
            "kind": "blind-mint-reveal",
            "collectionId": collection_id,
            "ticketId": commit["ticketId"],
            "mintIndex": mint_index,
            "nftId": engine_result["nftId"],
            "tokenId": engine_result["tokenId"],
            "expectedTxid": engine_result["transactionId"],
            "txid": None,
            "status": "prepared",
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "manifest": manifest,
            "feeSompi": engine_result["feeSompi"],
            "storageMass": engine_result["storageMass"],
            "nftOutput": safe_transaction["outputs"][0],
        }
        db_save_kcc721_operation(operation)
    return {
        **engine_result,
        "mode": "mint-reveal",
        "ticker": collection["ticker"],
        "maxSupply": collection["max_supply"],
        "metadataUri": collection["metadata_uri"],
        "metadataDigest": collection["metadata_digest"],
        "operationId": operation_id,
        "status": "prepared for automatic reveal broadcast",
    }


def prepare_kcc721_migration_issue(payload: dict) -> dict:
    collection_id = clean_txid(payload.get("collectionId"))
    token_id = parse_int_range(payload.get("tokenId"), 0, 1, 1_000_000, "Migration token ID")
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    recipient_address = clean_kaspa_address(payload.get("recipientAddress"))
    public_key = str(payload.get("publicKey") or "").strip().lower()
    if len(public_key) == 66 and public_key[:2] in ("02", "03"):
        public_key = public_key[2:]
    if not re.fullmatch(r"[0-9a-f]{64}", public_key):
        raise BadRequest("Kasware public key is required for migration issuance.")
    with kcc721_prepare_lock:
        collection = db_get_kcc721_collection(collection_id)
        if not collection or collection.get("status") != "live":
            raise BadRequest("KCC721 migration collection is not indexed as live.")
        manifest = kcc721_collection_manifest(collection)
        migration = kcc721_collection_migration(collection)
        if manifest.get("mintMode") != "migration-merkle-issue" or not migration:
            raise BadRequest("This collection does not support v0.2 migration issuance.")
        if collection["deployer_address"] != wallet_address:
            raise BadRequest("Only the migration deployer can issue source NFTs.")
        if token_id > int(collection["max_supply"]):
            raise BadRequest("Migration token ID exceeds the source collection supply.")
        source_tick = clean_kcc721_tick(migration.get("sourceTicker"))
        try:
            source_data = json_get(f"{KRC721_API}/nfts/{source_tick}/{token_id}", timeout=20)
        except HTTPError as exc:
            if exc.code == 404:
                raise BadRequest("This KRC721 source NFT does not exist.") from exc
            raise
        if not isinstance(source_data, dict) or not isinstance(source_data.get("result"), dict):
            raise BadRequest("This KRC721 source NFT does not exist.")
        if db_get_kcc721_nft_by_token(collection_id, token_id):
            raise BadRequest("This migration NFT has already been issued.")
        db_supersede_stale_kcc721_operations(collection_id, "migration-issue", 120)
        if db_get_active_kcc721_operation(collection_id, "migration-issue", 120):
            raise BadRequest("A migration issuance is currently awaiting signature or Mainnet acceptance.")
        artifact = load_kcc721_shuffle(collection_id)
        proof = build_kcc721_migration_issue(artifact, token_id)
        if proof["currentUnissuedRoot"] != artifact.get("unissuedRoot"):
            raise RuntimeError("Migration issuance artifact is inconsistent.")
        latest = db_latest_kcc721_collection_operation(collection_id)
        controller_output = latest.get("controllerOutput") or {}
        controller_txid = latest.get("txid")
        if not controller_txid or not controller_output.get("scriptPublicKey"):
            raise RuntimeError("Indexed migration controller outpoint is incomplete.")
        engine_result = run_kcc721_engine(
            "prepare-v2-migration-issue",
            {
                "collectionId": collection_id,
                "deployerPublicKey": public_key,
                "recipientAddress": recipient_address,
                "supply": int(collection["max_supply"]),
                "metadataUri": collection["metadata_uri"],
                "currentUnissuedRoot": proof["currentUnissuedRoot"],
                "nextUnissuedRoot": proof["nextUnissuedRoot"],
                "remaining": proof["remaining"],
                "tokenId": token_id,
                "siblings": proof["siblings"],
                "directions": proof["directions"],
                "controllerUtxo": {
                    "transactionId": controller_txid,
                    "index": 0,
                    "amount": str(controller_output.get("value") or 0),
                    "scriptPublicKey": controller_output["scriptPublicKey"],
                    "blockDaaScore": "0",
                    "isCoinbase": False,
                },
                "fundingUtxo": clean_kasware_utxo(payload.get("fundingUtxo")),
            },
        )
        if wallet_address != collection["deployer_address"]:
            raise BadRequest("Kasware address and migration deployment key do not match.")
        safe_transaction = json.loads(engine_result["txJsonString"])
        operation_id = uuid.uuid4().hex
        operation = {
            "id": operation_id,
            "walletAddress": wallet_address,
            "kind": "migration-issue",
            "collectionId": collection_id,
            "nftId": engine_result["nftId"],
            "tokenId": token_id,
            "recipientAddress": recipient_address,
            "expectedTxid": engine_result["transactionId"],
            "txid": None,
            "status": "prepared",
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "manifest": manifest,
            "feeSompi": engine_result["feeSompi"],
            "storageMass": engine_result["storageMass"],
            "currentUnissuedRoot": proof["currentUnissuedRoot"],
            "nextUnissuedRoot": proof["nextUnissuedRoot"],
            "controllerOutput": safe_transaction["outputs"][0],
            "nftOutput": safe_transaction["outputs"][1],
        }
        db_save_kcc721_operation(operation)
        return {
            **engine_result,
            "mode": "migration-issue",
            "ticker": collection["ticker"],
            "operationId": operation_id,
            "status": "prepared for Kasware approval",
        }


def prepare_kcc721_transfer(payload: dict) -> dict:
    nft_id = clean_txid(payload.get("nftId"))
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    recipient_address = clean_kaspa_address(payload.get("recipientAddress"))
    public_key = str(payload.get("publicKey") or "").strip().lower()
    if len(public_key) == 66 and public_key[:2] in ("02", "03"):
        public_key = public_key[2:]
    if not re.fullmatch(r"[0-9a-f]{64}", public_key):
        raise BadRequest("Kasware public key must be a 32-byte x-only key.")
    with kcc721_prepare_lock:
        nft = db_get_kcc721_nft(nft_id)
        if not nft or nft.get("status") != "live":
            raise BadRequest("KCC721 NFT is not indexed as live.")
        if nft["owner_address"] != wallet_address:
            raise BadRequest("The connected wallet is not the current NFT owner.")
        if db_has_active_kcc721_nft_operation(nft_id):
            raise BadRequest("Another transfer for this NFT is already prepared or pending.")
        collection = db_get_kcc721_collection(nft["collection_id"])
        manifest = kcc721_collection_manifest(collection)
        try:
            nft_data = json.loads(nft.get("data") or "{}")
        except ValueError:
            nft_data = {}
        current_output = nft_data.get("output") or {}
        if not current_output.get("scriptPublicKey"):
            raise RuntimeError("Indexed NFT outpoint is incomplete.")
        engine_result = run_kcc721_engine(
            "prepare-v2-transfer" if str(manifest.get("version") or "").startswith("0.2") else "prepare-transfer",
            {
                "collectionId": nft["collection_id"],
                "nftId": nft_id,
                "tokenId": int(nft["token_id"]),
                "metadataUri": collection["metadata_uri"],
                "currentOwnerPublicKey": public_key,
                "recipientAddress": recipient_address,
                "nftUtxo": {
                    "transactionId": nft["outpoint_txid"],
                    "index": int(nft["outpoint_index"]),
                    "amount": str(current_output.get("value") or 0),
                    "scriptPublicKey": current_output["scriptPublicKey"],
                    "blockDaaScore": "0",
                    "isCoinbase": False,
                },
                "fundingUtxo": clean_kasware_utxo(payload.get("fundingUtxo")),
            },
        )
        if engine_result.get("previousOwnerAddress") != wallet_address:
            raise BadRequest("Kasware address and public key do not match.")
        if engine_result.get("recipientAddress") != recipient_address:
            raise BadRequest("The recipient must be a Mainnet P2PK address.")
        safe_transaction = json.loads(engine_result["txJsonString"])
        operation_id = uuid.uuid4().hex
        operation = {
            "id": operation_id,
            "walletAddress": wallet_address,
            "kind": "nft-transfer",
            "collectionId": nft["collection_id"],
            "nftId": nft_id,
            "tokenId": int(nft["token_id"]),
            "recipientAddress": recipient_address,
            "recipientPublicKey": engine_result["recipientPublicKey"],
            "expectedTxid": engine_result["transactionId"],
            "txid": None,
            "status": "prepared",
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "feeSompi": engine_result["feeSompi"],
            "storageMass": engine_result["storageMass"],
            "nftOutput": safe_transaction["outputs"][0],
        }
        db_save_kcc721_operation(operation)
    return {**engine_result, "mode": "transfer", "operationId": operation_id, "status": "prepared for Kasware approval"}


def prepare_kcc721_batch_transfer(payload: dict) -> dict:
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    recipient_address = clean_kaspa_address(payload.get("recipientAddress"))
    public_key = str(payload.get("publicKey") or "").strip().lower()
    if len(public_key) == 66 and public_key[:2] in ("02", "03"):
        public_key = public_key[2:]
    if not re.fullmatch(r"[0-9a-f]{64}", public_key):
        raise BadRequest("Kasware public key must be a 32-byte x-only key.")
    raw_nft_ids = payload.get("nftIds")
    if not isinstance(raw_nft_ids, list) or not 2 <= len(raw_nft_ids) <= 22:
        raise BadRequest("Select between 2 and 22 KCC721 NFTs for an atomic transfer.")
    nft_ids = [clean_txid(value) for value in raw_nft_ids]
    if len(set(nft_ids)) != len(nft_ids):
        raise BadRequest("The same NFT cannot appear twice in an atomic batch.")

    with kcc721_prepare_lock:
        db_cancel_matching_prepared_kcc721_batches(wallet_address, set(nft_ids))
        engine_nfts = []
        operation_items = []
        first_collection_id = None
        for nft_id in nft_ids:
            nft = db_get_kcc721_nft(nft_id)
            if not nft or nft.get("status") != "live":
                raise BadRequest(f"KCC721 NFT {nft_id} is not indexed as live.")
            if nft["owner_address"] != wallet_address:
                raise BadRequest("The connected wallet is not the current owner of every selected NFT.")
            if db_has_active_kcc721_nft_operation(nft_id):
                raise BadRequest("Another transfer for one of the selected NFTs is already prepared or pending.")
            collection = db_get_kcc721_collection(nft["collection_id"])
            manifest = kcc721_collection_manifest(collection)
            if not str(manifest.get("version") or "").startswith("0.2"):
                raise BadRequest("Atomic batch transfers require KCC721 v0.2 NFTs.")
            try:
                nft_data = json.loads(nft.get("data") or "{}")
            except ValueError:
                nft_data = {}
            current_output = nft_data.get("output") or {}
            if not current_output.get("scriptPublicKey"):
                raise RuntimeError("An indexed NFT outpoint is incomplete.")
            first_collection_id = first_collection_id or nft["collection_id"]
            engine_nfts.append({
                "collectionId": nft["collection_id"],
                "nftId": nft_id,
                "tokenId": int(nft["token_id"]),
                "metadataUri": collection["metadata_uri"],
                "nftUtxo": {
                    "transactionId": nft["outpoint_txid"],
                    "index": int(nft["outpoint_index"]),
                    "amount": str(current_output.get("value") or 0),
                    "scriptPublicKey": current_output["scriptPublicKey"],
                    "blockDaaScore": "0",
                    "isCoinbase": False,
                },
            })
            operation_items.append({
                "collectionId": nft["collection_id"],
                "nftId": nft_id,
                "tokenId": int(nft["token_id"]),
            })

        engine_result = run_kcc721_engine("prepare-v2-batch-transfer", {
            "currentOwnerPublicKey": public_key,
            "recipientAddress": recipient_address,
            "nfts": engine_nfts,
            "fundingUtxo": clean_kasware_utxo(payload.get("fundingUtxo")),
        })
        if engine_result.get("previousOwnerAddress") != wallet_address:
            raise BadRequest("Kasware address and public key do not match.")
        if engine_result.get("recipientAddress") != recipient_address:
            raise BadRequest("The recipient must be a Mainnet P2PK address.")
        safe_transaction = json.loads(engine_result["txJsonString"])
        for item in operation_items:
            output_index = next(
                value["outputIndex"] for value in engine_result["items"]
                if value["nftId"] == item["nftId"]
            )
            item["outputIndex"] = output_index
            item["nftOutput"] = safe_transaction["outputs"][output_index]
        operation_id = uuid.uuid4().hex
        operation = {
            "id": operation_id,
            "walletAddress": wallet_address,
            "kind": "nft-batch-transfer",
            "collectionId": first_collection_id,
            "nftId": nft_ids[0],
            "nftIds": nft_ids,
            "items": operation_items,
            "recipientAddress": recipient_address,
            "recipientPublicKey": engine_result["recipientPublicKey"],
            "expectedTxid": engine_result["transactionId"],
            "txid": None,
            "status": "prepared",
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "feeSompi": engine_result["feeSompi"],
            "computeMass": engine_result["computeMass"],
            "transientMass": engine_result["transientMass"],
            "storageMass": engine_result["storageMass"],
        }
        db_save_kcc721_operation(operation)
    return {
        **engine_result,
        "mode": "batch-transfer",
        "operationId": operation_id,
        "status": "atomic batch prepared for one Kasware approval",
    }


def register_kcc721_broadcast(payload: dict) -> dict:
    operation_id = str(payload.get("operationId") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise BadRequest("Invalid KCC721 operation ID.")
    operation = db_get_kcc721_operation(operation_id)
    if not operation:
        raise BadRequest("KCC721 operation was not found.")
    if operation.get("status") not in ("prepared", "submitted"):
        raise BadRequest("This KCC721 operation is no longer available for broadcast.")
    txid = clean_txid(payload.get("txid"))
    if txid != operation.get("expectedTxid"):
        raise BadRequest("Broadcast transaction ID does not match the reviewed transaction.")
    operation["txid"] = txid
    operation["status"] = "submitted"
    operation["updatedAt"] = now_iso()
    db_save_kcc721_operation(operation)
    return {
        "operationId": operation_id,
        "txid": txid,
        "collectionId": operation.get("collectionId"),
        "status": operation["status"],
    }


def cancel_kcc721_operation(payload: dict) -> dict:
    operation_id = str(payload.get("operationId") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise BadRequest("Invalid KCC721 operation ID.")
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    operation = db_get_kcc721_operation(operation_id)
    if not operation or operation.get("walletAddress") != wallet_address:
        raise BadRequest("KCC721 operation was not found for this wallet.")
    if operation.get("status") == "prepared":
        operation["status"] = "cancelled"
        operation["updatedAt"] = now_iso()
        db_save_kcc721_operation(operation)
    return {"operationId": operation_id, "status": operation.get("status")}


def get_krc20_address_balance(address: str, tick: str) -> float:
    cursor = None
    while True:
        params = urlencode({"next": cursor}) if cursor else ""
        url = f"{KASPLEX_API}/krc20/address/{address}/tokenlist"
        if params:
            url = f"{url}?{params}"
        try:
            data = json_get(url, timeout=10)
            for token in data.get("result", []):
                if token.get("tick", "").upper() == tick.upper():
                    return int(token.get("balance", "0")) / (10**8)
            cursor = data.get("next")
            if not cursor:
                break
        except (HTTPError, URLError, TimeoutError, ValueError):
            break
    return 0.0


def get_krc20_snapshot(tick: str, min_amount: float, progress_cb=None) -> dict:
    token = clean_tick(tick)
    workers = parse_int_range(
        os.environ.get("KASPA_DEVTOOLS_KRC20_WORKERS"), 8, 1, 32, "KRC20 workers"
    )
    requests_per_minute = parse_int_range(
        os.environ.get("KASPA_DEVTOOLS_KRC20_REQUESTS_PER_MINUTE"),
        720,
        1,
        1000,
        "KRC20 requests per minute",
    )
    command = [
        sys.executable,
        str(KRC20_SNAPSHOT_SCRIPT),
        "--network",
        "mainnet",
        "--token",
        token,
        "--workers",
        str(workers),
        "--requests-per-minute",
        str(requests_per_minute),
        "--output-dir",
        str(KRC20_SNAPSHOT_DIR),
        "--quiet",
    ]

    if progress_cb:
        progress_cb("Waiting for the Mainnet KRC20 snapshot worker.")
    with krc20_snapshot_lock:
        if progress_cb:
            progress_cb("Updating the persistent KRC20 holder index from Kasplex.")
        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            text=True,
            capture_output=True,
            check=False,
        )

    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        message = detail[-1] if detail else "KRC20 snapshot worker failed."
        logger.error("KRC20 snapshot worker failed for %s: %s", token, completed.stderr.strip())
        raise RuntimeError(message)
    try:
        metadata = json.loads(completed.stdout)
        output_path = Path(metadata["output"]).resolve()
        output_path.relative_to(KRC20_SNAPSHOT_DIR.resolve())
        snapshot = json.loads(output_path.read_text(encoding="utf-8"))
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("KRC20 snapshot worker returned invalid output.") from exc
    if snapshot.get("network") != "mainnet":
        raise RuntimeError("KRC20 snapshot worker returned a non-Mainnet result.")

    decimals = int(snapshot.get("decimals", KRC20_DECIMALS))
    scale = Decimal(10) ** decimals
    threshold = Decimal(str(min_amount))
    results = {
        address: Decimal(str(raw_balance)) / scale
        for address, raw_balance in snapshot.get("balances", {}).items()
        if Decimal(str(raw_balance)) / scale >= threshold
    }
    if progress_cb:
        duration = snapshot.get("durationSeconds", 0)
        progress_cb(
            f"KRC20 index updated in {duration}s. "
            f"{len(results):,} of {snapshot.get('holderCount', 0):,} holders qualify."
        )
    return results


def collect_krc721_holders(tick: str, progress_cb=None) -> dict:
    holders = {}
    offset = None
    pages = 0
    consecutive_errors = 0
    while True:
        url = f"{KRC721_API}/owners/{tick.upper()}"
        if offset is not None:
            url = f"{url}?{urlencode({'offset': offset})}"
        try:
            data = json_get(url, timeout=20)
            consecutive_errors = 0
            for token in data.get("result", []):
                owner = token.get("owner")
                token_id = str(token.get("id", token.get("tokenId", token.get("nftId", ""))))
                if owner:
                    holders.setdefault(owner, [])
                    if token_id:
                        holders[owner].append(token_id)
            offset = data.get("next")
            pages += 1
            if progress_cb and pages % 10 == 0:
                total_tokens = sum(len(ids) for ids in holders.values())
                progress_cb(f"Scanned {total_tokens:,} {tick.upper()} NFTs, found {len(holders):,} holders.")
            if not offset:
                break
            time.sleep(0.15)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            consecutive_errors += 1
            logger.error("owners endpoint error page %s: %s", pages, exc)
            if consecutive_errors >= 5:
                break
            time.sleep(min(20, 2**consecutive_errors))
    return holders


def get_krc721_snapshot(tick: str, min_count: int, progress_cb=None) -> dict:
    if progress_cb:
        progress_cb(f"Fetching all {tick.upper()} owners from the KRC721 indexer.")
    holders = collect_krc721_holders(tick, progress_cb)
    return {addr: ids for addr, ids in holders.items() if len(ids) >= min_count}


def sort_ids(ids: list) -> list:
    try:
        return sorted(ids, key=lambda value: int(value))
    except (TypeError, ValueError):
        return sorted(ids)


def format_krc20_txt(tick: str, min_amount: float, results: dict) -> str:
    lines = [
        f"KRC20 Snapshot: {tick.upper()}",
        f"Minimum balance: {min_amount:,.8f} {tick.upper()}",
        f"Total qualifying wallets: {len(results)}",
        "=" * 60,
        "",
    ]
    for addr, balance in sorted(results.items(), key=lambda item: -item[1]):
        lines.append(f"{addr}  {balance:,.8f} {tick.upper()}")
    return "\n".join(lines)


def format_krc721_txt(tick: str, min_count: int, results: dict) -> str:
    lines = [
        f"KRC721 Snapshot: {tick.upper()}",
        f"Minimum NFT count: {min_count}",
        f"Total qualifying wallets: {len(results)}",
        "=" * 60,
        "",
    ]
    for addr, ids in sorted(results.items(), key=lambda item: -len(item[1])):
        ids_str = ", ".join(f"#{token_id}" for token_id in sort_ids(ids))
        lines.append(f"{addr}  {len(ids)} {tick.upper()}  [{ids_str}]")
    return "\n".join(lines)


def format_multi_krc721_txt(collections: list[dict], results: dict) -> str:
    col_str = " + ".join(f"{item['tick'].upper()} (min {item['min']})" for item in collections)
    lines = [
        "Advanced KRC721 Snapshot",
        f"Collections: {col_str}",
        f"Total qualifying wallets: {len(results)}",
        "=" * 60,
        "",
    ]
    for addr, info in sorted(results.items()):
        parts = []
        for item in collections:
            tick = item["tick"]
            ids = info[tick]
            ids_str = ", ".join(f"#{token_id}" for token_id in sort_ids(ids))
            parts.append(f"{len(ids)} {tick.upper()} [{ids_str}]")
        lines.append(f"{addr}  |  " + "  |  ".join(parts))
    return "\n".join(lines)


def format_advanced_txt(krc20_tick: str, krc20_min: float, collections: list[dict], results: dict) -> str:
    krc721_str = " + ".join(f"{item['tick'].upper()} (min {item['min']})" for item in collections)
    lines = [
        "Advanced KRC20 + KRC721 Snapshot",
        f"KRC20: {krc20_tick.upper()} (min {krc20_min:,.8f})",
        f"KRC721: {krc721_str}",
        f"Total qualifying wallets: {len(results)}",
        "=" * 60,
        "",
    ]
    for addr, info in sorted(results.items(), key=lambda item: -item[1]["krc20"]):
        parts = [f"{info['krc20']:,.8f} {krc20_tick.upper()}"]
        for item in collections:
            tick = item["tick"]
            ids = info[tick]
            ids_str = ", ".join(f"#{token_id}" for token_id in sort_ids(ids))
            parts.append(f"{len(ids)} {tick.upper()} [{ids_str}]")
        lines.append(f"{addr}  |  " + "  |  ".join(parts))
    return "\n".join(lines)


def price_for_params(params: dict) -> int:
    if params.get("type") == "krc721":
        return 0
    if params.get("type") == "multi721":
        return ADVANCED_KRC721_PRICE_KAS
    if params.get("type") == "xray":
        return XRAY_KRC20_PRICE_KAS if params.get("krc20Tick") else XRAY_PRICE_KAS
    if params.get("type") == "batchtransfer":
        return BATCH_TRANSFER_PRICE_KAS
    return SNAPSHOT_PRICE_KAS


def parse_payment_started_at(value) -> int:
    try:
        started_at = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return started_at if started_at > 0 else 0


def transaction_time_ms(transaction: dict) -> int:
    for key in ("accepting_block_time", "block_time", "timestamp", "time"):
        try:
            value = int(transaction.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value * 1000 if value < 100000000000 else value
    return 0


def now_ms() -> int:
    return int(time.time() * 1000)


def sign_payment_session(body: str) -> str:
    return hmac.new(PAYMENT_SESSION_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def create_payment_session(wallet_address: str, expected_kas: float) -> dict:
    expected_sompi = int(round(expected_kas * 100000000))
    created_at = now_ms()
    body = f"{created_at}:{expected_sompi}:{wallet_address.lower()}"
    signature = sign_payment_session(body)
    return {"paymentSession": f"{body}:{signature}", "paymentStartedAt": created_at, "expectedSompi": expected_sompi}


def verify_payment_session(token: str, wallet_address: str, expected_kas: float) -> int:
    parts = str(token or "").split(":")
    if len(parts) != 5:
        raise PaymentRequired("Payment session is missing. Please start a new payment from this page.")
    created_at_raw, expected_sompi_raw, prefix, payload, signature = parts
    session_wallet = f"{prefix}:{payload}".lower()
    try:
        created_at = int(created_at_raw)
        expected_sompi = int(expected_sompi_raw)
    except ValueError:
        raise PaymentRequired("Invalid payment session.") from None
    if session_wallet != wallet_address.lower() or expected_sompi != int(round(expected_kas * 100000000)):
        raise PaymentRequired("Payment session does not match this job.")
    body = f"{created_at}:{expected_sompi}:{session_wallet}"
    if not hmac.compare_digest(sign_payment_session(body), signature):
        raise PaymentRequired("Invalid payment session.")
    age_ms = now_ms() - created_at
    if age_ms < -300000 or age_ms > 60 * 60 * 1000:
        raise PaymentRequired("Payment session expired. Please start a new payment.")
    return created_at


def validate_payment_for_params(params: dict, job_id: str) -> None:
    expected_kas = price_for_params(params)
    if expected_kas <= 0:
        return
    payment_started_at = verify_payment_session(params.get("paymentSession"), params.get("walletAddress") or "", expected_kas)
    txid = clean_txid(params.get("paymentTxid"))
    params["paymentTxid"] = txid
    existing_payment_job = db_payment_job(txid)
    if existing_payment_job and existing_payment_job != job_id:
        raise PaymentRequired("This payment transaction was already used.")
    if existing_payment_job == job_id:
        return

    expected_sompi = int(expected_kas * 100000000)
    tx_data = None
    last_error = None
    url = f"{KASPA_API}/transactions/{txid}?inputs=false&outputs=true"
    for _attempt in range(45):
        try:
            tx_data = json_get(url, timeout=12)
            break
        except HTTPError as exc:
            last_error = exc
            if exc.code not in (404, 422):
                break
        except (URLError, TimeoutError, ValueError) as exc:
            last_error = exc
        time.sleep(2)

    if not isinstance(tx_data, dict):
        raise PaymentRequired("Payment transaction is not indexed yet. Please wait until Kasware no longer shows it as pending, then try again.")

    outputs = tx_data.get("outputs") or []
    paid_sompi = sum(
        sompi(item.get("amount"))
        for item in outputs
        if str(item.get("script_public_key_address") or "").lower() == PAYMENT_ADDRESS.lower()
    )
    if paid_sompi < expected_sompi:
        raise PaymentRequired(f"Payment is too small. Expected {expected_kas} KAS.")
    if tx_data.get("is_accepted") is False:
        raise PaymentRequired("Payment transaction is not accepted yet.")
    tx_time = transaction_time_ms(tx_data)
    if tx_time and tx_time < payment_started_at - 120000:
        raise PaymentRequired("This payment is older than the current payment session.")

    try:
        db_save_payment(txid, params.get("walletAddress") or "", job_id, paid_sompi, expected_sompi, tx_data)
    except sqlite3.IntegrityError:
        raise PaymentRequired("This payment transaction was already used.") from None


def transaction_addresses(transaction: dict, key: str) -> set[str]:
    addresses = set()
    for item in transaction.get(key) or []:
        for field in ("script_public_key_address", "previous_outpoint_address", "address"):
            address = str(item.get(field) or "").lower()
            if address.startswith("kaspa:"):
                addresses.add(address)
        previous = item.get("previous_outpoint") or {}
        if isinstance(previous, dict):
            address = str(previous.get("script_public_key_address") or previous.get("address") or "").lower()
            if address.startswith("kaspa:"):
                addresses.add(address)
    return addresses


def transaction_paid_sompi(transaction: dict, payment_address: str) -> int:
    return sum(
        sompi(item.get("amount"))
        for item in transaction.get("outputs") or []
        if str(item.get("script_public_key_address") or "").lower() == payment_address.lower()
    )


def recover_payment_tx(wallet_address: str, expected_kas: float, payment_started_at: int, max_pages: int = 4) -> dict:
    expected_sompi = int(round(expected_kas * 100000000))
    if not payment_started_at:
        return {}
    before = None
    for _page in range(max_pages):
        params = {"limit": "50", "resolve_previous_outpoints": "light"}
        if before:
            params["before"] = before
        url = f"{KASPA_API}/addresses/{PAYMENT_ADDRESS}/full-transactions-page?{urlencode(params)}"
        data, headers = json_get_with_headers(url, timeout=20)
        if not isinstance(data, list):
            break

        for transaction in data:
            if not isinstance(transaction, dict):
                continue
            txid = extract_txid(transaction.get("transaction_id") or transaction.get("hash") or transaction.get("id"))
            if not txid or db_payment_used(txid):
                continue
            if transaction.get("is_accepted") is False:
                continue
            tx_time = transaction_time_ms(transaction)
            if tx_time and tx_time < payment_started_at - 120000:
                continue
            paid_sompi = transaction_paid_sompi(transaction, PAYMENT_ADDRESS)
            if paid_sompi < expected_sompi:
                continue
            input_addresses = transaction_addresses(transaction, "inputs")
            if wallet_address.lower() not in input_addresses:
                continue
            return {"paymentTxid": txid, "amountSompi": paid_sompi, "expectedSompi": expected_sompi}

        before = headers.get("x-next-page-before")
        if not data or not before:
            break
        time.sleep(0.2)
    return {}


def check_rate_limit(key: str, limit: int, window_seconds: int = 3600) -> None:
    if limit <= 0:
        return
    now = time.time()
    cutoff = now - window_seconds
    with rate_limit_lock:
        timestamps = [stamp for stamp in rate_limits.get(key, []) if stamp >= cutoff]
        if len(timestamps) >= limit:
            raise BadRequest("Too many requests. Please wait before starting another job.")
        timestamps.append(now)
        rate_limits[key] = timestamps
        if len(rate_limits) > 5000:
            for item_key in list(rate_limits.keys()):
                rate_limits[item_key] = [stamp for stamp in rate_limits[item_key] if stamp >= cutoff]
                if not rate_limits[item_key]:
                    rate_limits.pop(item_key, None)


def require_payment(snapshot_type: str) -> bool:
    return snapshot_type != "krc721"


def validate_request(payload: dict) -> dict:
    snapshot_type = payload.get("type")
    wallet_address = clean_kaspa_address(payload.get("walletAddress"))
    payment_txid = str(payload.get("paymentTxid") or "").strip()
    validated = {
        "type": snapshot_type,
        "walletAddress": wallet_address,
        "paymentTxid": payment_txid,
        "paymentStartedAt": parse_payment_started_at(payload.get("paymentStartedAt")),
        "paymentSession": str(payload.get("paymentSession") or "").strip(),
    }

    if snapshot_type == "xray":
        validated["address"] = clean_kaspa_address(payload.get("address"))
        validated["maxTx"] = parse_int_range(payload.get("maxTx"), 500, 50, 5000, "Transaction limit")
        depth = str(payload.get("depth") or "direct").strip().lower()
        if depth not in ("direct", "one-hop"):
            raise BadRequest("Graph depth must be direct or one-hop.")
        validated["depth"] = depth
        validated["includeKrc20"] = parse_bool(payload.get("includeKrc20"))
        krc20_tick = str(payload.get("krc20Tick") or "").strip()
        validated["krc20Tick"] = clean_tick(krc20_tick) if krc20_tick else ""
    elif snapshot_type == "krc20":
        validated["krc20Tick"] = clean_tick(payload.get("krc20Tick"))
        validated["krc20Min"] = parse_min_float(payload.get("krc20Min"))
    elif snapshot_type == "krc721":
        validated["krc721Tick"] = clean_tick(payload.get("krc721Tick"))
        validated["krc721Min"] = parse_min_int(payload.get("krc721Min"))
    elif snapshot_type == "multi721":
        collections = payload.get("collections") or []
        if not 1 <= len(collections) <= 5:
            raise BadRequest("Advanced KRC721 snapshots require 1 to 5 collections.")
        validated["collections"] = [{"tick": clean_tick(item.get("tick")), "min": parse_min_int(item.get("min"))} for item in collections]
    elif snapshot_type == "advanced":
        collections = payload.get("collections") or []
        if not 1 <= len(collections) <= 5:
            raise BadRequest("Advanced snapshots require 1 to 5 KRC721 collections.")
        validated["krc20Tick"] = clean_tick(payload.get("krc20Tick"))
        validated["krc20Min"] = parse_min_float(payload.get("krc20Min"))
        validated["collections"] = [{"tick": clean_tick(item.get("tick")), "min": parse_min_int(item.get("min"))} for item in collections]
    else:
        raise BadRequest("Unknown snapshot type.")

    if require_payment(snapshot_type) and not payment_txid:
        raise PaymentRequired("Payment transaction is required for this job.")

    return validated


def sompi(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def format_kas(amount_sompi: int) -> float:
    return round(amount_sompi / 100000000, 8)


def format_decimal_amount(value: Decimal) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def format_krc20_amount(value) -> str:
    raw = str(value or "0").strip()
    try:
        if "." in raw:
            amount = Decimal(raw)
        else:
            amount = Decimal(int(raw)) / (Decimal(10) ** KRC20_DECIMALS)
    except (InvalidOperation, ValueError):
        return raw or "0"
    return format_decimal_amount(amount)


def normalize_xray_krc20_amounts(result: dict) -> dict:
    for rel in list(result.get("relationships") or []) + list(result.get("indirectRelationships") or []):
        for move in rel.get("krc20", {}).get("examples", []) or []:
            move["amount"] = format_krc20_amount(move.get("amount"))
            move.pop("amountRaw", None)
    return result


def fetch_address_transactions(address: str, max_tx: int, progress_cb=None) -> list[dict]:
    transactions = []
    before = None
    page = 0
    while len(transactions) < max_tx:
        limit = min(500, max_tx - len(transactions))
        params = {"limit": str(limit), "resolve_previous_outpoints": "light"}
        if before:
            params["before"] = before
        url = f"{KASPA_API}/addresses/{address}/full-transactions-page?{urlencode(params)}"
        data, headers = json_get_with_headers(url, timeout=25)
        if not isinstance(data, list):
            break
        transactions.extend(data)
        page += 1
        if progress_cb:
            progress_cb(f"Fetched {len(transactions):,}/{max_tx:,} full transactions from api.kaspa.org.")
        before = headers.get("x-next-page-before")
        if not data or not before:
            break
        time.sleep(0.12)
    return transactions[:max_tx]


def relation_entry(address: str) -> dict:
    return {
        "address": address,
        "txCount": 0,
        "sentSompi": 0,
        "receivedSompi": 0,
        "coSpendCount": 0,
        "fanoutCount": 0,
        "incomingTxCount": 0,
        "outgoingTxCount": 0,
        "firstSeen": None,
        "lastSeen": None,
        "examples": [],
        "krc20": {},
        "seenTxs": set(),
        "flags": set(),
    }


def touch_relation(rel: dict, tx: dict) -> None:
    timestamp = tx.get("accepting_block_time") or tx.get("block_time")
    if timestamp:
        rel["firstSeen"] = timestamp if rel["firstSeen"] is None else min(rel["firstSeen"], timestamp)
        rel["lastSeen"] = timestamp if rel["lastSeen"] is None else max(rel["lastSeen"], timestamp)
    tx_id = tx.get("transaction_id") or tx.get("hash") or ""
    if tx_id and tx_id not in rel["examples"] and len(rel["examples"]) < 3:
        rel["examples"].append(tx_id)


def add_counterparty(relations: dict, address: str, tx: dict, *, sent=0, received=0, co_spend=False, fanout=False, flags=None) -> None:
    if not address or flags is None:
        flags = set()
    if not address:
        return
    rel = relations.setdefault(address, relation_entry(address))
    tx_id = tx.get("transaction_id") or tx.get("hash") or ""
    if tx_id not in rel["seenTxs"]:
        rel["txCount"] += 1
        if tx_id:
            rel["seenTxs"].add(tx_id)
    rel["sentSompi"] += sent
    rel["receivedSompi"] += received
    if sent:
        rel["outgoingTxCount"] += 1
    if received:
        rel["incomingTxCount"] += 1
    if co_spend:
        rel["coSpendCount"] += 1
    if fanout:
        rel["fanoutCount"] += 1
    rel["flags"].update(flags)
    touch_relation(rel, tx)


def score_relationship(rel: dict, max_volume: int) -> dict:
    volume = rel["sentSompi"] + rel["receivedSompi"]
    tx_score = min(45, rel["txCount"] * 9)
    volume_score = 0 if max_volume <= 0 else min(35, int((volume / max_volume) * 35))
    repeat_score = 20 if rel["sentSompi"] and rel["receivedSompi"] else 0
    interaction = min(100, tx_score + volume_score + repeat_score)

    entity = 0
    if rel["coSpendCount"]:
        entity += min(65, 35 + rel["coSpendCount"] * 10)
    if rel["sentSompi"] and rel["receivedSompi"]:
        entity += 15
    if rel["txCount"] >= 5:
        entity += 10
    if rel["fanoutCount"] >= 3:
        entity = max(0, entity - 15)
    entity = max(0, min(95, entity))

    flags = set(rel["flags"])
    if rel["txCount"] >= 10:
        flags.add("recurring-counterparty")
    if rel["sentSompi"] and rel["receivedSompi"]:
        flags.add("bidirectional-flow")
    if rel["coSpendCount"]:
        flags.add("co-spend-heuristic")
    if rel["fanoutCount"] >= 3:
        flags.add("possible-funding-hub")

    risk = 0
    if "possible-funding-hub" in flags or "high-fan-out" in flags:
        risk += 30
    if "many-inputs" in flags:
        risk += 20
    if "recurring-counterparty" in flags:
        risk += 12
    if "co-spend-heuristic" in flags:
        risk += 10
    if rel["txCount"] >= 20:
        risk += 15
    risk = min(100, risk)

    if "possible-funding-hub" in flags or "high-fan-out" in flags:
        cluster = "hub-or-service"
    elif rel["coSpendCount"] or entity >= 45:
        cluster = "possible-same-entity"
    elif rel["receivedSompi"] and not rel["sentSompi"]:
        cluster = "funding-source"
    elif rel["sentSompi"] and not rel["receivedSompi"]:
        cluster = "recipient"
    elif rel["sentSompi"] and rel["receivedSompi"]:
        cluster = "recurring-flow"
    else:
        cluster = "related"

    rel["interactionStrength"] = interaction
    rel["entityLikelihood"] = entity
    rel["riskScore"] = risk
    rel["cluster"] = cluster
    rel["direction"] = "mixed" if rel["sentSompi"] and rel["receivedSompi"] else ("sent" if rel["sentSompi"] else "received")
    rel["volumeKas"] = format_kas(volume)
    rel["sentKas"] = format_kas(rel["sentSompi"])
    rel["receivedKas"] = format_kas(rel["receivedSompi"])
    rel["flags"] = sorted(flags)
    rel.pop("seenTxs", None)
    return rel


def analyze_direct_relationships(address: str, max_tx: int, progress_cb=None) -> tuple[list[dict], dict]:
    txs = fetch_address_transactions(address, max_tx, progress_cb)
    relations: dict[str, dict] = {}
    totals = {
        "sentSompi": 0,
        "receivedSompi": 0,
        "coSpendEvents": 0,
        "fanoutEvents": 0,
    }

    for tx in txs:
        inputs = tx.get("inputs") or []
        outputs = tx.get("outputs") or []
        input_addresses = [item.get("previous_outpoint_address") for item in inputs if item.get("previous_outpoint_address")]
        output_addresses = [item.get("script_public_key_address") for item in outputs if item.get("script_public_key_address")]
        target_inputs = [item for item in inputs if item.get("previous_outpoint_address") == address]
        target_outputs = [item for item in outputs if item.get("script_public_key_address") == address]
        target_in = sum(sompi(item.get("previous_outpoint_amount")) for item in target_inputs)
        target_out = sum(sompi(item.get("amount")) for item in target_outputs)
        other_inputs = [item for item in inputs if item.get("previous_outpoint_address") and item.get("previous_outpoint_address") != address]
        other_outputs = [item for item in outputs if item.get("script_public_key_address") and item.get("script_public_key_address") != address]
        fanout = len(set(output_addresses)) >= 10 or len(outputs) >= 20
        many_inputs = len(set(input_addresses)) >= 6

        if target_inputs:
            outgoing = sum(sompi(item.get("amount")) for item in other_outputs)
            totals["sentSompi"] += outgoing
            if fanout:
                totals["fanoutEvents"] += 1
            for item in other_outputs:
                counterparty = item.get("script_public_key_address")
                flags = {"high-fan-out"} if fanout else set()
                add_counterparty(relations, counterparty, tx, sent=sompi(item.get("amount")), fanout=fanout, flags=flags)
            for item in other_inputs:
                counterparty = item.get("previous_outpoint_address")
                flags = {"many-inputs"} if many_inputs else set()
                add_counterparty(relations, counterparty, tx, co_spend=True, flags=flags)
                totals["coSpendEvents"] += 1

        if target_outputs and not target_inputs:
            incoming = target_out
            totals["receivedSompi"] += incoming
            total_input = sum(sompi(item.get("previous_outpoint_amount")) for item in other_inputs)
            for item in other_inputs:
                counterparty = item.get("previous_outpoint_address")
                share = sompi(item.get("previous_outpoint_amount"))
                amount = incoming if total_input <= 0 else int(incoming * (share / total_input))
                add_counterparty(relations, counterparty, tx, received=amount)

        if target_inputs and target_outputs and target_out >= target_in * 0.8:
            for item in other_outputs:
                add_counterparty(relations, item.get("script_public_key_address"), tx, flags={"possible-change-pattern"})

    max_volume = max((rel["sentSompi"] + rel["receivedSompi"] for rel in relations.values()), default=0)
    scored = [score_relationship(rel, max_volume) for rel in relations.values()]
    scored.sort(key=lambda item: (item["interactionStrength"], item["volumeKas"], item["txCount"]), reverse=True)
    totals["transactionsScanned"] = len(txs)
    return scored, totals


def collect_krc20_movements(tick: str, watched_addresses: set[str], progress_cb=None, max_pages: int = 250) -> list[dict]:
    if not tick or not watched_addresses:
        return []
    movements = []
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"tick": tick.upper(), "limit": "50"}
        if cursor:
            params["next"] = cursor
        url = f"{KASPLEX_API}/krc20/oplist?{urlencode(params)}"
        try:
            data = json_get(url, timeout=20)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logger.warning("KRC20 overlay failed after %s pages: %s", pages, exc)
            break
        for item in data.get("result", []):
            sender = item.get("from") or ""
            receiver = item.get("to") or ""
            if sender in watched_addresses or receiver in watched_addresses:
                amount = item.get("amt") or item.get("amount") or item.get("balance") or "0"
                movements.append(
                    {
                        "tick": tick.upper(),
                        "from": sender,
                        "to": receiver,
                        "amount": format_krc20_amount(amount),
                        "op": item.get("op") or "",
                        "hash": item.get("hash") or item.get("txid") or item.get("txId") or "",
                    }
                )
        cursor = data.get("next")
        pages += 1
        if progress_cb and pages % 25 == 0:
            progress_cb(f"Scanned {pages * 50:,} KRC20 {tick.upper()} ops for overlay.")
        if not cursor:
            break
        time.sleep(0.12)
    return movements


def collect_krc20_balances(tick: str, watched_addresses: set[str], progress_cb=None) -> dict[str, float]:
    balances: dict[str, float] = {}
    addresses = sorted(watched_addresses)
    if not tick or not addresses:
        return balances
    checked = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        future_map = {pool.submit(get_krc20_address_balance, address, tick): address for address in addresses}
        for future, address in future_map.items():
            try:
                balance = future.result()
            except Exception as exc:
                logger.warning("KRC20 balance lookup failed for %s %s: %s", tick, address, exc)
                balance = 0.0
            if balance > 0:
                balances[address] = balance
            checked += 1
            if progress_cb and checked % 25 == 0:
                progress_cb(f"Checked {checked:,}/{len(addresses):,} visible wallets for {tick.upper()} balances.")
    return balances


def attach_krc20_overlay(relationships: list[dict], movements: list[dict], balances: dict[str, float]) -> dict:
    by_address: dict[str, list[dict]] = {}
    for move in movements:
        for key in ("from", "to"):
            address = move.get(key)
            if address:
                by_address.setdefault(address, []).append(move)
    for rel in relationships:
        moves = by_address.get(rel["address"], [])
        balance = balances.get(rel["address"], 0.0)
        if moves or balance:
            rel["krc20"] = {
                "balance": balance,
                "movementCount": len(moves),
                "examples": moves,
            }
            flags = {"krc20-holder"} if balance else set()
            if moves:
                flags.add("krc20-activity")
            rel["flags"] = sorted(set(rel.get("flags", [])) | flags)
            if rel.get("cluster") == "related":
                rel["cluster"] = "krc20-linked"
    return {
        "movementCount": len(movements),
        "movementAddresses": len(by_address),
        "holderCount": len(balances),
        "holders": [{"address": address, "balance": balance} for address, balance in sorted(balances.items(), key=lambda item: -item[1])[:25]],
    }


def build_clusters(relationships: list[dict]) -> list[dict]:
    clusters: dict[str, dict] = {}
    for rel in relationships:
        cluster_id = rel.get("cluster") or "related"
        cluster = clusters.setdefault(
            cluster_id,
            {
                "id": cluster_id,
                "label": cluster_id.replace("-", " ").title(),
                "count": 0,
                "volumeKas": 0.0,
                "maxRisk": 0,
                "addresses": [],
            },
        )
        cluster["count"] += 1
        cluster["volumeKas"] += float(rel.get("volumeKas") or 0)
        cluster["maxRisk"] = max(cluster["maxRisk"], int(rel.get("riskScore") or 0))
        if len(cluster["addresses"]) < 8:
            cluster["addresses"].append(rel["address"])
    return sorted(clusters.values(), key=lambda item: (item["maxRisk"], item["count"], item["volumeKas"]), reverse=True)


def run_wallet_xray(address: str, max_tx: int, depth: str = "direct", krc20_tick: str = "", progress_cb=None) -> dict:
    scored, raw_totals = analyze_direct_relationships(address, max_tx, progress_cb)
    top = scored[:40]
    indirect_relationships = []
    indirect_edges = []

    if depth == "one-hop":
        seeds = [rel for rel in top[:6] if rel.get("interactionStrength", 0) >= 10]
        for seed_index, seed in enumerate(seeds, start=1):
            if progress_cb:
                progress_cb(f"Expanding one-hop neighborhood {seed_index}/{len(seeds)}: {seed['address']}.")
            try:
                seed_rels, _seed_totals = analyze_direct_relationships(seed["address"], min(150, max(50, max_tx // 5)), None)
            except Exception as exc:
                logger.warning("One-hop expansion failed for %s: %s", seed["address"], exc)
                continue
            for rel in seed_rels[:10]:
                if rel["address"] == address:
                    continue
                rel = dict(rel)
                rel["via"] = seed["address"]
                rel["depth"] = 2
                indirect_relationships.append(rel)
                indirect_edges.append(
                    {
                        "source": seed["address"],
                        "target": rel["address"],
                        "direction": rel["direction"],
                        "weight": max(6, int(rel["interactionStrength"] * 0.55)),
                        "volumeKas": rel["volumeKas"],
                        "txCount": rel["txCount"],
                    }
                )
        indirect_relationships.sort(key=lambda item: (item["interactionStrength"], item["volumeKas"], item["txCount"]), reverse=True)
        indirect_relationships = indirect_relationships[:60]

    krc20_overlay = {}
    if krc20_tick:
        watched = {address}
        watched.update(rel["address"] for rel in top)
        watched.update(rel["address"] for rel in indirect_relationships[:30])
        if progress_cb:
            progress_cb(f"Checking KRC20 {krc20_tick.upper()} balances for visible graph wallets.")
        balances = collect_krc20_balances(krc20_tick, watched, progress_cb)
        if progress_cb:
            progress_cb(f"Scanning recent KRC20 {krc20_tick.upper()} movements for visible graph wallets.")
        movements = collect_krc20_movements(krc20_tick, watched, progress_cb)
        krc20_overlay = attach_krc20_overlay(top, movements, balances)
        attach_krc20_overlay(indirect_relationships, movements, balances)
        krc20_overlay["tick"] = krc20_tick.upper()
        krc20_overlay["targetBalance"] = balances.get(address, 0.0)

    nodes = [{"id": address, "label": "Target wallet", "role": "target", "score": 100}]
    edges = []
    for rel in top:
        nodes.append(
            {
                "id": rel["address"],
                "label": rel["address"],
                "role": "counterparty",
                "score": rel["interactionStrength"],
                "entityLikelihood": rel["entityLikelihood"],
                "riskScore": rel["riskScore"],
                "cluster": rel["cluster"],
                "flags": rel["flags"],
            }
        )
        edges.append(
            {
                "source": address,
                "target": rel["address"],
                "direction": rel["direction"],
                "weight": rel["interactionStrength"],
                "volumeKas": rel["volumeKas"],
                "txCount": rel["txCount"],
            }
        )
    existing_nodes = {node["id"] for node in nodes}
    for rel in indirect_relationships:
        if rel["address"] not in existing_nodes:
            nodes.append(
                {
                    "id": rel["address"],
                    "label": rel["address"],
                    "role": "indirect",
                    "score": rel["interactionStrength"],
                    "entityLikelihood": rel["entityLikelihood"],
                    "riskScore": rel["riskScore"],
                    "cluster": rel["cluster"],
                    "flags": rel["flags"],
                }
            )
            existing_nodes.add(rel["address"])
    edges.extend(indirect_edges)

    clusters = build_clusters(top + indirect_relationships)

    return {
        "address": address,
        "generatedAt": now_iso(),
        "maxTx": max_tx,
        "depth": depth,
        "krc20Tick": krc20_tick.upper() if krc20_tick else "",
        "transactionsScanned": raw_totals["transactionsScanned"],
        "totals": {
            "sentKas": format_kas(raw_totals["sentSompi"]),
            "receivedKas": format_kas(raw_totals["receivedSompi"]),
            "coSpendEvents": raw_totals["coSpendEvents"],
            "fanoutEvents": raw_totals["fanoutEvents"],
        },
        "relationships": top,
        "indirectRelationships": indirect_relationships,
        "clusters": clusters,
        "krc20Overlay": krc20_overlay,
        "nodes": nodes,
        "edges": edges,
        "notes": [
            "Scores are heuristic signals from public transaction data, not identity claims.",
            "Entity likelihood is mainly influenced by co-spend evidence, repeated flows and bidirectional activity.",
            "High fan-out or many-input patterns can indicate services, exchanges, payout tools or other hubs.",
        ],
    }


def format_xray_txt(result: dict) -> str:
    lines = [
        "Wallet X-Ray Report",
        f"Address: {result['address']}",
        f"Generated: {result['generatedAt']}",
        f"Transactions scanned: {result['transactionsScanned']}",
        f"Graph depth: {result.get('depth', 'direct')}",
        f"KRC20 overlay: {result.get('krc20Tick') or 'none'}",
        f"KRC20 visible holders: {result.get('krc20Overlay', {}).get('holderCount', 0)}",
        f"KRC20 recent movements: {result.get('krc20Overlay', {}).get('movementCount', 0)}",
        f"Received: {result['totals']['receivedKas']:,.8f} KAS",
        f"Sent: {result['totals']['sentKas']:,.8f} KAS",
        "",
        "Important: scores are heuristic relationship signals, not real-world identity claims.",
        "=" * 72,
        "",
    ]
    for index, rel in enumerate(result["relationships"], start=1):
        lines.extend(
            [
                f"{index}. {rel['address']}",
                f"   Interaction strength: {rel['interactionStrength']}/100",
                f"   Entity likelihood: {rel['entityLikelihood']}/100",
                f"   Risk score: {rel.get('riskScore', 0)}/100",
                f"   Cluster: {rel.get('cluster', 'related')}",
                f"   Transactions: {rel['txCount']}",
                f"   Volume: {rel['volumeKas']:,.8f} KAS",
                f"   Sent: {rel['sentKas']:,.8f} KAS | Received: {rel['receivedKas']:,.8f} KAS",
                f"   KRC20 balance: {rel.get('krc20', {}).get('balance', 0):,.8f}",
                f"   KRC20 movements: {rel.get('krc20', {}).get('movementCount', 0)}",
                *[
                    f"   KRC20 example: {move.get('tick')} {format_krc20_amount(move.get('amount'))} {move.get('from')} -> {move.get('to')}"
                    for move in rel.get("krc20", {}).get("examples", [])
                ],
                f"   Flags: {', '.join(rel['flags']) if rel['flags'] else 'none'}",
                f"   Evidence txids: {', '.join(rel.get('examples', [])) if rel.get('examples') else 'none'}",
                "",
            ]
        )
    return "\n".join(lines)


def set_job(job_id: str, **updates) -> None:
    snapshot = None
    with jobs_lock:
        jobs[job_id].update(updates)
        jobs[job_id]["updatedAt"] = now_iso()
        snapshot = dict(jobs[job_id])
    persist_vault_job(snapshot)


def run_snapshot(job_id: str, params: dict) -> None:
    def progress(message: str):
        set_job(job_id, progress=message)

    try:
        set_job(job_id, status="running", progress="Snapshot job started.")
        snapshot_type = params["type"]

        if snapshot_type == "xray":
            set_job(job_id, progress="Wallet X-Ray started. Fetching full transactions.")
            result = run_wallet_xray(params["address"], params["maxTx"], params.get("depth", "direct"), params.get("krc20Tick", ""), progress)
            content = format_xray_txt(result)
            filename = f"wallet_xray_{params['address'][-10:]}.txt"
            output_path = RUNTIME_DIR / f"{job_id}.txt"
            result_path = RUNTIME_DIR / f"{job_id}.json"
            output_path.write_text(content, encoding="utf-8")
            result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
            wallet_address = params.get("walletAddress")
            if wallet_address:
                vault = vault_dir(wallet_address)
                shutil.copyfile(output_path, vault / f"{job_id}.txt")
                shutil.copyfile(result_path, vault / f"{job_id}.json")
            set_job(
                job_id,
                status="complete",
                progress="Wallet X-Ray complete.",
                walletCount=len(result["relationships"]),
                filename=filename,
                downloadUrl=f"/api/jobs/{job_id}/download",
                resultUrl=f"/api/jobs/{job_id}/result",
            )
            return

        if snapshot_type == "krc20":
            results = get_krc20_snapshot(params["krc20Tick"], params["krc20Min"], progress)
            content = format_krc20_txt(params["krc20Tick"], params["krc20Min"], results)
            filename = f"snapshot_krc20_{params['krc20Tick']}_min{params['krc20Min']:g}.txt"
        elif snapshot_type == "krc721":
            results = get_krc721_snapshot(params["krc721Tick"], params["krc721Min"], progress)
            content = format_krc721_txt(params["krc721Tick"], params["krc721Min"], results)
            filename = f"snapshot_krc721_{params['krc721Tick']}_min{params['krc721Min']}.txt"
        elif snapshot_type == "multi721":
            all_results = []
            for collection in params["collections"]:
                progress(f"Scanning {collection['tick'].upper()} holders.")
                all_results.append(get_krc721_snapshot(collection["tick"], collection["min"], None))
            qualifying = set(all_results[0].keys())
            for result in all_results[1:]:
                qualifying &= set(result.keys())
            results = {
                addr: {collection["tick"]: all_results[index][addr] for index, collection in enumerate(params["collections"])}
                for addr in qualifying
            }
            content = format_multi_krc721_txt(params["collections"], results)
            filename = "snapshot_multi_krc721.txt"
        else:
            progress(f"Scanning KRC20 {params['krc20Tick'].upper()} holders.")
            krc20_results = get_krc20_snapshot(params["krc20Tick"], params["krc20Min"], progress)
            all_krc721 = []
            for collection in params["collections"]:
                progress(f"Scanning KRC721 {collection['tick'].upper()} holders.")
                all_krc721.append(get_krc721_snapshot(collection["tick"], collection["min"], None))
            qualifying = set(krc20_results.keys())
            for result in all_krc721:
                qualifying &= set(result.keys())
            results = {}
            for addr in qualifying:
                results[addr] = {"krc20": krc20_results[addr]}
                for index, collection in enumerate(params["collections"]):
                    results[addr][collection["tick"]] = all_krc721[index][addr]
            content = format_advanced_txt(params["krc20Tick"], params["krc20Min"], params["collections"], results)
            filename = f"snapshot_advanced_{params['krc20Tick']}.txt"

        output_path = RUNTIME_DIR / f"{job_id}.txt"
        output_path.write_text(content, encoding="utf-8")
        wallet_address = params.get("walletAddress")
        if wallet_address:
            vault_path = vault_dir(wallet_address) / f"{job_id}.txt"
            shutil.copyfile(output_path, vault_path)
        set_job(
            job_id,
            status="complete",
            progress="Snapshot complete.",
            walletCount=len(results),
            filename=filename,
            downloadUrl=f"/api/jobs/{job_id}/download",
        )
    except Exception as exc:
        logger.exception("Snapshot job failed: %s", exc)
        set_job(job_id, status="failed", error=str(exc))


class DevToolsHandler(BaseHTTPRequestHandler):
    server_version = "KaspaDevTools/1.0"

    def client_ip(self) -> str:
        forwarded = self.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            return
        if parsed.path == "/api/config":
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.end_headers()
            return
        if parsed.path.startswith("/api/"):
            self.send_response(404)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.end_headers()
            return
        return self.serve_static(parsed.path, head_only=True)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            return self.send_text(200, "OK")
        if path == "/api/config":
            return self.send_json(
                200,
                {
                    "paymentAddress": PAYMENT_ADDRESS,
                    "priceKas": SNAPSHOT_PRICE_KAS,
                    "advancedKrc721PriceKas": ADVANCED_KRC721_PRICE_KAS,
                    "xrayPriceKas": XRAY_PRICE_KAS,
                    "xrayKrc20PriceKas": XRAY_KRC20_PRICE_KAS,
                    "batchTransferPriceKas": BATCH_TRANSFER_PRICE_KAS,
                    "feeRateSompiPerGram": TOCCATA_FEE_RATE_SOMPI_PER_G,
                },
            )
        if path == "/api/payments/session":
            return self.handle_payment_session(parsed)
        if path == "/api/payments/recover":
            return self.handle_payment_recovery(parsed)
        if path == "/api/batchtransfer/holdings":
            return self.handle_batch_holdings(parsed)
        if path == "/api/batchtransfer/transaction":
            return self.handle_batch_transaction(parsed)
        if path == "/api/kcc721/krc721":
            return self.handle_kcc721_collection(parsed)
        if path == "/api/kcc721/transaction":
            return self.handle_kcc721_transaction(parsed)
        if path == "/api/kcc721/collection":
            return self.handle_kcc721_live_collection(parsed)
        if path == "/api/kcc721/collections":
            return self.handle_kcc721_live_collections(parsed)
        if path == "/api/kcc721/collection-nfts":
            return self.handle_kcc721_collection_nfts(parsed)
        if path == "/api/kcc721/nft-detail":
            return self.handle_kcc721_nft_detail(parsed)
        if path == "/api/kcc721/nft":
            return self.handle_kcc721_live_nft(parsed)
        if path == "/api/kcc721/wallet-nfts":
            return self.handle_kcc721_wallet_nfts(parsed)
        if path == "/api/kcc721/wallet-utxos":
            return self.handle_kcc721_wallet_utxos(parsed)
        if path == "/api/kcc721/mint-queue":
            return self.handle_kcc721_mint_queue(parsed)
        if path == "/api/vault":
            return self.handle_vault(parsed)
        if path.startswith("/api/vault/") and path.endswith("/download"):
            return self.handle_vault_download(parsed)
        if path.startswith("/api/jobs/") and path.endswith("/download"):
            return self.handle_download(path)
        if path.startswith("/api/jobs/") and path.endswith("/result"):
            return self.handle_result(path, parsed)
        if path.startswith("/api/jobs/"):
            return self.handle_job_status(path)
        if path.startswith("/api/"):
            return self.send_json(404, {"error": "Not found."})
        return self.serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/kcc721/plan":
            return self.handle_kcc721_plan()
        if parsed.path == "/api/kcc721/prepare-deploy":
            return self.handle_kcc721_prepare_deploy()
        if parsed.path == "/api/kcc721/prepare-mint":
            return self.handle_kcc721_prepare_mint()
        if parsed.path == "/api/kcc721/prepare-reveal":
            return self.handle_kcc721_prepare_reveal()
        if parsed.path == "/api/kcc721/prepare-migration-issue":
            return self.handle_kcc721_prepare_migration_issue()
        if parsed.path == "/api/kcc721/prepare-transfer":
            return self.handle_kcc721_prepare_transfer()
        if parsed.path == "/api/kcc721/prepare-batch-transfer":
            return self.handle_kcc721_prepare_batch_transfer()
        if parsed.path == "/api/kcc721/register-broadcast":
            return self.handle_kcc721_register_broadcast()
        if parsed.path == "/api/kcc721/cancel-operation":
            return self.handle_kcc721_cancel_operation()
        if parsed.path != "/api/jobs":
            return self.send_json(404, {"error": "Not found."})
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            params = validate_request(payload)
            client_key = self.client_ip()
            wallet_key_value = wallet_key(params.get("walletAddress") or "")
            check_rate_limit(f"job:ip:{client_key}", JOB_CREATE_LIMIT_PER_HOUR)
            check_rate_limit(f"job:wallet:{wallet_key_value}", JOB_CREATE_LIMIT_PER_HOUR)
            if price_for_params(params) <= 0:
                check_rate_limit(f"free:ip:{client_key}", FREE_JOB_LIMIT_PER_HOUR)
                check_rate_limit(f"free:wallet:{wallet_key_value}", FREE_JOB_LIMIT_PER_HOUR)
            job_id = uuid.uuid4().hex
            needs_payment = price_for_params(params) > 0
            with jobs_lock:
                jobs[job_id] = {
                    "id": job_id,
                    "status": "validating_payment" if needs_payment else "queued",
                    "progress": "Validating payment on-chain." if needs_payment else "Queued.",
                    "createdAt": now_iso(),
                    "updatedAt": now_iso(),
                    "type": params["type"],
                    "title": xray_title(params) if params["type"] == "xray" else snapshot_title(params),
                    "summary": xray_summary(params) if params["type"] == "xray" else snapshot_summary(params),
                    "paid": require_payment(params["type"]),
                    "priceKas": price_for_params(params),
                    "paymentTxid": params.get("paymentTxid"),
                    "walletAddress": params.get("walletAddress"),
                    "params": params,
                }
                job_snapshot = dict(jobs[job_id])
            persist_vault_job(job_snapshot)
            submit_job(job_id, params, validate_payment=needs_payment)
            return self.send_json(200, {"jobId": job_id})
        except PaymentRequired as exc:
            return self.send_json(402, {"error": str(exc)})
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except Exception as exc:
            logger.exception("Create job failed: %s", exc)
            return self.send_json(500, {"error": "Internal server error."})

    def handle_kcc721_collection(self, parsed):
        try:
            query = parse_qs(parsed.query)
            tick = clean_kcc721_tick((query.get("ticker") or [""])[0])
            wallet_address = clean_kaspa_address((query.get("walletAddress") or [""])[0])
            check_rate_limit(f"kcc721:lookup:{self.client_ip()}", 30)
            return self.send_json(200, kcc721_migration_preview(tick, wallet_address))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logger.warning("KCC721 source collection lookup failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 indexer is not available right now."})

    def handle_kcc721_plan(self):
        try:
            length = int(self.headers.get("content-length", "0"))
            if length < 1 or length > 32_768:
                raise BadRequest("Invalid request size.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            check_rate_limit(f"kcc721:plan:{self.client_ip()}", 20)
            return self.send_json(200, build_kcc721_plan(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logger.warning("KCC721 plan creation failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 indexer is not available right now."})
        except Exception as exc:
            logger.exception("KCC721 plan creation failed: %s", exc)
            return self.send_json(500, {"error": "Internal server error."})

    def read_kcc721_json_body(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        if length < 1 or length > 131_072:
            raise BadRequest("Invalid request size.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise BadRequest("JSON request must be an object.")
        return payload

    def handle_kcc721_prepare_deploy(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:prepare:{self.client_ip()}", 10)
            return self.send_json(200, prepare_kcc721_deploy(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "KCC721 transaction builder timed out."})
        except Exception as exc:
            logger.exception("KCC721 Mainnet preparation failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 Mainnet preparation failed."})

    def handle_kcc721_prepare_mint(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:mint:{self.client_ip()}", 20)
            return self.send_json(200, prepare_kcc721_mint(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "KCC721 mint builder timed out."})
        except Exception as exc:
            logger.exception("KCC721 Mainnet mint preparation failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 Mainnet mint preparation failed."})

    def handle_kcc721_mint_queue(self, parsed):
        try:
            query = parse_qs(parsed.query)
            operation_id = str((query.get("operationId") or [""])[0]).strip()
            if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                raise BadRequest("Invalid mint queue operation ID.")
            wallet_address = clean_kaspa_address((query.get("walletAddress") or [""])[0])
            check_rate_limit(f"kcc721:mint-queue:{self.client_ip()}", 600)
            return self.send_json(200, get_kcc721_mint_queue_status(operation_id, wallet_address))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            logger.exception("KCC721 mint queue lookup failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 mint queue lookup failed."})

    def handle_kcc721_prepare_reveal(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:reveal:{self.client_ip()}", 30)
            return self.send_json(200, prepare_kcc721_reveal(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "KCC721 reveal builder timed out."})
        except Exception as exc:
            logger.exception("KCC721 Mainnet reveal preparation failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 Mainnet reveal preparation failed."})

    def handle_kcc721_prepare_migration_issue(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:migration-issue:{self.client_ip()}", 30)
            return self.send_json(200, prepare_kcc721_migration_issue(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "KCC721 migration issuance builder timed out."})
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logger.warning("KCC721 migration source validation failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 source indexer is not available right now."})
        except Exception as exc:
            logger.exception("KCC721 migration issuance preparation failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 migration issuance preparation failed."})

    def handle_kcc721_prepare_transfer(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:transfer:{self.client_ip()}", 30)
            return self.send_json(200, prepare_kcc721_transfer(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "KCC721 transfer builder timed out."})
        except Exception as exc:
            logger.exception("KCC721 Mainnet transfer preparation failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 Mainnet transfer preparation failed."})

    def handle_kcc721_prepare_batch_transfer(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:batch-transfer:{self.client_ip()}", 20)
            return self.send_json(200, prepare_kcc721_batch_transfer(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "KCC721 atomic batch builder timed out."})
        except Exception as exc:
            logger.exception("KCC721 Mainnet atomic batch preparation failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 Mainnet atomic batch preparation failed."})

    def handle_kcc721_register_broadcast(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:broadcast:{self.client_ip()}", 20)
            return self.send_json(200, register_kcc721_broadcast(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except sqlite3.IntegrityError:
            return self.send_json(409, {"error": "This KCC721 transaction is already registered."})
        except Exception as exc:
            logger.exception("KCC721 broadcast registration failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 broadcast registration failed."})

    def handle_kcc721_cancel_operation(self):
        try:
            payload = self.read_kcc721_json_body()
            check_rate_limit(f"kcc721:cancel:{self.client_ip()}", 30)
            return self.send_json(200, cancel_kcc721_operation(payload))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "Invalid JSON."})
        except Exception as exc:
            logger.exception("KCC721 operation cancellation failed: %s", exc)
            return self.send_json(500, {"error": "KCC721 operation cancellation failed."})

    def handle_kcc721_transaction(self, parsed):
        try:
            txid = clean_txid((parse_qs(parsed.query).get("txid") or [""])[0])
            data = json_get(f"{KASPA_API}/transactions/{txid}?inputs=true&outputs=true", timeout=12)
            accepted = isinstance(data, dict) and data.get("is_accepted") is True
            operation = db_get_kcc721_operation_by_txid(txid)
            if operation and accepted and operation.get("status") != "accepted":
                operation["status"] = "accepted"
                operation["updatedAt"] = now_iso()
                db_save_kcc721_operation(operation)
                db_index_kcc721_operation(operation)
                operation = db_get_kcc721_operation(operation["id"])
            return self.send_json(
                200,
                {
                    "txid": txid,
                    "accepted": accepted,
                    "status": operation.get("status") if operation else "accepted" if accepted else "pending",
                    "registryError": operation.get("registryError") if operation else None,
                },
            )
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except HTTPError as exc:
            if exc.code in (404, 422):
                return self.send_json(404, {"error": "Transaction not indexed yet."})
            return self.send_json(502, {"error": "Kaspa transaction lookup failed."})
        except (URLError, TimeoutError, ValueError):
            return self.send_json(502, {"error": "Kaspa transaction lookup failed."})

    def handle_kcc721_live_collection(self, parsed):
        try:
            collection_id = clean_txid((parse_qs(parsed.query).get("id") or [""])[0])
            collection = db_get_kcc721_collection(collection_id)
            if not collection or collection.get("status") != "live":
                return self.send_json(404, {"error": "KCC721 collection was not found."})
            migration = kcc721_collection_migration(collection)
            manifest = kcc721_collection_manifest(collection)
            indexed_nfts = db_count_kcc721_collection_nfts(collection_id)
            return self.send_json(
                200,
                {
                    "collectionId": collection["collection_id"],
                    "ticker": collection["ticker"],
                    "deployerAddress": collection["deployer_address"],
                    "maxSupply": collection["max_supply"],
                    "metadataUri": collection["metadata_uri"],
                    "metadataDigest": collection["metadata_digest"],
                    "mintPriceSompi": str(collection["mint_price_sompi"]),
                    "mintDaaScore": str(collection["mint_daa_score"]),
                    "nextTokenId": collection["next_token_id"],
                    "indexedNfts": indexed_nfts,
                    "status": collection["status"],
                    "mode": "migration" if migration else "native",
                    "version": manifest.get("version"),
                    "mintMode": manifest.get("mintMode"),
                    "tokenIdBase": manifest.get("tokenIdBase", 1 if migration else 0),
                    "shuffleRoot": manifest.get("shuffleRoot"),
                    "migration": migration or None,
                },
            )
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})

    def handle_kcc721_live_collections(self, parsed):
        query = parse_qs(parsed.query)
        search = str((query.get("q") or [""])[0]).strip()
        if len(search) > 96:
            return self.send_json(400, {"error": "Indexer search is too long."})
        check_rate_limit(f"kcc721:index:{self.client_ip()}", 120)
        rows = db_list_kcc721_collections(search)
        items = []
        for row in rows:
            migration = kcc721_collection_migration(row)
            manifest = kcc721_collection_manifest(row)
            is_v2 = str(manifest.get("version") or "").startswith("0.2")
            items.append(
                {
                    "collectionId": row["collection_id"],
                    "ticker": row["ticker"],
                    "deployerAddress": row["deployer_address"],
                    "maxSupply": row["max_supply"],
                    "minted": row["indexed_nfts"] if migration else max(0, int(row["next_token_id"]) - 1) if is_v2 else row["next_token_id"],
                    "indexedNfts": row["indexed_nfts"],
                    "metadataUri": row["metadata_uri"],
                    "mintPriceSompi": str(row["mint_price_sompi"]),
                    "controllerTransactionId": row["controller_txid"],
                    "status": row["status"],
                    "mode": "migration" if migration else "native",
                    "version": manifest.get("version"),
                    "mintMode": manifest.get("mintMode"),
                    "migrationPhase": migration.get("status") if migration else None,
                    "sourceTicker": migration.get("sourceTicker") if migration else None,
                    "sourceDeployTransactionId": migration.get("sourceDeployTransactionId") if migration else None,
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                }
            )
        return self.send_json(
            200,
            {
                "count": len(items),
                "items": items,
            },
        )

    def handle_kcc721_live_nft(self, parsed):
        try:
            nft_id = clean_txid((parse_qs(parsed.query).get("id") or [""])[0])
            nft = db_get_kcc721_nft(nft_id)
            if not nft:
                return self.send_json(404, {"error": "KCC721 NFT was not found."})
            collection = db_get_kcc721_collection(nft["collection_id"])
            if not collection or collection.get("status") != "live":
                return self.send_json(404, {"error": "KCC721 NFT was not found."})
            return self.send_json(
                200,
                {
                    "nftId": nft["nft_id"],
                    "collectionId": nft["collection_id"],
                    "tokenId": nft["token_id"],
                    "ownerAddress": nft["owner_address"],
                    "status": nft["status"],
                },
            )
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})

    def handle_kcc721_wallet_nfts(self, parsed):
        try:
            query = parse_qs(parsed.query)
            address = clean_kaspa_address((query.get("address") or [""])[0])
            offset = parse_int_range((query.get("offset") or [""])[0], 0, 0, 10_000_000, "NFT offset")
            limit = parse_int_range((query.get("limit") or [""])[0], 48, 1, 100, "NFT page size")
            check_rate_limit(f"kcc721:wallet:{self.client_ip()}", 500)
            real_total = db_list_kcc721_wallet_nfts(address, 0, 1)[1]
            rows = []
            if offset < real_total:
                real_rows, _ = db_list_kcc721_wallet_nfts(address, offset, limit)
                rows.extend(real_rows)
            remaining_limit = limit - len(rows)
            virtual_offset = max(0, offset - real_total)
            virtual_rows, virtual_total = db_list_kcc721_migration_custody(
                address,
                virtual_offset,
                remaining_limit,
            )
            if remaining_limit > 0:
                rows.extend(virtual_rows)
            total = real_total + virtual_total
            with ThreadPoolExecutor(max_workers=min(8, len(rows) or 1)) as metadata_workers:
                items = list(metadata_workers.map(kcc721_wallet_nft_item, rows))
            next_offset = offset + len(items)
            return self.send_json(
                200,
                {
                    "address": address,
                    "total": total,
                    "items": items,
                    "next": next_offset if next_offset < total else None,
                },
            )
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})

    def handle_kcc721_wallet_utxos(self, parsed):
        try:
            address = clean_kaspa_address((parse_qs(parsed.query).get("address") or [""])[0])
            check_rate_limit(f"kcc721:utxos:{self.client_ip()}", 180)
            data = json_get(f"{KASPA_API}/addresses/{address}/utxos", timeout=12)
            if not isinstance(data, list):
                raise ValueError("Mainnet UTXO response is not a list")
            items = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                outpoint = item.get("outpoint") if isinstance(item.get("outpoint"), dict) else {}
                entry = item.get("utxoEntry") if isinstance(item.get("utxoEntry"), dict) else {}
                script_data = entry.get("scriptPublicKey")
                if isinstance(script_data, dict):
                    script = str(script_data.get("scriptPublicKey") or script_data.get("script") or "")
                    version = int(script_data.get("version") or 0)
                else:
                    script = str(script_data or "")
                    version = 0
                if not re.fullmatch(r"[0-9a-fA-F]{64}", str(outpoint.get("transactionId") or "")):
                    continue
                if not re.fullmatch(r"[0-9a-fA-F]+", script):
                    continue
                items.append(
                    {
                        "outpoint": {
                            "transactionId": str(outpoint["transactionId"]).lower(),
                            "index": int(outpoint.get("index") or 0),
                        },
                        "amount": str(entry.get("amount") or "0"),
                        "scriptPublicKey": {"version": version, "script": script.lower()},
                        "blockDaaScore": str(entry.get("blockDaaScore") or "0"),
                        "isCoinbase": bool(entry.get("isCoinbase")),
                    }
                )
            return self.send_json(200, {"address": address, "items": items})
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logger.warning("KCC721 wallet UTXO lookup failed: %s", exc)
            return self.send_json(502, {"error": "Mainnet wallet UTXOs are temporarily unavailable."})

    def handle_kcc721_collection_nfts(self, parsed):
        try:
            query = parse_qs(parsed.query)
            collection_id = clean_txid((query.get("id") or [""])[0])
            offset = parse_int_range((query.get("offset") or [""])[0], 1, 1, 1_000_001, "NFT offset")
            collection = db_get_kcc721_collection(collection_id)
            if not collection or collection.get("status") != "live":
                return self.send_json(404, {"error": "KCC721 collection was not found."})
            migration = kcc721_collection_migration(collection)
            if not migration:
                max_supply = int(collection["max_supply"])
                end = min(offset + 23, max_supply)

                def native_item(display_number: int) -> dict:
                    token_id = display_number
                    indexed_nft = db_get_kcc721_nft_by_token(collection_id, token_id)
                    metadata_uri = f"{collection['metadata_uri'].rstrip('/')}/{display_number}.json"
                    metadata = {}
                    try:
                        fetched = json_get(ipfs_gateway_url(metadata_uri), timeout=15)
                        if isinstance(fetched, dict):
                            metadata = fetched
                    except (HTTPError, URLError, TimeoutError, ValueError):
                        pass
                    return {
                        "tokenId": str(token_id),
                        "displayNumber": display_number,
                        "name": str(metadata.get("name") or f"{collection['ticker']} #{display_number}"),
                        "owner": indexed_nft.get("owner_address"),
                        "kcc721Owner": indexed_nft.get("owner_address"),
                        "kcc721OwnerType": "wallet" if indexed_nft else "unowned",
                        "kcc721NftId": indexed_nft.get("nft_id"),
                        "kcc721State": "live" if indexed_nft else "not minted",
                        "state": "KCC721 live" if indexed_nft else "Not minted",
                        "imageUrl": ipfs_gateway_url(metadata.get("image")),
                        "metadataUri": metadata_uri,
                        "metadataAvailable": bool(metadata),
                        "detailUrl": f"/kcc721/nft?id={collection_id}&tokenId={token_id}",
                        "migrationStatus": None,
                    }

                display_numbers = list(range(offset, end + 1)) if offset <= max_supply else []
                with ThreadPoolExecutor(max_workers=min(8, len(display_numbers) or 1)) as metadata_workers:
                    items = list(metadata_workers.map(native_item, display_numbers))
                return self.send_json(
                    200,
                    {
                        "items": items,
                        "next": end + 1 if end < max_supply else None,
                        "source": "kcc721",
                    },
                )
            tick = clean_kcc721_tick(migration.get("sourceTicker"))
            check_rate_limit(f"kcc721:nfts:{self.client_ip()}", 180)
            data = json_get(
                f"{KRC721_API}/owners/{tick}?{urlencode({'limit': 48, 'offset': offset})}",
                timeout=30,
            )
            result = data.get("result") if isinstance(data, dict) else []
            if not isinstance(result, list):
                result = []
            items = []
            for nft in result:
                if not isinstance(nft, dict):
                    continue
                token_id = str(nft.get("tokenId") or "").strip()
                if not re.fullmatch(r"[0-9]+", token_id):
                    continue
                status = nft.get("status") if isinstance(nft.get("status"), dict) else {}
                indexed_nft = db_get_kcc721_nft_by_token(collection_id, int(token_id))
                kcc721_owner = indexed_nft.get("owner_address") or collection["deployer_address"]
                items.append(
                    {
                        "tokenId": token_id,
                        "name": f"{tick} #{token_id}",
                        "owner": kcc721_owner,
                        "kcc721Owner": kcc721_owner,
                        "kcc721OwnerType": "wallet" if indexed_nft else "deployer custody",
                        "kcc721NftId": indexed_nft.get("nft_id"),
                        "kcc721State": "live" if indexed_nft else "not issued",
                        "krc721Owner": str(nft.get("owner") or ""),
                        "krc721State": str(status.get("state") or "unknown"),
                        "state": "KCC721 live" if indexed_nft else "KCC721 not issued",
                        "imageUrl": f"https://krc721-cache.kaspa.com/krc721/mainnet/optimized/{tick.lower()}/{token_id}",
                        "detailUrl": f"/kcc721/nft?id={collection_id}&tokenId={token_id}",
                        "migrationStatus": "KCC721 NFT live" if indexed_nft else "source NFT / not yet airdropped",
                    }
                )
            next_value = data.get("next") if isinstance(data, dict) else None
            return self.send_json(
                200,
                {
                    "items": items,
                    "next": int(next_value) if str(next_value or "").isdigit() else None,
                    "source": "krc721",
                    "sourceTicker": tick,
                },
            )
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logger.warning("KCC721 source collection lookup failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 source indexer is not available right now."})

    def handle_kcc721_nft_detail(self, parsed):
        try:
            query = parse_qs(parsed.query)
            collection_id = clean_txid((query.get("id") or [""])[0])
            collection = db_get_kcc721_collection(collection_id)
            if not collection or collection.get("status") != "live":
                return self.send_json(404, {"error": "KCC721 collection was not found."})
            migration = kcc721_collection_migration(collection)
            manifest = kcc721_collection_manifest(collection)
            is_v2 = str(manifest.get("version") or "").startswith("0.2")
            minimum_token_id = 1 if migration or is_v2 else 0
            token_id = parse_int_range(
                (query.get("tokenId") or [""])[0],
                minimum_token_id,
                minimum_token_id,
                1_000_000,
                "Token ID",
            )
            check_rate_limit(f"kcc721:nft:{self.client_ip()}", 180)
            source = {}
            tick = collection["ticker"]
            metadata_number = token_id if is_v2 else token_id + 1
            if migration:
                tick = clean_kcc721_tick(migration.get("sourceTicker"))
                if token_id > int(migration.get("mintedAtPreview") or collection["max_supply"]):
                    raise BadRequest("Token ID exceeds the linked KRC721 minted supply.")
                source_data = json_get(f"{KRC721_API}/nfts/{tick}/{token_id}", timeout=20)
                source = source_data.get("result") if isinstance(source_data, dict) else None
                if not isinstance(source, dict):
                    return self.send_json(404, {"error": "Source KRC721 NFT was not found."})
                metadata_number = token_id
            elif (token_id > int(collection["max_supply"]) if is_v2 else token_id >= int(collection["max_supply"])):
                raise BadRequest("Token ID exceeds the KCC721 maximum supply.")
            metadata_uri = f"{collection['metadata_uri'].rstrip('/')}/{metadata_number}.json"
            metadata_url = ipfs_gateway_url(metadata_uri)
            metadata = {}
            metadata_error = None
            try:
                fetched = json_get(metadata_url, timeout=20)
                if isinstance(fetched, dict):
                    metadata = fetched
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                metadata_error = str(exc)
            status = source.get("status") if isinstance(source.get("status"), dict) else {}
            indexed_nft = db_get_kcc721_nft_by_token(collection_id, token_id)
            utxo_history = db_list_kcc721_nft_history(indexed_nft["nft_id"]) if indexed_nft else []
            kcc721_owner = indexed_nft.get("owner_address")
            if migration and not indexed_nft:
                kcc721_owner = collection["deployer_address"]
            can_migration_issue = bool(
                migration
                and not indexed_nft
                and manifest.get("mintMode") == "migration-merkle-issue"
            )
            return self.send_json(
                200,
                {
                    "collectionId": collection_id,
                    "ticker": tick,
                    "tokenId": str(token_id),
                    "displayNumber": metadata_number,
                    "mode": "migration" if migration else "native",
                    "owner": kcc721_owner,
                    "kcc721Owner": kcc721_owner,
                    "kcc721OwnerType": "wallet" if indexed_nft else "deployer custody" if migration else "unowned",
                    "kcc721NftId": indexed_nft.get("nft_id"),
                    "kcc721State": "live" if indexed_nft else "not issued" if migration else "not minted",
                    "krc721Owner": str(source.get("owner") or "") if migration else None,
                    "krc721State": str(status.get("state") or "unknown") if migration else None,
                    "imageUrl": (
                        f"https://krc721-cache.kaspa.com/krc721/mainnet/optimized/{tick.lower()}/{token_id}"
                        if migration
                        else ipfs_gateway_url(metadata.get("image"))
                    ),
                    "metadataUri": metadata_uri,
                    "metadata": metadata,
                    "metadataAvailable": bool(metadata),
                    "metadataError": metadata_error,
                    "migrationStatus": "source NFT / not yet airdropped" if migration else None,
                    "canMigrationIssue": can_migration_issue,
                    "utxoHistory": utxo_history,
                },
            )
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except HTTPError as exc:
            if exc.code == 404:
                return self.send_json(404, {"error": "Source KRC721 NFT was not found."})
            logger.warning("KCC721 source NFT lookup failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 source indexer is not available right now."})
        except (URLError, TimeoutError, ValueError) as exc:
            logger.warning("KCC721 source NFT lookup failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 source indexer is not available right now."})

    def handle_batch_holdings(self, parsed):
        try:
            query = parse_qs(parsed.query)
            network = str((query.get("network") or ["mainnet"])[0]).strip().lower()
            if network not in ("mainnet", "testnet-10"):
                raise BadRequest("Invalid batch-transfer network.")
            raw_address = str((query.get("address") or [""])[0]).strip().lower()
            if network == "testnet-10":
                if not re.fullmatch(r"kaspatest:[a-z0-9]{61,63}", raw_address):
                    raise BadRequest("Invalid Testnet 10 address.")
                address = raw_address
                api_base = KRC721_T10_API
            else:
                address = clean_kaspa_address(raw_address)
                api_base = KRC721_API
            next_token = str((query.get("next") or [""])[0] or "").strip()
            url = f"{api_base}/address/{address}"
            if next_token:
                url = f"{url}?{urlencode({'offset': next_token})}"
            data = json_get(url, timeout=30)
            items = data.get("result") if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
            return self.send_json(200, {"items": items, "next": data.get("next") if isinstance(data, dict) else ""})
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except HTTPError as exc:
            if exc.code == 404:
                return self.send_json(200, {"items": [], "next": ""})
            logger.warning("KRC721 holdings lookup failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 indexer is not available right now."})
        except (URLError, TimeoutError, ValueError) as exc:
            logger.warning("KRC721 holdings lookup failed: %s", exc)
            return self.send_json(502, {"error": "KRC721 indexer is not available right now."})

    def handle_batch_transaction(self, parsed):
        try:
            query = parse_qs(parsed.query)
            txid = clean_txid((query.get("txid") or [""])[0])
            network = str((query.get("network") or ["mainnet"])[0]).strip().lower()
            if network == "testnet-10":
                try:
                    data = json_get(f"{KRC721_T10_API}/ops/txid/{txid}", timeout=12)
                except HTTPError as exc:
                    if exc.code not in (404, 422):
                        raise
                    chain_data = json_get(
                        f"{KASPA_T10_API}/transactions/{txid}?inputs=false&outputs=false",
                        timeout=12,
                    )
                    return self.send_json(
                        200,
                        {
                            "txid": txid,
                            "indexed": False,
                            "accepted": False,
                            "rejected": False,
                            "onChainAccepted": isinstance(chain_data, dict)
                            and chain_data.get("is_accepted") is True,
                        },
                    )
                result = data.get("result") if isinstance(data, dict) else None
                error = None
                if isinstance(result, dict):
                    error = result.get("opError", result.get("error"))
                return self.send_json(
                    200,
                    {
                        "txid": txid,
                        "indexed": isinstance(result, dict),
                        "accepted": isinstance(result, dict) and error is None,
                        "rejected": isinstance(result, dict) and error is not None,
                        "error": error,
                    },
                )
            if network != "mainnet":
                raise BadRequest("Invalid batch-transfer network.")
            data = json_get(f"{KASPA_API}/transactions/{txid}?inputs=false&outputs=false", timeout=12)
            return self.send_json(
                200,
                {
                    "txid": txid,
                    "indexed": isinstance(data, dict),
                    "accepted": data.get("is_accepted") is not False if isinstance(data, dict) else False,
                },
            )
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except HTTPError as exc:
            if exc.code in (404, 422):
                return self.send_json(404, {"error": "Transaction not indexed yet."})
            logger.warning("Batch transfer transaction lookup failed: %s", exc)
            return self.send_json(502, {"error": "Kaspa transaction lookup failed."})
        except (URLError, TimeoutError, ValueError) as exc:
            logger.warning("Batch transfer transaction lookup failed: %s", exc)
            return self.send_json(502, {"error": "Kaspa transaction lookup failed."})

    def handle_payment_session(self, parsed):
        try:
            query = parse_qs(parsed.query)
            wallet_address = clean_kaspa_address((query.get("walletAddress") or [""])[0])
            amount_kas = float((query.get("amountKas") or [""])[0])
            if amount_kas <= 0 or amount_kas > SNAPSHOT_PRICE_KAS:
                raise BadRequest("Invalid payment amount.")
            return self.send_json(200, create_payment_session(wallet_address, amount_kas))
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "Invalid payment amount."})

    def handle_payment_recovery(self, parsed):
        try:
            query = parse_qs(parsed.query)
            wallet_address = clean_kaspa_address((query.get("walletAddress") or [""])[0])
            amount_kas = float((query.get("amountKas") or [""])[0])
            payment_session = (query.get("paymentSession") or [""])[0]
            if amount_kas <= 0 or amount_kas > SNAPSHOT_PRICE_KAS:
                raise BadRequest("Invalid payment amount.")
            payment_started_at = verify_payment_session(payment_session, wallet_address, amount_kas)
            payment = recover_payment_tx(wallet_address, amount_kas, payment_started_at)
            if not payment:
                return self.send_json(404, {"error": "Matching payment not indexed yet."})
            return self.send_json(200, payment)
        except PaymentRequired as exc:
            return self.send_json(400, {"error": str(exc)})
        except BadRequest as exc:
            return self.send_json(400, {"error": str(exc)})
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "Invalid payment amount."})
        except Exception as exc:
            logger.exception("Payment recovery failed: %s", exc)
            return self.send_json(500, {"error": "Payment recovery failed."})

    def handle_job_status(self, path: str):
        job_id = path.rstrip("/").split("/")[-1]
        with jobs_lock:
            job = dict(jobs.get(job_id) or {})
        if not job:
            job = db_get_job(job_id)
        if not job:
            return self.send_json(404, {"error": "Job not found."})
        return self.send_json(200, public_job(job))

    def handle_vault(self, parsed):
        wallet_address = (parse_qs(parsed.query).get("walletAddress") or [""])[0].strip()
        if not wallet_address:
            return self.send_json(400, {"error": "Wallet address is required."})
        items = read_vault_index(wallet_address)
        with jobs_lock:
            live_jobs = {job_id: dict(job) for job_id, job in jobs.items() if job.get("walletAddress") == wallet_address}
        merged = []
        seen = set()
        for item in items:
            job_id = item.get("id")
            if job_id in live_jobs:
                live = public_job(live_jobs[job_id])
                item = {**item, **live, "downloadUrl": f"/api/vault/{job_id}/download"}
            merged.append(item)
            seen.add(job_id)
        for job_id, job in live_jobs.items():
            if job_id not in seen:
                live = public_job(job)
                live["downloadUrl"] = f"/api/vault/{job_id}/download"
                merged.insert(0, live)
        merged.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
        return self.send_json(200, {"items": merged[:500]})

    def handle_download(self, path: str):
        job_id = path.split("/")[-2]
        with jobs_lock:
            job = dict(jobs.get(job_id) or {})
        if not job:
            job = db_get_job(job_id)
        output_path = RUNTIME_DIR / f"{job_id}.txt"
        if job.get("status") != "complete" or not output_path.exists():
            return self.send_text(404, "Snapshot file not found.")
        if job.get("type") == "xray":
            result_path = RUNTIME_DIR / f"{job_id}.json"
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    data = format_xray_txt(result).encode("utf-8")
                except (OSError, ValueError):
                    data = output_path.read_bytes()
            else:
                data = output_path.read_bytes()
        else:
            data = output_path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.send_header("content-disposition", f"attachment; filename=\"{job.get('filename', 'snapshot.txt')}\"")
        self.end_headers()
        self.wfile.write(data)

    def handle_result(self, path: str, parsed):
        job_id = path.split("/")[-2]
        wallet_address = (parse_qs(parsed.query).get("walletAddress") or [""])[0].strip()
        with jobs_lock:
            job = dict(jobs.get(job_id) or {})
        if not job:
            job = db_get_job(job_id)
        output_path = RUNTIME_DIR / f"{job_id}.json"
        if wallet_address:
            vault_path = vault_dir(wallet_address) / f"{job_id}.json"
            if vault_path.exists():
                output_path = vault_path
        if job and job.get("status") != "complete":
            return self.send_json(409, {"error": "Result is not ready yet."})
        if not output_path.exists():
            return self.send_json(404, {"error": "Result not found."})
        try:
            result = normalize_xray_krc20_amounts(json.loads(output_path.read_text(encoding="utf-8")))
            data = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
        except (OSError, ValueError):
            data = output_path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_vault_download(self, parsed):
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 4:
            return self.send_text(404, "Snapshot file not found.")
        job_id = parts[2]
        wallet_address = (parse_qs(parsed.query).get("walletAddress") or [""])[0].strip()
        if not wallet_address:
            return self.send_text(400, "Wallet address is required.")
        items = read_vault_index(wallet_address)
        item = next((entry for entry in items if entry.get("id") == job_id), None)
        if not item or item.get("status") != "complete":
            return self.send_text(404, "Snapshot file not found.")
        output_path = vault_dir(wallet_address) / f"{job_id}.txt"
        if not output_path.exists():
            with jobs_lock:
                live_job = dict(jobs.get(job_id) or {})
            fallback = RUNTIME_DIR / f"{job_id}.txt"
            if live_job.get("walletAddress") == wallet_address and fallback.exists():
                output_path = fallback
            else:
                return self.send_text(404, "Snapshot file not found.")
        if item.get("type") == "xray":
            result_path = vault_dir(wallet_address) / f"{job_id}.json"
            if not result_path.exists():
                result_path = RUNTIME_DIR / f"{job_id}.json"
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    data = format_xray_txt(result).encode("utf-8")
                except (OSError, ValueError):
                    data = output_path.read_bytes()
            else:
                data = output_path.read_bytes()
        else:
            data = output_path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.send_header("content-disposition", f"attachment; filename=\"{item.get('filename', 'snapshot.txt')}\"")
        self.end_headers()
        self.wfile.write(data)

    def serve_static(self, path: str, head_only: bool = False):
        if path == "/":
            file_path = PUBLIC_DIR / "index.html"
        elif path in ("/batchtransfer", "/batchtransfer/"):
            file_path = PUBLIC_DIR / "batchtransfer.html"
        elif path in ("/batchtransfer-t10", "/batchtransfer-t10/"):
            file_path = PUBLIC_DIR / "batchtransfer-t10.html"
        elif path in ("/kcc721", "/kcc721/"):
            file_path = PUBLIC_DIR / "kcc721.html"
        elif path in ("/kcc721/collection", "/kcc721/collection/"):
            file_path = PUBLIC_DIR / "kcc721-collection.html"
        elif path in ("/kcc721/nft", "/kcc721/nft/"):
            file_path = PUBLIC_DIR / "kcc721-nft.html"
        elif path == "/favicon.ico":
            file_path = PUBLIC_DIR / "assets" / "devtools-logo-uploaded.png"
        else:
            relative = Path(path.lstrip("/"))
            if ".." in relative.parts:
                return self.send_text(403, "Forbidden.")
            if any(part.startswith(".") for part in relative.parts):
                return self.send_text(404, "Not found.")
            file_path = PUBLIC_DIR / relative
            if not file_path.exists() or file_path.is_dir():
                return self.send_text(404, "Not found.")
        data = file_path.read_bytes()
        mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("content-type", mime_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def send_json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, status: int, text: str):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def main():
    init_db()
    history_count = backfill_kcc721_nft_history()
    logger.info("KCC721 UTXO history ready (%s indexed lineage entries added).", history_count)
    migrate_vault_indexes_to_db()
    migrate_job_payments_to_db()
    migrate_orphan_jobs_to_single_wallet()
    resumable_jobs = load_jobs_from_db()
    for job_id, params, validate_payment in resumable_jobs:
        if submit_job(job_id, params, validate_payment=validate_payment):
            logger.info("Resumed queued job %s after startup.", job_id)
    threading.Thread(target=kcc721_indexer_loop, name="kcc721-indexer", daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DevToolsHandler)
    logger.info("Kaspa Dev Tools running on http://127.0.0.1:%s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
