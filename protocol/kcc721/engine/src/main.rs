use std::io::{self, Read};

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use kaspa_addresses::{Address, Prefix, Version as AddressVersion};
use kaspa_consensus_client::string::SerializableTransaction;
use kaspa_consensus_core::{
    config::params::MAINNET_PARAMS,
    constants::TX_VERSION_TOCCATA,
    hashing::covenant_id::covenant_id,
    mass::{ComputeBudget, MassCalculator},
    subnets::SUBNETWORK_ID_NATIVE,
    tx::{
        ComputeCommit, CovenantBinding, SignableTransaction, Transaction, TransactionInput,
        TransactionOutpoint, TransactionOutput, UtxoEntry,
    },
};
use kaspa_txscript::{
    EngineFlags, pay_to_address_script, pay_to_script_hash_script, script_builder::ScriptBuilder,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use silverscript_lang::{
    ast::Expr,
    compiler::{
        CompileOptions, CompiledContract, CovenantDeclCallOptions, compile_contract, struct_object,
    },
};

const NFT_CELL_VALUE: u64 = 50_000_000;
const CONTROLLER_CELL_VALUE: u64 = 50_000_000;
const BLIND_TICKET_VALUE: u64 = 60_000_000;
const FEE_RATE_SOMPI_PER_GRAM: u64 = 100;
const P2PK_COMPUTE_BUDGET: u16 = 10;
const COVENANT_COMPUTE_BUDGET: u16 = 40;
const MAX_GENESIS_PREMINT: u64 = 3;
const SIGNATURE_SCRIPT_ESTIMATE: usize = 66;

const NFT_SOURCE: &str = include_str!("../../kcc721-nft.sil");
const COLLECTION_SOURCE: &str = include_str!("../../kcc721-collection.sil");
const V2_NFT_SOURCE: &str = include_str!("../../kcc721-v2-nft.sil");
const V2_TICKET_SOURCE: &str = include_str!("../../kcc721-v2-ticket.sil");
const V2_COLLECTION_SOURCE: &str = include_str!("../../kcc721-v2-collection.sil");
const V2_MIGRATION_SOURCE: &str = include_str!("../../kcc721-v2-migration.sil");

#[derive(Parser)]
#[command(
    name = "kcc721-engine",
    about = "Experimental KCC721 Mainnet transaction builder"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    CompileInfo,
    PrepareDeploy,
    PrepareMint,
    PrepareTransfer,
    PrepareV2Deploy,
    PrepareV2Commit,
    PrepareV2Reveal,
    PrepareV2Transfer,
    PrepareV2MigrationDeploy,
    PrepareV2MigrationIssue,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FundingUtxo {
    transaction_id: String,
    index: u32,
    amount: String,
    script_public_key: String,
    block_daa_score: String,
    #[serde(default)]
    is_coinbase: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct DeployRequest {
    public_key: String,
    ticker: String,
    supply: u64,
    metadata_uri: String,
    mint_price_sompi: String,
    mint_daa_score: String,
    #[serde(default)]
    premint: u64,
    funding_utxo: FundingUtxo,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct MigrationRequest {
    source_protocol: String,
    source_network: String,
    source_ticker: String,
    source_deploy_transaction_id: String,
    source_deployer: String,
    source_royalty_sompi: String,
    source_premint: u64,
    source_mint_daa_score: u64,
    minted_at_preview: u64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct MintRequest {
    collection_id: String,
    deployer_public_key: String,
    recipient_public_key: String,
    supply: u64,
    metadata_uri: String,
    mint_price_sompi: String,
    mint_daa_score: String,
    next_token_id: u64,
    controller_utxo: FundingUtxo,
    funding_utxo: FundingUtxo,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct V2DeployRequest {
    public_key: String,
    ticker: String,
    supply: u64,
    metadata_uri: String,
    shuffle_root: String,
    mint_price_sompi: String,
    mint_daa_score: String,
    funding_utxo: FundingUtxo,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct V2CommitRequest {
    collection_id: String,
    deployer_public_key: String,
    recipient_public_key: String,
    supply: u64,
    metadata_uri: String,
    shuffle_root: String,
    mint_price_sompi: String,
    mint_daa_score: String,
    next_mint_index: u64,
    controller_utxo: FundingUtxo,
    funding_utxo: FundingUtxo,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct V2RevealRequest {
    collection_id: String,
    recipient_public_key: String,
    supply: u64,
    metadata_uri: String,
    shuffle_root: String,
    mint_index: u64,
    token_id: u64,
    salt: String,
    siblings: Vec<String>,
    directions: Vec<u8>,
    ticket_id: String,
    ticket_utxo: FundingUtxo,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct V2MigrationDeployRequest {
    public_key: String,
    ticker: String,
    supply: u64,
    metadata_uri: String,
    unissued_root: String,
    migration: MigrationRequest,
    funding_utxo: FundingUtxo,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct V2MigrationIssueRequest {
    collection_id: String,
    deployer_public_key: String,
    recipient_address: String,
    supply: u64,
    metadata_uri: String,
    current_unissued_root: String,
    next_unissued_root: String,
    remaining: u64,
    token_id: u64,
    siblings: Vec<String>,
    directions: Vec<u8>,
    controller_utxo: FundingUtxo,
    funding_utxo: FundingUtxo,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TransferRequest {
    collection_id: String,
    nft_id: String,
    token_id: u64,
    metadata_uri: String,
    current_owner_public_key: String,
    recipient_address: String,
    nft_utxo: FundingUtxo,
    funding_utxo: FundingUtxo,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CompileInfo {
    protocol: &'static str,
    version: &'static str,
    compiler_version: String,
    nft_program_bytes: usize,
    nft_template_hash: String,
    fee_rate_sompi_per_gram: u64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DeployResponse {
    protocol: &'static str,
    network: &'static str,
    transaction_kind: &'static str,
    tx_json_string: String,
    sign_inputs: Vec<SignInput>,
    transaction_id: String,
    collection_id: String,
    premint_nft_ids: Vec<String>,
    owner_address: String,
    fee_sompi: String,
    compute_mass: u64,
    transient_mass: u64,
    normalized_fee_mass: u64,
    storage_mass: u64,
    controller_value_sompi: String,
    nft_value_sompi: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct MintResponse {
    protocol: &'static str,
    network: &'static str,
    transaction_kind: &'static str,
    tx_json_string: String,
    sign_inputs: Vec<SignInput>,
    transaction_id: String,
    collection_id: String,
    nft_id: String,
    token_id: u64,
    recipient_address: String,
    fee_sompi: String,
    compute_mass: u64,
    transient_mass: u64,
    normalized_fee_mass: u64,
    storage_mass: u64,
    nft_value_sompi: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct V2CommitResponse {
    protocol: &'static str,
    version: &'static str,
    network: &'static str,
    transaction_kind: &'static str,
    tx_json_string: String,
    sign_inputs: Vec<SignInput>,
    transaction_id: String,
    collection_id: String,
    ticket_id: String,
    mint_index: u64,
    recipient_address: String,
    fee_sompi: String,
    storage_mass: u64,
    ticket_value_sompi: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct V2RevealResponse {
    protocol: &'static str,
    version: &'static str,
    network: &'static str,
    transaction_kind: &'static str,
    tx_json_string: String,
    sign_inputs: Vec<SignInput>,
    transaction_id: String,
    collection_id: String,
    ticket_id: String,
    nft_id: String,
    mint_index: u64,
    token_id: u64,
    recipient_address: String,
    fee_sompi: String,
    storage_mass: u64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct TransferResponse {
    protocol: &'static str,
    network: &'static str,
    transaction_kind: &'static str,
    tx_json_string: String,
    sign_inputs: Vec<SignInput>,
    transaction_id: String,
    collection_id: String,
    nft_id: String,
    token_id: u64,
    previous_owner_address: String,
    recipient_address: String,
    recipient_public_key: String,
    fee_sompi: String,
    compute_mass: u64,
    transient_mass: u64,
    normalized_fee_mass: u64,
    storage_mass: u64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct SignInput {
    index: usize,
    sighash_type: u8,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct GenesisDescriptor<'a> {
    protocol: &'static str,
    version: &'static str,
    ticker: &'a str,
    max_supply: u64,
    metadata_uri: &'a str,
    metadata_digest: String,
    mint_price_sompi: String,
    mint_daa_score: String,
    premint_allocation: u64,
    deployer_pubkey: String,
}

#[derive(Debug, Serialize)]
struct V2GenesisDescriptor<'a> {
    protocol: &'static str,
    version: &'static str,
    ticker: &'a str,
    max_supply: u64,
    metadata_uri: &'a str,
    metadata_digest: String,
    shuffle_root: &'a str,
    mint_price_sompi: String,
    mint_daa_score: String,
    token_id_base: u8,
    mint_mode: &'static str,
    deployer_pubkey: String,
}

struct TemplateParts {
    prefix: Vec<u8>,
    suffix: Vec<u8>,
    hash: [u8; 32],
}

fn main() -> Result<()> {
    match Cli::parse().command {
        Command::CompileInfo => println!("{}", serde_json::to_string_pretty(&compile_info()?)?),
        Command::PrepareDeploy => {
            let request: DeployRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_deploy(request)?)?);
        }
        Command::PrepareMint => {
            let request: MintRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_mint(request)?)?);
        }
        Command::PrepareTransfer => {
            let request: TransferRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_transfer(request)?)?);
        }
        Command::PrepareV2Deploy => {
            let request: V2DeployRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_v2_deploy(request)?)?);
        }
        Command::PrepareV2Commit => {
            let request: V2CommitRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_v2_commit(request)?)?);
        }
        Command::PrepareV2Reveal => {
            let request: V2RevealRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_v2_reveal(request)?)?);
        }
        Command::PrepareV2Transfer => {
            let request: TransferRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_v2_transfer(request)?)?);
        }
        Command::PrepareV2MigrationDeploy => {
            let request: V2MigrationDeployRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_v2_migration_deploy(request)?)?);
        }
        Command::PrepareV2MigrationIssue => {
            let request: V2MigrationIssueRequest = read_stdin_json()?;
            println!("{}", serde_json::to_string(&prepare_v2_migration_issue(request)?)?);
        }
    }
    Ok(())
}

fn decode_bytes32(value: &str, label: &str) -> Result<[u8; 32]> {
    hex::decode(value)
        .with_context(|| format!("{label} must be hexadecimal"))?
        .try_into()
        .map_err(|_| anyhow::anyhow!("{label} must be 32 bytes"))
}

