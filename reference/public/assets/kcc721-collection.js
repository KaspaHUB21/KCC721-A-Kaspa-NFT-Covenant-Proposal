(() => {
  const params = new URLSearchParams(window.location.search);
  const collectionId = String(params.get("id") || "").toLowerCase();
  const state = { collection: null, next: 1, loaded: 0, loading: false };
  const els = {
    loading: document.querySelector("#collectionLoading"),
    content: document.querySelector("#collectionContent"),
    eyebrow: document.querySelector("#collectionEyebrow"),
    title: document.querySelector("#collectionTitle"),
    description: document.querySelector("#collectionDescription"),
    badge: document.querySelector("#collectionBadge"),
    stats: document.querySelector("#collectionStats"),
    data: document.querySelector("#collectionData"),
    gallery: document.querySelector("#nftGallery"),
    galleryCount: document.querySelector("#galleryCount"),
    loadMore: document.querySelector("#loadMoreNfts"),
  };

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
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

  function renderCollection() {
    const item = state.collection;
    const migration = item.migration || {};
    const sourceMinted = Number(migration.mintedAtPreview || 0);
    document.title = `${item.ticker} | KCC721`;
    els.eyebrow.textContent = item.mode === "migration" ? "KRC721-linked KCC721 Collection" : "KCC721 Collection";
    els.title.textContent = item.ticker;
    els.description.textContent = item.mode === "migration"
      ? "The complete linked source collection is browsable below. Source NFTs are visible but have not been airdropped as KCC721 NFTs."
      : "UTXO-native KCC721 collection.";
    els.badge.textContent = item.mode === "migration" ? "Migration" : item.status;
    els.stats.innerHTML = item.mode === "migration" ? `
      <div><span>Source NFTs</span><strong>${sourceMinted.toLocaleString()}</strong></div>
      <div><span>KCC721 NFTs</span><strong>${Number(item.indexedNfts || 0).toLocaleString()}</strong></div>
      <div><span>Maximum supply</span><strong>${Number(item.maxSupply || 0).toLocaleString()}</strong></div>
    ` : `
      <div><span>Minted</span><strong>${Math.max(0, Number(item.nextTokenId || 0) - (String(item.version || "").startsWith("0.2") ? 1 : 0)).toLocaleString()}</strong></div>
      <div><span>Metadata items</span><strong>${Number(item.maxSupply || 0).toLocaleString()}</strong></div>
      <div><span>Maximum supply</span><strong>${Number(item.maxSupply || 0).toLocaleString()}</strong></div>
    `;
    els.data.innerHTML = `
      <dt>Collection ID</dt><dd><code>${escapeHtml(item.collectionId)}</code></dd>
      <dt>Deployer</dt><dd><code>${escapeHtml(item.deployerAddress)}</code></dd>
      <dt>Metadata</dt><dd><code>${escapeHtml(item.metadataUri)}</code></dd>
      ${item.version ? `<dt>Protocol</dt><dd><strong>KCC721 ${escapeHtml(item.version)}</strong></dd>` : ""}
      ${item.shuffleRoot ? `<dt>Shuffle commitment</dt><dd><code>${escapeHtml(item.shuffleRoot)}</code></dd>` : ""}
      ${item.mode === "native" ? `<dt>Mint</dt><dd><a href="/kcc721?mode=mint&collectionId=${encodeURIComponent(item.collectionId)}">Open mint form</a></dd>` : ""}
      ${migration.sourceDeployTransactionId ? `<dt>KRC721 deploy tx</dt><dd><code>${escapeHtml(migration.sourceDeployTransactionId)}</code></dd>` : ""}
      ${migration.status ? `<dt>Migration phase</dt><dd><strong>${escapeHtml(migration.status)}</strong></dd>` : ""}
    `;
    els.loading.hidden = true;
    els.content.hidden = false;
  }

  function appendNfts(items) {
    els.gallery.insertAdjacentHTML("beforeend", items.map((nft) => `
      <a class="kcc-nft-tile" href="${escapeHtml(nft.detailUrl)}">
        <div class="kcc-nft-thumb">
          <img src="${escapeHtml(nft.imageUrl || "/assets/devtools-logo-uploaded.png?v=2")}" alt="${escapeHtml(nft.name)}" loading="lazy" decoding="async" />
        </div>
        <div class="kcc-nft-tile-copy">
          <strong>${escapeHtml(nft.name)}</strong>
          <span>${escapeHtml(nft.state)}</span>
        </div>
      </a>
    `).join(""));
    state.loaded += items.length;
    els.galleryCount.textContent = `${state.loaded.toLocaleString()} loaded`;
  }

  async function loadNfts() {
    if (state.loading || state.next === null) return;
    state.loading = true;
    els.loadMore.disabled = true;
    els.loadMore.textContent = "Loading...";
    try {
      const query = new URLSearchParams({ id: collectionId, offset: String(state.next) });
      const data = await readJson(await fetch(`/api/kcc721/collection-nfts?${query}`, { cache: "no-store" }));
      appendNfts(Array.isArray(data.items) ? data.items : []);
      state.next = data.next ?? null;
      els.loadMore.hidden = state.next === null;
    } finally {
      state.loading = false;
      els.loadMore.disabled = false;
      els.loadMore.textContent = "Load more";
    }
  }

  async function init() {
    if (!/^[0-9a-f]{64}$/.test(collectionId)) throw new Error("Invalid KCC721 collection ID.");
    state.collection = await readJson(await fetch(`/api/kcc721/collection?id=${encodeURIComponent(collectionId)}`, { cache: "no-store" }));
    renderCollection();
    await loadNfts();
  }

  els.loadMore.addEventListener("click", () => loadNfts().catch(showError));
  init().catch(showError);
})();
