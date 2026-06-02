let currentDeck = null;
let reviewIndex = 0;
let reviewStats = { correct: 0, again: 0 };
let reviewCards = [];
let reviewResults = [];
let reviewFlipped = false;

const deckCacheKey = "dino-decks-cache-v1";

const deckList = document.querySelector("#deckList");
const generatorPanel = document.querySelector("#generatorPanel");
const deckPanel = document.querySelector("#deckPanel");
const reviewPanel = document.querySelector("#reviewPanel");
const pageTitle = document.querySelector("#pageTitle");
const toast = document.querySelector("#toast");
const sourceType = document.querySelector("#sourceType");
const sourceUrlInput = document.querySelector("#sourceUrlInput");
const sourceUrlLabel = document.querySelector("#sourceUrlLabel");

const sourceCopy = {
  google_doc: {
    label: "Google Doc URL or ID",
    placeholder: "https://docs.google.com/document/d/.../edit",
    loading: "Reading the Google Doc and writing cards. This can take up to a minute.",
  },
  webpage: {
    label: "Webpage URL",
    placeholder: "https://example.com/article",
    loading: "Reading the webpage and writing cards. This can take up to a minute.",
  },
};

function updateSourceInputCopy() {
  const copy = sourceCopy[sourceType.value] || sourceCopy.google_doc;
  sourceUrlLabel.textContent = copy.label;
  sourceUrlInput.placeholder = copy.placeholder;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 4200);
}

function readDeckCache() {
  try {
    return JSON.parse(window.localStorage.getItem(deckCacheKey) || "{}");
  } catch {
    return {};
  }
}

function writeDeckCache(cache) {
  try {
    window.localStorage.setItem(deckCacheKey, JSON.stringify(cache));
  } catch {
    // Local cache is best-effort; live API behavior should not depend on it.
  }
}

function cacheDeck(deck) {
  if (!deck || !deck.slug) return;
  const cache = readDeckCache();
  cache[deck.slug] = deck;
  writeDeckCache(cache);
}

function removeDeckFromCache(slug) {
  const cache = readDeckCache();
  delete cache[slug];
  writeDeckCache(cache);
}

function cachedDeckList() {
  return Object.values(readDeckCache())
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))
    .map((deck) => ({
      slug: deck.slug,
      title: deck.title,
      summary: deck.summary,
      created_at: deck.created_at,
      card_count: (deck.cards || []).length,
      generator: deck.generator || "cached",
      cached: true,
    }));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("Session expired. Redirecting to sign in.");
  }
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