fn prepare_v2_deploy(request: V2DeployRequest) -> Result<DeployResponse> {
    if request.supply == 0 || request.supply > i64::MAX as u64 {
        bail!("supply must be between 1 and the covenant integer maximum");
    }
    if !request.metadata_uri.starts_with("ipfs://") || request.metadata_uri.len() > 256 {
        bail!("metadataUri must be an immutable ipfs:// URI");
    }
    let owner = decode_pubkey(&request.public_key, "publicKey")?;
    let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &owner);
    let owner_spk = pay_to_address_script(&owner_address);
    let funding = parse_funding(&request.funding_utxo)?;
    if funding.script_public_key != owner_spk {
        bail!("funding UTXO does not belong to the supplied Kasware public key");
    }
    let shuffle_root = decode_bytes32(&request.shuffle_root, "shuffleRoot")?;
    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let mint_price = request.mint_price_sompi.parse::<u64>()?;
    let mint_daa = request.mint_daa_score.parse::<u64>()?;
    let nft_template = v2_nft_template_parts()?;
    let ticket_template = v2_ticket_template_parts(&nft_template)?;
    let controller = compile_v2_collection(
        owner,
        request.supply,
        metadata_digest,
        shuffle_root,
        mint_price,
        mint_daa,
        1,
        &ticket_template,
    )?;
    let outpoint = TransactionOutpoint::new(
        request.funding_utxo.transaction_id.parse()?,
        request.funding_utxo.index,
    );
    let unbound = TransactionOutput::new(
        CONTROLLER_CELL_VALUE,
        pay_to_script_hash_script(&controller.script),
    );
    let collection_id = covenant_id(outpoint, std::iter::once((0u32, &unbound)));
    let output = TransactionOutput::with_covenant(
        CONTROLLER_CELL_VALUE,
        unbound.script_public_key,
        Some(CovenantBinding::new(0, collection_id)),
    );
    let descriptor = V2GenesisDescriptor {
        protocol: "kcc-721",
        version: "0.2.0",
        ticker: &request.ticker,
        max_supply: request.supply,
        metadata_uri: &request.metadata_uri,
        metadata_digest: hex::encode(metadata_digest),
        shuffle_root: &request.shuffle_root,
        mint_price_sompi: mint_price.to_string(),
        mint_daa_score: mint_daa.to_string(),
        token_id_base: 1,
        mint_mode: "commit-reveal",
        deployer_pubkey: hex::encode(owner),
    };
    let prepared = build_funded_transaction(
        outpoint,
        funding,
        vec![output],
        owner_spk,
        serde_json::to_vec(&descriptor)?,
        CONTROLLER_CELL_VALUE,
    )?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?
        .serialize_to_json()?;
    Ok(DeployResponse {
        protocol: "kcc-721",
        network: "mainnet",
        transaction_kind: "collection-genesis",
        tx_json_string: safe,
        sign_inputs: vec![SignInput { index: 0, sighash_type: 1 }],
        transaction_id: prepared.signable.tx.id().to_string(),
        collection_id: collection_id.to_string(),
        premint_nft_ids: vec![],
        owner_address: owner_address.to_string(),
        fee_sompi: prepared.fee.to_string(),
        compute_mass: prepared.compute_mass,
        transient_mass: prepared.transient_mass,
        normalized_fee_mass: prepared.normalized_fee_mass,
        storage_mass: prepared.storage_mass,
        controller_value_sompi: CONTROLLER_CELL_VALUE.to_string(),
        nft_value_sompi: NFT_CELL_VALUE.to_string(),
    })
}

fn prepare_v2_migration_deploy(request: V2MigrationDeployRequest) -> Result<DeployResponse> {
    if request.supply == 0 || request.supply > i64::MAX as u64 {
        bail!("supply must be between 1 and the covenant integer maximum");
    }
    if !request.metadata_uri.starts_with("ipfs://") || request.metadata_uri.len() > 512 {
        bail!("metadataUri must be an immutable ipfs:// URI");
    }
    if request.migration.source_protocol != "krc-721"
        || request.migration.source_network != "mainnet"
        || request.migration.source_ticker != request.ticker
        || request.migration.minted_at_preview != request.supply
    {
        bail!("migration descriptor does not match the complete KRC721 source collection");
    }
    let owner = decode_pubkey(&request.public_key, "publicKey")?;
    let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &owner);
    if request.migration.source_deployer != owner_address.to_string() {
        bail!("migration source deployer must match the deployment key");
    }
    let owner_spk = pay_to_address_script(&owner_address);
    let funding = parse_funding(&request.funding_utxo)?;
    if funding.script_public_key != owner_spk {
        bail!("funding UTXO does not belong to the supplied Kasware public key");
    }
    let unissued_root = decode_bytes32(&request.unissued_root, "unissuedRoot")?;
    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let nft_template = v2_nft_template_parts()?;
    let controller = compile_v2_migration(
        owner,
        request.supply,
        metadata_digest,
        unissued_root,
        request.supply,
        &nft_template,
    )?;
    let outpoint = TransactionOutpoint::new(
        request.funding_utxo.transaction_id.parse()?,
        request.funding_utxo.index,
    );
    let unbound = TransactionOutput::new(
        CONTROLLER_CELL_VALUE,
        pay_to_script_hash_script(&controller.script),
    );
    let collection_id = covenant_id(outpoint, std::iter::once((0u32, &unbound)));
    let output = TransactionOutput::with_covenant(
        CONTROLLER_CELL_VALUE,
        unbound.script_public_key,
        Some(CovenantBinding::new(0, collection_id)),
    );
    let descriptor = serde_json::json!({
        "protocol": "kcc-721",
        "version": "0.2.0",
        "ticker": request.ticker,
        "max_supply": request.supply,
        "metadata_uri": request.metadata_uri,
        "metadata_digest": hex::encode(metadata_digest),
        "token_id_base": 1,
        "mint_mode": "migration-merkle-issue",
        "unissued_root": request.unissued_root,
        "deployer_pubkey": hex::encode(owner),
        "migration": {
            "source_protocol": request.migration.source_protocol,
            "source_network": request.migration.source_network,
            "source_ticker": request.migration.source_ticker,
            "source_deploy_txid": request.migration.source_deploy_transaction_id,
            "source_deployer": request.migration.source_deployer,
            "source_royalty_sompi": request.migration.source_royalty_sompi,
            "source_premint": request.migration.source_premint,
            "source_mint_daa_score": request.migration.source_mint_daa_score,
            "minted_at_deployment": request.migration.minted_at_preview,
        }
    });
    let prepared = build_funded_transaction(
        outpoint,
        funding,
        vec![output],
        owner_spk,
        serde_json::to_vec(&descriptor)?,
        CONTROLLER_CELL_VALUE,
    )?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?
        .serialize_to_json()?;
    Ok(DeployResponse {
        protocol: "kcc-721",
        network: "mainnet",
        transaction_kind: "migration-genesis",
        tx_json_string: safe,
        sign_inputs: vec![SignInput { index: 0, sighash_type: 1 }],
        transaction_id: prepared.signable.tx.id().to_string(),
        collection_id: collection_id.to_string(),
        premint_nft_ids: vec![],
        owner_address: owner_address.to_string(),
        fee_sompi: prepared.fee.to_string(),
        compute_mass: prepared.compute_mass,
        transient_mass: prepared.transient_mass,
        normalized_fee_mass: prepared.normalized_fee_mass,
        storage_mass: prepared.storage_mass,
        controller_value_sompi: CONTROLLER_CELL_VALUE.to_string(),
        nft_value_sompi: NFT_CELL_VALUE.to_string(),
    })
}

fn prepare_v2_migration_issue(request: V2MigrationIssueRequest) -> Result<MintResponse> {
    if request.token_id < 1 || request.token_id > request.supply || request.remaining == 0 {
        bail!("tokenId must be an unissued ID within 1..maxSupply");
    }
    if request.siblings.len() != request.directions.len() || request.siblings.len() > 32 {
        bail!("Merkle siblings and directions must have the same supported length");
    }
    if request.directions.iter().any(|direction| *direction > 1) {
        bail!("Merkle directions must contain only 0 or 1");
    }
    let collection_id: kaspa_consensus_core::Hash = request.collection_id.parse()?;
    let deployer = decode_pubkey(&request.deployer_public_key, "deployerPublicKey")?;
    let deployer_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &deployer);
    let recipient_address = Address::try_from(request.recipient_address.as_str())
        .context("recipientAddress is invalid")?;
    if recipient_address.prefix != Prefix::Mainnet || recipient_address.version != AddressVersion::PubKey {
        bail!("recipientAddress must be a Mainnet P2PK address");
    }
    let recipient: [u8; 32] = recipient_address.payload.as_slice().try_into()
        .map_err(|_| anyhow::anyhow!("recipientAddress must contain a 32-byte x-only public key"))?;
    let current_root = decode_bytes32(&request.current_unissued_root, "currentUnissuedRoot")?;
    let next_root = decode_bytes32(&request.next_unissued_root, "nextUnissuedRoot")?;
    let siblings: Vec<[u8; 32]> = request.siblings.iter()
        .map(|value| decode_bytes32(value, "Merkle sibling"))
        .collect::<Result<_>>()?;
    let mut live_data = request.token_id.to_le_bytes().to_vec();
    live_data.push(1);
    let mut spent_data = request.token_id.to_le_bytes().to_vec();
    spent_data.push(0);
    let mut live_node: [u8; 32] = Sha256::digest(&live_data).into();
    let mut spent_node: [u8; 32] = Sha256::digest(&spent_data).into();
    for (sibling, direction) in siblings.iter().zip(request.directions.iter()) {
        let (live_branch, spent_branch) = if *direction == 0 {
            ([live_node.as_slice(), sibling.as_slice()].concat(), [spent_node.as_slice(), sibling.as_slice()].concat())
        } else {
            ([sibling.as_slice(), live_node.as_slice()].concat(), [sibling.as_slice(), spent_node.as_slice()].concat())
        };
        live_node = Sha256::digest(live_branch).into();
        spent_node = Sha256::digest(spent_branch).into();
    }
    if live_node != current_root || spent_node != next_root {
        bail!("migration issuance proof does not match the controller roots");
    }
    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let nft_template = v2_nft_template_parts()?;
    let current = compile_v2_migration(
        deployer, request.supply, metadata_digest, current_root, request.remaining, &nft_template,
    )?;
    let next = compile_v2_migration(
        deployer, request.supply, metadata_digest, next_root, request.remaining - 1, &nft_template,
    )?;
    let controller_entry = parse_funding_with_covenant(&request.controller_utxo, Some(collection_id))?;
    if controller_entry.script_public_key != pay_to_script_hash_script(&current.script)
        || controller_entry.amount != CONTROLLER_CELL_VALUE
    {
        bail!("controller UTXO does not match the migration issuance state");
    }
    let funding_entry = parse_funding(&request.funding_utxo)?;
    if funding_entry.script_public_key != pay_to_address_script(&deployer_address) {
        bail!("funding UTXO must belong to the migration deployer");
    }
    let next_state = struct_object(vec![
        ("deployer", Expr::bytes(deployer.to_vec())),
        ("maxSupply", Expr::int(i64::try_from(request.supply)?)),
        ("metadataDigest", Expr::bytes(metadata_digest.to_vec())),
        ("unissuedRoot", Expr::bytes(next_root.to_vec())),
        ("remaining", Expr::int(i64::try_from(request.remaining - 1)?)),
    ]);
    let mut witness = current.build_sig_script_for_covenant_decl(
        "issue",
        vec![
            next_state,
            Expr::int(i64::try_from(request.token_id)?),
            Expr::bytes(recipient.to_vec()),
            Expr::int(1),
            Expr::int(1),
            Expr::from(siblings.iter().map(|value| value.to_vec()).collect::<Vec<_>>()),
            Expr::bytes(request.directions.clone()),
        ],
        CovenantDeclCallOptions { is_leader: true },
    ).map_err(|error| anyhow::anyhow!("cannot build migration issue witness: {error}"))?;
    witness.extend_from_slice(&ScriptBuilder::with_flags(EngineFlags { covenants_enabled: true, ..Default::default() })
        .add_data(&current.script)?.drain());
    let controller_outpoint = TransactionOutpoint::new(
        request.controller_utxo.transaction_id.parse()?, request.controller_utxo.index,
    );
    let nft = compile_v2_nft(collection_id.as_bytes(), request.token_id, metadata_digest, recipient)?;
    let nft_unbound = TransactionOutput::new(NFT_CELL_VALUE, pay_to_script_hash_script(&nft.script));
    let nft_id = covenant_id(controller_outpoint, std::iter::once((1u32, &nft_unbound)));
    let outputs = vec![
        TransactionOutput::with_covenant(
            CONTROLLER_CELL_VALUE, pay_to_script_hash_script(&next.script), Some(CovenantBinding::new(0, collection_id)),
        ),
        TransactionOutput::with_covenant(
            NFT_CELL_VALUE, nft_unbound.script_public_key, Some(CovenantBinding::new(0, nft_id)),
        ),
    ];
    let prepared = build_mint_transaction(
        controller_outpoint,
        controller_entry,
        witness,
        TransactionOutpoint::new(request.funding_utxo.transaction_id.parse()?, request.funding_utxo.index),
        funding_entry,
        outputs,
        pay_to_address_script(&deployer_address),
        0,
        0,
    )?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?.serialize_to_json()?;
    Ok(MintResponse {
        protocol: "kcc-721",
        network: "mainnet",
        transaction_kind: "migration-issue",
        tx_json_string: safe,
        sign_inputs: vec![SignInput { index: 1, sighash_type: 1 }],
        transaction_id: prepared.signable.tx.id().to_string(),
        collection_id: collection_id.to_string(),
        nft_id: nft_id.to_string(),
        token_id: request.token_id,
        recipient_address: recipient_address.to_string(),
        fee_sompi: prepared.fee.to_string(),
        compute_mass: prepared.compute_mass,
        transient_mass: prepared.transient_mass,
        normalized_fee_mass: prepared.normalized_fee_mass,
        storage_mass: prepared.storage_mass,
        nft_value_sompi: NFT_CELL_VALUE.to_string(),
    })
}

