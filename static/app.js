const state = {
  bootstrap: null,
  mode: "block",
  vocabulary: [],
  filteredVocabulary: [],
  selectedIds: new Set(),
  targetedIds: new Set(),
  selectedBlocks: new Set(),
  quiz: null,
  aiDraftToken: null,
  previewVocabulary: [],
  previewRequest: 0,
  awaitingContinue: false,
  throughTimer: null,
  paused: false,
  activeStudyListId: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(body.error || `Fehler ${response.status}`);
  return body;
}

function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("#toast-region").append(node);
  setTimeout(() => node.remove(), 4200);
}

async function loadBootstrap() {
  state.bootstrap = await api("/api/bootstrap");
  const sources = state.bootstrap.sources;
  const sourceSelect = $("#source-select");
  const current = new Set([...sourceSelect.selectedOptions].map(option => Number(option.value)));
  sourceSelect.innerHTML = sources.map(source => `<option value="${source.id}">${escapeHtml(source.name)} · ${source.vocabulary_count}</option>`).join("");
  if (current.size) {
    [...sourceSelect.options].forEach(option => option.selected = current.has(Number(option.value)));
  } else if (sourceSelect.options.length) {
    sourceSelect.options[0].selected = true;
  }
  const filter = $("#vocab-source-filter");
  const filterValue = filter.value;
  filter.innerHTML = `<option value="">Alle Datenquellen</option>` + sources.map(source => `<option value="${source.id}">${escapeHtml(source.name)}</option>`).join("");
  filter.value = filterValue;
  $("#word-source").innerHTML = sources.map(source => `<option value="${source.id}">${escapeHtml(source.name)}</option>`).join("");
  $("#bulk-source").innerHTML = sources.map(source => `<option value="${source.id}">${escapeHtml(source.name)}</option>`).join("");
  $("#ai-destination").innerHTML = `<option value="new">Neue Datenquelle</option>` + sources.map(source => `<option value="${source.id}">Zu „${escapeHtml(source.name)}“ hinzufügen</option>`).join("");
  $("#declension-toggle").checked = state.bootstrap.settings.check_declension;
  $("#typo-toggle").checked = state.bootstrap.settings.allow_typos;
  $("#ai-status").textContent = state.bootstrap.ai_available ? "KI-Verbindung ist konfiguriert." : "Auf dem Server fehlt noch OPENAI_API_KEY.";
  const listOptions = (state.bootstrap.study_lists || []).map(list => `<option value="${list.id}">${escapeHtml(list.name)} · ${list.vocabulary_count}</option>`).join("");
  $("#study-list-select").innerHTML = `<option value="">Keine Lernliste</option>${listOptions}`;
  if (state.activeStudyListId) $("#study-list-select").value = String(state.activeStudyListId);
  $("#mistake-list-target").innerHTML = `<option value="new">Neue Lernliste</option>${listOptions}`;
  $("#logout-button").classList.toggle("hidden", !state.bootstrap.auth.enabled);
  document.body.classList.toggle("learner-role", state.bootstrap.auth.role === "learner");
  const learner = state.bootstrap.auth.role === "learner";
  $("#add-word").classList.toggle("hidden", learner);
  $("#open-bulk-entry").classList.toggle("hidden", learner);
  $("#data-import-grid").classList.toggle("hidden", learner);
  renderLessons();
  renderSources();
}

function selectedSourceIds() {
  return [...$("#source-select").selectedOptions].map(option => Number(option.value));
}

function selectedLessons() {
  return $$("#lesson-list input:checked").map(input => input.value);
}

function selectedDifficulties() {
  return $$("#difficulty-picker input:checked").map(input => input.value);
}

function relevantSources() {
  const ids = new Set(selectedSourceIds());
  return state.bootstrap.sources.filter(source => ids.has(source.id));
}

function renderLessons() {
  const previous = new Set(selectedLessons());
  const grouped = new Map();
  for (const source of relevantSources()) {
    for (const lesson of source.lessons) grouped.set(lesson.name, (grouped.get(lesson.name) || 0) + lesson.count);
  }
  const sorted = [...grouped.entries()].sort((a, b) => a[0].localeCompare(b[0], "de", {numeric: true}));
  $("#lesson-list").innerHTML = sorted.map(([name, count]) => `
    <label class="check-row">
      <input type="checkbox" value="${escapeHtml(name)}" ${previous.has(name) ? "checked" : ""}>
      <span>${escapeHtml(name || "Ohne Lektion")}</span><small>${count}</small>
    </label>`).join("");
  $$("#lesson-list input").forEach(input => input.addEventListener("change", () => {
    state.selectedBlocks.clear();
    updateTotals();
    refreshSelectionPreview();
  }));
  updateTotals();
  refreshSelectionPreview();
}

function availableCount() {
  if (state.targetedIds.size || selectedDifficulties().length) return state.previewVocabulary.length;
  const chosen = new Set(selectedLessons());
  return relevantSources().reduce((sum, source) => sum + source.lessons.reduce((part, lesson) => part + (chosen.has(lesson.name) ? lesson.count : 0), 0), 0);
}