async function precacheAllDecks() {
  let decks = [];
  try {
    decks = (await api("/api/decks")).decks || [];
  } catch {
    return; // offline or unauthorized; cached decks already cover review.
  }
  await Promise.all(
    decks.map(async (summary) => {
      if (!summary.slug) return;
      try {
        cacheDeck((await api(`/api/decks/${summary.slug}`)).deck);
      } catch {
        // best-effort: skip decks that fail to fetch.
      }
    }),
  );
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function loadDecks() {
  let decks = [];
  try {
    const payload = await api("/api/decks");
    decks = payload.decks || [];
  } catch (error) {
    decks = cachedDeckList();
    if (decks.length) {
      showToast("Offline: showing cached decks.");
    } else {
      throw error;
    }
  }
  deckList.innerHTML = "";
  if (!decks.length) {
    deckList.innerHTML = '<p class="eyebrow">No decks yet</p>';
    return;
  }
  decks.forEach((deck) => {
    const button = document.createElement("button");
    button.className = "deck-item";
    button.innerHTML = `
      <strong>${escapeHtml(deck.title)}</strong>
      <span>${deck.card_count} cards · ${escapeHtml(deck.generator)}${deck.cached ? " · offline" : ""}</span>
    `;
    button.addEventListener("click", () => openDeck(deck.slug));
    deckList.appendChild(button);
  });
}

async function openDeck(slug) {
  try {
    const { deck } = await api(`/api/decks/${slug}`);
    cacheDeck(deck);
    renderDeck(deck);
  } catch (error) {
    const cached = readDeckCache()[slug];
    if (!cached) throw error;
    showToast("Offline: opened cached deck.");
    renderDeck(cached);
  }
}

function showGenerator() {
  currentDeck = null;
  pageTitle.textContent = "Generate a study deck";
  generatorPanel.classList.remove("hidden");
  deckPanel.classList.add("hidden");
  reviewPanel.classList.add("hidden");
  setDeckActionsVisible(false);
}

function setDeckActionsVisible(isVisible) {
  ["#startReview", "#exportSlides", "#deleteDeck"].forEach((selector) => {
    const button = document.querySelector(selector);
    button.disabled = !isVisible;
    button.classList.toggle("hidden", !isVisible);
  });
  document.querySelector("#exportProviderField").classList.toggle("hidden", !isVisible);
  document.querySelector("#exportProvider").disabled = !isVisible;
}

function renderDeck(deck) {
  currentDeck = deck;
  cacheDeck(deck);
  pageTitle.textContent = "Deck preview";
  generatorPanel.classList.add("hidden");
  reviewPanel.classList.add("hidden");
  deckPanel.classList.remove("hidden");
  setDeckActionsVisible(true);
  document.querySelector("#deckGenerator").textContent = `${deck.cards.length} cards · ${deck.generator}`;
  document.querySelector("#deckTitle").textContent = deck.title;
  document.querySelector("#deckSummary").textContent = deck.summary || "Ready for review.";
  const path = document.querySelector("#markdownPath");
  path.textContent = deck.markdown_path || `decks/${deck.slug}.md`;
  path.href = deck.markdown_url || `/decks/${deck.slug}.md`;
  const reviewLimit = document.querySelector("#reviewLimit");
  reviewLimit.max = String(deck.cards.length);
  reviewLimit.value = String(deck.cards.length);
  const cardGrid = document.querySelector("#cardGrid");
  cardGrid.innerHTML = "";
  deck.cards.forEach((card, index) => {
    const article = document.createElement("article");
    article.className = "study-card";
    const tags = (card.tags || []).map((tag) => `<span class="tag">#${escapeHtml(tag)}</span>`).join("");
    article.innerHTML = `
      <p class="eyebrow">Card ${index + 1}</p>
      <h3>${escapeHtml(card.question)}</h3>
      <p>${escapeHtml(card.answer)}</p>
      <div class="tags">${tags}</div>
    `;
    cardGrid.appendChild(article);
  });
}

function shuffledCards(cards) {
  const copy = [...cards];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[swapIndex]] = [copy[swapIndex], copy[index]];
  }
  return copy;
}

function buildReviewSession() {
  const limitInput = document.querySelector("#reviewLimit");
  const shuffle = document.querySelector("#shuffleReview").checked;
  const requestedLimit = Number.parseInt(limitInput.value, 10);
  const limit = Math.min(
    currentDeck.cards.length,
    Math.max(1, Number.isFinite(requestedLimit) ? requestedLimit : currentDeck.cards.length),
  );
  limitInput.value = String(limit);
  const cards = shuffle ? shuffledCards(currentDeck.cards) : [...currentDeck.cards];
  reviewCards = cards.slice(0, limit);
  reviewResults = Array(reviewCards.length).fill(null);
}

function calculateReviewStats() {
  reviewStats = reviewResults.reduce(
    (stats, result) => {
      if (result === "correct") stats.correct += 1;
      if (result === "again") stats.again += 1;
      return stats;
    },
    { correct: 0, again: 0 },
  );
}

function startReview() {
  if (!currentDeck) return;
  buildReviewSession();
  reviewIndex = 0;
  reviewFlipped = false;
  calculateReviewStats();
  generatorPanel.classList.add("hidden");
  deckPanel.classList.add("hidden");
  reviewPanel.classList.remove("hidden");
  setDeckActionsVisible(false);
  pageTitle.textContent = "Review mode";
  showReviewCardView();
  renderReviewCard();
}

function showReviewCardView() {
  document.querySelector("#reviewSummary").classList.add("hidden");
  document.querySelector(".review-card").classList.remove("hidden");
  document.querySelector(".review-controls").classList.remove("hidden");
}