fn prepare_v2_commit(request: V2CommitRequest) -> Result<V2CommitResponse> {
    if request.next_mint_index < 1 || request.next_mint_index > request.supply {
        bail!("nextMintIndex must be within 1..maxSupply");
    }
    let collection_id = request.collection_id.parse()?;
    let deployer = decode_pubkey(&request.deployer_public_key, "deployerPublicKey")?;
    let recipient = decode_pubkey(&request.recipient_public_key, "recipientPublicKey")?;
    let shuffle_root = decode_bytes32(&request.shuffle_root, "shuffleRoot")?;
    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let mint_price = request.mint_price_sompi.parse::<u64>()?;
    let mint_daa = request.mint_daa_score.parse::<u64>()?;
    let nft_template = v2_nft_template_parts()?;
    let ticket_template = v2_ticket_template_parts(&nft_template)?;
    let current = compile_v2_collection(
        deployer, request.supply, metadata_digest, shuffle_root, mint_price, mint_daa,
        request.next_mint_index, &ticket_template,
    )?;
    let next = compile_v2_collection(
        deployer, request.supply, metadata_digest, shuffle_root, mint_price, mint_daa,
        request.next_mint_index + 1, &ticket_template,
    )?;
    let controller_entry = parse_funding_with_covenant(&request.controller_utxo, Some(collection_id))?;
    if controller_entry.script_public_key != pay_to_script_hash_script(&current.script) {
        bail!("controller UTXO does not match the v0.2 collection state");
    }
    let funding_entry = parse_funding(&request.funding_utxo)?;
    let recipient_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &recipient);
    if funding_entry.script_public_key != pay_to_address_script(&recipient_address) {
        bail!("funding UTXO must belong to the recipient wallet");
    }
    let ticket = compile_v2_ticket(
        &nft_template, collection_id.as_bytes(), request.next_mint_index, request.supply,
        metadata_digest, shuffle_root, recipient,
    )?;
    let controller_outpoint = TransactionOutpoint::new(
        request.controller_utxo.transaction_id.parse()?, request.controller_utxo.index,
    );
    let ticket_unbound = TransactionOutput::new(
        BLIND_TICKET_VALUE, pay_to_script_hash_script(&ticket.script),
    );
    let ticket_id = covenant_id(controller_outpoint, std::iter::once((1u32, &ticket_unbound)));
    let next_state = struct_object(vec![
        ("deployer", Expr::bytes(deployer.to_vec())),
        ("maxSupply", Expr::int(i64::try_from(request.supply)?)),
        ("metadataDigest", Expr::bytes(metadata_digest.to_vec())),
        ("shuffleRoot", Expr::bytes(shuffle_root.to_vec())),
        ("mintPrice", Expr::int(i64::try_from(mint_price)?)),
        ("mintDaaScore", Expr::int(i64::try_from(mint_daa)?)),
        ("nextMintIndex", Expr::int(i64::try_from(request.next_mint_index + 1)?)),
    ]);
    let payment_output_index = if mint_price > 0 { 2 } else { -1 };
    let mut witness = current.build_sig_script_for_covenant_decl(
        "commit",
        vec![next_state, Expr::bytes(recipient.to_vec()), Expr::int(1), Expr::int(payment_output_index)],
        CovenantDeclCallOptions { is_leader: true },
    ).map_err(|error| anyhow::anyhow!("cannot build v0.2 commit witness: {error}"))?;
    witness.extend_from_slice(&ScriptBuilder::with_flags(EngineFlags { covenants_enabled: true, ..Default::default() })
        .add_data(&current.script)?.drain());
    let mut outputs = vec![
        TransactionOutput::with_covenant(CONTROLLER_CELL_VALUE, pay_to_script_hash_script(&next.script), Some(CovenantBinding::new(0, collection_id))),
        TransactionOutput::with_covenant(BLIND_TICKET_VALUE, ticket_unbound.script_public_key, Some(CovenantBinding::new(0, ticket_id))),
    ];
    if mint_price > 0 {
        let deployer_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &deployer);
        outputs.push(TransactionOutput::new(mint_price, pay_to_address_script(&deployer_address)));
    }
    let commit_locked = mint_price
        .checked_add(BLIND_TICKET_VALUE - NFT_CELL_VALUE)
        .context("blind mint locked value overflow")?;
    let prepared = build_mint_transaction(
        controller_outpoint, controller_entry, witness,
        TransactionOutpoint::new(request.funding_utxo.transaction_id.parse()?, request.funding_utxo.index),
        funding_entry, outputs, pay_to_address_script(&recipient_address), commit_locked, mint_daa,
    )?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?.serialize_to_json()?;
    Ok(V2CommitResponse {
        protocol: "kcc-721", version: "0.2.0", network: "mainnet",
        transaction_kind: "blind-mint-commit", tx_json_string: safe,
        sign_inputs: vec![SignInput { index: 1, sighash_type: 1 }],
        transaction_id: prepared.signable.tx.id().to_string(), collection_id: collection_id.to_string(),
        ticket_id: ticket_id.to_string(), mint_index: request.next_mint_index,
        recipient_address: recipient_address.to_string(), fee_sompi: prepared.fee.to_string(),
        storage_mass: prepared.storage_mass, ticket_value_sompi: BLIND_TICKET_VALUE.to_string(),
    })
}

fn prepare_v2_reveal(request: V2RevealRequest) -> Result<V2RevealResponse> {
    if request.mint_index < 1 || request.mint_index > request.supply
        || request.token_id < 1 || request.token_id > request.supply
    {
        bail!("mintIndex and tokenId must be within 1..maxSupply");
    }
    if request.siblings.len() != request.directions.len() || request.siblings.len() > 32 {
        bail!("Merkle siblings and directions must have the same supported length");
    }
    if request.directions.iter().any(|direction| *direction > 1) {
        bail!("Merkle directions must contain only 0 or 1");
    }
    let collection_id: kaspa_consensus_core::Hash = request.collection_id.parse()?;
    let ticket_id: kaspa_consensus_core::Hash = request.ticket_id.parse()?;
    let recipient = decode_pubkey(&request.recipient_public_key, "recipientPublicKey")?;
    let shuffle_root = decode_bytes32(&request.shuffle_root, "shuffleRoot")?;
    let salt = decode_bytes32(&request.salt, "salt")?;
    let siblings: Vec<[u8; 32]> = request.siblings.iter()
        .map(|value| decode_bytes32(value, "Merkle sibling"))
        .collect::<Result<_>>()?;
    let mut leaf_data = Vec::with_capacity(48);
    leaf_data.extend_from_slice(&request.mint_index.to_le_bytes());
    leaf_data.extend_from_slice(&request.token_id.to_le_bytes());
    leaf_data.extend_from_slice(&salt);
    let mut node: [u8; 32] = Sha256::digest(&leaf_data).into();
    for (sibling, direction) in siblings.iter().zip(request.directions.iter()) {
        let mut branch = Vec::with_capacity(64);
        if *direction == 0 {
            branch.extend_from_slice(&node);
            branch.extend_from_slice(sibling);
        } else {
            branch.extend_from_slice(sibling);
            branch.extend_from_slice(&node);
        }
        node = Sha256::digest(&branch).into();
    }
    if node != shuffle_root {
        bail!("reveal proof does not match the collection shuffle root");
    }
    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let nft_template = v2_nft_template_parts()?;
    let ticket = compile_v2_ticket(
        &nft_template, collection_id.as_bytes(), request.mint_index, request.supply,
        metadata_digest, shuffle_root, recipient,
    )?;
    let ticket_entry = parse_funding_with_covenant(&request.ticket_utxo, Some(ticket_id))?;
    if ticket_entry.script_public_key != pay_to_script_hash_script(&ticket.script)
        || ticket_entry.amount != BLIND_TICKET_VALUE
    {
        bail!("ticket UTXO does not match the blind mint commitment");
    }
    let ticket_outpoint = TransactionOutpoint::new(
        request.ticket_utxo.transaction_id.parse()?, request.ticket_utxo.index,
    );
    let nft = compile_v2_nft(collection_id.as_bytes(), request.token_id, metadata_digest, recipient)?;
    let nft_unbound = TransactionOutput::new(NFT_CELL_VALUE, pay_to_script_hash_script(&nft.script));
    let nft_id = covenant_id(ticket_outpoint, std::iter::once((0u32, &nft_unbound)));
    let mut witness = ticket.build_sig_script_for_covenant_decl(
        "reveal",
        vec![
            Expr::from(Vec::<Expr>::new()),
            Expr::int(i64::try_from(request.token_id)?),
            Expr::bytes(salt.to_vec()),
            Expr::from(siblings.iter().map(|value| value.to_vec()).collect::<Vec<_>>()),
            Expr::bytes(request.directions.clone()),
            Expr::int(0),
        ],
        CovenantDeclCallOptions { is_leader: true },
    ).map_err(|error| anyhow::anyhow!("cannot build v0.2 reveal witness: {error}"))?;
    witness.extend_from_slice(&ScriptBuilder::with_flags(EngineFlags { covenants_enabled: true, ..Default::default() })
        .add_data(&ticket.script)?.drain());
    let output = TransactionOutput::with_covenant(
        NFT_CELL_VALUE, nft_unbound.script_public_key, Some(CovenantBinding::new(0, nft_id)),
    );
    let prepared = build_v2_reveal_transaction(ticket_outpoint, ticket_entry, witness, output)?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?.serialize_to_json()?;
    let recipient_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &recipient);
    Ok(V2RevealResponse {
        protocol: "kcc-721", version: "0.2.0", network: "mainnet",
        transaction_kind: "blind-mint-reveal", tx_json_string: safe, sign_inputs: vec![],
        transaction_id: prepared.signable.tx.id().to_string(), collection_id: collection_id.to_string(),
        ticket_id: ticket_id.to_string(), nft_id: nft_id.to_string(), mint_index: request.mint_index,
        token_id: request.token_id, recipient_address: recipient_address.to_string(),
        fee_sompi: prepared.fee.to_string(), storage_mass: prepared.storage_mass,
    })
}

