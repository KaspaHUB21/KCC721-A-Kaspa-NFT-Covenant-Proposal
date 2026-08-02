(() => {
  const PENDING_MINT_KEY = "kcc721-pending-blind-mint";
  const WALLET_NFT_PAGE_SIZE = 4;
  const state = {
    mode: "deploy",
    walletAddress: "",
    publicKey: "",
    migration: null,
    plan: null,
    broadcasting: false,
    walletNftNext: 0,
    walletNftLoading: false,
    batchSelectionMode: false,
    selectedNftIds: new Set(),
  };
  const els = {
    form: document.querySelector("#kcc721Form"),
    ticker: document.querySelector("#kccTicker"),
    supply: document.querySelector("#kccSupply"),
    metadata: document.querySelector("#kccMetadata"),
    mintPrice: document.querySelector("#kccMintPrice"),
    mintDaa: document.querySelector("#kccMintDaa"),
    connect: document.querySelector("#kccConnect"),
    disconnect: document.querySelector("#kccDisconnect"),
    walletPill: document.querySelector("#kccWalletPill"),
    migrationPanel: document.querySelector("#migrationPanel"),
    mintPanel: document.querySelector("#mintPanel"),
    transferPanel: document.querySelector("#transferPanel"),
    migrationIssuePanel: document.querySelector("#migrationIssuePanel"),
    collectionId: document.querySelector("#kccCollectionId"),
    nftId: document.querySelector("#kccNftId"),
    recipientAddress: document.querySelector("#kccRecipientAddress"),
    migrationCollectionId: document.querySelector("#kccMigrationCollectionId"),
    migrationTokenId: document.querySelector("#kccMigrationTokenId"),
    migrationRecipient: document.querySelector("#kccMigrationRecipient"),
    migrationResult: document.querySelector("#migrationResult"),
    inspectMigration: document.querySelector("#inspectMigration"),
    prepare: document.querySelector("#prepareKcc721"),
    planEmpty: document.querySelector("#planEmpty"),
    protocolPlan: document.querySelector("#protocolPlan"),
    signPlan: document.querySelector("#signKccPlan"),
    downloadPlan: document.querySelector("#downloadKccPlan"),
    riskWrap: document.querySelector("#kccRiskWrap"),
    riskConfirm: document.querySelector("#kccRiskConfirm"),
    indexerSearch: document.querySelector("#kccIndexerSearch"),
    indexerRefresh: document.querySelector("#refreshKccIndexer"),
    indexerStatus: document.querySelector("#kccIndexerStatus"),
    collectionList: document.querySelector("#kccCollectionList"),
    walletNftStatus: document.querySelector("#walletNftStatus"),
    walletNftGrid: document.querySelector("#walletNftGrid"),
    refreshWalletNfts: document.querySelector("#refreshWalletNfts"),
    loadMoreWalletNfts: document.querySelector("#loadMoreWalletNfts"),
    toggleBatchTransfer: document.querySelector("#toggleBatchTransfer"),
    atomicBatchPanel: document.querySelector("#atomicBatchPanel"),
    batchRecipientAddress: document.querySelector("#batchRecipientAddress"),
    batchSelectedCount: document.querySelector("#batchSelectedCount"),
    prepareBatchTransfer: document.querySelector("#prepareBatchTransfer"),
    clearBatchSelection: document.querySelector("#clearBatchSelection"),
    toast: document.querySelector("#kccToast"),
    mintSuccessModal: document.querySelector("#mintSuccessModal"),
    mintSuccessImage: document.querySelector("#mintSuccessImage"),
    mintSuccessTicker: document.querySelector("#mintSuccessTicker"),
    mintSuccessName: document.querySelector("#mintSuccessName"),
    mintSuccessTokenId: document.querySelector("#mintSuccessTokenId"),
    mintSuccessNftId: document.querySelector("#mintSuccessNftId"),
    mintSuccessStatus: document.querySelector("#mintSuccessStatus"),
    openMintedNft: document.querySelector("#openMintedNft"),
    closeMintSuccess: document.querySelector("#closeMintSuccess"),
    closeMintSuccessBackdrop: document.querySelector("#closeMintSuccessBackdrop"),
  };

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  function formatSompi(value) {
    const raw = BigInt(value || 0);
    const whole = raw / 100_000_000n;
    const fraction = String(raw % 100_000_000n).padStart(8, "0").replace(/0+$/, "");
    return `${whole}${fraction ? `.${fraction}` : ""} KAS`;
  }

  function toast(message) {
    els.toast.textContent = message;
    els.toast.hidden = false;
    clearTimeout(els.toast._timer);
    els.toast._timer = setTimeout(() => { els.toast.hidden = true; }, 5200);
  }

  function shortAddress(address) {
    return address ? `${address.slice(0, 13)}...${address.slice(-8)}` : "Not connected";
  }

  function withTimeout(promise, message, timeout = 30000) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error(message)), timeout)),
    ]);
  }

  function prepareLabel() {
    return state.mode === "migrate"
      ? "Prepare Mainnet migration genesis"
      : state.mode === "mint"
        ? "Prepare Mainnet mint"
      : state.mode === "transfer"
        ? "Prepare Mainnet transfer"
        : state.mode === "migration-issue"
          ? "Prepare NFT airdrop"
          : "Prepare Mainnet deployment";
  }

  async function readJson(response) {
    const text = await response.text();
    const data = text ? JSON.parse(text) : {};
    if (!response.ok) throw new Error(data.error || `Request failed with HTTP ${response.status}.`);
    return data;
  }

  function updateWallet() {
    const label = shortAddress(state.walletAddress);
    els.connect.textContent = state.walletAddress ? label : "Kasware Login";
    els.walletPill.textContent = label;
    els.disconnect.disabled = !state.walletAddress;
  }

  function clearPlan() {
    state.plan = null;
    els.riskConfirm.checked = false;
    renderPlan();
  }

  function updateBatchSelection() {
    const count = state.selectedNftIds.size;
    els.batchSelectedCount.textContent = String(count);
    els.prepareBatchTransfer.disabled = count < 2 || count > 50;
    els.walletNftGrid.querySelectorAll(".kcc-wallet-nft-item").forEach((card) => {
      const selected = state.selectedNftIds.has(card.dataset.nftId);
      card.classList.toggle("selected", selected);
      const checkbox = card.querySelector(".kcc-nft-select input");
      if (checkbox) checkbox.checked = selected;
    });
  }

  function setBatchSelectionMode(enabled) {
    state.batchSelectionMode = Boolean(enabled);
    els.atomicBatchPanel.hidden = !state.batchSelectionMode;
    els.toggleBatchTransfer.textContent = state.batchSelectionMode ? "Cancel batch" : "Batch transfer";
    els.walletNftGrid.classList.toggle("selecting", state.batchSelectionMode);
    if (!state.batchSelectionMode) state.selectedNftIds.clear();
    updateBatchSelection();
  }

  async function connect() {
    if (!window.kasware) throw new Error("Kasware extension not found.");
    els.connect.disabled = true;
    toast("Waiting for Kasware wallet access...");
    try {
      if (window.kasware.getNetwork) {
        const network = String(await window.kasware.getNetwork()).toLowerCase();
        if (network !== "kaspa_mainnet") {
          if (!window.kasware.switchNetwork) throw new Error("Switch Kasware to Mainnet before connecting.");
          await window.kasware.switchNetwork("kaspa_mainnet");
        }
      }
      let accounts = [];
      if (window.kasware.getAccounts) {
        try {
          accounts = await withTimeout(
            window.kasware.getAccounts(),
            "Kasware did not return the connected account.",
          );
        } catch {
          accounts = [];
        }
      }
      if ((!Array.isArray(accounts) || !accounts.length) && window.kasware.requestAccounts) {
        accounts = await withTimeout(
          window.kasware.requestAccounts(),
          "Kasware wallet approval timed out. Open Kasware and try again.",
          60000,
        );
      }
      state.walletAddress = Array.isArray(accounts) ? accounts[0] : accounts?.address || "";
      if (!state.walletAddress?.startsWith("kaspa:")) throw new Error("Kasware did not return a Mainnet address.");
      state.publicKey = window.kasware.getPublicKey
        ? await withTimeout(window.kasware.getPublicKey(), "Kasware did not return the wallet public key.")
        : "";
      if (!/^(?:[0-9a-f]{64}|0[23][0-9a-f]{64})$/i.test(state.publicKey)) {
        throw new Error("Kasware did not return a valid wallet public key.");
      }
      localStorage.setItem("kaspa-devtools-wallet", state.walletAddress);
      clearPlan();
      updateWallet();
      resumePendingMint().catch((error) => toast(error.message || String(error)));
      loadWalletNfts();
      return state.walletAddress;
    } finally {
      els.connect.disabled = false;
    }
  }

  async function disconnect() {
    if (window.kasware?.disconnect) {
      try {
        await window.kasware.disconnect(window.location.origin);
      } catch {
        // The local session is still cleared if the extension rejects disconnect.
      }
    }
    state.walletAddress = "";
    state.publicKey = "";
    state.migration = null;
    localStorage.removeItem("kaspa-devtools-wallet");
    clearPlan();
    updateWallet();
    renderMigration();
    await loadWalletNfts();
  }

  function setMode(mode) {
    if (state.mode !== mode) clearPlan();
    state.mode = mode;
    state.migration = null;
    document.querySelectorAll("[data-mode]").forEach((button) => {
      const active = button.dataset.mode === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".deploy-field").forEach((field) => { field.hidden = mode !== "deploy"; });
    document.querySelectorAll(".collection-field").forEach((field) => { field.hidden = ["mint", "transfer", "migration-issue"].includes(mode); });
    els.migrationPanel.hidden = mode !== "migrate";
    els.mintPanel.hidden = mode !== "mint";
    els.transferPanel.hidden = mode !== "transfer";
    els.migrationIssuePanel.hidden = mode !== "migration-issue";
    els.ticker.required = mode === "deploy" || mode === "migrate";
    els.supply.required = mode === "deploy";
    els.metadata.required = mode === "deploy";
    els.collectionId.required = mode === "mint";
    els.nftId.required = mode === "transfer";
    els.recipientAddress.required = mode === "transfer";
    els.migrationRecipient.required = mode === "migration-issue";
    els.prepare.textContent = prepareLabel();
    renderMigration();
  }

  function renderMigration() {
    els.migrationResult.hidden = !state.migration;
    if (!state.migration) {
      els.migrationResult.innerHTML = "";
      return;
    }
    const item = state.migration;
    els.migrationResult.innerHTML = `
      <div><span>Ticker</span><strong>${escapeHtml(item.tick)}</strong></div>
      <div><span>Supply</span><strong>${escapeHtml(item.supply)}</strong></div>
      <div><span>Minted</span><strong>${escapeHtml(item.minted)}</strong></div>
      <div><span>Deployer</span><code>${escapeHtml(item.deployer)}</code></div>
      <div><span>Metadata</span><code>${escapeHtml(item.metadataUrl)}</code></div>
      <div><span>Eligibility</span><strong class="${item.eligible ? "ok-text" : "error-text"}">${item.eligible ? "Deployment key matches" : "Different deployment key"}</strong></div>
    `;
  }

  async function inspectMigration() {
    const wallet = state.walletAddress || await connect();
    const ticker = els.ticker.value.trim().toUpperCase();
    if (!/^[A-Z0-9]{1,10}$/.test(ticker)) throw new Error("Enter a valid KRC721 ticker first.");
    els.inspectMigration.disabled = true;
    els.inspectMigration.textContent = "Inspecting...";
    try {
      const query = new URLSearchParams({ ticker, walletAddress: wallet });
      state.migration = await readJson(await fetch(`/api/kcc721/krc721?${query}`, { cache: "no-store" }));
      renderMigration();
      if (!state.migration.eligible) throw new Error("This wallet is not the KRC721 deployment address.");
    } finally {
      els.inspectMigration.disabled = false;
      els.inspectMigration.textContent = "Inspect collection";
    }
  }

  function formPayload() {
    if (state.mode === "mint") {
      return {
        mode: state.mode,
        walletAddress: state.walletAddress,
        publicKey: state.publicKey,
        collectionId: els.collectionId.value.trim().toLowerCase(),
      };
    }
    if (state.mode === "transfer") {
      return {
        mode: state.mode,
        walletAddress: state.walletAddress,
        publicKey: state.publicKey,
        nftId: els.nftId.value.trim().toLowerCase(),
        recipientAddress: els.recipientAddress.value.trim().toLowerCase(),
      };
    }
    if (state.mode === "migration-issue") {
      return {
        mode: state.mode,
        walletAddress: state.walletAddress,
        publicKey: state.publicKey,
        collectionId: els.migrationCollectionId.value.trim().toLowerCase(),
        tokenId: els.migrationTokenId.value,
        recipientAddress: els.migrationRecipient.value.trim().toLowerCase(),
      };
    }
    return {
      mode: state.mode,
      walletAddress: state.walletAddress,
      publicKey: state.publicKey,
      ticker: els.ticker.value.trim(),
      supply: els.supply.value,
      metadataUrl: els.metadata.value.trim(),
      mintPriceKas: els.mintPrice.value,
      premint: "0",
      mintDaaScore: els.mintDaa.value,
    };
  }

  function normalizeUtxo(item) {
    const entry = item?.entry || item?.utxoEntry || item || {};
    const outpoint = entry.outpoint || item?.outpoint || {};
    const spk = entry.scriptPublicKey || item?.scriptPublicKey || {};
    const version = Number(spk.version ?? 0);
    const script = String(spk.script || "").toLowerCase();
    const amount = entry.amount ?? item?.amount ?? "0";
    const covenantId = entry.covenantId ?? item?.covenantId ?? null;
    if (!/^[0-9a-f]{64}$/i.test(String(outpoint.transactionId || "")) || !/^[0-9a-f]+$/i.test(script)) return null;
    if (covenantId) return null;
    const versionHex = (version & 255).toString(16).padStart(2, "0") + ((version >> 8) & 255).toString(16).padStart(2, "0");
    return {
      transactionId: String(outpoint.transactionId).toLowerCase(),
      index: Number(outpoint.index || 0),
      amount: String(amount),
      scriptPublicKey: `${versionHex}${script}`,
      blockDaaScore: String(entry.blockDaaScore ?? item?.blockDaaScore ?? 0),
      isCoinbase: Boolean(entry.isCoinbase ?? item?.isCoinbase),
    };
  }

  async function selectFundingUtxoForMinimum(minimum, purpose = "operation", { preferSmallest = false } = {}) {
    let raw = [];
    try {
      const query = new URLSearchParams({ address: state.walletAddress });
      const indexed = await readJson(await withTimeout(
        fetch(`/api/kcc721/wallet-utxos?${query}`, { cache: "no-store" }),
        "Mainnet UTXO lookup timed out.",
        12000,
      ));
      raw = Array.isArray(indexed.items) ? indexed.items : [];
    } catch (indexerError) {
      if (!window.kasware?.getUtxoEntries) throw indexerError;
      raw = await withTimeout(
        window.kasware.getUtxoEntries(state.walletAddress),
        "Neither Mainnet nor Kasware returned current wallet UTXOs.",
        8000,
      );
    }
    minimum = BigInt(minimum);
    const entries = (Array.isArray(raw) ? raw : [])
      .map(normalizeUtxo)
      .filter(Boolean)
      .filter((item) => !item.isCoinbase && BigInt(item.amount) >= minimum)
      .sort((a, b) => {
        const left = BigInt(a.amount);
        const right = BigInt(b.amount);
        if (left === right) return 0;
        if (preferSmallest) return left < right ? -1 : 1;
        return left > right ? -1 : 1;
      });
    if (!entries.length) {
      throw new Error(`The wallet needs one confirmed plain UTXO of at least ${Number(minimum) / 100_000_000} KAS for this ${purpose}.`);
    }
    return entries[0];
  }

  function deploymentMinimum(premint) {
    return BigInt(50_000_000) * BigInt(1 + Number(premint || 0)) + BigInt(20_000_000);
  }

  function renderPlan() {
    els.planEmpty.hidden = Boolean(state.plan);
    els.protocolPlan.hidden = !state.plan;
    els.signPlan.disabled = !state.plan || state.plan.mode === "mint-queued";
    els.downloadPlan.disabled = !state.plan;
    els.riskWrap.hidden = !state.plan || state.plan.status === "accepted" || state.plan.mode === "mint-queued";
    if (!state.plan) return;
    const plan = state.plan;
    els.protocolPlan.innerHTML = `
      <div><span>Mode</span><strong>${escapeHtml(plan.mode)}</strong></div>
      ${plan.ticker ? `<div><span>Collection</span><strong>${escapeHtml(plan.ticker)}</strong></div>` : ""}
      ${plan.maxSupply ? `<div><span>Supply</span><strong>${escapeHtml(plan.maxSupply)}</strong></div>` : ""}
      ${plan.metadataUri ? `<div><span>Metadata</span><code>${escapeHtml(plan.metadataUri)}</code></div>` : ""}
      ${plan.metadataDigest ? `<div><span>Metadata commitment</span><code>${escapeHtml(plan.metadataDigest)}</code></div>` : ""}
      ${plan.shuffleRoot ? `<div><span>Blind shuffle commitment</span><code>${escapeHtml(plan.shuffleRoot)}</code></div>` : ""}
      ${plan.mintIndex ? `<div><span>Mint position</span><strong>${escapeHtml(plan.mintIndex)}${plan.mode === "mint-commit" ? " (outcome hidden)" : ""}</strong></div>` : ""}
      ${plan.queuePosition ? `<div><span>Queue position</span><strong>${escapeHtml(plan.queuePosition)}</strong></div>` : ""}
      ${plan.tokenId !== undefined ? `<div><span>Token ID</span><strong>${escapeHtml(plan.tokenId)}</strong></div>` : ""}
      ${plan.nftId ? `<div><span>NFT ID</span><code>${escapeHtml(plan.nftId)}</code></div>` : ""}
      ${plan.nftCount ? `<div><span>Atomic batch</span><strong>${escapeHtml(plan.nftCount)} NFTs</strong></div>` : ""}
      ${plan.recipientAddress ? `<div><span>Recipient</span><code>${escapeHtml(plan.recipientAddress)}</code></div>` : ""}
      ${plan.migration ? `<div><span>Migration status</span><strong>${escapeHtml(plan.migration.status)}</strong></div>` : ""}
      ${plan.migration?.sourceDeployTransactionId ? `<div><span>KRC721 deploy tx</span><code>${escapeHtml(plan.migration.sourceDeployTransactionId)}</code></div>` : ""}
      ${plan.migration?.sourceDeployer ? `<div><span>KRC721 deployer</span><code>${escapeHtml(plan.migration.sourceDeployer)}</code></div>` : ""}
      ${plan.migration ? `<div><span>KRC721 minted</span><strong>${escapeHtml(plan.migration.mintedAtPreview)}</strong></div>` : ""}
      ${plan.manifestHash ? `<div><span>Manifest hash</span><code>${escapeHtml(plan.manifestHash)}</code></div>` : ""}
      ${plan.collectionId ? `<div><span>Collection ID</span><code>${escapeHtml(plan.collectionId)}</code></div>` : ""}
      ${plan.transactionId ? `<div><span>Transaction ID</span><code>${escapeHtml(plan.transactionId)}</code></div>` : ""}
      ${plan.mintPriceSompi !== undefined ? `<div><span>Mint price</span><strong>${formatSompi(plan.mintPriceSompi)}</strong></div>` : ""}
      ${plan.controllerValueSompi ? `<div><span>Controller cell</span><strong>${formatSompi(plan.controllerValueSompi)} locked</strong></div>` : ""}
      ${plan.nftValueSompi ? `<div><span>NFT cell</span><strong>${formatSompi(plan.nftValueSompi)} locked per NFT</strong></div>` : ""}
      ${plan.feeSompi ? `<div><span>Network fee</span><strong>${formatSompi(plan.feeSompi)}</strong></div>` : ""}
      ${plan.storageMass ? `<div><span>Storage mass</span><strong>${escapeHtml(plan.storageMass)}</strong></div>` : ""}
      <div><span>Status</span><strong>${escapeHtml(plan.status)}</strong></div>
    `;
    els.signPlan.textContent = plan.mode === "mint-queued" ? "Waiting for mint slot" : "Sign & broadcast Mainnet";
  }

  async function continueQueuedMint(queuePlan, minimum) {
    savePendingMint(queuePlan);
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const query = new URLSearchParams({
        operationId: queuePlan.operationId,
        walletAddress: state.walletAddress,
      });
      const queue = await readJson(await fetch(`/api/kcc721/mint-queue?${query}`, { cache: "no-store" }));
      if (queue.expired) throw new Error("The inactive mint queue position expired. Prepare the mint again.");
      queuePlan = { ...queuePlan, ...queue };
      state.plan = queuePlan;
      savePendingMint(queuePlan);
      renderPlan();
      if (!queue.ready) continue;

      els.prepare.textContent = "Mint slot ready - reading wallet UTXOs...";
      const payload = formPayload();
      payload.queueOperationId = queue.operationId;
      payload.fundingUtxo = await selectFundingUtxoForMinimum(minimum);
      const prepared = await readJson(await withTimeout(
        fetch("/api/kcc721/prepare-mint", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        }),
        "KCC721 mint preparation timed out.",
        60000,
      ));
      if (prepared.mode === "mint-queued") {
        queuePlan = prepared;
        continue;
      }
      clearPendingMint();
      state.plan = prepared;
      els.riskConfirm.checked = false;
      renderPlan();
      toast("Your mint slot is ready. Review and sign the Mainnet transaction.");
      return prepared;
    }
  }

  async function prepare(event) {
    event.preventDefault();
    els.prepare.disabled = true;
    els.prepare.textContent = !state.walletAddress || !state.publicKey ? "Connecting Kasware..." : "Preparing...";
    try {
      if (!state.walletAddress || !state.publicKey) await connect();
      els.prepare.textContent = "Preparing...";
      if (state.mode === "migrate" && !state.migration?.eligible) await inspectMigration();
      const payload = formPayload();
      let endpoint = "/api/kcc721/plan";
      let mintMinimum = 0n;
      if (state.mode === "deploy" || state.mode === "migrate") {
        els.prepare.textContent = "Reading wallet UTXOs...";
        const premint = 0;
        payload.fundingUtxo = await selectFundingUtxoForMinimum(deploymentMinimum(premint));
        endpoint = "/api/kcc721/prepare-deploy";
      } else if (state.mode === "mint") {
        if (!/^[0-9a-f]{64}$/.test(payload.collectionId)) throw new Error("Enter a valid 64-character collection ID.");
        els.prepare.textContent = "Loading collection...";
        const collectionResponse = await withTimeout(
          fetch(`/api/kcc721/collection?id=${encodeURIComponent(payload.collectionId)}`, { cache: "no-store" }),
          "The KCC721 registry did not respond.",
          20000,
        );
        const collection = await readJson(collectionResponse);
        mintMinimum = BigInt(50_000_000) + BigInt(collection.mintPriceSompi || 0) + BigInt(20_000_000);
        els.prepare.textContent = "Reading wallet UTXOs...";
        payload.fundingUtxo = await selectFundingUtxoForMinimum(mintMinimum);
        endpoint = "/api/kcc721/prepare-mint";
      } else if (state.mode === "transfer") {
        if (!/^[0-9a-f]{64}$/.test(payload.nftId)) throw new Error("Enter a valid 64-character NFT ID.");
        if (!payload.recipientAddress.startsWith("kaspa:")) throw new Error("Enter a Mainnet Kaspa destination address.");
        els.prepare.textContent = "Reading current wallet UTXOs...";
        payload.fundingUtxo = await selectFundingUtxoForMinimum(BigInt(20_000_000), "transfer");
        endpoint = "/api/kcc721/prepare-transfer";
      } else if (state.mode === "migration-issue") {
        if (!/^[0-9a-f]{64}$/.test(payload.collectionId)) throw new Error("Invalid migration collection ID.");
        if (!/^[0-9]+$/.test(String(payload.tokenId)) || Number(payload.tokenId) < 1) throw new Error("Invalid source token ID.");
        if (!payload.recipientAddress.startsWith("kaspa:")) throw new Error("Enter a Mainnet Kaspa destination address.");
        els.prepare.textContent = "Reading current wallet UTXOs...";
        payload.fundingUtxo = await selectFundingUtxoForMinimum(BigInt(70_000_000), "migration airdrop");
        endpoint = "/api/kcc721/prepare-migration-issue";
      }
      els.prepare.textContent = "Building transaction...";
      const prepareResponse = await withTimeout(
        fetch(endpoint, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        }),
        "KCC721 transaction preparation timed out.",
        60000,
      );
      state.plan = await readJson(prepareResponse);
      if (state.plan.mode === "mint-queued") {
        renderPlan();
        toast(`Mint request queued at position ${state.plan.queuePosition}. It will advance automatically.`);
        await continueQueuedMint(state.plan, mintMinimum);
        return;
      }
      els.riskConfirm.checked = false;
      renderPlan();
      toast("Mainnet transaction prepared. Review it before signing.");
    } finally {
      els.prepare.disabled = false;
      els.prepare.textContent = prepareLabel();
    }
  }

  async function signPlan() {
    if (!state.plan) throw new Error("Prepare a protocol plan first.");
    return signAndBroadcast();
  }

  function extractTxid(value, fallback) {
    if (typeof value === "string" && /^[0-9a-f]{64}$/i.test(value.trim())) return value.trim().toLowerCase();
    try {
      const parsed = typeof value === "string" ? JSON.parse(value) : value;
      const candidate = parsed?.id || parsed?.txid || parsed?.transactionId;
      if (/^[0-9a-f]{64}$/i.test(String(candidate || ""))) return String(candidate).toLowerCase();
    } catch {
      // Kasware may return a bare transaction ID instead of JSON.
    }
    return fallback;
  }

  async function waitForAcceptance(txid) {
    for (let attempt = 0; attempt < 45; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const response = await fetch(`/api/kcc721/transaction?txid=${encodeURIComponent(txid)}`, { cache: "no-store" });
      if (response.ok) {
        const result = await readJson(response);
        if (result.registryError) throw new Error(result.registryError);
        if (result.accepted) return true;
      }
    }
    return false;
  }

  function closeMintSuccess() {
    els.mintSuccessModal.hidden = true;
  }

  async function loadMintedNft(plan) {
    els.mintSuccessModal.hidden = false;
    els.mintSuccessTicker.textContent = plan.ticker || "KCC721";
    els.mintSuccessName.textContent = `${plan.ticker || "KCC721"} token ${plan.tokenId}`;
    els.mintSuccessTokenId.textContent = String(plan.tokenId);
    els.mintSuccessNftId.textContent = plan.nftId || "Indexing...";
    els.mintSuccessImage.src = "/assets/devtools-logo-uploaded.png?v=2";
    els.mintSuccessStatus.textContent = "Mainnet accepted. Loading indexed metadata...";
    els.openMintedNft.href = `/kcc721/nft?id=${encodeURIComponent(plan.collectionId)}&tokenId=${encodeURIComponent(plan.tokenId)}`;

    for (let attempt = 0; attempt < 12; attempt += 1) {
      try {
        const detail = await readJson(await fetch(`/api/kcc721/nft-detail?id=${encodeURIComponent(plan.collectionId)}&tokenId=${encodeURIComponent(plan.tokenId)}`, { cache: "no-store" }));
        const metadata = detail.metadata || {};
        els.mintSuccessName.textContent = metadata.name || `${plan.ticker || "KCC721"} token ${plan.tokenId}`;
        els.mintSuccessImage.src = detail.imageUrl || "/assets/devtools-logo-uploaded.png?v=2";
        els.mintSuccessImage.alt = metadata.name || "Minted KCC721 NFT";
        els.mintSuccessNftId.textContent = detail.kcc721NftId || plan.nftId || "Indexing...";
        if (detail.kcc721State === "live") {
          els.mintSuccessStatus.textContent = "NFT indexed and ready in your wallet.";
          return;
        }
      } catch {
        // Acceptance can arrive a moment before the local registry update.
      }
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
    els.mintSuccessStatus.textContent = "Mint accepted. Registry indexing is still finishing in the background.";
  }

  async function loadWalletNfts({ append = false } = {}) {
    if (append && state.walletNftLoading) return;
    const requestId = append
      ? (loadWalletNfts.requestId || 0)
      : (loadWalletNfts.requestId || 0) + 1;
    if (!append) {
      loadWalletNfts.requestId = requestId;
      state.walletNftNext = 0;
      els.walletNftGrid.innerHTML = "";
      els.loadMoreWalletNfts.hidden = true;
      state.selectedNftIds.clear();
      updateBatchSelection();
    }
    state.walletNftLoading = true;
    els.refreshWalletNfts.disabled = true;
    els.loadMoreWalletNfts.disabled = true;
    if (!state.walletAddress) {
      els.walletNftStatus.textContent = "Connect Kasware to load your NFTs.";
      els.walletNftGrid.innerHTML = "";
      els.loadMoreWalletNfts.hidden = true;
      state.walletNftLoading = false;
      els.refreshWalletNfts.disabled = false;
      return;
    }
    els.walletNftStatus.textContent = append ? "Loading more wallet NFTs..." : "Loading wallet NFTs...";
    els.loadMoreWalletNfts.textContent = "Loading...";
    try {
      const query = new URLSearchParams({
        address: state.walletAddress,
        offset: String(append ? state.walletNftNext : 0),
        limit: String(WALLET_NFT_PAGE_SIZE),
      });
      const data = await readJson(await fetch(`/api/kcc721/wallet-nfts?${query}`, { cache: "no-store" }));
      if (requestId !== loadWalletNfts.requestId) return;
      const items = Array.isArray(data.items) ? data.items : [];
      const total = Number(data.total || 0);
      els.walletNftGrid.insertAdjacentHTML("beforeend", items.map((item) => `
        <article class="kcc-wallet-nft-item" data-nft-id="${escapeHtml(item.nftId || "")}">
          ${item.nftId ? `<label class="kcc-nft-select" title="Select ${escapeHtml(item.name)}">
            <input type="checkbox" value="${escapeHtml(item.nftId)}" aria-label="Select ${escapeHtml(item.name)} for atomic transfer" />
            <span></span>
          </label>` : ""}
          <a class="kcc-wallet-nft-link" href="${escapeHtml(item.detailUrl)}">
            <div class="kcc-wallet-nft-image">
              <img src="${escapeHtml(item.imageUrl || "/assets/devtools-logo-uploaded.png?v=2")}" alt="${escapeHtml(item.name)}" loading="lazy" decoding="async" />
            </div>
            <div class="kcc-wallet-nft-copy">
              <strong>${escapeHtml(item.name)}</strong>
              <span>${escapeHtml(item.ticker)} #${escapeHtml(item.tokenId)}</span>
              <span>${escapeHtml(item.nftId || item.custodyState)}</span>
            </div>
          </a>
        </article>
      `).join(""));
      els.walletNftGrid.classList.toggle("selecting", state.batchSelectionMode);
      updateBatchSelection();
      state.walletNftNext = data.next === null || data.next === undefined ? null : Number(data.next);
      const loaded = els.walletNftGrid.children.length;
      els.walletNftStatus.textContent = total
        ? `${loaded.toLocaleString()} of ${total.toLocaleString()} KCC721 NFTs loaded`
        : "No KCC721 NFTs currently held by this wallet.";
      els.loadMoreWalletNfts.hidden = state.walletNftNext === null;
    } catch (error) {
      if (requestId === loadWalletNfts.requestId) els.walletNftStatus.textContent = error.message || String(error);
    } finally {
      if (requestId === loadWalletNfts.requestId) {
        state.walletNftLoading = false;
        els.refreshWalletNfts.disabled = false;
        els.loadMoreWalletNfts.disabled = false;
        els.loadMoreWalletNfts.textContent = "Load more";
      }
    }
  }

  async function prepareAtomicBatchTransfer() {
    if (!state.walletAddress || !state.publicKey) await connect();
    const nftIds = [...state.selectedNftIds];
    if (nftIds.length < 2 || nftIds.length > 22) throw new Error("Select between 2 and 22 live KCC721 NFTs.");
    const recipientAddress = els.batchRecipientAddress.value.trim().toLowerCase();
    if (!recipientAddress.startsWith("kaspa:")) throw new Error("Enter a Mainnet Kaspa destination address.");
    els.prepareBatchTransfer.disabled = true;
    els.prepareBatchTransfer.textContent = "Reading wallet UTXOs...";
    try {
      const fundingUtxo = await selectFundingUtxoForMinimum(
        BigInt(100_000_000),
        "atomic batch transfer",
        { preferSmallest: true },
      );
      els.prepareBatchTransfer.textContent = "Building atomic transaction...";
      state.plan = await readJson(await withTimeout(fetch("/api/kcc721/prepare-batch-transfer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          walletAddress: state.walletAddress,
          publicKey: state.publicKey,
          recipientAddress,
          nftIds,
          fundingUtxo,
        }),
      }), "KCC721 atomic batch preparation timed out.", 60000));
      els.riskConfirm.checked = false;
      renderPlan();
      toast(`${nftIds.length} NFTs prepared in one atomic Mainnet transaction.`);
    } finally {
      els.prepareBatchTransfer.textContent = "Prepare atomic transfer";
      updateBatchSelection();
    }
  }

  function savePendingMint(plan) {
    localStorage.setItem(PENDING_MINT_KEY, JSON.stringify({ ...plan, walletAddress: state.walletAddress }));
  }

  function clearPendingMint() {
    localStorage.removeItem(PENDING_MINT_KEY);
  }

  async function resumePendingMint() {
    let pending = null;
    try {
      pending = JSON.parse(localStorage.getItem(PENDING_MINT_KEY) || "null");
    } catch {
      clearPendingMint();
    }
    if (!pending || pending.walletAddress !== state.walletAddress || !["mint-queued", "mint-commit", "mint-reveal"].includes(pending.mode)) return;
    state.plan = pending;
    renderPlan();
    if (pending.mode === "mint-queued") {
      const collection = await readJson(await fetch(`/api/kcc721/collection?id=${encodeURIComponent(pending.collectionId)}`, { cache: "no-store" }));
      const minimum = BigInt(50_000_000) + BigInt(collection.mintPriceSompi || 0) + BigInt(20_000_000);
      await continueQueuedMint(pending, minimum);
      return;
    }
    if (pending.mode === "mint-reveal") {
      if (pending.transactionId && await waitForAcceptance(pending.transactionId)) {
        pending.status = "accepted";
        state.plan = pending;
        clearPendingMint();
        renderPlan();
        await loadIndexer();
        await loadMintedNft(pending);
        return;
      }
      const txid = await pushReviewedTransaction(pending, false);
      savePendingMint({ ...pending, transactionId: txid, status: "submitted" });
      if (await registerAndWait(pending, txid)) {
        pending.status = "accepted";
        clearPendingMint();
        renderPlan();
        await loadIndexer();
        await loadMintedNft(pending);
      }
      return;
    }
    if (pending.transactionId && await waitForAcceptance(pending.transactionId)) {
      pending.status = "accepted";
      state.plan = pending;
      renderPlan();
      await revealBlindMint(pending);
    } else {
      toast("A blind mint commitment is still waiting for Mainnet acceptance.");
    }
  }

  async function pushReviewedTransaction(plan, needsSignature) {
    let transaction = plan.txJsonString;
    if (needsSignature) {
      toast("Kasware approval requested. Open the extension if its window is hidden.");
      transaction = await withTimeout(
        window.kasware.signPskt({
          txJsonString: plan.txJsonString,
          options: { signInputs: plan.signInputs },
        }),
        "Kasware did not answer the signing request within 60 seconds. Open Kasware, close any pending approval and prepare the batch again.",
        60000,
      );
    }
    try {
      const pushed = await window.kasware.pushTx(transaction);
      return extractTxid(pushed, extractTxid(transaction, plan.transactionId));
    } catch (error) {
      if (plan.transactionId && await waitForAcceptance(plan.transactionId)) return plan.transactionId;
      throw error;
    }
  }

  async function cancelPreparedBatch(plan) {
    if (plan?.mode !== "batch-transfer" || plan?.status !== "atomic batch prepared for one Kasware approval") return;
    try {
      await fetch("/api/kcc721/cancel-operation", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ operationId: plan.operationId, walletAddress: state.walletAddress }),
      });
      plan.status = "signing cancelled - prepare the batch again";
      state.plan = plan;
      renderPlan();
    } catch {
      // The prepared reservation expires server-side even if cancellation cannot be confirmed.
    }
  }

  async function registerAndWait(plan, txid) {
    if (!/^[0-9a-f]{64}$/.test(txid || "")) throw new Error("Kasware did not return a valid Mainnet transaction ID.");
    await readJson(await fetch("/api/kcc721/register-broadcast", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ operationId: plan.operationId, txid }),
    }));
    plan.transactionId = txid;
    plan.status = "submitted - waiting for Mainnet acceptance";
    state.plan = plan;
    renderPlan();
    return waitForAcceptance(txid);
  }

  async function revealBlindMint(commitPlan) {
    toast("Payment accepted. Revealing the random NFT...");
    const reveal = await readJson(await fetch("/api/kcc721/prepare-reveal", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ commitOperationId: commitPlan.operationId, walletAddress: state.walletAddress }),
    }));
    state.plan = reveal;
    savePendingMint(reveal);
    renderPlan();
    els.signPlan.textContent = "Broadcasting reveal...";
    const revealTxid = await pushReviewedTransaction(reveal, false);
    savePendingMint({ ...reveal, transactionId: revealTxid, status: "submitted" });
    if (!await registerAndWait(reveal, revealTxid)) {
      reveal.status = "reveal submitted - acceptance check still pending";
      renderPlan();
      throw new Error("The reveal is submitted and still waiting for Mainnet acceptance.");
    }
    reveal.status = "accepted";
    state.plan = reveal;
    clearPendingMint();
    renderPlan();
    await loadIndexer();
    await loadWalletNfts();
    await loadMintedNft(reveal);
    toast("Random KCC721 NFT revealed and accepted on Mainnet.");
  }

  async function signAndBroadcast() {
    if (state.broadcasting) return;
    if (!els.riskConfirm.checked) throw new Error("Confirm the experimental Mainnet warning before signing.");
    if (!window.kasware?.signPskt || !window.kasware?.pushTx) {
      throw new Error("This Kasware version does not support PSKT signing and broadcasting.");
    }
    state.broadcasting = true;
    els.signPlan.disabled = true;
    els.signPlan.textContent = "Waiting for Kasware...";
    try {
      const plan = state.plan;
      if (plan.mode === "mint-commit" && plan.status === "accepted") {
        await revealBlindMint(plan);
        return;
      }
      const txid = await pushReviewedTransaction(plan, true);
      plan.transactionId = txid;
      plan.status = "submitted - registering Mainnet transaction";
      state.plan = plan;
      renderPlan();
      if (plan.mode === "mint-commit") savePendingMint({ ...plan, transactionId: txid, status: "submitted" });
      toast("KCC721 transaction submitted to Mainnet.");
      if (await registerAndWait(plan, txid)) {
        plan.status = "accepted";
        state.plan = plan;
        renderPlan();
        await loadIndexer();
        await loadWalletNfts();
        toast("KCC721 transaction accepted on Mainnet.");
        if (plan.mode === "mint-commit") {
          savePendingMint(plan);
          await revealBlindMint(plan);
        } else if (plan.mode === "mint") {
          await loadMintedNft(plan);
        }
      } else {
        plan.status = "submitted - acceptance check still pending";
        state.plan = plan;
        if (plan.mode === "mint-commit") savePendingMint(plan);
        renderPlan();
      }
    } catch (error) {
      await cancelPreparedBatch(state.plan);
      throw error;
    } finally {
      state.broadcasting = false;
      els.signPlan.disabled = state.plan?.status === "accepted";
      els.signPlan.textContent = state.plan?.status === "accepted" ? "Accepted on Mainnet" : "Sign & broadcast Mainnet";
    }
  }

  function downloadPlan() {
    if (!state.plan) return;
    const blob = new Blob([JSON.stringify(state.plan, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    const subject = state.plan.ticker || state.plan.nftId || state.plan.collectionId || "operation";
    link.download = `kcc721-${String(subject).toLowerCase()}-${state.plan.mode}-plan.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  async function loadIndexer() {
    els.indexerRefresh.disabled = true;
    els.indexerStatus.textContent = "Loading accepted Mainnet collections...";
    try {
      const query = new URLSearchParams();
      const search = els.indexerSearch.value.trim();
      if (search) query.set("q", search);
      const data = await readJson(await fetch(`/api/kcc721/collections?${query}`, { cache: "no-store" }));
      const items = Array.isArray(data.items) ? data.items : [];
      els.indexerStatus.textContent = items.length ? `${items.length} accepted Mainnet collection${items.length === 1 ? "" : "s"}` : "No accepted KCC721 Mainnet collections indexed yet.";
      els.collectionList.innerHTML = items.map((item) => `
        <article class="kcc-collection-item">
          <div class="kcc-collection-title">
            <a href="/kcc721/collection?id=${encodeURIComponent(item.collectionId)}">${escapeHtml(item.ticker)}</a>
            <span>${escapeHtml(item.mode === "migration" ? "migration" : item.status)}</span>
          </div>
          <div class="kcc-collection-stats">
            <div><span>Minted</span><strong>${escapeHtml(item.minted)} / ${escapeHtml(item.maxSupply)}</strong></div>
            <div><span>Indexed NFTs</span><strong>${escapeHtml(item.indexedNfts)}</strong></div>
            <div><span>Mint price</span><strong>${formatSompi(item.mintPriceSompi)}</strong></div>
          </div>
          <dl class="kcc-collection-data">
            <dt>Collection ID</dt><dd><code>${escapeHtml(item.collectionId)}</code></dd>
            <dt>Deployer</dt><dd><code>${escapeHtml(item.deployerAddress)}</code></dd>
            <dt>Controller tx</dt><dd><code>${escapeHtml(item.controllerTransactionId)}</code></dd>
            <dt>Metadata</dt><dd><code>${escapeHtml(item.metadataUri)}</code></dd>
            ${item.mode === "migration" ? `<dt>Migration</dt><dd><code>${escapeHtml(item.migrationPhase || "manual issue")}</code></dd>` : ""}
            ${item.sourceDeployTransactionId ? `<dt>KRC721 tx</dt><dd><code>${escapeHtml(item.sourceDeployTransactionId)}</code></dd>` : ""}
          </dl>
        </article>
      `).join("");
    } catch (error) {
      els.indexerStatus.textContent = error.message || String(error);
      els.collectionList.innerHTML = "";
    } finally {
      els.indexerRefresh.disabled = false;
    }
  }

  document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  els.connect.addEventListener("click", () => connect().catch((error) => toast(error.message || String(error))));
  els.disconnect.addEventListener("click", () => disconnect().catch((error) => toast(error.message || String(error))));
  els.inspectMigration.addEventListener("click", () => inspectMigration().catch((error) => toast(error.message || String(error))));
  els.form.addEventListener("submit", (event) => prepare(event).catch((error) => toast(error.message || String(error))));
  els.signPlan.addEventListener("click", () => signPlan().catch((error) => toast(error.message || String(error))));
  els.downloadPlan.addEventListener("click", downloadPlan);
  els.closeMintSuccess.addEventListener("click", closeMintSuccess);
  els.closeMintSuccessBackdrop.addEventListener("click", closeMintSuccess);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.mintSuccessModal.hidden) closeMintSuccess();
  });
  els.indexerRefresh.addEventListener("click", loadIndexer);
  els.refreshWalletNfts.addEventListener("click", () => loadWalletNfts());
  els.loadMoreWalletNfts.addEventListener("click", () => loadWalletNfts({ append: true }));
  els.toggleBatchTransfer.addEventListener("click", () => setBatchSelectionMode(!state.batchSelectionMode));
  els.clearBatchSelection.addEventListener("click", () => {
    state.selectedNftIds.clear();
    updateBatchSelection();
  });
  els.prepareBatchTransfer.addEventListener("click", () => prepareAtomicBatchTransfer().catch((error) => toast(error.message || String(error))));
  els.walletNftGrid.addEventListener("change", (event) => {
    const checkbox = event.target.closest(".kcc-nft-select input");
    if (!checkbox) return;
    if (checkbox.checked && state.selectedNftIds.size >= 22) {
      checkbox.checked = false;
      toast("Mainnet Storage Mass limits one atomic batch to 22 NFTs.");
      return;
    }
    if (checkbox.checked) state.selectedNftIds.add(checkbox.value);
    else state.selectedNftIds.delete(checkbox.value);
    updateBatchSelection();
  });
  els.indexerSearch.addEventListener("input", () => {
    clearTimeout(els.indexerSearch._timer);
    els.indexerSearch._timer = setTimeout(loadIndexer, 250);
  });

  const saved = localStorage.getItem("kaspa-devtools-wallet") || "";
  if (saved.startsWith("kaspa:")) state.walletAddress = saved;
  updateWallet();
  const initialParams = new URLSearchParams(window.location.search);
  const initialCollectionId = String(initialParams.get("collectionId") || "").trim().toLowerCase();
  const initialNftId = String(initialParams.get("nftId") || "").trim().toLowerCase();
  const initialMigrationCollectionId = String(initialParams.get("collectionId") || "").trim().toLowerCase();
  const initialMigrationTokenId = String(initialParams.get("tokenId") || "").trim();
  if (/^[0-9a-f]{64}$/.test(initialCollectionId)) els.collectionId.value = initialCollectionId;
  if (/^[0-9a-f]{64}$/.test(initialNftId)) els.nftId.value = initialNftId;
  if (/^[0-9a-f]{64}$/.test(initialMigrationCollectionId)) els.migrationCollectionId.value = initialMigrationCollectionId;
  if (/^[0-9]+$/.test(initialMigrationTokenId)) els.migrationTokenId.value = initialMigrationTokenId;
  const initialMode = initialParams.get("mode");
  setMode(["mint", "transfer", "migration-issue"].includes(initialMode) ? initialMode : "deploy");
  loadIndexer();
  loadWalletNfts();
})();