function updateTotals() {
  const available = availableCount();
  $("#selection-count").textContent = `${available} Vokabel${available === 1 ? "" : "n"}`;
  if (state.mode === "block" || state.mode === "through") {
    renderBlockPicker(available);
    const blockSize = Math.max(1, Number($("#block-size").value) || 1);
    const selected = [...state.selectedBlocks].reduce((sum, block) => {
      const start = (block - 1) * blockSize;
      return sum + (start < available ? Math.min(blockSize, available - start) : 0);
    }, 0);
    const repetitions = Math.max(1, Number($("#repetitions").value) || 1);
    $("#question-total").textContent = state.mode === "through"
      ? `${selected * repetitions} Wörter · ${selected * repetitions * 2} Seiten`
      : `${selected * repetitions} Abfragen · ${selected} Vokabeln`;
  } else {
    const selected = Math.min(available, Math.max(1, Number($("#card-count").value) || 1));
    $("#question-total").textContent = `${selected} Vokabeln · 5 Boxen`;
  }
  $("#targeted-summary").classList.toggle("hidden", !state.targetedIds.size);
  $("#targeted-summary").textContent = state.targetedIds.size ? `${state.targetedIds.size} gezielt ausgewählte Vokabeln haben Vorrang.` : "";
  renderSelectionPreview();
}

function renderBlockPicker(available = availableCount()) {
  const blockSize = Math.max(1, Number($("#block-size").value) || 1);
  const blockTotal = Math.ceil(available / blockSize);
  state.selectedBlocks = new Set([...state.selectedBlocks].filter(block => block >= 1 && block <= blockTotal));
  if (!blockTotal) {
    $("#block-picker").innerHTML = `<span class="hint">Zuerst Lektionen wählen</span>`;
    return;
  }
  $("#block-picker").innerHTML = Array.from({length: blockTotal}, (_, index) => {
    const block = index + 1;
    return `<label class="block-choice" title="Block ${block}"><input type="checkbox" value="${block}" ${state.selectedBlocks.has(block) ? "checked" : ""}><span>${block}</span></label>`;
  }).join("");
  $$("#block-picker input").forEach(input => input.addEventListener("change", event => {
    const block = Number(event.target.value);
    if (event.target.checked) state.selectedBlocks.add(block); else state.selectedBlocks.delete(block);
    updateTotals();
  }));
}

async function refreshSelectionPreview() {
  const requestNumber = ++state.previewRequest;
  const lessons = selectedLessons();
  const sources = selectedSourceIds();
  if (!state.targetedIds.size && (!sources.length || !lessons.length)) {
    state.previewVocabulary = [];
    renderSelectionPreview();
    return;
  }
  try {
    const params = new URLSearchParams();
    if (!state.targetedIds.size) {
      sources.forEach(id => params.append("source_id", id));
      lessons.forEach(lesson => params.append("lesson", lesson));
    }
    const items = (await api(`/api/vocabulary?${params}`)).items;
    if (requestNumber !== state.previewRequest) return;
    const difficulties = new Set(selectedDifficulties());
    state.previewVocabulary = items
      .filter(item => !state.targetedIds.size || state.targetedIds.has(item.id))
      .filter(item => !difficulties.size || difficulties.has(item.difficulty));
    renderSelectionPreview();
  } catch (error) {
    if (requestNumber === state.previewRequest) toast(error.message, true);
  }
}

function previewWords(items) {
  return items.map(item => `<div class="preview-word"><strong>${escapeHtml(item.foreign_text)}</strong><span>${escapeHtml(item.german_text)}${item.declension ? ` · ${escapeHtml(item.declension)}` : ""}</span></div>`).join("");
}

function renderSelectionPreview() {
  const items = state.previewVocabulary || [];
  $("#preview-count").textContent = `${items.length} ausgewählt`;
  if (!items.length) {
    $("#preview-note").textContent = "Wähle links mindestens eine Lektion aus.";
    $("#selected-preview").innerHTML = `<div class="preview-empty">Noch keine Vokabeln ausgewählt.</div>`;
    return;
  }
  if (state.mode === "block" || state.mode === "through") {
    const blockSize = Math.max(1, Number($("#block-size").value) || 1);
    const blocks = [];
    let includedCount = 0;
    for (let offset = 0; offset < items.length; offset += blockSize) {
      const blockNumber = Math.floor(offset / blockSize) + 1;
      const block = items.slice(offset, offset + blockSize);
      const selected = state.selectedBlocks.has(blockNumber);
      if (selected) includedCount += block.length;
      blocks.push(`<section class="preview-block ${selected ? "" : "unused"}"><h3>Block ${blockNumber} · ${block.length} Wörter · ${selected ? "ausgewählt" : "nicht ausgewählt"}</h3>${previewWords(block)}</section>`);
    }
    const selectedList = [...state.selectedBlocks].sort((a, b) => a - b).join(", ");
    $("#preview-note").textContent = selectedList
      ? `${includedCount} der ${items.length} Wörter aus Block ${selectedList} werden gelernt. Die Abfragereihenfolge wird zufällig gemischt.`
      : `Alle ${items.length} Wörter sind unten in Blöcke aufgeteilt. Markiere oben die Blöcke, die du lernen willst.`;
    $("#selected-preview").innerHTML = blocks.join("");
  } else {
    const count = Math.min(items.length, Math.max(1, Number($("#card-count").value) || 1));
    const included = items.slice(0, count);
    const unused = items.slice(count);
    const groups = [`<section class="preview-block"><h3>Start in Box 1 · ${included.length} Wörter</h3>${previewWords(included)}</section>`];
    if (unused.length) groups.push(`<section class="preview-block unused"><h3>Nicht in diesem Durchlauf · ${unused.length} Wörter</h3>${previewWords(unused)}</section>`);
    $("#preview-note").textContent = `${included.length} der ${items.length} ausgewählten Wörter starten in Box 1. Die Reihenfolge wird zufällig gemischt.`;
    $("#selected-preview").innerHTML = groups.join("");
  }
}