fn prepare_transfer(request: TransferRequest) -> Result<TransferResponse> {
    prepare_transfer_with_version(request, false)
}

fn prepare_v2_transfer(request: TransferRequest) -> Result<TransferResponse> {
    prepare_transfer_with_version(request, true)
}

fn prepare_transfer_with_version(request: TransferRequest, is_v2: bool) -> Result<TransferResponse> {
    let collection_id: kaspa_consensus_core::Hash = request.collection_id.parse()?;
    let nft_id: kaspa_consensus_core::Hash = request.nft_id.parse()?;
    let current_owner = decode_pubkey(&request.current_owner_public_key, "currentOwnerPublicKey")?;
    let recipient_address = Address::try_from(request.recipient_address.as_str())
        .context("recipientAddress is invalid")?;
    if recipient_address.prefix != Prefix::Mainnet
        || recipient_address.version != AddressVersion::PubKey
    {
        bail!("recipientAddress must be a Mainnet P2PK address");
    }
    let recipient: [u8; 32] = recipient_address
        .payload
        .as_slice()
        .try_into()
        .map_err(|_| {
            anyhow::anyhow!("recipientAddress must contain a 32-byte x-only public key")
        })?;
    if request.token_id > i64::MAX as u64 || (is_v2 && request.token_id == 0) {
        bail!("tokenId exceeds the covenant integer range");
    }
    if !request.metadata_uri.starts_with("ipfs://") || request.metadata_uri.len() > 256 {
        bail!("metadataUri must be an immutable ipfs:// URI");
    }
    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let current_nft = if is_v2 {
        compile_v2_nft(collection_id.as_bytes(), request.token_id, metadata_digest, current_owner)?
    } else {
        compile_nft(collection_id.as_bytes(), request.token_id, metadata_digest, current_owner)?
    };
    let next_nft = if is_v2 {
        compile_v2_nft(collection_id.as_bytes(), request.token_id, metadata_digest, recipient)?
    } else {
        compile_nft(collection_id.as_bytes(), request.token_id, metadata_digest, recipient)?
    };
    let nft_entry = parse_funding_with_covenant(&request.nft_utxo, Some(nft_id))?;
    if nft_entry.script_public_key != pay_to_script_hash_script(&current_nft.script)
        || nft_entry.amount != NFT_CELL_VALUE
    {
        bail!("NFT UTXO does not match the declared token state");
    }
    let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &current_owner);
    let funding_entry = parse_funding(&request.funding_utxo)?;
    if funding_entry.script_public_key != pay_to_address_script(&owner_address) {
        bail!("funding UTXO must belong to the current NFT owner");
    }
    let next_state = struct_object(vec![
        (
            "collectionId",
            Expr::bytes(collection_id.as_bytes().to_vec()),
        ),
        ("tokenId", Expr::int(i64::try_from(request.token_id)?)),
        ("metadataDigest", Expr::bytes(metadata_digest.to_vec())),
        ("owner", Expr::bytes(recipient.to_vec())),
    ]);
    let mut nft_witness = current_nft
        .build_sig_script_for_covenant_decl(
            "transfer",
            vec![next_state, Expr::int(1)],
            CovenantDeclCallOptions { is_leader: true },
        )
        .map_err(|error| anyhow::anyhow!("cannot build NFT transfer witness: {error}"))?;
    nft_witness.extend_from_slice(
        &ScriptBuilder::with_flags(EngineFlags {
            covenants_enabled: true,
            ..Default::default()
        })
        .add_data(&current_nft.script)
        .map_err(|error| anyhow::anyhow!("cannot append NFT redeem script: {error}"))?
        .drain(),
    );
    let fixed_outputs = vec![TransactionOutput::with_covenant(
        NFT_CELL_VALUE,
        pay_to_script_hash_script(&next_nft.script),
        Some(CovenantBinding::new(0, nft_id)),
    )];
    let prepared = build_transfer_transaction(
        TransactionOutpoint::new(
            request.nft_utxo.transaction_id.parse()?,
            request.nft_utxo.index,
        ),
        nft_entry,
        nft_witness,
        TransactionOutpoint::new(
            request.funding_utxo.transaction_id.parse()?,
            request.funding_utxo.index,
        ),
        funding_entry,
        fixed_outputs,
        pay_to_address_script(&owner_address),
    )?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?
        .serialize_to_json()?;
    Ok(TransferResponse {
        protocol: "kcc-721",
        network: "mainnet",
        transaction_kind: "nft-transfer",
        tx_json_string: safe,
        sign_inputs: vec![SignInput {
            index: 1,
            sighash_type: 1,
        }],
        transaction_id: prepared.signable.tx.id().to_string(),
        collection_id: collection_id.to_string(),
        nft_id: nft_id.to_string(),
        token_id: request.token_id,
        previous_owner_address: owner_address.to_string(),
        recipient_address: recipient_address.to_string(),
        recipient_public_key: hex::encode(recipient),
        fee_sompi: prepared.fee.to_string(),
        compute_mass: prepared.compute_mass,
        transient_mass: prepared.transient_mass,
        normalized_fee_mass: prepared.normalized_fee_mass,
        storage_mass: prepared.storage_mass,
    })
}

fn prepare_mint(request: MintRequest) -> Result<MintResponse> {
    validate_mint_request(&request)?;
    let collection_id = request.collection_id.parse()?;
    let deployer = decode_pubkey(&request.deployer_public_key, "deployerPublicKey")?;
    let recipient = decode_pubkey(&request.recipient_public_key, "recipientPublicKey")?;
    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let mint_price = request.mint_price_sompi.parse::<u64>()?;
    let mint_daa = request.mint_daa_score.parse::<u64>()?;
    let nft_template = nft_template_parts()?;
    let current_controller = compile_collection(
        deployer,
        request.supply,
        metadata_digest,
        mint_price,
        mint_daa,
        request.next_token_id,
        &nft_template,
    )?;
    let next_controller = compile_collection(
        deployer,
        request.supply,
        metadata_digest,
        mint_price,
        mint_daa,
        request.next_token_id + 1,
        &nft_template,
    )?;

    let controller_entry =
        parse_funding_with_covenant(&request.controller_utxo, Some(collection_id))?;
    let expected_controller_spk = pay_to_script_hash_script(&current_controller.script);
    if controller_entry.script_public_key != expected_controller_spk
        || controller_entry.amount != CONTROLLER_CELL_VALUE
    {
        bail!("controller UTXO does not match the declared collection state");
    }
    let funding_entry = parse_funding(&request.funding_utxo)?;
    let funding_outpoint = TransactionOutpoint::new(
        request.funding_utxo.transaction_id.parse()?,
        request.funding_utxo.index,
    );
    let controller_outpoint = TransactionOutpoint::new(
        request.controller_utxo.transaction_id.parse()?,
        request.controller_utxo.index,
    );

    let next_state = struct_object(vec![
        ("deployer", Expr::bytes(deployer.to_vec())),
        ("maxSupply", Expr::int(i64::try_from(request.supply)?)),
        ("metadataDigest", Expr::bytes(metadata_digest.to_vec())),
        ("mintPrice", Expr::int(i64::try_from(mint_price)?)),
        ("mintDaaScore", Expr::int(i64::try_from(mint_daa)?)),
        (
            "nextTokenId",
            Expr::int(i64::try_from(request.next_token_id + 1)?),
        ),
    ]);
    let payment_output_index = if mint_price > 0 { 2 } else { -1 };
    let mut controller_witness = current_controller
        .build_sig_script_for_covenant_decl(
            "mint",
            vec![
                next_state,
                Expr::bytes(recipient.to_vec()),
                Expr::int(1),
                Expr::int(payment_output_index),
            ],
            CovenantDeclCallOptions { is_leader: true },
        )
        .map_err(|error| anyhow::anyhow!("cannot build controller mint witness: {error}"))?;
    controller_witness.extend_from_slice(
        &ScriptBuilder::with_flags(EngineFlags {
            covenants_enabled: true,
            ..Default::default()
        })
        .add_data(&current_controller.script)
        .map_err(|error| anyhow::anyhow!("cannot append controller redeem script: {error}"))?
        .drain(),
    );

    let nft = compile_nft(
        collection_id.as_bytes(),
        request.next_token_id,
        metadata_digest,
        recipient,
    )?;
    let nft_unbound =
        TransactionOutput::new(NFT_CELL_VALUE, pay_to_script_hash_script(&nft.script));
    let nft_id = covenant_id(controller_outpoint, std::iter::once((1u32, &nft_unbound)));
    let mut fixed_outputs = vec![
        TransactionOutput::with_covenant(
            CONTROLLER_CELL_VALUE,
            pay_to_script_hash_script(&next_controller.script),
            Some(CovenantBinding::new(0, collection_id)),
        ),
        TransactionOutput::with_covenant(
            NFT_CELL_VALUE,
            nft_unbound.script_public_key,
            Some(CovenantBinding::new(0, nft_id)),
        ),
    ];
    if mint_price > 0 {
        let deployer_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &deployer);
        fixed_outputs.push(TransactionOutput::new(
            mint_price,
            pay_to_address_script(&deployer_address),
        ));
    }
    let recipient_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &recipient);
    let prepared = build_mint_transaction(
        controller_outpoint,
        controller_entry,
        controller_witness,
        funding_outpoint,
        funding_entry,
        fixed_outputs,
        pay_to_address_script(&recipient_address),
        mint_price,
        mint_daa,
    )?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?
        .serialize_to_json()?;
    Ok(MintResponse {
        protocol: "kcc-721",
        network: "mainnet",
        transaction_kind: "public-mint",
        tx_json_string: safe,
        sign_inputs: vec![SignInput {
            index: 1,
            sighash_type: 1,
        }],
        transaction_id: prepared.signable.tx.id().to_string(),
        collection_id: collection_id.to_string(),
        nft_id: nft_id.to_string(),
        token_id: request.next_token_id,
        recipient_address: recipient_address.to_string(),
        fee_sompi: prepared.fee.to_string(),
        compute_mass: prepared.compute_mass,
        transient_mass: prepared.transient_mass,
        normalized_fee_mass: prepared.normalized_fee_mass,
        storage_mass: prepared.storage_mass,
        nft_value_sompi: NFT_CELL_VALUE.to_string(),
    })
}

