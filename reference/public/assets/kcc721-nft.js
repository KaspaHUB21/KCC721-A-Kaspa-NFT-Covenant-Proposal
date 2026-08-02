(() => {
  const params = new URLSearchParams(window.location.search);
  const collectionId = String(params.get("id") || "").toLowerCase();
  const tokenId = String(params.get("tokenId") || "");
  const els = {
    loading: document.querySelector("#nftLoading"),
    content: document.querySelector("#nftContent"),
    back: document.querySelector("#backToCollection"),
    image: document.querySelector("#nftImage"),
    ticker: document.querySelector("#nftTicker"),
    name: document.querySelector("#nftName"),
    migrationStatus: document.querySelector("#nftMigrationStatus"),
    transfer: document.querySelector("#transferNft"),
    description: document.querySelector("#nftDescription"),
    data: document.querySelector("#nftData"),
    attributes: document.querySelector("#nftAttributes"),
    history: document.querySelector("#nftHistory"),
    historyCount: document.querySelector("#nftHistoryCount"),
    raw: document.querySelector("#nftRawMetadata"),
  };

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  function ipfsUrl(value) {
    const raw = String(value || "").trim();
    return raw.startsWith("ipfs://") ? `https://ipfs.io/ipfs/${raw.slice(7)}` : raw;
  }

  async function readJson(response) {
    const text = await response.text();
    const data = text ? JSON.parse(text) : {};
    if (!response.ok) throw new Error(data.error || `Request failed with HTTP ${response.status}.`);
    return data;
  }

  function showError(error) {
    els.loading.classList.add("error-text");
    els.loading.textContent = error.message || String(error);
  }

  function displayTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value || "Unknown");
    return `${date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, "")} UTC`;
  }

  function renderHistory(history) {
    const items = Array.isArray(history) ? history : [];
    els.historyCount.textContent = `${items.length} ${items.length === 1 ? "outpoint" : "outpoints"}`;
    if (!items.length) {
      els.history.innerHTML = '<p class="kcc-history-empty">No live KCC721 UTXO has been issued for this NFT.</p>';
      return;
    }
    els.history.innerHTML = items.map((entry) => `
      <article class="kcc-history-entry${entry.isCurrent ? " is-current" : ""}">
        <header>
          <div><span>Step ${escapeHtml(entry.step)}</span><strong>${escapeHtml(entry.eventType)}</strong></div>
          <b>${entry.isCurrent ? "UNSPENT" : "SPENT"}</b>
        </header>
        <dl>
          <dt>Created outpoint</dt><dd><code>${escapeHtml(entry.outpoint)}</code></dd>
          <dt>Spent outpoint</dt><dd>${entry.previousOutpoint ? `<code>${escapeHtml(entry.previousOutpoint)}</code>` : "Genesis"}</dd>
          <dt>From</dt><dd>${entry.fromAddress ? `<code>${escapeHtml(entry.fromAddress)}</code>` : "Genesis"}</dd>
          <dt>Owner</dt><dd><code>${escapeHtml(entry.ownerAddress)}</code></dd>
          <dt>Accepted</dt><dd><time datetime="${escapeHtml(entry.acceptedAt)}">${escapeHtml(displayTime(entry.acceptedAt))}</time></dd>
        </dl>
      </article>
    `).join("");
  }

  function render(item) {
    const metadata = item.metadata || {};
    const attributes = Array.isArray(metadata.attributes) ? metadata.attributes : [];
    const name = metadata.name || `${item.ticker} #${item.tokenId}`;
    document.title = `${name} | KCC721`;
    els.back.href = `/kcc721/collection?id=${encodeURIComponent(item.collectionId)}`;
    els.ticker.textContent = item.ticker;
    els.name.textContent = name;
    els.migrationStatus.textContent = item.kcc721State === "live"
      ? "This KCC721 NFT is live. Its owner is resolved from the latest indexed covenant outpoint."
      : item.mode === "migration"
        ? "Linked source NFT. No KCC721 NFT has been issued yet; custody remains with the deployer."
        : "Metadata preview. This KCC721 NFT has not been minted and has no owner.";
    if (item.kcc721State === "live" && /^[0-9a-f]{64}$/.test(String(item.kcc721NftId || ""))) {
      const transferQuery = new URLSearchParams({ mode: "transfer", nftId: item.kcc721NftId });
      els.transfer.href = `/kcc721?${transferQuery}`;
      els.transfer.hidden = false;
      els.transfer.textContent = "Send NFT";
    } else if (item.canMigrationIssue) {
      const issueQuery = new URLSearchParams({
        mode: "migration-issue",
        collectionId: item.collectionId,
        tokenId: item.tokenId,
      });
      els.transfer.href = `/kcc721?${issueQuery}`;
      els.transfer.textContent = "Airdrop NFT";
      els.transfer.hidden = false;
    } else {
      els.transfer.hidden = true;
    }
    els.description.textContent = metadata.description || "No description in the linked metadata.";
    els.image.src = ipfsUrl(metadata.image) || item.imageUrl;
    els.image.alt = name;
    els.image.onerror = () => {
      if (els.image.src !== item.imageUrl) els.image.src = item.imageUrl;
    };
    els.data.innerHTML = `
      <dt>Token ID</dt><dd><strong>${escapeHtml(item.tokenId)}</strong></dd>
      <dt>KCC721 owner / custody</dt><dd>${item.kcc721Owner ? `<code>${escapeHtml(item.kcc721Owner)}</code><br>` : ""}<small>${escapeHtml(item.kcc721OwnerType)}</small></dd>
      <dt>KCC721 state</dt><dd><strong>${escapeHtml(item.kcc721State)}</strong></dd>
      ${item.kcc721NftId ? `<dt>KCC721 NFT ID</dt><dd><code>${escapeHtml(item.kcc721NftId)}</code></dd>` : ""}
      ${item.mode === "migration" ? `<dt>KRC721 owner</dt><dd><code>${escapeHtml(item.krc721Owner)}</code></dd>` : ""}
      ${item.mode === "migration" ? `<dt>KRC721 state</dt><dd><strong>${escapeHtml(item.krc721State)}</strong></dd>` : ""}
      <dt>Collection ID</dt><dd><code>${escapeHtml(item.collectionId)}</code></dd>
      <dt>Metadata URI</dt><dd><code>${escapeHtml(item.metadataUri)}</code></dd>
    `;
    els.attributes.innerHTML = attributes.map((attribute) => `
      <div><span>${escapeHtml(attribute.trait_type || attribute.type || "Attribute")}</span><strong>${escapeHtml(attribute.value)}</strong></div>
    `).join("");
    renderHistory(item.utxoHistory);
    els.raw.textContent = JSON.stringify(metadata, null, 2);
    els.loading.hidden = true;
    els.content.hidden = false;
  }

  async function init() {
    if (!/^[0-9a-f]{64}$/.test(collectionId) || !/^[0-9]+$/.test(tokenId)) throw new Error("Invalid NFT detail link.");
    const query = new URLSearchParams({ id: collectionId, tokenId });
    render(await readJson(await fetch(`/api/kcc721/nft-detail?${query}`, { cache: "no-store" })));
  }

  init().catch(showError);
})();