function setMode(mode) {
  state.mode = mode;
  $$(".mode-card").forEach(card => {
    const selected = card.dataset.mode === mode;
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-checked", selected ? "true" : "false");
  });
  $("#block-config").classList.toggle("hidden", !["block", "through"].includes(mode));
  $("#cards-config").classList.toggle("hidden", mode !== "cards");
  $("#through-config").classList.toggle("hidden", mode !== "through");
  $("#declension-setting").classList.toggle("hidden", mode === "through");
  $("#typo-setting").classList.toggle("hidden", mode === "through");
  $("#config-title").textContent = mode === "block" ? "Blöcke konfigurieren" : mode === "cards" ? "Karteikasten konfigurieren" : "Durchlauf konfigurieren";
  updateTotals();
}

async function startQuiz() {
  const button = $("#start-quiz");
  button.disabled = true;
  try {
    const payload = {
      mode: state.mode,
      source_ids: selectedSourceIds(),
      lessons: selectedLessons(),
      vocabulary_ids: [...state.targetedIds],
      difficulties: selectedDifficulties(),
      check_declension: $("#declension-toggle").checked,
      allow_typos: $("#typo-toggle").checked,
      block_size: Number($("#block-size").value),
      block_numbers: [...state.selectedBlocks].sort((a, b) => a - b),
      repetitions: Number($("#repetitions").value),
      card_count: Number($("#card-count").value),
      timer_enabled: $("#timer-toggle").checked,
      timer_seconds: Number($("#timer-seconds").value),
    };
    state.quiz = await api("/api/quiz/start", {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload)});
    localStorage.setItem("velox_active_quiz", state.quiz.token);
    showQuiz();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function showQuiz() {
  state.awaitingContinue = false;
  $("#answer-form").classList.remove("hidden");
  $(".prompt-label", $(".prompt-card")).textContent = "Übersetze";
  $(".keyboard-help", $(".prompt-card")).innerHTML = `<kbd>Enter</kbd> prüfen und sofort weiter`;
  $("#setup-screen").classList.add("hidden");
  $("#summary-screen").classList.add("hidden");
  $("#quiz-screen").classList.remove("hidden");
  $("#quiz-feedback").textContent = "";
  $("#mark-correct-button").classList.add("hidden");
  renderQuiz();
}

function renderQuiz(result = null) {
  const quiz = state.quiz;
  if (quiz.completed) return showSummary();
  clearTimeout(state.throughTimer);
  $("#quiz-mode-label").textContent = quiz.mode === "block" ? "Blockmodus" : `Karteikasten · Box ${quiz.box}`;
  if (quiz.mode === "through") {
    $("#quiz-mode-label").textContent = "Durchlaufmodus";
    $("#quiz-word").textContent = quiz.side === "foreign" ? quiz.current.foreign_text : quiz.current.german_text;
    $("#quiz-lesson").textContent = quiz.current.lesson ? `Lektion ${quiz.current.lesson}` : "Ohne Lektion";
    $(".prompt-label", $(".prompt-card")).textContent = quiz.side === "foreign" ? "Latein" : "Lösung";
    $("#quiz-progress-label").textContent = `${quiz.step} / ${quiz.total_steps}`;
    $("#quiz-progress").style.width = `${quiz.total_steps ? quiz.step / quiz.total_steps * 100 : 0}%`;
    $("#box-strip").classList.add("hidden");
    $("#answer-form").classList.add("hidden");
    $("#mark-correct-button").classList.add("hidden");
    $("#quiz-feedback").className = "quiz-feedback";
    $("#quiz-feedback").textContent = state.paused ? "Pausiert" : "";
    $(".keyboard-help", $(".prompt-card")).innerHTML = `<kbd>Enter</kbd> weiter · <kbd>Leertaste</kbd> Pause`;
    if (quiz.timer_enabled && !state.paused) {
      state.throughTimer = setTimeout(advanceThrough, Math.max(1, quiz.timer_seconds) * 1000);
    }
    return;
  }
  $("#quiz-word").textContent = quiz.current.foreign_text;
  $("#quiz-lesson").textContent = quiz.current.lesson ? `Lektion ${quiz.current.lesson}` : "Ohne Lektion";
  if (quiz.mode === "block") {
    $("#quiz-progress-label").textContent = `${quiz.answered} / ${quiz.total}`;
    $("#quiz-progress").style.width = `${quiz.total ? quiz.answered / quiz.total * 100 : 0}%`;
    $("#box-strip").classList.add("hidden");
  } else {
    $("#quiz-progress-label").textContent = `${quiz.mastered} / ${quiz.total} gemeistert · ${quiz.answered} Antworten`;
    $("#quiz-progress").style.width = `${quiz.total ? quiz.mastered / quiz.total * 100 : 0}%`;
    $("#box-strip").classList.remove("hidden");
    $("#box-strip").innerHTML = [1,2,3,4,5].map(box => `<div class="box-item ${quiz.box === box ? "active" : ""}">Box ${box}<strong>${quiz.boxes[String(box)]}</strong></div>`).join("");
  }
  if (result) {
    const feedback = $("#quiz-feedback");
    feedback.className = `quiz-feedback ${result.correct ? "correct" : "wrong"}`;
    feedback.innerHTML = result.correct
      ? result.accepted_as_typo
        ? `Richtig · Tippfehler erkannt: <strong>${escapeHtml(result.expected)}</strong>`
        : `Richtig · <strong>${escapeHtml(result.foreign_text)}</strong>`
      : `Nicht ganz · <strong>${escapeHtml(result.expected)}</strong>`;
  } else {
    const feedback = $("#quiz-feedback");
    feedback.className = "quiz-feedback";
    feedback.textContent = "";
  }
  $("#mark-correct-button").classList.add("hidden");
  $("#answer-input").value = "";
  requestAnimationFrame(() => $("#answer-input").focus());
}

function showWrongReveal(result) {
  state.awaitingContinue = true;
  const feedback = $("#quiz-feedback");
  feedback.className = "quiz-feedback wrong";
  feedback.innerHTML = `<strong>${escapeHtml(result.foreign_text)}</strong> war falsch beantwortet.`;
  $(".prompt-label", $(".prompt-card")).textContent = "Richtige Antwort";
  $("#quiz-word").textContent = result.expected;
  $("#answer-form").classList.add("hidden");
  $("#mark-correct-button").classList.remove("hidden");
  $(".keyboard-help", $(".prompt-card")).innerHTML = `<kbd>Enter</kbd> weiter${state.quiz.mode === "block" ? " · dieselbe Vokabel kommt sofort erneut" : ""}`;
  $("#answer-input").blur();
}

async function continueAfterWrong() {
  if (!state.quiz || !state.awaitingContinue) return;
  try {
    const continued = await api(`/api/quiz/${state.quiz.token}/continue`, {method: "POST"});
    state.quiz = {...state.quiz, ...continued};
    state.awaitingContinue = false;
    $(".prompt-label", $(".prompt-card")).textContent = "Übersetze";
    $("#answer-form").classList.remove("hidden");
    $("#mark-correct-button").classList.add("hidden");
    $(".keyboard-help", $(".prompt-card")).innerHTML = `<kbd>Enter</kbd> prüfen`;
    renderQuiz();
  } catch (error) { toast(error.message, true); }
}

async function markCorrect() {
  if (!state.quiz || !state.awaitingContinue) return;
  const button = $("#mark-correct-button");
  button.disabled = true;
  try {
    const corrected = await api(`/api/quiz/${state.quiz.token}/mark-correct`, {method: "POST"});
    state.quiz = {...state.quiz, ...corrected};
    state.awaitingContinue = false;
    $("#answer-form").classList.remove("hidden");
    $("#mark-correct-button").classList.add("hidden");
    toast("Antwort wurde als richtig gewertet.");
    renderQuiz();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function advanceThrough() {
  if (!state.quiz || state.quiz.mode !== "through" || state.paused) return;
  clearTimeout(state.throughTimer);
  try {
    state.quiz = await api(`/api/quiz/${state.quiz.token}/advance`, {method: "POST"});
    renderQuiz();
  } catch (error) { toast(error.message, true); }
}

function toggleThroughPause() {
  if (!state.quiz || state.quiz.mode !== "through") return;
  state.paused = !state.paused;
  renderQuiz();
}

async function submitAnswer(event) {
  event.preventDefault();
  const input = $("#answer-input");
  if (!input.value.trim() || input.disabled) return;
  input.disabled = true;
  try {
    const response = await api(`/api/quiz/${state.quiz.token}/answer`, {
      method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({answer: input.value})
    });
    state.quiz = {...state.quiz, ...response};
    if (response.result.correct) renderQuiz(response.result);
    else showWrongReveal(response.result);
  } catch (error) {
    toast(error.message, true);
  } finally {
    input.disabled = false;
    if (!state.awaitingContinue) input.focus();
  }
}

function showSummary() {
  clearTimeout(state.throughTimer);
  $("#quiz-screen").classList.add("hidden");
  $("#summary-screen").classList.remove("hidden");
  const quiz = state.quiz;
  $("#summary-answered").textContent = quiz.answered;
  $("#summary-rate").textContent = quiz.mode === "through" ? "—" : `${quiz.answered ? Math.round(quiz.correct / quiz.answered * 100) : 0}%`;
  $("#summary-wrong").textContent = quiz.wrong;
  $("#summary-rate-stat").classList.toggle("hidden", quiz.mode === "through");
  $("#summary-wrong-stat").classList.toggle("hidden", quiz.mode === "through");
  $("#summary-details").classList.add("hidden");
  loadQuizSummary();
  localStorage.removeItem("velox_active_quiz");
}

async function loadQuizSummary() {
  if (!state.quiz) return;
  try {
    const summary = await api(`/api/quiz/${state.quiz.token}/summary`);
    state.quizSummary = summary;
    const details = $("#summary-details");
    details.classList.toggle("hidden", !summary.mistakes.length);
    $("#summary-mistakes").innerHTML = summary.mistakes.map(item => `
      <div class="mistake-row"><strong>${escapeHtml(item.foreign_text)}</strong><span>${escapeHtml(item.expected)}<br>Deine Antwort: ${escapeHtml(item.answers.join(" · "))}</span><small>${item.wrong_count}× falsch</small></div>`).join("");
  } catch (error) { toast(error.message, true); }
}

function returnToSetup() {
  clearTimeout(state.throughTimer);
  localStorage.removeItem("velox_active_quiz");
  state.quiz = null;
  state.awaitingContinue = false;
  $("#answer-form").classList.remove("hidden");
  $("#quiz-screen").classList.add("hidden");
  $("#summary-screen").classList.add("hidden");
  $("#setup-screen").classList.remove("hidden");
}

function switchView(name) {
  $$(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-button").forEach(button => button.classList.toggle("active", button.dataset.view === name));
  if (name === "vocabulary") loadVocabulary();
}

async function loadVocabulary() {
  try {
    const source = $("#vocab-source-filter").value;
    const params = new URLSearchParams();
    if (source) params.append("source_id", source);
    state.vocabulary = (await api(`/api/vocabulary?${params}`)).items;
    filterVocabulary();
  } catch (error) { toast(error.message, true); }
}

function filterVocabulary() {
  const search = $("#vocab-search").value.trim().toLocaleLowerCase("de");
  const difficulty = $("#difficulty-filter").value;
  state.filteredVocabulary = state.vocabulary.filter(item => {
    const haystack = `${item.foreign_text} ${item.german_text} ${item.declension} ${item.lesson}`.toLocaleLowerCase("de");
    return (!search || haystack.includes(search)) && (!difficulty || item.difficulty === difficulty);
  });
  const sort = $("#vocab-sort").value;
  if (sort === "hard-first") state.filteredVocabulary.sort((a, b) => b.difficulty_score - a.difficulty_score);
  if (sort === "easy-first") state.filteredVocabulary.sort((a, b) => a.difficulty_score - b.difficulty_score);
  if (sort === "alphabetical") state.filteredVocabulary.sort((a, b) => a.foreign_text.localeCompare(b.foreign_text, "la"));
  renderVocabulary();
}

function renderVocabulary() {
  const canEdit = state.bootstrap?.auth?.role !== "learner";
  $("#vocab-result-count").textContent = `${state.filteredVocabulary.length} Einträge`;
  $("#empty-vocabulary").classList.toggle("hidden", state.filteredVocabulary.length > 0);
  $("#vocab-table").innerHTML = state.filteredVocabulary.map(item => `
    <tr data-id="${item.id}">
      <td><input class="vocab-check" type="checkbox" ${state.selectedIds.has(item.id) ? "checked" : ""} aria-label="${escapeHtml(item.foreign_text)} auswählen"></td>
      <td>${escapeHtml(item.foreign_text)}</td><td>${escapeHtml(item.german_text)}</td><td>${escapeHtml(item.declension || "—")}</td>
      <td>${escapeHtml(item.lesson || "—")}</td><td><span class="difficulty" data-level="${escapeHtml(item.difficulty)}">${escapeHtml(item.difficulty)}</span></td>
      <td><div class="row-actions">${canEdit ? `<button class="icon-button edit-word" title="Bearbeiten">✎</button><button class="icon-button delete-button delete-word" title="Löschen">×</button>` : ""}</div></td>
    </tr>`).join("");
  $$(".vocab-check").forEach(input => input.addEventListener("change", event => toggleVocabulary(Number(event.target.closest("tr").dataset.id), event.target.checked)));
  $$(".edit-word").forEach(button => button.addEventListener("click", () => openWordDialog(Number(button.closest("tr").dataset.id))));
  $$(".delete-word").forEach(button => button.addEventListener("click", () => deleteWord(Number(button.closest("tr").dataset.id))));
  updateSelectionDock();
}

function toggleVocabulary(id, selected) {
  if (selected) state.selectedIds.add(id); else state.selectedIds.delete(id);
  updateSelectionDock();
}

function updateSelectionDock() {
  const count = state.selectedIds.size;
  $("#selection-dock").classList.toggle("hidden", count === 0);
  $("#dock-count").textContent = `${count} ausgewählt`;
  $("#select-visible").checked = !!state.filteredVocabulary.length && state.filteredVocabulary.every(item => state.selectedIds.has(item.id));
}

function openWordDialog(id = null) {
  const item = id ? state.vocabulary.find(word => word.id === id) : null;
  $("#word-dialog-title").textContent = item ? "Vokabel bearbeiten" : "Vokabel hinzufügen";
  $("#word-id").value = item?.id || "";
  $("#word-source").disabled = !!item;
  if (item) $("#word-source").value = item.source_id;
  $("#word-foreign").value = item?.foreign_text || "";
  $("#word-german").value = item?.german_text || "";
  $("#word-declension").value = item?.declension || "";
  $("#word-lesson").value = item?.lesson || "";
  $("#word-dialog").showModal();
  requestAnimationFrame(() => $("#word-foreign").focus());
}

async function saveWord(event) {
  event.preventDefault();
  const id = Number($("#word-id").value) || null;
  const payload = {
    source_id: Number($("#word-source").value), foreign_text: $("#word-foreign").value,
    german_text: $("#word-german").value, declension: $("#word-declension").value, lesson: $("#word-lesson").value,
  };
  try {
    await api(id ? `/api/vocabulary/${id}` : "/api/vocabulary", {method: id ? "PATCH" : "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload)});
    $("#word-dialog").close();
    toast(id ? "Vokabel aktualisiert." : "Vokabel hinzugefügt.");
    await Promise.all([loadBootstrap(), loadVocabulary()]);
  } catch (error) { toast(error.message, true); }
}

async function deleteWord(id) {
  const item = state.vocabulary.find(word => word.id === id);
  if (!confirm(`„${item.foreign_text}“ wirklich löschen?`)) return;
  try {
    await api(`/api/vocabulary/${id}`, {method:"DELETE"});
    state.selectedIds.delete(id); state.targetedIds.delete(id);
    toast("Vokabel gelöscht.");
    await Promise.all([loadBootstrap(), loadVocabulary()]);
  } catch (error) { toast(error.message, true); }
}

async function openTargetDialog() {
  try {
    const params = new URLSearchParams();
    selectedSourceIds().forEach(id => params.append("source_id", id));
    selectedLessons().forEach(lesson => params.append("lesson", lesson));
    state.targetVocabulary = (await api(`/api/vocabulary?${params}`)).items;
    renderTargets();
    $("#target-dialog").showModal();
  } catch (error) { toast(error.message, true); }
}

function renderTargets() {
  const search = $("#target-search").value.trim().toLocaleLowerCase("de");
  const items = (state.targetVocabulary || []).filter(item => `${item.foreign_text} ${item.german_text}`.toLocaleLowerCase("de").includes(search));
  $("#target-list").innerHTML = items.map(item => `<label class="target-item"><input type="checkbox" value="${item.id}" ${state.targetedIds.has(item.id) ? "checked" : ""}><span>${escapeHtml(item.foreign_text)}</span><span>${escapeHtml(item.german_text)}</span><small>${escapeHtml(item.lesson || "—")}</small></label>`).join("");
  $$("#target-list input").forEach(input => input.addEventListener("change", event => {
    const id = Number(event.target.value); if (event.target.checked) state.targetedIds.add(id); else state.targetedIds.delete(id); updateTargetCount();
  }));
  updateTargetCount();
}

function updateTargetCount() { $("#target-count").textContent = `${state.targetedIds.size} ausgewählt`; }

async function chooseStudyList() {
  const id = Number($("#study-list-select").value) || null;
  state.activeStudyListId = id;
  state.targetedIds.clear();
  state.selectedBlocks.clear();
  if (!id) {
    await refreshSelectionPreview();
    updateTotals();
    return;
  }
  try {
    const list = await api(`/api/study-lists/${id}`);
    state.targetedIds = new Set(list.items.map(item => item.id));
    state.previewVocabulary = list.items.filter(item => {
      const difficulties = new Set(selectedDifficulties());
      return !difficulties.size || difficulties.has(item.difficulty);
    });
    updateTotals();
    renderSelectionPreview();
  } catch (error) { toast(error.message, true); }
}

async function saveMistakes() {
  if (!state.quizSummary?.mistakes?.length) return;
  const target = $("#mistake-list-target").value;
  const payload = target === "new"
    ? {name: $("#mistake-list-name").value.trim()}
    : {list_id: Number(target)};
  const button = $("#save-mistakes");
  button.disabled = true;
  try {
    const result = await api(`/api/quiz/${state.quiz.token}/mistakes/save`, {
      method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload),
    });
    toast(`${result.added} neue Wörter zu „${result.name}“ hinzugefügt · ${result.total} insgesamt.`);
    await loadBootstrap();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function bulkRow(item = {}) {
  const row = reviewRow(item);
  row.classList.add("bulk-row");
  $("button", row).addEventListener("click", updateBulkCount);
  return row;
}

function openBulkDialog() {
  const list = $("#bulk-list");
  list.innerHTML = "";
  for (let index = 0; index < 3; index += 1) list.append(bulkRow());
  updateBulkCount();
  $("#bulk-dialog").showModal();
  requestAnimationFrame(() => $("input", list)?.focus());
}

function updateBulkCount() {
  $("#bulk-count").textContent = `${$$(".bulk-row", $("#bulk-list")).length} Zeilen`;
}

async function saveBulkVocabulary() {
  const items = $$(".bulk-row", $("#bulk-list")).map(row => Object.fromEntries(
    $$("input", row).map(input => [input.dataset.field, input.value])
  ));
  const button = $("#save-bulk");
  button.disabled = true;
  try {
    const result = await api("/api/vocabulary/bulk", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({source_id: Number($("#bulk-source").value), items}),
    });
    $("#bulk-dialog").close();
    toast(`${result.count} Vokabeln gespeichert.`);
    await loadBootstrap();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function logout() {
  try {
    await api("/logout", {method: "POST"});
    localStorage.removeItem("velox_active_quiz");
    location.href = "/access";
  } catch (error) { toast(error.message, true); }
}

async function resumeActiveQuiz() {
  const token = localStorage.getItem("velox_active_quiz");
  if (!token) return false;
  try {
    state.quiz = await api(`/api/quiz/${token}`);
    if (state.quiz.completed) {
      showSummary();
    } else {
      showQuiz();
      if (state.quiz.pending_wrong) showWrongReveal(state.quiz.pending_wrong);
    }
    toast("Dein laufender Durchgang wurde fortgesetzt.");
    return true;
  } catch (_error) {
    localStorage.removeItem("velox_active_quiz");
    return false;
  }
}

function renderSources() {
  const canEdit = state.bootstrap?.auth?.role !== "learner";
  $("#source-cards").innerHTML = state.bootstrap.sources.map(source => `
    <div class="source-row" data-id="${source.id}"><div><strong>${escapeHtml(source.name)}</strong><small>${source.vocabulary_count} Vokabeln</small></div>
    <span class="source-lessons hint">${source.lessons.length} Lektionen/Gruppen</span>
    <a class="secondary-button" href="/api/sources/${source.id}/export">Export</a>
    ${canEdit ? `<button class="icon-button delete-button delete-source" title="Datenquelle löschen">×</button>` : ""}</div>`).join("");
  $$(".delete-source").forEach(button => button.addEventListener("click", () => deleteSource(Number(button.closest(".source-row").dataset.id))));
}

async function deleteSource(id) {
  const source = state.bootstrap.sources.find(item => item.id === id);
  if (!confirm(`Datenquelle „${source.name}“ samt ${source.vocabulary_count} Vokabeln und Lernverlauf wirklich löschen?`)) return;
  try { await api(`/api/sources/${id}`, {method:"DELETE"}); toast("Datenquelle gelöscht."); await loadBootstrap(); }
  catch (error) { toast(error.message, true); }
}

async function importCsv(event) {
  event.preventDefault();
  const button = event.target.querySelector("button[type=submit]"); button.disabled = true;
  try {
    const result = await api("/api/sources/import", {method:"POST", body:new FormData(event.target)});
    toast(`${result.count} Vokabeln als „${result.name}“ importiert.`); event.target.reset(); await loadBootstrap();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function extractAi(event) {
  event.preventDefault();
  const button = $("#ai-extract-button"); button.disabled = true; button.textContent = "Bilder werden gelesen …";
  try {
    const result = await api("/api/ai/extract", {method:"POST", body:new FormData(event.target)});
    state.aiDraftToken = result.draft_token;
    renderAiReview(result.items);
    $("#ai-review-dialog").showModal();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Vokabeln erkennen"; }
}

function reviewRow(item = {}) {
  const row = document.createElement("div"); row.className = "review-row";
  row.innerHTML = `<input data-field="foreign_text" value="${escapeHtml(item.foreign_text || "")}" placeholder="Latein"><input data-field="german_text" value="${escapeHtml(item.german_text || "")}" placeholder="Deutsch"><input data-field="declension" value="${escapeHtml(item.declension || "")}" placeholder="Formen"><input data-field="lesson" value="${escapeHtml(item.lesson || "")}" placeholder="Lektion"><button class="icon-button delete-button" title="Zeile entfernen">×</button>`;
  $("button", row).addEventListener("click", () => { row.remove(); updateReviewCount(); });
  return row;
}

function renderAiReview(items) {
  const list = $("#ai-review-list"); list.innerHTML = ""; items.forEach(item => list.append(reviewRow(item))); updateReviewCount();
}

function updateReviewCount() { $("#review-count").textContent = `${$$(".review-row", $("#ai-review-list")).length} Zeilen`; }

async function commitAi() {
  const items = $$(".review-row", $("#ai-review-list")).map(row => Object.fromEntries($$("input", row).map(input => [input.dataset.field, input.value])));
  const destination = $("#ai-destination").value;
  const payload = {draft_token: state.aiDraftToken, items, source_id: destination === "new" ? null : Number(destination), source_name: $("#ai-source-name").value};
  const button = $("#commit-ai"); button.disabled = true;
  try {
    const result = await api("/api/ai/commit", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
    $("#ai-review-dialog").close(); toast(`${result.count} geprüfte Vokabeln importiert.`); await loadBootstrap();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function bindEvents() {
  $$(".nav-button").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$(".mode-card").forEach(card => card.addEventListener("click", () => setMode(card.dataset.mode)));
  $("#source-select").addEventListener("change", () => { state.selectedBlocks.clear(); renderLessons(); });
  $$("#difficulty-picker input").forEach(input => input.addEventListener("change", () => { state.selectedBlocks.clear(); updateTotals(); refreshSelectionPreview(); }));
  $("#study-list-select").addEventListener("change", chooseStudyList);
  ["#block-size", "#repetitions", "#card-count"].forEach(selector => $(selector).addEventListener("input", updateTotals));
  $("#select-all-blocks").addEventListener("click", () => {
    const blockSize = Math.max(1, Number($("#block-size").value) || 1);
    const total = Math.ceil(availableCount() / blockSize);
    const allSelected = total > 0 && state.selectedBlocks.size === total;
    state.selectedBlocks.clear();
    if (!allSelected) for (let block = 1; block <= total; block += 1) state.selectedBlocks.add(block);
    updateTotals();
  });
  $("#select-all-lessons").addEventListener("click", () => { const boxes = $$("#lesson-list input"); const all = boxes.length > 0 && boxes.every(box => box.checked); boxes.forEach(box => box.checked = !all); state.selectedBlocks.clear(); updateTotals(); refreshSelectionPreview(); });
  $("#declension-toggle").addEventListener("change", event => api("/api/settings", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({check_declension:event.target.checked})}).catch(error => toast(error.message, true)));
  $("#typo-toggle").addEventListener("change", event => api("/api/settings", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({allow_typos:event.target.checked})}).catch(error => toast(error.message, true)));
  $("#timer-toggle").addEventListener("change", event => $("#timer-seconds-label").classList.toggle("hidden", !event.target.checked));
  $("#start-quiz").addEventListener("click", startQuiz);
  $("#answer-form").addEventListener("submit", submitAnswer);
  $("#mark-correct-button").addEventListener("click", markCorrect);
  $("#quit-quiz").addEventListener("click", returnToSetup);
  $("#restart-setup").addEventListener("click", returnToSetup);
  $("#vocab-search").addEventListener("input", filterVocabulary);
  $("#difficulty-filter").addEventListener("change", filterVocabulary);
  $("#vocab-sort").addEventListener("change", filterVocabulary);
  $("#vocab-source-filter").addEventListener("change", loadVocabulary);
  $("#add-word").addEventListener("click", () => openWordDialog());
  $("#word-form").addEventListener("submit", saveWord);
  $("#close-word-dialog").addEventListener("click", () => $("#word-dialog").close());
  $("#cancel-word-dialog").addEventListener("click", () => $("#word-dialog").close());
  $("#select-visible").addEventListener("change", event => { state.filteredVocabulary.forEach(item => event.target.checked ? state.selectedIds.add(item.id) : state.selectedIds.delete(item.id)); renderVocabulary(); });
  $("#clear-selection").addEventListener("click", () => { state.selectedIds.clear(); renderVocabulary(); });
  $("#learn-selection").addEventListener("click", () => { state.targetedIds = new Set(state.selectedIds); state.activeStudyListId = null; $("#study-list-select").value = ""; $("#advanced-selection").open = true; state.selectedBlocks.clear(); updateTotals(); refreshSelectionPreview(); switchView("learn"); });
  $("#targeted-toggle").addEventListener("click", openTargetDialog);
  $("#target-search").addEventListener("input", renderTargets);
  $("#close-target-dialog").addEventListener("click", () => $("#target-dialog").close());
  $("#apply-targets").addEventListener("click", () => { $("#target-dialog").close(); state.activeStudyListId = null; $("#study-list-select").value = ""; state.selectedBlocks.clear(); updateTotals(); refreshSelectionPreview(); });
  $("#csv-import-form").addEventListener("submit", importCsv);
  $("#ai-import-form").addEventListener("submit", extractAi);
  $("#close-ai-review").addEventListener("click", () => $("#ai-review-dialog").close());
  $("#add-review-row").addEventListener("click", () => { $("#ai-review-list").append(reviewRow()); updateReviewCount(); });
  $("#ai-destination").addEventListener("change", event => $("#ai-source-name-label").classList.toggle("hidden", event.target.value !== "new"));
  $("#commit-ai").addEventListener("click", commitAi);
  $("#mistake-list-target").addEventListener("change", event => $("#mistake-list-name").classList.toggle("hidden", event.target.value !== "new"));
  $("#save-mistakes").addEventListener("click", saveMistakes);
  $("#open-bulk-entry").addEventListener("click", openBulkDialog);
  $("#close-bulk-dialog").addEventListener("click", () => $("#bulk-dialog").close());
  $("#add-bulk-row").addEventListener("click", () => { $("#bulk-list").append(bulkRow()); updateBulkCount(); });
  $("#save-bulk").addEventListener("click", saveBulkVocabulary);
  $("#logout-button").addEventListener("click", logout);
  $$('input[type="file"]').forEach(input => input.addEventListener("change", () => { const span = input.closest(".file-drop").querySelector("span"); span.textContent = input.files.length === 1 ? input.files[0].name : `${input.files.length} Dateien ausgewählt`; }));
  document.addEventListener("keydown", event => {
    if (state.awaitingContinue && event.key === "Enter") {
      event.preventDefault();
      continueAfterWrong();
      return;
    }
    if ($$("dialog[open]").length || ["INPUT","SELECT","TEXTAREA"].includes(document.activeElement.tagName)) return;
    if (state.quiz?.mode === "through" && !$("#quiz-screen").classList.contains("hidden")) {
      if (event.key === "Enter") { event.preventDefault(); advanceThrough(); return; }
      if (event.key === " ") { event.preventDefault(); toggleThroughPause(); return; }
    }
    if (event.key === "1") switchView("learn");
    if (event.key === "2") switchView("vocabulary");
    if (event.key === "3") switchView("data");
    if (event.key === "Enter" && $("#view-learn").classList.contains("active") && !$("#setup-screen").classList.contains("hidden")) startQuiz();
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  try { await loadBootstrap(); setMode("block"); await resumeActiveQuiz(); }
  catch (error) { toast(`Start fehlgeschlagen: ${error.message}`, true); }
});