fn read_stdin_json<T: for<'de> Deserialize<'de>>() -> Result<T> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    serde_json::from_str(&input).context("request is not valid JSON")
}

fn compile_info() -> Result<CompileInfo> {
    let nft = compile_v2_nft([0; 32], 1, [0; 32], [1; 32])?;
    Ok(CompileInfo {
        protocol: "kcc-721",
        version: "0.2.0",
        compiler_version: nft.compiler_version.clone(),
        nft_program_bytes: nft.script.len(),
        nft_template_hash: hex::encode(nft.template_hash()),
        fee_rate_sompi_per_gram: FEE_RATE_SOMPI_PER_GRAM,
    })
}

fn prepare_deploy(request: DeployRequest) -> Result<DeployResponse> {
    validate_deploy_request(&request)?;
    let owner: [u8; 32] = hex::decode(&request.public_key)?
        .try_into()
        .map_err(|_| anyhow::anyhow!("publicKey must be 32 bytes"))?;
    let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &owner);
    let owner_spk = pay_to_address_script(&owner_address);
    let funding = parse_funding(&request.funding_utxo)?;
    if funding.script_public_key != owner_spk {
        bail!("funding UTXO does not belong to the supplied Kasware public key");
    }

    let metadata_digest: [u8; 32] = Sha256::digest(request.metadata_uri.as_bytes()).into();
    let mint_price = request
        .mint_price_sompi
        .parse::<u64>()
        .context("mintPriceSompi must be an unsigned integer")?;
    let mint_daa = request
        .mint_daa_score
        .parse::<u64>()
        .context("mintDaaScore must be an unsigned integer")?;
    let outpoint = TransactionOutpoint::new(
        request.funding_utxo.transaction_id.parse()?,
        request.funding_utxo.index,
    );
    let nft_template = nft_template_parts()?;
    let controller = compile_collection(
        owner,
        request.supply,
        metadata_digest,
        mint_price,
        mint_daa,
        request.premint,
        &nft_template,
    )?;

    let controller_spk = pay_to_script_hash_script(&controller.script);
    let controller_unbound = TransactionOutput::new(CONTROLLER_CELL_VALUE, controller_spk.clone());
    let collection_id = covenant_id(outpoint, std::iter::once((0u32, &controller_unbound)));
    let mut outputs = vec![TransactionOutput::with_covenant(
        CONTROLLER_CELL_VALUE,
        controller_spk,
        Some(CovenantBinding::new(0, collection_id)),
    )];
    let mut nft_ids = Vec::with_capacity(request.premint as usize);
    for token_id in 0..request.premint {
        let nft = compile_nft(collection_id.as_bytes(), token_id, metadata_digest, owner)?;
        let output_index = outputs.len() as u32;
        let unbound =
            TransactionOutput::new(NFT_CELL_VALUE, pay_to_script_hash_script(&nft.script));
        let nft_id = covenant_id(outpoint, std::iter::once((output_index, &unbound)));
        outputs.push(TransactionOutput::with_covenant(
            NFT_CELL_VALUE,
            unbound.script_public_key,
            Some(CovenantBinding::new(0, nft_id)),
        ));
        nft_ids.push(nft_id.to_string());
    }

    let descriptor = GenesisDescriptor {
        protocol: "kcc-721",
        version: "0.1.0-experimental",
        ticker: &request.ticker,
        max_supply: request.supply,
        metadata_uri: &request.metadata_uri,
        metadata_digest: hex::encode(metadata_digest),
        mint_price_sompi: mint_price.to_string(),
        mint_daa_score: mint_daa.to_string(),
        premint_allocation: request.premint,
        deployer_pubkey: hex::encode(owner),
    };
    let payload = serde_json::to_vec(&descriptor)?;
    let locked = CONTROLLER_CELL_VALUE
        .checked_add(
            NFT_CELL_VALUE
                .checked_mul(request.premint)
                .context("premint value overflow")?,
        )
        .context("locked value overflow")?;
    let prepared =
        build_funded_transaction(outpoint, funding, outputs, owner_spk, payload, locked)?;
    let safe = SerializableTransaction::from_signable_transaction(&prepared.signable)?
        .serialize_to_json()?;

    Ok(DeployResponse {
        protocol: "kcc-721",
        network: "mainnet",
        transaction_kind: "collection-genesis",
        tx_json_string: safe,
        sign_inputs: vec![SignInput {
            index: 0,
            sighash_type: 1,
        }],
        transaction_id: prepared.signable.tx.id().to_string(),
        collection_id: collection_id.to_string(),
        premint_nft_ids: nft_ids,
        owner_address: owner_address.to_string(),
        fee_sompi: prepared.fee.to_string(),
        compute_mass: prepared.compute_mass,
        transient_mass: prepared.transient_mass,
        normalized_fee_mass: prepared.normalized_fee_mass,
        storage_mass: prepared.storage_mass,
        controller_value_sompi: CONTROLLER_CELL_VALUE.to_string(),
        nft_value_sompi: NFT_CELL_VALUE.to_string(),
    })
}

struct PreparedTransaction {
    signable: SignableTransaction,
    fee: u64,
    compute_mass: u64,
    transient_mass: u64,
    normalized_fee_mass: u64,
    storage_mass: u64,
}

fn build_funded_transaction(
    outpoint: TransactionOutpoint,
    funding: UtxoEntry,
    covenant_outputs: Vec<TransactionOutput>,
    change_spk: kaspa_consensus_core::tx::ScriptPublicKey,
    payload: Vec<u8>,
    locked: u64,
) -> Result<PreparedTransaction> {
    let mut fee = 0u64;
    let calculator = MassCalculator::new_with_consensus_params(&MAINNET_PARAMS);
    let cofactors = MAINNET_PARAMS.mempool_block_mass_cofactors().raw_post();
    let mut final_result = None;

    for _ in 0..4 {
        let change = funding
            .amount
            .checked_sub(locked + fee)
            .context("selected UTXO cannot cover covenant cells and network fee")?;
        if change < 10_000_000 {
            bail!(
                "selected UTXO must leave at least 0.1 KAS change after funding covenant cells and fees"
            );
        }
        let mut outputs = covenant_outputs.clone();
        outputs.push(TransactionOutput::new(change, change_spk.clone()));
        let input = TransactionInput::new_with_mass(
            outpoint,
            vec![0; SIGNATURE_SCRIPT_ESTIMATE],
            0,
            ComputeCommit::ComputeBudget(ComputeBudget(P2PK_COMPUTE_BUDGET)),
        );
        let mut tx = Transaction::new(
            TX_VERSION_TOCCATA,
            vec![input],
            outputs,
            0,
            SUBNETWORK_ID_NATIVE,
            0,
            payload.clone(),
        );
        let with_entry = SignableTransaction::with_entries(tx.clone(), vec![funding.clone()]);
        let non_contextual = calculator.calc_non_contextual_masses(&tx);
        let normalized_fee_mass = non_contextual.normalized_max(&cofactors);
        let next_fee = normalized_fee_mass
            .checked_mul(FEE_RATE_SOMPI_PER_GRAM)
            .context("fee overflow")?;
        let storage_mass = calculator
            .calc_contextual_masses(&with_entry.as_verifiable())
            .context("storage mass is not computable")?
            .storage_mass;
        let storage_limit = MAINNET_PARAMS.block_mass_limits().raw_post().storage;
        if storage_mass > storage_limit {
            bail!(
                "storage mass {storage_mass} exceeds Mainnet limit {storage_limit}; use a larger funding UTXO or lower premint"
            );
        }
        tx.set_storage_mass(storage_mass);
        tx.inputs[0].signature_script.clear();
        let signable = SignableTransaction::with_entries(tx, vec![funding.clone()]);
        final_result = Some(PreparedTransaction {
            signable,
            fee: next_fee,
            compute_mass: non_contextual.compute_mass,
            transient_mass: non_contextual.transient_mass,
            normalized_fee_mass,
            storage_mass,
        });
        if next_fee == fee {
            break;
        }
        fee = next_fee;
    }
    let result = final_result.context("failed to converge transaction fee")?;
    if result.fee != fee {
        bail!("failed to converge transaction fee");
    }
    Ok(result)
}

#[allow(clippy::too_many_arguments)]
fn build_mint_transaction(
    controller_outpoint: TransactionOutpoint,
    controller_entry: UtxoEntry,
    controller_witness: Vec<u8>,
    funding_outpoint: TransactionOutpoint,
    funding_entry: UtxoEntry,
    fixed_outputs: Vec<TransactionOutput>,
    change_spk: kaspa_consensus_core::tx::ScriptPublicKey,
    mint_price: u64,
    lock_time: u64,
) -> Result<PreparedTransaction> {
    let mut fee = 0u64;
    let calculator = MassCalculator::new_with_consensus_params(&MAINNET_PARAMS);
    let cofactors = MAINNET_PARAMS.mempool_block_mass_cofactors().raw_post();
    let mut final_result = None;
    for _ in 0..4 {
        let change = funding_entry
            .amount
            .checked_sub(NFT_CELL_VALUE + mint_price + fee)
            .context("selected UTXO cannot cover NFT cell, mint price, and network fee")?;
        if change < 10_000_000 {
            bail!("selected UTXO must leave at least 0.1 KAS change after minting and fees");
        }
        let mut outputs = fixed_outputs.clone();
        outputs.push(TransactionOutput::new(change, change_spk.clone()));
        let inputs = vec![
            TransactionInput::new_with_mass(
                controller_outpoint,
                controller_witness.clone(),
                0,
                ComputeCommit::ComputeBudget(ComputeBudget(COVENANT_COMPUTE_BUDGET)),
            ),
            TransactionInput::new_with_mass(
                funding_outpoint,
                vec![0; SIGNATURE_SCRIPT_ESTIMATE],
                0,
                ComputeCommit::ComputeBudget(ComputeBudget(P2PK_COMPUTE_BUDGET)),
            ),
        ];
        let mut tx = Transaction::new(
            TX_VERSION_TOCCATA,
            inputs,
            outputs,
            lock_time,
            SUBNETWORK_ID_NATIVE,
            0,
            vec![],
        );
        let entries = vec![controller_entry.clone(), funding_entry.clone()];
        let populated = SignableTransaction::with_entries(tx.clone(), entries.clone());
        let non_contextual = calculator.calc_non_contextual_masses(&tx);
        let normalized_fee_mass = non_contextual.normalized_max(&cofactors);
        let next_fee = normalized_fee_mass
            .checked_mul(FEE_RATE_SOMPI_PER_GRAM)
            .context("fee overflow")?;
        let storage_mass = calculator
            .calc_contextual_masses(&populated.as_verifiable())
            .context("storage mass is not computable")?
            .storage_mass;
        let storage_limit = MAINNET_PARAMS.block_mass_limits().raw_post().storage;
        if storage_mass > storage_limit {
            bail!(
                "storage mass {storage_mass} exceeds Mainnet limit {storage_limit}; use a larger funding UTXO"
            );
        }
        tx.set_storage_mass(storage_mass);
        tx.inputs[1].signature_script.clear();
        final_result = Some(PreparedTransaction {
            signable: SignableTransaction::with_entries(tx, entries),
            fee: next_fee,
            compute_mass: non_contextual.compute_mass,
            transient_mass: non_contextual.transient_mass,
            normalized_fee_mass,
            storage_mass,
        });
        if next_fee == fee {
            break;
        }
        fee = next_fee;
    }
    let result = final_result.context("failed to converge transaction fee")?;
    if result.fee != fee {
        bail!("failed to converge transaction fee");
    }
    Ok(result)
}