function renderReviewCard() {
  const card = reviewCards[reviewIndex];
  const result = reviewResults[reviewIndex];
  const total = reviewCards.length;
  document.querySelector("#reviewProgress").textContent = `Card ${reviewIndex + 1} of ${total} · ${reviewStats.correct} correct · ${reviewStats.again} again`;
  document.querySelector("#reviewProgressFill").style.width = `${((reviewIndex + 1) / total) * 100}%`;
  document.querySelector("#reviewQuestion").textContent = card.question;
  document.querySelector("#answerText").textContent = card.answer;
  document.querySelector("#explanationText").textContent = card.explanation || "";
  document.querySelector("#reviewAnswer").classList.toggle("hidden", !reviewFlipped);
  document.querySelector("#revealAnswer").classList.toggle("hidden", reviewFlipped);
  document.querySelector("#gradeActions").classList.toggle("hidden", !reviewFlipped);
  document.querySelector("#previousCard").disabled = reviewIndex === 0;
  document.querySelector("#nextCard").disabled = reviewIndex + 1 >= reviewCards.length;
  document.querySelector("#markAgain").classList.toggle("is-selected", result === "again");
  document.querySelector("#markCorrect").classList.toggle("is-selected", result === "correct");
}

function renderReviewSummary() {
  const total = reviewCards.length;
  const { correct, again } = reviewStats;
  const pct = total ? Math.round((correct / total) * 100) : 0;
  document.querySelector("#summaryScore").textContent = `${correct} / ${total} correct`;
  const revisit = again === 1 ? "1 to revisit" : `${again} to revisit`;
  document.querySelector("#summaryDetail").textContent = `${pct}% · ${revisit}`;
  document.querySelector(".review-card").classList.add("hidden");
  document.querySelector(".review-controls").classList.add("hidden");
  document.querySelector("#reviewSummary").classList.remove("hidden");
}

function handleReviewCardTap(event) {
  if (event.target.closest("button, a, input, select, textarea")) return;
  toggleReviewFlip();
}

function setReviewFlipped(isFlipped) {
  reviewFlipped = isFlipped;
  renderReviewCard();
}

function toggleReviewFlip() {
  setReviewFlipped(!reviewFlipped);
}

function goToReviewCard(index) {
  if (index < 0 || index >= reviewCards.length) return;
  reviewIndex = index;
  reviewFlipped = false;
  renderReviewCard();
}

function markReviewResult(result) {
  reviewResults[reviewIndex] = result;
  calculateReviewStats();
  renderReviewCard();
  const complete = reviewResults.every(Boolean);
  if (complete) {
    renderReviewSummary();
    return;
  }
  if (reviewIndex + 1 < reviewCards.length) {
    goToReviewCard(reviewIndex + 1);
  }
}

function handleReviewKeydown(event) {
  if (reviewPanel.classList.contains("hidden")) return;
  if (!document.querySelector("#reviewSummary").classList.contains("hidden")) return;
  if (event.target.closest("input, select, textarea")) return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    goToReviewCard(reviewIndex - 1);
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    goToReviewCard(reviewIndex + 1);
  }
  if (event.key === " " || event.key === "ArrowUp" || event.key === "ArrowDown") {
    event.preventDefault();
    toggleReviewFlip();
  }
  if (reviewFlipped && (event.key === "1" || event.key === "2")) {
    event.preventDefault();
    markReviewResult(event.key === "1" ? "again" : "correct");
  }
}

async function exportSlides() {
  if (!currentDeck) return;
  const dialog = document.querySelector("#exportDialog");
  const link = document.querySelector("#slidesLink");
  const script = document.querySelector("#appsScript");
  link.classList.add("hidden");
  script.classList.add("hidden");
  document.querySelector("#exportMessage").textContent = "Exporting...";
  dialog.showModal();
  try {
    const provider = document.querySelector("#exportProvider").value;
    const result = await api("/api/export/slides", {
      method: "POST",
      body: JSON.stringify({ slug: currentDeck.slug, provider }),
    });
    if (result.mode === "direct") {
      document.querySelector("#exportMessage").textContent = "Created a Google Slides deck.";
      link.href = result.url;
      link.classList.remove("hidden");
    } else {
      document.querySelector("#exportMessage").textContent = result.message;
      script.value = result.apps_script || "";
      script.classList.remove("hidden");
    }
  } catch (error) {
    document.querySelector("#exportMessage").textContent = error.message;
  }
}