fn build_v2_reveal_transaction(
    ticket_outpoint: TransactionOutpoint,
    ticket_entry: UtxoEntry,
    ticket_witness: Vec<u8>,
    nft_output: TransactionOutput,
) -> Result<PreparedTransaction> {
    let calculator = MassCalculator::new_with_consensus_params(&MAINNET_PARAMS);
    let cofactors = MAINNET_PARAMS.mempool_block_mass_cofactors().raw_post();
    let input = TransactionInput::new_with_mass(
        ticket_outpoint,
        ticket_witness,
        0,
        ComputeCommit::ComputeBudget(ComputeBudget(COVENANT_COMPUTE_BUDGET)),
    );
    let tx = Transaction::new(
        TX_VERSION_TOCCATA,
        vec![input],
        vec![nft_output],
        0,
        SUBNETWORK_ID_NATIVE,
        0,
        vec![],
    );
    let populated = SignableTransaction::with_entries(tx.clone(), vec![ticket_entry.clone()]);
    let masses = calculator.calc_non_contextual_masses(&tx);
    let normalized_fee_mass = masses.normalized_max(&cofactors);
    let required_fee = normalized_fee_mass
        .checked_mul(FEE_RATE_SOMPI_PER_GRAM)
        .context("reveal fee overflow")?;
    let paid_fee = BLIND_TICKET_VALUE - NFT_CELL_VALUE;
    if required_fee > paid_fee {
        bail!("blind ticket does not cover the reveal network fee");
    }
    let storage_mass = calculator
        .calc_contextual_masses(&populated.as_verifiable())
        .context("reveal storage mass is not computable")?
        .storage_mass;
    tx.set_storage_mass(storage_mass);
    Ok(PreparedTransaction {
        signable: SignableTransaction::with_entries(tx, vec![ticket_entry]),
        fee: paid_fee,
        compute_mass: masses.compute_mass,
        transient_mass: masses.transient_mass,
        normalized_fee_mass,
        storage_mass,
    })
}

fn build_transfer_transaction(
    nft_outpoint: TransactionOutpoint,
    nft_entry: UtxoEntry,
    nft_witness: Vec<u8>,
    funding_outpoint: TransactionOutpoint,
    funding_entry: UtxoEntry,
    fixed_outputs: Vec<TransactionOutput>,
    change_spk: kaspa_consensus_core::tx::ScriptPublicKey,
) -> Result<PreparedTransaction> {
    let mut fee = 0u64;
    let calculator = MassCalculator::new_with_consensus_params(&MAINNET_PARAMS);
    let cofactors = MAINNET_PARAMS.mempool_block_mass_cofactors().raw_post();
    let mut final_result = None;
    for _ in 0..4 {
        let change = funding_entry
            .amount
            .checked_sub(fee)
            .context("selected UTXO cannot cover the network fee")?;
        if change < 10_000_000 {
            bail!("selected UTXO must leave at least 0.1 KAS change after the transfer fee");
        }
        let mut outputs = fixed_outputs.clone();
        outputs.push(TransactionOutput::new(change, change_spk.clone()));
        let inputs = vec![
            TransactionInput::new_with_mass(
                nft_outpoint,
                nft_witness.clone(),
                0,
                ComputeCommit::ComputeBudget(ComputeBudget(COVENANT_COMPUTE_BUDGET)),
            ),
            TransactionInput::new_with_mass(
                funding_outpoint,
                vec![0; SIGNATURE_SCRIPT_ESTIMATE],
                0,
                ComputeCommit::ComputeBudget(ComputeBudget(P2PK_COMPUTE_BUDGET)),
            ),
        ];
        let mut tx = Transaction::new(
            TX_VERSION_TOCCATA,
            inputs,
            outputs,
            0,
            SUBNETWORK_ID_NATIVE,
            0,
            vec![],
        );
        let entries = vec![nft_entry.clone(), funding_entry.clone()];
        let populated = SignableTransaction::with_entries(tx.clone(), entries.clone());
        let non_contextual = calculator.calc_non_contextual_masses(&tx);
        let normalized_fee_mass = non_contextual.normalized_max(&cofactors);
        let next_fee = normalized_fee_mass
            .checked_mul(FEE_RATE_SOMPI_PER_GRAM)
            .context("fee overflow")?;
        let storage_mass = calculator
            .calc_contextual_masses(&populated.as_verifiable())
            .context("storage mass is not computable")?
            .storage_mass;
        let storage_limit = MAINNET_PARAMS.block_mass_limits().raw_post().storage;
        if storage_mass > storage_limit {
            bail!(
                "storage mass {storage_mass} exceeds Mainnet limit {storage_limit}; use a larger funding UTXO"
            );
        }
        tx.set_storage_mass(storage_mass);
        tx.inputs[1].signature_script.clear();
        final_result = Some(PreparedTransaction {
            signable: SignableTransaction::with_entries(tx, entries),
            fee: next_fee,
            compute_mass: non_contextual.compute_mass,
            transient_mass: non_contextual.transient_mass,
            normalized_fee_mass,
            storage_mass,
        });
        if next_fee == fee {
            break;
        }
        fee = next_fee;
    }
    let result = final_result.context("failed to converge transaction fee")?;
    if result.fee != fee {
        bail!("failed to converge transaction fee");
    }
    Ok(result)
}

fn parse_funding(input: &FundingUtxo) -> Result<UtxoEntry> {
    parse_funding_with_covenant(input, None)
}

fn parse_funding_with_covenant(
    input: &FundingUtxo,
    covenant_id: Option<kaspa_consensus_core::Hash>,
) -> Result<UtxoEntry> {
    let script =
        hex::decode(&input.script_public_key).context("funding scriptPublicKey is not hex")?;
    if script.len() < 2 {
        bail!("funding scriptPublicKey is too short");
    }
    let version = u16::from_le_bytes([script[0], script[1]]);
    Ok(UtxoEntry::new(
        input
            .amount
            .parse()
            .context("funding amount must be an unsigned integer")?,
        kaspa_consensus_core::tx::ScriptPublicKey::new(version, script[2..].to_vec().into()),
        input
            .block_daa_score
            .parse()
            .context("funding blockDaaScore must be an unsigned integer")?,
        input.is_coinbase,
        covenant_id,
    ))
}

fn decode_pubkey(value: &str, field: &str) -> Result<[u8; 32]> {
    if value.len() != 64 || !value.bytes().all(|c| c.is_ascii_hexdigit()) {
        bail!("{field} must be a 32-byte hexadecimal x-only key");
    }
    hex::decode(value)?
        .try_into()
        .map_err(|_| anyhow::anyhow!("{field} must be 32 bytes"))
}

fn validate_deploy_request(request: &DeployRequest) -> Result<()> {
    if request.public_key.len() != 64 || !request.public_key.bytes().all(|c| c.is_ascii_hexdigit())
    {
        bail!("publicKey must be a 32-byte hexadecimal x-only key");
    }
    if request.ticker.is_empty()
        || request.ticker.len() > 10
        || !request
            .ticker
            .bytes()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
    {
        bail!("ticker must contain 1 to 10 uppercase ASCII letters or digits");
    }
    if request.supply == 0 || request.supply > i64::MAX as u64 {
        bail!("supply must be between 1 and 9223372036854775807");
    }
    if request.premint > request.supply || request.premint > MAX_GENESIS_PREMINT {
        bail!("experimental Mainnet genesis premint is limited to {MAX_GENESIS_PREMINT} NFTs");
    }
    let uri = request.metadata_uri.as_str();
    if !uri.starts_with("ipfs://")
        || uri.len() > 256
        || uri.contains("..")
        || uri.contains('?')
        || uri.contains('#')
    {
        bail!("metadataUri must be an immutable ipfs:// URI without query or fragment");
    }
    let mint_price = request
        .mint_price_sompi
        .parse::<u64>()
        .context("mintPriceSompi must be an unsigned integer")?;
    let mint_daa = request
        .mint_daa_score
        .parse::<u64>()
        .context("mintDaaScore must be an unsigned integer")?;
    if mint_price > i64::MAX as u64 || mint_daa > i64::MAX as u64 {
        bail!("mint price and DAA score must fit in signed 64-bit covenant integers");
    }
    Ok(())
}

fn validate_mint_request(request: &MintRequest) -> Result<()> {
    decode_pubkey(&request.deployer_public_key, "deployerPublicKey")?;
    decode_pubkey(&request.recipient_public_key, "recipientPublicKey")?;
    if request.supply == 0
        || request.supply > i64::MAX as u64
        || request.next_token_id >= request.supply
    {
        bail!("nextTokenId must be below the valid collection supply");
    }
    if !request.metadata_uri.starts_with("ipfs://") || request.metadata_uri.len() > 256 {
        bail!("metadataUri must be an immutable ipfs:// URI");
    }
    let price = request
        .mint_price_sompi
        .parse::<u64>()
        .context("mintPriceSompi must be an unsigned integer")?;
    let daa = request
        .mint_daa_score
        .parse::<u64>()
        .context("mintDaaScore must be an unsigned integer")?;
    if price > i64::MAX as u64 || daa > i64::MAX as u64 {
        bail!("mint price and DAA score must fit in signed 64-bit covenant integers");
    }
    Ok(())
}

fn nft_template_parts() -> Result<TemplateParts> {
    let compiled = compile_nft([0; 32], 0, [0; 32], [0; 32])?;
    let start = compiled.state_layout.start;
    let end = start + compiled.state_layout.len;
    Ok(TemplateParts {
        prefix: compiled.script[..start].to_vec(),
        suffix: compiled.script[end..].to_vec(),
        hash: compiled.template_hash(),
    })
}

fn compile_nft(
    collection: [u8; 32],
    token_id: u64,
    metadata: [u8; 32],
    owner: [u8; 32],
) -> Result<CompiledContract<'static>> {
    compile_contract(
        NFT_SOURCE,
        &[
            Expr::bytes(collection.to_vec()),
            Expr::int(i64::try_from(token_id)?),
            Expr::bytes(metadata.to_vec()),
            Expr::bytes(owner.to_vec()),
        ],
        CompileOptions::default(),
    )
    .map_err(|error| anyhow::anyhow!("NFT covenant compilation failed: {error}"))
}

fn compile_collection(
    deployer: [u8; 32],
    supply: u64,
    metadata: [u8; 32],
    mint_price: u64,
    mint_daa: u64,
    next_token_id: u64,
    nft: &TemplateParts,
) -> Result<CompiledContract<'static>> {
    compile_contract(
        COLLECTION_SOURCE,
        &[
            Expr::bytes(deployer.to_vec()),
            Expr::int(i64::try_from(supply)?),
            Expr::bytes(metadata.to_vec()),
            Expr::int(i64::try_from(mint_price)?),
            Expr::int(i64::try_from(mint_daa)?),
            Expr::int(i64::try_from(next_token_id)?),
            Expr::bytes(nft.prefix.clone()),
            Expr::bytes(nft.suffix.clone()),
            Expr::bytes(nft.hash.to_vec()),
        ],
        CompileOptions::default(),
    )
    .map_err(|error| anyhow::anyhow!("collection covenant compilation failed: {error}"))
}

fn compile_v2_nft(
    collection: [u8; 32],
    token_id: u64,
    metadata: [u8; 32],
    owner: [u8; 32],
) -> Result<CompiledContract<'static>> {
    compile_contract(
        V2_NFT_SOURCE,
        &[
            Expr::bytes(collection.to_vec()),
            Expr::int(i64::try_from(token_id)?),
            Expr::bytes(metadata.to_vec()),
            Expr::bytes(owner.to_vec()),
        ],
        CompileOptions::default(),
    )
    .map_err(|error| anyhow::anyhow!("KCC721 v0.2 NFT compilation failed: {error}"))
}

fn v2_nft_template_parts() -> Result<TemplateParts> {
    let compiled = compile_v2_nft([0; 32], 1, [0; 32], [0; 32])?;
    let start = compiled.state_layout.start;
    let end = start + compiled.state_layout.len;
    Ok(TemplateParts {
        prefix: compiled.script[..start].to_vec(),
        suffix: compiled.script[end..].to_vec(),
        hash: compiled.template_hash(),
    })
}

#[allow(clippy::too_many_arguments)]
fn compile_v2_ticket(
    nft: &TemplateParts,
    collection: [u8; 32],
    mint_index: u64,
    supply: u64,
    metadata: [u8; 32],
    shuffle_root: [u8; 32],
    recipient: [u8; 32],
) -> Result<CompiledContract<'static>> {
    compile_contract(
        V2_TICKET_SOURCE,
        &[
            Expr::bytes(nft.prefix.clone()),
            Expr::bytes(nft.suffix.clone()),
            Expr::bytes(nft.hash.to_vec()),
            Expr::bytes(collection.to_vec()),
            Expr::int(i64::try_from(mint_index)?),
            Expr::int(i64::try_from(supply)?),
            Expr::bytes(metadata.to_vec()),
            Expr::bytes(shuffle_root.to_vec()),
            Expr::bytes(recipient.to_vec()),
        ],
        CompileOptions::default(),
    )
    .map_err(|error| anyhow::anyhow!("KCC721 v0.2 ticket compilation failed: {error}"))
}

fn v2_ticket_template_parts(nft: &TemplateParts) -> Result<TemplateParts> {
    let compiled = compile_v2_ticket(nft, [0; 32], 1, 1, [0; 32], [0; 32], [0; 32])?;
    let start = compiled.state_layout.start;
    let end = start + compiled.state_layout.len;
    Ok(TemplateParts {
        prefix: compiled.script[..start].to_vec(),
        suffix: compiled.script[end..].to_vec(),
        hash: compiled.template_hash(),
    })
}

#[allow(clippy::too_many_arguments)]
fn compile_v2_collection(
    deployer: [u8; 32],
    supply: u64,
    metadata: [u8; 32],
    shuffle_root: [u8; 32],
    mint_price: u64,
    mint_daa: u64,
    next_mint_index: u64,
    ticket: &TemplateParts,
) -> Result<CompiledContract<'static>> {
    compile_contract(
        V2_COLLECTION_SOURCE,
        &[
            Expr::bytes(deployer.to_vec()),
            Expr::int(i64::try_from(supply)?),
            Expr::bytes(metadata.to_vec()),
            Expr::bytes(shuffle_root.to_vec()),
            Expr::int(i64::try_from(mint_price)?),
            Expr::int(i64::try_from(mint_daa)?),
            Expr::int(i64::try_from(next_mint_index)?),
            Expr::bytes(ticket.prefix.clone()),
            Expr::bytes(ticket.suffix.clone()),
            Expr::bytes(ticket.hash.to_vec()),
        ],
        CompileOptions::default(),
    )
    .map_err(|error| anyhow::anyhow!("KCC721 v0.2 collection compilation failed: {error}"))
}