async function deleteCurrentDeck() {
  if (!currentDeck) return;
  const confirmed = window.confirm(`Delete "${currentDeck.title}"? This removes the markdown deck file.`);
  if (!confirmed) return;
  try {
    await api(`/api/decks/${currentDeck.slug}`, { method: "DELETE" });
    removeDeckFromCache(currentDeck.slug);
    showToast("Deck deleted.");
    await loadDecks();
    showGenerator();
  } catch (error) {
    showToast(error.message);
  }
}

function renderSkeletonGrid(cardCount) {
  const grid = document.querySelector("#skeletonGrid");
  const requested = Number.parseInt(cardCount, 10);
  const count = Math.min(8, Math.max(3, Number.isFinite(requested) ? requested : 12));
  grid.innerHTML = "";
  for (let index = 0; index < count; index += 1) {
    const card = document.createElement("div");
    card.className = "skeleton-card";
    card.innerHTML = `
      <span class="skeleton-line skeleton-title"></span>
      <span class="skeleton-line"></span>
      <span class="skeleton-line"></span>
      <span class="skeleton-line skeleton-short"></span>
    `;
    grid.appendChild(card);
  }
}

function setGeneratorLoading(isLoading, cardCount) {
  const form = document.querySelector("#generateForm");
  const loading = document.querySelector("#generatorLoading");
  const copy = sourceCopy[sourceType.value] || sourceCopy.google_doc;
  generatorPanel.classList.toggle("is-loading", isLoading);
  loading.classList.toggle("hidden", !isLoading);
  loading.setAttribute("aria-busy", String(isLoading));
  document.querySelector("#loadingStatus").textContent = copy.loading;
  form.querySelectorAll("input, select, button").forEach((control) => {
    control.disabled = isLoading;
  });
  if (isLoading) renderSkeletonGrid(cardCount);
}

document.querySelector("#generateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.source_url = payload.doc_url;
  setGeneratorLoading(true, payload.card_count);
  button.textContent = "Generating...";
  try {
    const { deck } = await api("/api/generate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadDecks();
    renderDeck(deck);
    showToast("Deck saved as markdown.");
  } catch (error) {
    showToast(error.message);
  } finally {
    setGeneratorLoading(false);
    button.textContent = "Generate flashcards";
  }
});

document.querySelector("#newDeckButton").addEventListener("click", showGenerator);
document.querySelector("#refreshDecks").addEventListener("click", () => loadDecks().catch((error) => showToast(error.message)));
document.querySelector("#startReview").addEventListener("click", startReview);
document.querySelector("#exitReview").addEventListener("click", () => renderDeck(currentDeck));
document.querySelector("#previousCard").addEventListener("click", () => goToReviewCard(reviewIndex - 1));
document.querySelector("#nextCard").addEventListener("click", () => goToReviewCard(reviewIndex + 1));
document.querySelector("#revealAnswer").addEventListener("click", toggleReviewFlip);
document.querySelector("#markAgain").addEventListener("click", () => markReviewResult("again"));
document.querySelector("#markCorrect").addEventListener("click", () => markReviewResult("correct"));
document.querySelector("#reviewAgain").addEventListener("click", startReview);
document.querySelector("#backToDeck").addEventListener("click", () => renderDeck(currentDeck));
document.querySelector("#exportSlides").addEventListener("click", exportSlides);
document.querySelector("#deleteDeck").addEventListener("click", deleteCurrentDeck);
document.querySelector(".review-card").addEventListener("click", handleReviewCardTap);
document.addEventListener("keydown", handleReviewKeydown);
sourceType.addEventListener("change", updateSourceInputCopy);

async function initAuthControls() {
  try {
    const { auth_enabled: authEnabled } = await api("/api/config");
    document.querySelector("#logoutLink").classList.toggle("hidden", !authEnabled);
  } catch {
    // Config is best-effort; leave the sign-out control hidden if it fails.
  }
}

initAuthControls();
updateSourceInputCopy();
loadDecks()
  .then(() => precacheAllDecks())
  .catch((error) => showToast(error.message));

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  });
}