fn compile_v2_migration(
    deployer: [u8; 32],
    supply: u64,
    metadata: [u8; 32],
    unissued_root: [u8; 32],
    remaining: u64,
    nft: &TemplateParts,
) -> Result<CompiledContract<'static>> {
    compile_contract(
        V2_MIGRATION_SOURCE,
        &[
            Expr::bytes(deployer.to_vec()),
            Expr::int(i64::try_from(supply)?),
            Expr::bytes(metadata.to_vec()),
            Expr::bytes(unissued_root.to_vec()),
            Expr::int(i64::try_from(remaining)?),
            Expr::bytes(nft.prefix.clone()),
            Expr::bytes(nft.suffix.clone()),
            Expr::bytes(nft.hash.to_vec()),
        ],
        CompileOptions::default(),
    )
    .map_err(|error| anyhow::anyhow!("KCC721 v0.2 migration compilation failed: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use kaspa_consensus_core::{
        hashing::sighash::SigHashReusedValuesUnsync,
        tx::{PopulatedTransaction, VerifiableTransaction},
    };
    use kaspa_txscript::{EngineCtx, TxScriptEngine, caches::Cache, covenants::CovenantsContext};

    fn spk_hex(spk: &kaspa_consensus_core::tx::ScriptPublicKey) -> String {
        let mut bytes = spk.version().to_le_bytes().to_vec();
        bytes.extend_from_slice(spk.script());
        hex::encode(bytes)
    }

    #[test]
    fn templates_compile_and_owner_is_state() {
        let a = compile_nft([3; 32], 7, [4; 32], [5; 32]).unwrap();
        let b = compile_nft([3; 32], 7, [4; 32], [6; 32]).unwrap();
        assert_eq!(a.template_hash(), b.template_hash());
        assert_ne!(a.script, b.script);
        assert_eq!(a.state_layout, b.state_layout);
    }

    #[test]
    fn collection_template_compiles() {
        let nft = nft_template_parts().unwrap();
        let controller =
            compile_collection([1; 32], 100, [2; 32], 100_000_000, 0, 0, &nft).unwrap();
        assert!(!controller.script.is_empty());
        assert!(controller.compiler_version.starts_with("0."));
    }

    #[test]
    fn v2_blind_mint_templates_compile_with_one_based_ids() {
        let nft = v2_nft_template_parts().unwrap();
        let ticket = v2_ticket_template_parts(&nft).unwrap();
        let controller = compile_v2_collection(
            [1; 32],
            287,
            [2; 32],
            [3; 32],
            300_000_000,
            0,
            1,
            &ticket,
        )
        .unwrap();
        let first = compile_v2_nft([4; 32], 1, [2; 32], [5; 32]).unwrap();
        let last = compile_v2_nft([4; 32], 287, [2; 32], [5; 32]).unwrap();
        assert!(!controller.script.is_empty());
        assert_eq!(first.template_hash(), last.template_hash());
        assert_ne!(first.script, last.script);
    }

    #[test]
    fn v2_commit_and_reveal_witnesses_execute() {
        let owner = [1u8; 32];
        let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &owner);
        let collection_id = kaspa_consensus_core::Hash::from_bytes([9; 32]);
        let uri = "ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3wljymbk7buz4m5v2l3k4m5aa";
        let metadata: [u8; 32] = Sha256::digest(uri.as_bytes()).into();
        let salt = [7u8; 32];
        let mut leaf = Vec::new();
        leaf.extend_from_slice(&1u64.to_le_bytes());
        leaf.extend_from_slice(&1u64.to_le_bytes());
        leaf.extend_from_slice(&salt);
        let root: [u8; 32] = Sha256::digest(&leaf).into();
        let nft_template = v2_nft_template_parts().unwrap();
        let ticket_template = v2_ticket_template_parts(&nft_template).unwrap();
        let current = compile_v2_collection(owner, 1, metadata, root, 0, 0, 1, &ticket_template).unwrap();
        let commit = prepare_v2_commit(V2CommitRequest {
            collection_id: collection_id.to_string(),
            deployer_public_key: hex::encode(owner),
            recipient_public_key: hex::encode(owner),
            supply: 1,
            metadata_uri: uri.into(),
            shuffle_root: hex::encode(root),
            mint_price_sompi: "0".into(),
            mint_daa_score: "0".into(),
            next_mint_index: 1,
            controller_utxo: FundingUtxo {
                transaction_id: "66".repeat(32), index: 0,
                amount: CONTROLLER_CELL_VALUE.to_string(),
                script_public_key: spk_hex(&pay_to_script_hash_script(&current.script)),
                block_daa_score: "210000000".into(), is_coinbase: false,
            },
            funding_utxo: FundingUtxo {
                transaction_id: "77".repeat(32), index: 0, amount: "200000000".into(),
                script_public_key: spk_hex(&pay_to_address_script(&owner_address)),
                block_daa_score: "210000000".into(), is_coinbase: false,
            },
        }).unwrap();
        execute_covenant_input(&commit.tx_json_string, 0);

        let ticket = compile_v2_ticket(
            &nft_template, collection_id.as_bytes(), 1, 1, metadata, root, owner,
        ).unwrap();
        let reveal = prepare_v2_reveal(V2RevealRequest {
            collection_id: collection_id.to_string(), recipient_public_key: hex::encode(owner),
            supply: 1, metadata_uri: uri.into(), shuffle_root: hex::encode(root),
            mint_index: 1, token_id: 1, salt: hex::encode(salt), siblings: vec![],
            directions: vec![], ticket_id: commit.ticket_id,
            ticket_utxo: FundingUtxo {
                transaction_id: commit.transaction_id, index: 1,
                amount: BLIND_TICKET_VALUE.to_string(),
                script_public_key: spk_hex(&pay_to_script_hash_script(&ticket.script)),
                block_daa_score: "210000000".into(), is_coinbase: false,
            },
        }).unwrap();
        execute_covenant_input(&reveal.tx_json_string, 0);
        assert_eq!(reveal.token_id, 1);

        let live_nft = compile_v2_nft(collection_id.as_bytes(), 1, metadata, owner).unwrap();
        let transfer = prepare_v2_transfer(TransferRequest {
            collection_id: collection_id.to_string(),
            nft_id: reveal.nft_id,
            token_id: 1,
            metadata_uri: uri.into(),
            current_owner_public_key: hex::encode(owner),
            recipient_address: Address::new(Prefix::Mainnet, AddressVersion::PubKey, &[2u8; 32]).to_string(),
            nft_utxo: FundingUtxo {
                transaction_id: reveal.transaction_id, index: 0,
                amount: NFT_CELL_VALUE.to_string(),
                script_public_key: spk_hex(&pay_to_script_hash_script(&live_nft.script)),
                block_daa_score: "210000000".into(), is_coinbase: false,
            },
            funding_utxo: FundingUtxo {
                transaction_id: "88".repeat(32), index: 0, amount: "200000000".into(),
                script_public_key: spk_hex(&pay_to_address_script(&owner_address)),
                block_daa_score: "210000000".into(), is_coinbase: false,
            },
        }).unwrap();
        execute_covenant_input(&transfer.tx_json_string, 0);
    }

    #[test]
    fn v2_migration_issues_any_one_based_id_once() {
        let owner = [1u8; 32];
        let recipient = [2u8; 32];
        let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &owner);
        let recipient_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &recipient);
        let collection_id = kaspa_consensus_core::Hash::from_bytes([9; 32]);
        let uri = "ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3wljymbk7buz4m5v2l3k4m5aa";
        let metadata: [u8; 32] = Sha256::digest(uri.as_bytes()).into();
        let mut first_live = 1u64.to_le_bytes().to_vec();
        first_live.push(1);
        let mut second_live = 2u64.to_le_bytes().to_vec();
        second_live.push(1);
        let mut second_spent = 2u64.to_le_bytes().to_vec();
        second_spent.push(0);
        let first_leaf: [u8; 32] = Sha256::digest(first_live).into();
        let second_leaf: [u8; 32] = Sha256::digest(second_live).into();
        let second_spent_leaf: [u8; 32] = Sha256::digest(second_spent).into();
        let root: [u8; 32] = Sha256::digest([first_leaf.as_slice(), second_leaf.as_slice()].concat()).into();
        let next_root: [u8; 32] = Sha256::digest([first_leaf.as_slice(), second_spent_leaf.as_slice()].concat()).into();
        let nft_template = v2_nft_template_parts().unwrap();
        let current = compile_v2_migration(owner, 2, metadata, root, 2, &nft_template).unwrap();
        let issue = prepare_v2_migration_issue(V2MigrationIssueRequest {
            collection_id: collection_id.to_string(),
            deployer_public_key: hex::encode(owner),
            recipient_address: recipient_address.to_string(),
            supply: 2,
            metadata_uri: uri.into(),
            current_unissued_root: hex::encode(root),
            next_unissued_root: hex::encode(next_root),
            remaining: 2,
            token_id: 2,
            siblings: vec![hex::encode(first_leaf)],
            directions: vec![1],
            controller_utxo: FundingUtxo {
                transaction_id: "99".repeat(32), index: 0,
                amount: CONTROLLER_CELL_VALUE.to_string(),
                script_public_key: spk_hex(&pay_to_script_hash_script(&current.script)),
                block_daa_score: "210000000".into(), is_coinbase: false,
            },
            funding_utxo: FundingUtxo {
                transaction_id: "aa".repeat(32), index: 0, amount: "200000000".into(),
                script_public_key: spk_hex(&pay_to_address_script(&owner_address)),
                block_daa_score: "210000000".into(), is_coinbase: false,
            },
        }).unwrap();
        execute_covenant_input(&issue.tx_json_string, 0);
        assert_eq!(issue.token_id, 2);
        assert_eq!(issue.recipient_address, recipient_address.to_string());

        let advanced = compile_v2_migration(owner, 2, metadata, next_root, 1, &nft_template).unwrap();
        let duplicate = prepare_v2_migration_issue(V2MigrationIssueRequest {
            collection_id: collection_id.to_string(),
            deployer_public_key: hex::encode(owner),
            recipient_address: recipient_address.to_string(),
            supply: 2,
            metadata_uri: uri.into(),
            current_unissued_root: hex::encode(next_root),
            next_unissued_root: hex::encode(next_root),
            remaining: 1,
            token_id: 2,
            siblings: vec![hex::encode(first_leaf)],
            directions: vec![1],
            controller_utxo: FundingUtxo {
                transaction_id: issue.transaction_id, index: 0,
                amount: CONTROLLER_CELL_VALUE.to_string(),
                script_public_key: spk_hex(&pay_to_script_hash_script(&advanced.script)),
                block_daa_score: "210000001".into(), is_coinbase: false,
            },
            funding_utxo: FundingUtxo {
                transaction_id: "bb".repeat(32), index: 0, amount: "200000000".into(),
                script_public_key: spk_hex(&pay_to_address_script(&owner_address)),
                block_daa_score: "210000001".into(), is_coinbase: false,
            },
        });
        assert!(duplicate.is_err());
    }

    fn execute_covenant_input(tx_json: &str, input_index: usize) {
        let serialized = SerializableTransaction::deserialize_from_json(tx_json).unwrap();
        let signable: SignableTransaction = serialized.try_into().unwrap();
        let entries = signable.entries.iter().cloned().map(Option::unwrap).collect();
        let populated = PopulatedTransaction::new(&signable.tx, entries);
        let covenants = CovenantsContext::from_tx(&populated).unwrap();
        let reused = SigHashReusedValuesUnsync::new();
        let cache = Cache::new(128);
        let mut engine = TxScriptEngine::from_transaction_input(
            &populated, &populated.tx.inputs[input_index], input_index,
            populated.utxo(input_index).unwrap(),
            EngineCtx::new(&cache).with_reused(&reused).with_covenants_ctx(&covenants),
            EngineFlags { covenants_enabled: true, sigop_script_units: 0.into() },
        );
        engine.execute().unwrap();
    }

    #[test]
    fn prepared_mint_controller_witness_executes() {
        let owner = [1u8; 32];
        let collection_id = kaspa_consensus_core::Hash::from_bytes([9; 32]);
        let uri = "ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3wljymbk7buz4m5v2l3k4m5aa";
        let digest: [u8; 32] = Sha256::digest(uri.as_bytes()).into();
        let template = nft_template_parts().unwrap();
        let current = compile_collection(owner, 100, digest, 100_000_000, 0, 0, &template).unwrap();
        let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &owner);
        let owner_spk = pay_to_address_script(&owner_address);
        let request = MintRequest {
            collection_id: collection_id.to_string(),
            deployer_public_key: hex::encode(owner),
            recipient_public_key: hex::encode(owner),
            supply: 100,
            metadata_uri: uri.into(),
            mint_price_sompi: "100000000".into(),
            mint_daa_score: "0".into(),
            next_token_id: 0,
            controller_utxo: FundingUtxo {
                transaction_id: "22".repeat(32),
                index: 0,
                amount: CONTROLLER_CELL_VALUE.to_string(),
                script_public_key: spk_hex(&pay_to_script_hash_script(&current.script)),
                block_daa_score: "210000000".into(),
                is_coinbase: false,
            },
            funding_utxo: FundingUtxo {
                transaction_id: "33".repeat(32),
                index: 0,
                amount: "300000000".into(),
                script_public_key: spk_hex(&owner_spk),
                block_daa_score: "210000000".into(),
                is_coinbase: false,
            },
        };
        let response = prepare_mint(request).unwrap();
        let serialized =
            SerializableTransaction::deserialize_from_json(&response.tx_json_string).unwrap();
        let signable: SignableTransaction = serialized.try_into().unwrap();
        let entries = signable
            .entries
            .iter()
            .cloned()
            .map(Option::unwrap)
            .collect();
        let populated = PopulatedTransaction::new(&signable.tx, entries);
        let covenants = CovenantsContext::from_tx(&populated).unwrap();
        let reused = SigHashReusedValuesUnsync::new();
        let cache = Cache::new(128);
        let mut engine = TxScriptEngine::from_transaction_input(
            &populated,
            &populated.tx.inputs[0],
            0,
            populated.utxo(0).unwrap(),
            EngineCtx::new(&cache)
                .with_reused(&reused)
                .with_covenants_ctx(&covenants),
            EngineFlags {
                covenants_enabled: true,
                sigop_script_units: 0.into(),
            },
        );
        engine.execute().unwrap();
    }

    #[test]
    fn prepared_transfer_nft_witness_executes() {
        let owner = [1u8; 32];
        let recipient = [2u8; 32];
        let collection_id = kaspa_consensus_core::Hash::from_bytes([8; 32]);
        let nft_id = kaspa_consensus_core::Hash::from_bytes([7; 32]);
        let uri = "ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3wljymbk7buz4m5v2l3k4m5aa";
        let digest: [u8; 32] = Sha256::digest(uri.as_bytes()).into();
        let current = compile_nft(collection_id.as_bytes(), 42, digest, owner).unwrap();
        let owner_address = Address::new(Prefix::Mainnet, AddressVersion::PubKey, &owner);
        let request = TransferRequest {
            collection_id: collection_id.to_string(),
            nft_id: nft_id.to_string(),
            token_id: 42,
            metadata_uri: uri.into(),
            current_owner_public_key: hex::encode(owner),
            recipient_address: Address::new(Prefix::Mainnet, AddressVersion::PubKey, &recipient)
                .to_string(),
            nft_utxo: FundingUtxo {
                transaction_id: "44".repeat(32),
                index: 0,
                amount: NFT_CELL_VALUE.to_string(),
                script_public_key: spk_hex(&pay_to_script_hash_script(&current.script)),
                block_daa_score: "210000000".into(),
                is_coinbase: false,
            },
            funding_utxo: FundingUtxo {
                transaction_id: "55".repeat(32),
                index: 0,
                amount: "100000000".into(),
                script_public_key: spk_hex(&pay_to_address_script(&owner_address)),
                block_daa_score: "210000000".into(),
                is_coinbase: false,
            },
        };
        let response = prepare_transfer(request).unwrap();
        let serialized =
            SerializableTransaction::deserialize_from_json(&response.tx_json_string).unwrap();
        let signable: SignableTransaction = serialized.try_into().unwrap();
        let entries = signable
            .entries
            .iter()
            .cloned()
            .map(Option::unwrap)
            .collect();
        let populated = PopulatedTransaction::new(&signable.tx, entries);
        let covenants = CovenantsContext::from_tx(&populated).unwrap();
        let reused = SigHashReusedValuesUnsync::new();
        let cache = Cache::new(128);
        let mut engine = TxScriptEngine::from_transaction_input(
            &populated,
            &populated.tx.inputs[0],
            0,
            populated.utxo(0).unwrap(),
            EngineCtx::new(&cache)
                .with_reused(&reused)
                .with_covenants_ctx(&covenants),
            EngineFlags {
                covenants_enabled: true,
                sigop_script_units: 0.into(),
            },
        );
        engine.execute().unwrap();
    }
}
